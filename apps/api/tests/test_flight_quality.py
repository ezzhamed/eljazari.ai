"""Phase 11 tests — production-grade flight quality solver + MonitorAgent integration."""

from __future__ import annotations

import math

import pytest

from pipelines.flight_quality_solver import (
    PathSegment,
    compute_flight_quality,
    _grade_from_score,
    _peak_frequency_hz,
)
from utils.coordinates import NED


# ─────────────────────────────────────────────────────────────────────────────
# Primitives
# ─────────────────────────────────────────────────────────────────────────────


class TestPathSegment:
    def test_perpendicular_distance_to_origin_segment(self):
        seg = PathSegment(a=NED(0, 0), b=NED(10, 0))
        # Point 3m east of midpoint → perpendicular distance 3m.
        d = seg.cross_track_distance_m(NED(north_m=5, east_m=3))
        assert d == pytest.approx(3.0, abs=0.001)

    def test_point_on_segment_has_zero_distance(self):
        seg = PathSegment(a=NED(0, 0), b=NED(10, 0))
        d = seg.cross_track_distance_m(NED(north_m=5, east_m=0))
        assert d == pytest.approx(0.0, abs=0.001)

    def test_point_before_segment_uses_endpoint_distance(self):
        seg = PathSegment(a=NED(0, 0), b=NED(10, 0))
        # Point 4m south of A — distance should be 4m to A, not perpendicular.
        d = seg.cross_track_distance_m(NED(north_m=-4, east_m=0))
        assert d == pytest.approx(4.0, abs=0.001)

    def test_zero_length_segment_returns_point_distance(self):
        seg = PathSegment(a=NED(5, 5), b=NED(5, 5))
        d = seg.cross_track_distance_m(NED(north_m=8, east_m=9))
        assert d == pytest.approx(math.hypot(3.0, 4.0), abs=0.001)


# ─────────────────────────────────────────────────────────────────────────────
# Peak frequency detection
# ─────────────────────────────────────────────────────────────────────────────


class TestPeakFrequency:
    def test_pure_sine_is_detected(self):
        sample_rate = 100.0
        target_hz = 10.0
        n = 200
        signal = [5.0 * math.sin(2 * math.pi * target_hz * t / sample_rate) for t in range(n)]
        freq, power = _peak_frequency_hz(signal, sample_rate)
        assert freq == pytest.approx(target_hz, abs=1.0)
        assert power > 0.0

    def test_silent_signal_has_zero_peak(self):
        freq, power = _peak_frequency_hz([0.0] * 100, 50.0)
        assert freq == 0.0
        assert power == 0.0

    def test_short_signal_returns_zero(self):
        freq, power = _peak_frequency_hz([1.0, 2.0, 3.0], 50.0)
        assert freq == 0.0
        assert power == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Flight quality solver
# ─────────────────────────────────────────────────────────────────────────────


def _straight_north_telemetry(
    n_samples: int = 50,
    target_north_m: float = 10.0,
    altitude_m: float = 4.0,
    east_drift_m: float = 0.0,
    altitude_noise_m: float = 0.0,
) -> list[dict]:
    """A simple north-leg flight: starts at home, ends at (target_north, 0, alt)."""
    pts = []
    for i in range(n_samples):
        t = i / (n_samples - 1)
        north = target_north_m * t
        east = east_drift_m * t  # linear drift
        alt = altitude_m + (altitude_noise_m * math.sin(2 * math.pi * i / n_samples))
        pts.append(
            {
                "timestamp_ms": i * 100,  # 10 Hz
                "north_m": north,
                "east_m": east,
                "alt_m": alt,
                "roll_deg": 0.0,
                "pitch_deg": 0.0,
            }
        )
    return pts


