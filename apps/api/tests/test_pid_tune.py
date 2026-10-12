"""Phase 10 tests — PID auto-tune agent: diagnosis, gain adjustment, ZN seed."""

from __future__ import annotations

import math

import pytest

from agents.pid_tune_agent import (
    PIDGains,
    PIDTuneAgent,
    StepResponseFit,
    TuningCycleResult,
    adjust_for_lag,
    adjust_for_oscillation,
    adjust_for_saturation,
    diagnose_axis,
    fit_step_response,
    render_param_script,
    ziegler_nichols_seed,
)


# ─────────────────────────────────────────────────────────────────────────────
# Diagnosis
# ─────────────────────────────────────────────────────────────────────────────


class TestDiagnose:
    def test_clean_axis_returns_none(self):
        d = diagnose_axis(
            "roll",
            oscillation_power=0.1,
            oscillation_freq_hz=12,
            rms_deg=0.5,
            saturation_pct_peak=70.0,
        )
        assert d is None

    def test_high_oscillation_flagged_as_oscillation(self):
        d = diagnose_axis(
            "roll",
            oscillation_power=2.5,
            oscillation_freq_hz=14,
            rms_deg=1.0,
            saturation_pct_peak=60.0,
        )
        assert d is not None
        assert d.issue == "oscillation"
        assert "reduce" in d.suggestion.lower()

    def test_low_freq_oscillation_ignored(self):
        # Below 5 Hz is too low to be a controller ringing.
        d = diagnose_axis(
            "roll",
            oscillation_power=5.0,
            oscillation_freq_hz=2.0,
            rms_deg=0.5,
            saturation_pct_peak=60.0,
        )
        assert d is None

    def test_high_rms_without_oscillation_flagged_as_lag(self):
        d = diagnose_axis(
            "pitch",
            oscillation_power=0.2,
            oscillation_freq_hz=0,
            rms_deg=5.0,
            saturation_pct_peak=70.0,
        )
        assert d is not None
        assert d.issue == "lag"

    def test_saturation_flagged(self):
        d = diagnose_axis(
            "roll",
            oscillation_power=0.0,
            oscillation_freq_hz=0,
            rms_deg=1.0,
            saturation_pct_peak=98.0,
        )
        assert d is not None
        assert d.issue == "saturation"


# ─────────────────────────────────────────────────────────────────────────────
# Gain adjusters
# ─────────────────────────────────────────────────────────────────────────────


class TestAdjusters:
    def test_oscillation_reduces_p_and_raises_d(self):
        before = PIDGains(axis="roll", p=0.15, i=0.20, d=0.003)
        after = adjust_for_oscillation(before)
        assert after.p < before.p
        assert after.d > before.d

    def test_lag_raises_p(self):
        before = PIDGains(axis="pitch", p=0.15, i=0.20, d=0.003)
        after = adjust_for_lag(before)
        assert after.p > before.p

    def test_saturation_reduces_all_terms(self):
        before = PIDGains(axis="roll", p=0.20, i=0.20, d=0.005)
        after = adjust_for_saturation(before)
        assert after.p < before.p
        assert after.i < before.i
        assert after.d < before.d

    def test_clamping_keeps_gains_in_bounds(self):
        # Push P above the envelope; adjuster must clip.
        absurd = PIDGains(axis="roll", p=0.49, i=0.20, d=0.003)
        after = adjust_for_lag(absurd)  # would push to 0.539
        assert after.p <= 0.50

    def test_clamping_keeps_d_in_bounds(self):
        absurd = PIDGains(axis="roll", p=0.15, i=0.20, d=0.019)
        after = adjust_for_oscillation(absurd)
        assert after.d <= 0.020


# ─────────────────────────────────────────────────────────────────────────────
# Ziegler-Nichols seed
# ─────────────────────────────────────────────────────────────────────────────


class TestZieglerNichols:
    def test_fit_step_response_recovers_known_FOPDT(self):
        # Synthesise a step: dead-time 0.1s, tau 0.2s, gain 1.0, sample_rate 100Hz
        sample_hz = 100.0
        dead = 0.1
        tau = 0.2
        gain = 1.0
        response: list[float] = []
        n = 100
        for i in range(n):
            t = i / sample_hz
            if t < dead:
                response.append(0.0)
            else:
                response.append(gain * (1 - math.exp(-(t - dead) / tau)))
        fit = fit_step_response(1.0, response, sample_hz)
        assert fit is not None
        assert fit.dead_time_s == pytest.approx(dead, abs=0.05)
        assert fit.time_constant_s == pytest.approx(tau, abs=0.1)

    def test_fit_returns_none_for_unresponsive_signal(self):
        # Response stays at zero — never crosses 63%.
        fit = fit_step_response(1.0, [0.0] * 50, 50.0)
        assert fit is None

    def test_fit_returns_none_for_zero_step(self):
        fit = fit_step_response(0.0, [1.0] * 20, 50.0)
        assert fit is None

    def test_zn_seed_within_bounds(self):
        fit = StepResponseFit(dead_time_s=0.05, time_constant_s=0.15, gain=1.0)
        gains = ziegler_nichols_seed(fit, "roll")
        # Within the engineering envelope
        assert 0.05 <= gains.p <= 0.50
        assert 0.0 <= gains.d <= 0.020


# ─────────────────────────────────────────────────────────────────────────────
# Agent end-to-end
# ─────────────────────────────────────────────────────────────────────────────


