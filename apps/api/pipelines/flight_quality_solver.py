"""eljazari.ai — Phase 11: Production-grade flight-quality solver.

The legacy MonitorAgent decides pass/fail on "did the drone take off and
land?". That misses what drone engineers actually care about:

  • Did it track the commanded path? (cross-track error, position RMSE)
  • Did it hold altitude? (altitude RMSE, drift rate)
  • Did the controller oscillate? (roll/pitch FFT peak frequency + power)
  • Did motors saturate? (throttle headroom margin)
  • Did it return home accurately? (final RTL error)

This module computes all of those from real telemetry and a known mission
plan, then returns a :class:`FlightQualityMetrics` Pydantic object that
:class:`MonitorAgent` can include in its ReActEval cycle.

Inputs are deliberately loose: a list of telemetry-point dicts (the same
format the orchestrator persists) and an optional commanded-path list of
NED waypoints. When the mission plan is unknown we still emit the
oscillation/saturation/drift metrics — only the *tracking* ones go zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Literal

from pydantic import BaseModel, Field

from utils.coordinates import LLA, NED


Grade = Literal["A", "B", "C", "D", "F"]


# ─────────────────────────────────────────────────────────────────────────────
# Output model
# ─────────────────────────────────────────────────────────────────────────────


class FlightQualityMetrics(BaseModel):
    """Engineering-grade flight-quality numbers, real units, no templated values."""

    # Tracking — `*_attempted` flags distinguish "no commanded path" (no
    # tracking computed) from "perfect tracking" (RMSE = 0).
    tracking_attempted: bool = False
    altitude_tracking_attempted: bool = False
    position_rmse_m: float = Field(0.0, ge=0)
    altitude_rmse_m: float = Field(0.0, ge=0)
    cross_track_error_max_m: float = Field(0.0, ge=0)
    cross_track_error_mean_m: float = Field(0.0, ge=0)
    heading_rmse_deg: float = Field(0.0, ge=0)

    # Stability (computed from attitude history)
    roll_oscillation_freq_hz: float = Field(0.0, ge=0)
    pitch_oscillation_freq_hz: float = Field(0.0, ge=0)
    roll_oscillation_power: float = Field(0.0, ge=0)
    pitch_oscillation_power: float = Field(0.0, ge=0)
    attitude_rms_deg: float = Field(0.0, ge=0)

    # Saturation / energy
    motor_saturation_percent_peak: float = Field(0.0, ge=0, le=100)
    motor_saturation_percent_mean: float = Field(0.0, ge=0, le=100)

    # Drift / settling
    hover_drift_m_per_min: float = Field(0.0, ge=0)
    # Hover drift is only meaningful while station-keeping. A survey/waypoint
    # mission that traverses continuously has no hover segment, so the metric is
    # marked not-evaluated and dropped from the score (like the tracking pillar).
    hover_drift_evaluated: bool = False
    return_to_home_error_m: float = Field(0.0, ge=0)
    settling_time_s: float | None = None

    # Summary
    setpoint_tracking_grade: Grade = "F"
    stability_grade: Grade = "F"
    overall_flight_quality_score: float = Field(0.0, ge=0, le=100)
    grade: Grade = "F"

    # Diagnostics for the LLM section
    warnings: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PathSegment:
    """A linear leg of the commanded mission path in NED."""

    a: NED
    b: NED

    def length_m(self) -> float:
        dn = self.b.north_m - self.a.north_m
        de = self.b.east_m - self.a.east_m
        dd = self.b.down_m - self.a.down_m
        return math.sqrt(dn * dn + de * de + dd * dd)

    def cross_track_distance_m(self, point: NED) -> float:
        """Perpendicular distance from a point to this line segment, in metres.

        Uses 2-D projection in the NE plane (the altitude axis is reported
        separately as `altitude_rmse_m`).
        """
        ax, ay = self.a.north_m, self.a.east_m
        bx, by = self.b.north_m, self.b.east_m
        px, py = point.north_m, point.east_m
        dx, dy = bx - ax, by - ay
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq < 1e-9:
            # Degenerate segment — fall back to point-to-point distance.
            return math.hypot(px - ax, py - ay)
        t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
        t = max(0.0, min(1.0, t))
        proj_x = ax + t * dx
        proj_y = ay + t * dy
        return math.hypot(px - proj_x, py - proj_y)


def _sample_value(sample: dict, *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in sample and sample[key] is not None:
            try:
                return float(sample[key])
            except (TypeError, ValueError):
                continue
        # Allow dotted-path lookup like "position.north_m"
        if "." in key:
            cur = sample
            ok = True
            for part in key.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok and cur is not None:
                try:
                    return float(cur)
                except (TypeError, ValueError):
                    continue
    return default


def _extract_position_ned(telemetry: Iterable[dict]) -> list[NED]:
    """Best-effort NED extraction from heterogeneous telemetry dicts."""
    out: list[NED] = []
    for s in telemetry:
        n = _sample_value(s, "north_m", "position.north_m", "n", default=0.0)
        e = _sample_value(s, "east_m", "position.east_m", "e", default=0.0)
        alt = _sample_value(s, "alt_m", "altitude_m", "position.alt_m", default=0.0)
        # PX4 telemetry usually stores altitude (positive up); convert to NED down.
        out.append(NED(north_m=n, east_m=e, down_m=-alt))
    return out


def _extract_attitude(telemetry: Iterable[dict]) -> tuple[list[float], list[float]]:
    """Return (roll_deg, pitch_deg) lists with non-zero length when present."""
    roll, pitch = [], []
    for s in telemetry:
        r = _sample_value(s, "roll_deg", "attitude.roll_deg", "roll", default=math.nan)
        p = _sample_value(s, "pitch_deg", "attitude.pitch_deg", "pitch", default=math.nan)
        if not math.isnan(r):
            roll.append(r)
        if not math.isnan(p):
            pitch.append(p)
    return roll, pitch


def _extract_motor_outputs(telemetry: Iterable[dict]) -> list[list[float]]:
    """Return per-sample motor PWM/throttle vectors when available (0–1 or 0–100).

    Tries multiple key conventions: `motors`, `actuator_outputs`, `motor_pwms`.
    """
    out: list[list[float]] = []
    for s in telemetry:
        for key in ("motors", "actuator_outputs", "motor_pwms", "outputs"):
            if key in s and isinstance(s[key], (list, tuple)):
                out.append([float(x) for x in s[key]])
                break
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Spectral / statistical primitives
#
# These are deliberately written in pure Python (no numpy/scipy) because
# the MonitorAgent runs in environments where heavy deps aren't guaranteed,
# and the data sizes are modest (≤ 5000 samples) so a naive DFT is fine.
# ─────────────────────────────────────────────────────────────────────────────


def _rms(values: Iterable[float]) -> float:
    values = list(values)
    if not values:
        return 0.0
    return math.sqrt(sum(v * v for v in values) / len(values))


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _median(values: Iterable[float]) -> float:
    vs = sorted(values)
    if not vs:
        return 0.0
    mid = len(vs) // 2
    return vs[mid] if len(vs) % 2 else (vs[mid - 1] + vs[mid]) / 2.0


def _detrend(values: list[float]) -> list[float]:
    mu = _mean(values)
    return [v - mu for v in values]


def _peak_frequency_hz(signal: list[float], sample_rate_hz: float) -> tuple[float, float]:
    """Return (peak_freq_hz, peak_power_normalised) from a 1-D real signal.

    Uses a naive DFT over the first N/2 bins. Adequate for detecting whether
    the controller is ringing at 5-50 Hz, which is what we care about.
    """
    n = len(signal)
    if n < 8 or sample_rate_hz <= 0.0:
        return 0.0, 0.0
    detrended = _detrend(signal)
    bins = n // 2
    peak_freq = 0.0
    peak_power = 0.0
    for k in range(1, bins):  # skip DC bin
        re = 0.0
        im = 0.0
        for t, x in enumerate(detrended):
            angle = -2.0 * math.pi * k * t / n
            re += x * math.cos(angle)
            im += x * math.sin(angle)
        power = (re * re + im * im) / (n * n)
        if power > peak_power:
            peak_power = power
            peak_freq = k * sample_rate_hz / n
    return peak_freq, peak_power


def _estimate_sample_rate_hz(telemetry: list[dict]) -> float:
    """Use timestamp_ms deltas (or assume 50 Hz if missing)."""
    if len(telemetry) < 2:
        return 50.0
    ts = [t.get("timestamp_ms") or t.get("ts") for t in telemetry]
    ts = [t for t in ts if isinstance(t, (int, float))]
    if len(ts) < 2:
        return 50.0
    deltas = [(ts[i + 1] - ts[i]) / 1000.0 for i in range(len(ts) - 1)]
    deltas = [d for d in deltas if d > 0]
    if not deltas:
        return 50.0
    mean_dt = sum(deltas) / len(deltas)
    return 1.0 / max(mean_dt, 0.001)


def _grade_from_score(score: float) -> Grade:
    if score >= 90.0:
        return "A"
    if score >= 75.0:
        return "B"
    if score >= 60.0:
        return "C"
    if score >= 45.0:
        return "D"
    return "F"


# ─────────────────────────────────────────────────────────────────────────────
# Public solver
# ─────────────────────────────────────────────────────────────────────────────


def compute_flight_quality(
    telemetry: list[dict],
    commanded_waypoints_ned: list[NED] | None = None,
    target_altitude_m: float | None = None,
    home_lla: LLA | None = None,
) -> FlightQualityMetrics:
    """Compute production-grade flight-quality metrics for a finished mission.

    All inputs are tolerant to missing fields. The solver returns zeroed
    metrics + a warning rather than raising.
    """
    metrics = FlightQualityMetrics()
    warnings: list[str] = []
    strengths: list[str] = []

    if not telemetry:
        warnings.append("No telemetry available — cannot compute flight quality.")
        metrics.warnings = warnings
        return metrics

    positions = _extract_position_ned(telemetry)
    roll_series, pitch_series = _extract_attitude(telemetry)
    motor_outputs = _extract_motor_outputs(telemetry)
    sample_rate_hz = _estimate_sample_rate_hz(telemetry)

    # ── Tracking error (only when commanded path supplied) ───────────────
    if commanded_waypoints_ned and len(commanded_waypoints_ned) >= 2:
        metrics.tracking_attempted = True
        segments = [
            PathSegment(a=commanded_waypoints_ned[i], b=commanded_waypoints_ned[i + 1])
            for i in range(len(commanded_waypoints_ned) - 1)
        ]
        cross_errors: list[float] = []
        for p in positions:
            d_min = min(seg.cross_track_distance_m(p) for seg in segments)
            cross_errors.append(d_min)
        if cross_errors:
            metrics.cross_track_error_max_m = round(max(cross_errors), 3)
            metrics.cross_track_error_mean_m = round(_mean(cross_errors), 3)
            metrics.position_rmse_m = round(_rms(cross_errors), 3)

    # ── Altitude RMSE (vs target altitude when given) ────────────────────
    if target_altitude_m is not None and positions:
        metrics.altitude_tracking_attempted = True
        alt_errors = [(-p.down_m) - target_altitude_m for p in positions]
        # Skip the takeoff/landing transient — first 5% and last 5% of samples
        skip = max(1, len(alt_errors) // 20)
        cruise_errors = alt_errors[skip:-skip] if len(alt_errors) > 2 * skip else alt_errors
        metrics.altitude_rmse_m = round(_rms(cruise_errors), 3)

    # ── Stability (attitude oscillation) ─────────────────────────────────
    if roll_series:
        rf, rp = _peak_frequency_hz(roll_series, sample_rate_hz)
        metrics.roll_oscillation_freq_hz = round(rf, 2)
        metrics.roll_oscillation_power = round(rp, 5)
    if pitch_series:
        pf, pp = _peak_frequency_hz(pitch_series, sample_rate_hz)
        metrics.pitch_oscillation_freq_hz = round(pf, 2)
        metrics.pitch_oscillation_power = round(pp, 5)
    if roll_series or pitch_series:
        combined_attitude = [*roll_series, *pitch_series]
        metrics.attitude_rms_deg = round(_rms(_detrend(combined_attitude)), 3)

    # ── Motor saturation ─────────────────────────────────────────────────
    if motor_outputs:
        flat = [v for sample in motor_outputs for v in sample]
        # Detect units: PX4 typically uses 0..1 PWM normalised; some agents
        # send 0..100 throttle percent.
        if flat and max(flat) <= 1.5:
            flat = [v * 100.0 for v in flat]
        if flat:
            metrics.motor_saturation_percent_peak = round(max(flat), 2)
            metrics.motor_saturation_percent_mean = round(_mean(flat), 2)

    # ── Hover drift  +  RTH closure ──────────────────────────────────────
    if positions:
        # Hover drift only makes sense while the aircraft is commanded to hold
        # position. Slide a ~3 s window across the flight; a window where the
        # aircraft moved < 2 m (≈ < 0.7 m/s) counts as station-keeping. We grade
        # drift over those windows only. A survey mission that traverses
        # continuously has no such window → hover drift is marked not-evaluated
        # (and excluded from the score) instead of reporting its cruise speed
        # as 1000+ m/min "drift".
        n = len(positions)
        lo, hi = int(n * 0.2), int(n * 0.8)
        cruise = positions[lo:hi] if hi - lo >= 4 else positions
        window = max(2, int(round(max(sample_rate_hz, 1.0) * 3.0)))
        if len(cruise) >= window:
            window_s = window / max(sample_rate_hz, 1.0)
            hover_spreads: list[float] = []
            for i in range(0, len(cruise) - window + 1, max(1, window // 2)):
                seg = cruise[i : i + window]
                sn = [p.north_m for p in seg]
                se = [p.east_m for p in seg]
                spread = math.hypot(max(sn) - min(sn), max(se) - min(se))
                if spread < 2.0:
                    hover_spreads.append(spread)
            if hover_spreads:
                metrics.hover_drift_evaluated = True
                metrics.hover_drift_m_per_min = round(
                    _median(hover_spreads) * 60.0 / max(window_s, 1e-6), 3
                )

        # Return-to-home closure: distance from start to end (NE plane only).
        rth_err = math.hypot(
            positions[-1].north_m - positions[0].north_m,
            positions[-1].east_m - positions[0].east_m,
        )
        metrics.return_to_home_error_m = round(rth_err, 3)

    # ── Grades + score ───────────────────────────────────────────────────
    metrics.setpoint_tracking_grade = _grade_tracking(metrics)
    metrics.stability_grade = _grade_stability(metrics)

    score = _compute_overall_score(metrics)
    metrics.overall_flight_quality_score = round(score, 1)
    metrics.grade = _grade_from_score(score)

    # ── Narrative diagnostics ────────────────────────────────────────────
    if metrics.position_rmse_m > 0:
        if metrics.position_rmse_m < 1.0:
            strengths.append(
                f"Excellent path tracking: cross-track RMSE {metrics.position_rmse_m:.2f} m."
            )
        elif metrics.position_rmse_m > 5.0:
            warnings.append(
                f"Poor path tracking: cross-track RMSE {metrics.position_rmse_m:.2f} m — "
                f"tune position controller or check wind."
            )
    if metrics.altitude_rmse_m > 1.0:
        warnings.append(
            f"Altitude tracking RMSE {metrics.altitude_rmse_m:.2f} m exceeds 1 m — "
            f"check altitude/position-Z gains."
        )
    if metrics.roll_oscillation_power > 1.0 and metrics.roll_oscillation_freq_hz > 5.0:
        warnings.append(
            f"Roll axis oscillating at {metrics.roll_oscillation_freq_hz:.1f} Hz — "
            f"reduce MC_ROLLRATE_P or increase MC_ROLLRATE_D."
        )
    if metrics.pitch_oscillation_power > 1.0 and metrics.pitch_oscillation_freq_hz > 5.0:
        warnings.append(
            f"Pitch axis oscillating at {metrics.pitch_oscillation_freq_hz:.1f} Hz — "
            f"reduce MC_PITCHRATE_P or increase MC_PITCHRATE_D."
        )
    if metrics.motor_saturation_percent_peak > 95.0:
        warnings.append(
            f"Peak motor output {metrics.motor_saturation_percent_peak:.1f}% — "
            f"vehicle ran out of thrust headroom; reduce payload or upsize props."
        )
    if metrics.return_to_home_error_m > 5.0:
        warnings.append(
            f"RTL closure error {metrics.return_to_home_error_m:.2f} m — "
            f"check EKF home-position fix and yaw alignment."
        )
    if 0 < metrics.hover_drift_m_per_min < 1.0:
        strengths.append(f"Tight hover hold: drift {metrics.hover_drift_m_per_min:.2f} m/min.")

    metrics.warnings = warnings
    metrics.strengths = strengths
    return metrics


# ─────────────────────────────────────────────────────────────────────────────
# Grading helpers
# ─────────────────────────────────────────────────────────────────────────────


def _grade_tracking(m: FlightQualityMetrics) -> Grade:
    """Grade based on the largest tracking error among attempted axes.

    When no path or altitude was supplied to grade against, return "F" with
    the meaning "not evaluated" — the score formula then ignores the
    tracking pillar (see :func:`_compute_overall_score`).
    """
    if not (m.tracking_attempted or m.altitude_tracking_attempted):
        return "F"
    rmse = 0.0
    if m.tracking_attempted:
        rmse = max(rmse, m.position_rmse_m)
    if m.altitude_tracking_attempted:
        rmse = max(rmse, m.altitude_rmse_m)
    if rmse < 0.5:
        return "A"
    if rmse < 1.5:
        return "B"
    if rmse < 3.0:
        return "C"
    if rmse < 6.0:
        return "D"
    return "F"


def _grade_stability(m: FlightQualityMetrics) -> Grade:
    osc_power = max(m.roll_oscillation_power, m.pitch_oscillation_power)
    rms = m.attitude_rms_deg
    if osc_power < 0.5 and rms < 1.5:
        return "A"
    if osc_power < 1.5 and rms < 3.0:
        return "B"
    if osc_power < 3.0 and rms < 6.0:
        return "C"
    if osc_power < 5.0 and rms < 10.0:
        return "D"
    return "F"


def _compute_overall_score(m: FlightQualityMetrics) -> float:
    """Weighted blend of the engineering pillars.

    When tracking wasn't attempted (no commanded path supplied), the
    tracking pillar is dropped and the remaining weights are renormalised
    so a flight with great stability isn't unfairly penalised for missing
    inputs.
    """
    grade_to_pts = {"A": 100.0, "B": 80.0, "C": 65.0, "D": 50.0, "F": 30.0}

    # (points, weight) per pillar. Optional pillars (tracking, hover drift) are
    # only included when actually evaluated; the remaining weights are then
    # renormalised so a flight isn't penalised for inputs it never had.
    pillars: list[tuple[float, float]] = [
        (grade_to_pts[m.stability_grade], 0.25),
        (max(0.0, 100.0 - max(0.0, m.motor_saturation_percent_peak - 80.0) * 5.0), 0.15),
        (max(0.0, 100.0 - m.return_to_home_error_m * 8.0), 0.15),
    ]
    if m.tracking_attempted or m.altitude_tracking_attempted:
        pillars.append((grade_to_pts[m.setpoint_tracking_grade], 0.30))
    if m.hover_drift_evaluated:
        pillars.append((max(0.0, 100.0 - m.hover_drift_m_per_min * 20.0), 0.15))

    total_w = sum(w for _, w in pillars)
    score = sum(pts * w for pts, w in pillars) / total_w if total_w else 0.0
    return max(0.0, min(100.0, score))