class TestFlightQualitySolver:
    def test_empty_telemetry_returns_warning(self):
        m = compute_flight_quality(telemetry=[])
        assert m.position_rmse_m == 0.0
        assert any("No telemetry" in w for w in m.warnings)

    def test_perfect_tracking_grades_A(self):
        telemetry = _straight_north_telemetry(
            n_samples=50,
            target_north_m=10.0,
            altitude_m=4.0,
            east_drift_m=0.0,
            altitude_noise_m=0.0,
        )
        commanded = [NED(0, 0), NED(north_m=10, east_m=0)]
        m = compute_flight_quality(telemetry, commanded, target_altitude_m=4.0)
        assert m.position_rmse_m < 0.1
        assert m.altitude_rmse_m < 0.1
        assert m.setpoint_tracking_grade == "A"

    def test_drifted_flight_lowers_tracking_grade(self):
        # Drone drifted 5m east while flying north — significant cross-track error.
        telemetry = _straight_north_telemetry(
            n_samples=50,
            target_north_m=10.0,
            altitude_m=4.0,
            east_drift_m=5.0,
            altitude_noise_m=0.0,
        )
        commanded = [NED(0, 0), NED(north_m=10, east_m=0)]
        m = compute_flight_quality(telemetry, commanded, target_altitude_m=4.0)
        assert m.position_rmse_m > 1.0
        assert m.setpoint_tracking_grade in ("C", "D", "F")

    def test_altitude_oscillation_increases_altitude_rmse(self):
        # 1m peak altitude noise → altitude RMSE around 0.7m.
        telemetry = _straight_north_telemetry(
            n_samples=100,
            target_north_m=10.0,
            altitude_m=4.0,
            east_drift_m=0.0,
            altitude_noise_m=1.0,
        )
        m = compute_flight_quality(telemetry, target_altitude_m=4.0)
        assert m.altitude_rmse_m > 0.4

    def test_no_commanded_path_zeroes_tracking(self):
        telemetry = _straight_north_telemetry(n_samples=20)
        m = compute_flight_quality(telemetry)
        assert m.position_rmse_m == 0.0
        assert m.cross_track_error_max_m == 0.0

    def test_attitude_oscillation_emits_warning(self):
        # Inject roll oscillation at 12 Hz with large amplitude.
        n_samples = 200
        sample_rate = 50.0
        telemetry = []
        for i in range(n_samples):
            t = i / sample_rate
            roll = 8.0 * math.sin(2 * math.pi * 12.0 * t)
            telemetry.append(
                {
                    "timestamp_ms": int(t * 1000),
                    "north_m": 0.0,
                    "east_m": 0.0,
                    "alt_m": 4.0,
                    "roll_deg": roll,
                    "pitch_deg": 0.0,
                }
            )
        m = compute_flight_quality(telemetry)
        assert m.roll_oscillation_freq_hz == pytest.approx(12.0, abs=2.0)
        assert m.roll_oscillation_power > 0.0
        # At amplitude 8°, power should be high enough to emit a warning.
        assert any("oscillating" in w.lower() for w in m.warnings)

    def test_motor_saturation_warning(self):
        telemetry = [
            {"north_m": 0, "east_m": 0, "alt_m": 4.0, "motors": [0.97, 0.98, 0.96, 0.95]}
            for _ in range(20)
        ]
        m = compute_flight_quality(telemetry)
        assert m.motor_saturation_percent_peak >= 95.0
        assert any("motor output" in w.lower() for w in m.warnings)

    def test_rth_closure_error(self):
        telemetry = _straight_north_telemetry(n_samples=30, target_north_m=10.0)
        # Drone never returns home — RTH error = 10m.
        m = compute_flight_quality(telemetry)
        assert m.return_to_home_error_m == pytest.approx(10.0, abs=0.1)
        assert any("closure error" in w.lower() for w in m.warnings)

    def test_overall_score_decreases_with_drift(self):
        perfect = compute_flight_quality(
            _straight_north_telemetry(50, east_drift_m=0.0),
            [NED(0, 0), NED(north_m=10, east_m=0)],
            target_altitude_m=4.0,
        )
        drifted = compute_flight_quality(
            _straight_north_telemetry(50, east_drift_m=5.0),
            [NED(0, 0), NED(north_m=10, east_m=0)],
            target_altitude_m=4.0,
        )
        assert perfect.overall_flight_quality_score > drifted.overall_flight_quality_score

    def test_grade_thresholds(self):
        assert _grade_from_score(95.0) == "A"
        assert _grade_from_score(82.0) == "B"
        assert _grade_from_score(67.0) == "C"
        assert _grade_from_score(50.0) == "D"
        assert _grade_from_score(20.0) == "F"


