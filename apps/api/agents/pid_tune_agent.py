"""eljazari.ai — Phase 10: PID auto-tune agent.

Reads the flight-quality metrics from a completed simulation, identifies
which control axis is oscillating, lagging, or overshooting, and proposes
concrete PID gain adjustments. Optionally re-runs the simulation with the
new gains and iterates until the metrics fall inside the engineering
envelope (oscillation power < 0.5, attitude RMS < 1.5°, overshoot < 10%).

Two tuning strategies are supported:

  1. **Ziegler-Nichols open-loop** — used for the initial guess when we
     have no prior gain values. Requires a step-response slice of the
     rate-loop telemetry.

  2. **Heuristic LLM-guided refinement** — given an analyzer report
     ("roll axis oscillating at 12 Hz") the agent emits a small,
     proportional adjustment (reduce MC_ROLLRATE_P by 15%, increase D by
     20%). The math is deterministic; the LLM is only used to pick the
     *direction* when the diagnostics are ambiguous.

All tuning produces a typed :class:`PIDGainUpdate` that maps directly to
PX4 ``param set`` commands so the orchestrator can stage them between
iterations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────────────────────────────────────────────────────────
# Tuning models
# ─────────────────────────────────────────────────────────────────────────────

Axis = Literal["roll", "pitch", "yaw"]
Issue = Literal[
    "oscillation",
    "overshoot",
    "lag",
    "drift",
    "saturation",
    "noise",
]


class PIDGains(BaseModel):
    """The PX4 rate-loop PID gain set for one attitude axis."""

    model_config = ConfigDict(frozen=False)

    axis: Axis
    p: float = Field(..., gt=0)
    i: float = Field(..., ge=0)
    d: float = Field(..., ge=0)
    ff: float = Field(0.0, ge=0)

    @property
    def px4_param_prefix(self) -> str:
        """Maps the axis to the PX4 parameter prefix (MC_ROLLRATE_*)."""
        return {"roll": "MC_ROLLRATE", "pitch": "MC_PITCHRATE", "yaw": "MC_YAWRATE"}[self.axis]


class TuningDiagnosis(BaseModel):
    """What the analyzer thinks is wrong with the current gains."""

    axis: Axis
    issue: Issue
    severity: Literal["low", "medium", "high"]
    measured_value: float
    target_value: float
    suggestion: str


class PIDGainUpdate(BaseModel):
    """A proposed gain change for one axis, with rationale + PX4 commands."""

    axis: Axis
    before: PIDGains
    after: PIDGains
    diagnosis: TuningDiagnosis

    def px4_param_commands(self) -> list[str]:
        prefix = self.after.px4_param_prefix
        cmds = [
            f"param set {prefix}_P {self.after.p:.4f}",
            f"param set {prefix}_I {self.after.i:.4f}",
            f"param set {prefix}_D {self.after.d:.4f}",
        ]
        if self.after.ff > 0 or self.before.ff > 0:
            cmds.append(f"param set {prefix}_FF {self.after.ff:.4f}")
        return cmds


class TuningCycleResult(BaseModel):
    """Full output of one tuning iteration."""

    iteration: int
    updates: list[PIDGainUpdate] = Field(default_factory=list)
    overall_score_before: float = 0.0
    overall_score_after_estimate: float = 0.0
    requires_simulation: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Reference / default gain sets
# ─────────────────────────────────────────────────────────────────────────────

#: PX4 v1.16 stock gains for a generic 5" quadcopter (matches gz_x500).
DEFAULT_GAINS: dict[Axis, PIDGains] = {
    "roll": PIDGains(axis="roll", p=0.15, i=0.20, d=0.003, ff=0.0),
    "pitch": PIDGains(axis="pitch", p=0.15, i=0.20, d=0.003, ff=0.0),
    "yaw": PIDGains(axis="yaw", p=0.20, i=0.10, d=0.000, ff=0.0),
}


# Engineering envelope — gains outside these bounds are refused.
_GAIN_BOUNDS: dict[Axis, dict[str, tuple[float, float]]] = {
    "roll": {"p": (0.05, 0.50), "i": (0.0, 1.0), "d": (0.0, 0.020)},
    "pitch": {"p": (0.05, 0.50), "i": (0.0, 1.0), "d": (0.0, 0.020)},
    "yaw": {"p": (0.05, 0.80), "i": (0.0, 1.0), "d": (0.0, 0.010)},
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _clamp_gains(gains: PIDGains) -> PIDGains:
    b = _GAIN_BOUNDS[gains.axis]
    return PIDGains(
        axis=gains.axis,
        p=_clamp(gains.p, *b["p"]),
        i=_clamp(gains.i, *b["i"]),
        d=_clamp(gains.d, *b["d"]),
        ff=gains.ff,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostic helpers
# ─────────────────────────────────────────────────────────────────────────────


def diagnose_axis(
    axis: Axis,
    oscillation_power: float,
    oscillation_freq_hz: float,
    rms_deg: float,
    saturation_pct_peak: float,
) -> TuningDiagnosis | None:
    """Identify the dominant control issue on one axis, if any."""
    if oscillation_power >= 1.0 and oscillation_freq_hz >= 5.0:
        return TuningDiagnosis(
            axis=axis,
            issue="oscillation",
            severity="high" if oscillation_power > 3.0 else "medium",
            measured_value=oscillation_power,
            target_value=0.5,
            suggestion=(
                f"Reduce {axis} rate P by ~15%; increase D by ~20% to damp "
                f"the {oscillation_freq_hz:.1f} Hz oscillation."
            ),
        )
    if rms_deg >= 3.0:
        return TuningDiagnosis(
            axis=axis,
            issue="lag",
            severity="medium" if rms_deg < 6.0 else "high",
            measured_value=rms_deg,
            target_value=1.5,
            suggestion=(
                f"Attitude RMS {rms_deg:.2f}° — increase {axis} rate P by ~10% "
                f"to tighten the inner loop."
            ),
        )
    if saturation_pct_peak >= 95.0:
        return TuningDiagnosis(
            axis=axis,
            issue="saturation",
            severity="high",
            measured_value=saturation_pct_peak,
            target_value=85.0,
            suggestion=(
                "Motor output saturating — reduce inner-loop gains by ~15% "
                "across all three axes so the controller doesn't demand "
                "more thrust than the airframe can deliver."
            ),
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Gain adjustment rules
# ─────────────────────────────────────────────────────────────────────────────


def adjust_for_oscillation(gains: PIDGains) -> PIDGains:
    """Reduce P, raise D — the canonical fix for ringing."""
    return _clamp_gains(
        PIDGains(
            axis=gains.axis,
            p=gains.p * 0.85,
            i=gains.i,
            d=max(gains.d * 1.20, gains.d + 0.0005),
            ff=gains.ff,
        )
    )


def adjust_for_lag(gains: PIDGains) -> PIDGains:
    """Raise P slightly — tighter response."""
    return _clamp_gains(
        PIDGains(
            axis=gains.axis,
            p=gains.p * 1.10,
            i=gains.i,
            d=gains.d,
            ff=gains.ff,
        )
    )


def adjust_for_saturation(gains: PIDGains) -> PIDGains:
    """Reduce all three terms by 15% so the controller stops asking for more thrust."""
    return _clamp_gains(
        PIDGains(
            axis=gains.axis,
            p=gains.p * 0.85,
            i=gains.i * 0.85,
            d=gains.d * 0.85,
            ff=gains.ff,
        )
    )


_ADJUSTERS = {
    "oscillation": adjust_for_oscillation,
    "lag": adjust_for_lag,
    "saturation": adjust_for_saturation,
}


# ─────────────────────────────────────────────────────────────────────────────
# Ziegler-Nichols (first-pass tune from a step response)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class StepResponseFit:
    """Parameters of the first-order-plus-dead-time model fitted to a step."""

    dead_time_s: float
    time_constant_s: float
    gain: float


def fit_step_response(
    setpoint_step: float,
    response: list[float],
    sample_hz: float,
) -> StepResponseFit | None:
    """Fit FOPDT model to a step response. Used as the Z-N seed.

    Returns None if the data is too short or the response never reaches
    63 % of the setpoint.
    """
    if len(response) < 10 or setpoint_step == 0 or sample_hz <= 0:
        return None
    final = response[-1]
    if abs(final) < 1e-9:
        return None
    threshold_63 = 0.632 * final
    threshold_10 = 0.1 * final
    t63 = None
    t10 = None
    for i, v in enumerate(response):
        if t10 is None and abs(v) >= abs(threshold_10):
            t10 = i / sample_hz
        if t63 is None and abs(v) >= abs(threshold_63):
            t63 = i / sample_hz
            break
    if t63 is None or t10 is None:
        return None
    dead_time = t10
    tau = max(t63 - t10, 1.0 / sample_hz)
    gain = final / setpoint_step
    return StepResponseFit(dead_time_s=dead_time, time_constant_s=tau, gain=gain)


def ziegler_nichols_seed(fit: StepResponseFit, axis: Axis) -> PIDGains:
    """Compute initial P, I, D from the FOPDT fit using Z-N open-loop rules."""
    if fit.time_constant_s <= 0:
        return DEFAULT_GAINS[axis]
    # Classic Z-N open-loop (modified for inner-loop rate controllers).
    kp = 0.6 * fit.time_constant_s / max(fit.gain * fit.dead_time_s, 1e-6)
    ti = 2.0 * fit.dead_time_s
    td = 0.5 * fit.dead_time_s
    return _clamp_gains(
        PIDGains(
            axis=axis,
            p=kp,
            i=kp / max(ti, 1e-6) * 0.1,  # scale down — rate-loop I is tiny in PX4
            d=kp * td * 0.01,
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────────────────


class PIDTuneAgent:
    """Reads MissionMetrics / FlightQualityMetrics → emits PIDGainUpdate list."""

    def tune(
        self,
        *,
        roll_oscillation_power: float,
        roll_oscillation_freq_hz: float,
        pitch_oscillation_power: float,
        pitch_oscillation_freq_hz: float,
        attitude_rms_deg: float,
        motor_saturation_percent_peak: float,
        current_gains: dict[Axis, PIDGains] | None = None,
        iteration: int = 0,
    ) -> TuningCycleResult:
        """Run one tuning cycle. Returns the proposed updates (may be empty)."""
        gains = dict(current_gains or DEFAULT_GAINS)
        updates: list[PIDGainUpdate] = []

        # Per-axis diagnosis
        roll_diag = diagnose_axis(
            "roll",
            oscillation_power=roll_oscillation_power,
            oscillation_freq_hz=roll_oscillation_freq_hz,
            rms_deg=attitude_rms_deg,
            saturation_pct_peak=motor_saturation_percent_peak,
        )
        pitch_diag = diagnose_axis(
            "pitch",
            oscillation_power=pitch_oscillation_power,
            oscillation_freq_hz=pitch_oscillation_freq_hz,
            rms_deg=attitude_rms_deg,
            saturation_pct_peak=motor_saturation_percent_peak,
        )

        for axis, diag in (("roll", roll_diag), ("pitch", pitch_diag)):
            if diag is None:
                continue
            before = gains[axis]
            adjuster = _ADJUSTERS.get(diag.issue)
            if adjuster is None:
                continue
            after = adjuster(before)
            if after == before:
                continue
            updates.append(
                PIDGainUpdate(
                    axis=axis,
                    before=before,
                    after=after,
                    diagnosis=diag,
                )
            )

        # Motor saturation is a vehicle-wide problem, not axis-specific.
        # When it's the dominant issue, replace any per-axis saturation
        # updates with a full roll/pitch/yaw pullback so the controller
        # stops demanding more thrust than the airframe can deliver.
        if motor_saturation_percent_peak >= 95.0:
            updates = [u for u in updates if u.diagnosis.issue != "saturation"]
            seen = {u.axis for u in updates}
            for axis in ("roll", "pitch", "yaw"):
                if axis in seen:
                    continue
                before = gains[axis]
                after = adjust_for_saturation(before)
                if after != before:
                    updates.append(
                        PIDGainUpdate(
                            axis=axis,
                            before=before,
                            after=after,
                            diagnosis=TuningDiagnosis(
                                axis=axis,
                                issue="saturation",
                                severity="high",
                                measured_value=motor_saturation_percent_peak,
                                target_value=85.0,
                                suggestion="Reduce inner-loop gains across all axes.",
                            ),
                        )
                    )

        return TuningCycleResult(
            iteration=iteration,
            updates=updates,
            requires_simulation=bool(updates),
        )


def render_param_script(updates: list[PIDGainUpdate]) -> str:
    """Render a PX4 param script applying all updates in one shot."""
    lines = ["# PID tune cycle — auto-generated"]
    for u in updates:
        lines.append(
            f"# {u.axis} {u.diagnosis.issue} ({u.diagnosis.severity}): {u.diagnosis.suggestion}"
        )
        lines.extend(u.px4_param_commands())
    lines.append("param save")
    return "\n".join(lines) + "\n"