class TestPIDTuneAgent:
    def test_clean_metrics_produce_no_updates(self):
        agent = PIDTuneAgent()
        result = agent.tune(
            roll_oscillation_power=0.0,
            roll_oscillation_freq_hz=0.0,
            pitch_oscillation_power=0.0,
            pitch_oscillation_freq_hz=0.0,
            attitude_rms_deg=0.5,
            motor_saturation_percent_peak=70.0,
        )
        assert isinstance(result, TuningCycleResult)
        assert result.updates == []
        assert result.requires_simulation is False

    def test_oscillation_produces_axis_update(self):
        agent = PIDTuneAgent()
        result = agent.tune(
            roll_oscillation_power=2.5,
            roll_oscillation_freq_hz=12.0,
            pitch_oscillation_power=0.0,
            pitch_oscillation_freq_hz=0.0,
            attitude_rms_deg=0.5,
            motor_saturation_percent_peak=70.0,
        )
        assert len(result.updates) >= 1
        update = result.updates[0]
        assert update.axis == "roll"
        assert update.diagnosis.issue == "oscillation"
        assert update.after.p < update.before.p
        assert update.after.d >= update.before.d

    def test_saturation_pulls_all_three_axes_back(self):
        agent = PIDTuneAgent()
        result = agent.tune(
            roll_oscillation_power=0.0,
            roll_oscillation_freq_hz=0.0,
            pitch_oscillation_power=0.0,
            pitch_oscillation_freq_hz=0.0,
            attitude_rms_deg=0.5,
            motor_saturation_percent_peak=98.0,
        )
        axes = {u.axis for u in result.updates}
        assert axes == {"roll", "pitch", "yaw"}

    def test_param_commands_include_px4_prefix(self):
        agent = PIDTuneAgent()
        result = agent.tune(
            roll_oscillation_power=2.5,
            roll_oscillation_freq_hz=12.0,
            pitch_oscillation_power=0.0,
            pitch_oscillation_freq_hz=0.0,
            attitude_rms_deg=0.5,
            motor_saturation_percent_peak=70.0,
        )
        cmds = result.updates[0].px4_param_commands()
        assert any("MC_ROLLRATE_P" in c for c in cmds)
        assert any("MC_ROLLRATE_D" in c for c in cmds)

    def test_render_param_script_groups_axes(self):
        agent = PIDTuneAgent()
        result = agent.tune(
            roll_oscillation_power=2.5,
            roll_oscillation_freq_hz=12.0,
            pitch_oscillation_power=2.5,
            pitch_oscillation_freq_hz=12.0,
            attitude_rms_deg=0.5,
            motor_saturation_percent_peak=70.0,
        )
        script = render_param_script(result.updates)
        assert "param set MC_ROLLRATE_P" in script
        assert "param set MC_PITCHRATE_P" in script
        assert "param save" in script

    def test_custom_starting_gains_respected(self):
        agent = PIDTuneAgent()
        custom = {
            "roll": PIDGains(axis="roll", p=0.20, i=0.30, d=0.005),
            "pitch": PIDGains(axis="pitch", p=0.20, i=0.30, d=0.005),
            "yaw": PIDGains(axis="yaw", p=0.30, i=0.10, d=0.0),
        }
        result = agent.tune(
            roll_oscillation_power=2.5,
            roll_oscillation_freq_hz=12.0,
            pitch_oscillation_power=0.0,
            pitch_oscillation_freq_hz=0.0,
            attitude_rms_deg=0.5,
            motor_saturation_percent_peak=70.0,
            current_gains=custom,
        )
        assert result.updates[0].before.p == 0.20

    def test_iteration_counter_propagates(self):
        agent = PIDTuneAgent()
        result = agent.tune(
            roll_oscillation_power=2.5,
            roll_oscillation_freq_hz=12.0,
            pitch_oscillation_power=0.0,
            pitch_oscillation_freq_hz=0.0,
            attitude_rms_deg=0.5,
            motor_saturation_percent_peak=70.0,
            iteration=3,
        )
        assert result.iteration == 3


# ─────────────────────────────────────────────────────────────────────────────
# Phase 11 integration — agent consumes the metrics solver's output
# ─────────────────────────────────────────────────────────────────────────────


class TestPhase11Integration:
    def test_metrics_with_high_oscillation_trigger_tune(self):
        """The agent must accept the exact dict shape produced by
        ``flight_quality_solver._compute_flight_quality_tool``."""
        # Simulate a flight quality observation dict.
        observation = {
            "roll_oscillation_freq_hz": 12.5,
            "roll_oscillation_power": 3.0,
            "pitch_oscillation_freq_hz": 0.0,
            "pitch_oscillation_power": 0.0,
            "attitude_rms_deg": 1.0,
            "motor_saturation_percent_peak": 78.0,
        }
        agent = PIDTuneAgent()
        result = agent.tune(
            roll_oscillation_power=observation["roll_oscillation_power"],
            roll_oscillation_freq_hz=observation["roll_oscillation_freq_hz"],
            pitch_oscillation_power=observation["pitch_oscillation_power"],
            pitch_oscillation_freq_hz=observation["pitch_oscillation_freq_hz"],
            attitude_rms_deg=observation["attitude_rms_deg"],
            motor_saturation_percent_peak=observation["motor_saturation_percent_peak"],
        )
        assert result.updates
        assert result.updates[0].axis == "roll"