# ─────────────────────────────────────────────────────────────────────────────
# MonitorAgent integration
# ─────────────────────────────────────────────────────────────────────────────


class TestMonitorAgentIntegration:
    def test_compute_flight_quality_tool_aggregates(self):
        from agents.monitor_agent import _compute_flight_quality_tool

        sim_results = [
            {
                "telemetry": _straight_north_telemetry(40, east_drift_m=2.0),
                "waypoints": [
                    {"north_m": 0.0, "east_m": 0.0},
                    {"north_m": 10.0, "east_m": 0.0},
                ],
                "target_altitude_m": 4.0,
            }
        ]
        obs = _compute_flight_quality_tool(sim_results)
        assert "position_rmse_m" in obs
        assert obs["position_rmse_m"] > 0.0
        assert obs["flight_quality_score"] >= 0.0
        assert obs["flight_quality_grade"] in {"A", "B", "C", "D", "F"}

    def test_compute_flight_quality_tool_handles_no_waypoints(self):
        from agents.monitor_agent import _compute_flight_quality_tool

        sim_results = [
            {
                "telemetry": _straight_north_telemetry(20),
            }
        ]
        obs = _compute_flight_quality_tool(sim_results)
        assert obs["position_rmse_m"] == 0.0
        # Stability metrics still computed (zero, since attitude is flat).
        assert "attitude_rms_deg" in obs

    def test_compute_flight_quality_tool_empty_results(self):
        from agents.monitor_agent import _compute_flight_quality_tool

        obs = _compute_flight_quality_tool([])
        assert obs == {}

    def test_mission_metrics_score_uses_flight_quality(self):
        """Phase 11 score blends flight_quality_score when available."""
        from agents.monitor_agent import MonitorAgent

        agent = MonitorAgent.__new__(MonitorAgent)  # no LLM init
        metrics = agent._build_metrics(
            {
                "mission_success_rate": 1.0,
                "battery_efficiency": 1.0,
                "safety_score": 1.0,
                "avg_semantic_score": 80.0,
                "flight_quality_score": 90.0,
                "path_efficiency": 0.5,  # legacy fallback (lower)
            }
        )
        score_with_fq = MonitorAgent._compute_score(metrics)

        metrics2 = agent._build_metrics(
            {
                "mission_success_rate": 1.0,
                "battery_efficiency": 1.0,
                "safety_score": 1.0,
                "avg_semantic_score": 80.0,
                "flight_quality_score": 0.0,  # absent → use path_efficiency
                "path_efficiency": 0.5,
            }
        )
        score_with_legacy = MonitorAgent._compute_score(metrics2)

        # With high flight_quality_score we should score better than with
        # only the lower path_efficiency fallback.
        assert score_with_fq > score_with_legacy

    def test_monitor_dispatch_includes_new_tool(self):
        from agents.monitor_agent import _TOOL_DISPATCH

        assert "compute_flight_quality_metrics" in _TOOL_DISPATCH

    def test_indoor_inspection_regression(self):
        """Regression for the actual May-2026 e2e run: north_span=5.38,
        east_span=5.1 vs commanded north=6 east=0. The legacy evaluator
        gave grade B / score 79.9 — but with 5m east drift, the production
        evaluator must grade tracking as C or worse.
        """
        # Reconstruct a telemetry timeline that matches the live run's
        # actual span (5.38 north / 5.1 east) over 47 samples.
        n = 47
        tele = []
        for i in range(n):
            t = i / (n - 1)
            tele.append(
                {
                    "timestamp_ms": i * 1000,
                    "north_m": 5.38 * t,
                    "east_m": 5.1 * t,
                    "alt_m": 4.8,
                    "roll_deg": 0.5,
                    "pitch_deg": 0.3,
                }
            )
        m = compute_flight_quality(
            telemetry=tele,
            commanded_waypoints_ned=[NED(0, 0), NED(north_m=6, east_m=0)],
            target_altitude_m=4.0,
        )
        # 5m east drift should produce significant cross-track error.
        assert m.cross_track_error_max_m >= 3.0
        # Tracking grade should NOT be A.
        assert m.setpoint_tracking_grade != "A"
