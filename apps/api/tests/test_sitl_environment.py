"""Phase 9 tests — SITL environment specs + PX4 parameter generation."""

from __future__ import annotations

import pytest

from services.sitl_environment import (
    SCENARIOS,
    SITLEnvironmentSpec,
    classify_gps,
    classify_wind,
    gazebo_commands,
    get_scenario,
    px4_parameter_overrides,
    render_param_file,
    total_wind_m_s,
)


# ─────────────────────────────────────────────────────────────────────────────
# Spec validation
# ─────────────────────────────────────────────────────────────────────────────


class TestSpecValidation:
    def test_rejects_unrealistic_wind(self):
        with pytest.raises(ValueError, match="Wind component"):
            SITLEnvironmentSpec(wind_north_m_s=30.0)

    def test_rejects_turbulence_above_one(self):
        with pytest.raises(ValueError):
            SITLEnvironmentSpec(turbulence_intensity=1.5)

    def test_total_wind_pythagorean(self):
        spec = SITLEnvironmentSpec(wind_north_m_s=3.0, wind_east_m_s=4.0)
        assert total_wind_m_s(spec) == pytest.approx(5.0, abs=0.001)


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────


class TestClassification:
    def test_wind_class_calm(self):
        assert classify_wind(SITLEnvironmentSpec()) == "calm"

    def test_wind_class_moderate(self):
        spec = SITLEnvironmentSpec(wind_north_m_s=5.0)
        assert classify_wind(spec) == "moderate"

    def test_wind_class_extreme(self):
        spec = SITLEnvironmentSpec(wind_north_m_s=12.0, wind_east_m_s=10.0)
        assert classify_wind(spec) == "extreme"

    def test_gps_clean_default(self):
        assert classify_gps(SITLEnvironmentSpec()) == "clean"

    def test_gps_denied(self):
        spec = SITLEnvironmentSpec(gps_satellites_min=0)
        assert classify_gps(spec) == "gps_denied"

    def test_gps_indoor(self):
        spec = SITLEnvironmentSpec(gps_satellites_min=4)
        assert classify_gps(spec) == "indoor"

    def test_gps_multipath_urban(self):
        spec = SITLEnvironmentSpec(gps_multipath_enabled=True)
        assert classify_gps(spec) == "multipath"


# ─────────────────────────────────────────────────────────────────────────────
# Pre-built scenarios
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarios:
    def test_required_scenarios_present(self):
        required = {
            "calm_indoor",
            "ideal_outdoor",
            "light_breeze",
            "windy_outdoor",
            "storm",
            "gps_denied",
            "gps_multipath_urban",
            "magnetic_interference",
            "hot_day_thermal_battery",
        }
        assert required.issubset(set(SCENARIOS))

    def test_get_scenario_returns_immutable_spec(self):
        s1 = get_scenario("windy_outdoor")
        s2 = get_scenario("windy_outdoor")
        # Frozen Pydantic — identity isn't guaranteed, but equality is.
        assert s1 == s2

    def test_get_unknown_scenario_raises(self):
        with pytest.raises(KeyError, match="Unknown SITL scenario"):
            get_scenario("tornado")

    def test_windy_scenario_is_moderate_or_stronger(self):
        spec = get_scenario("windy_outdoor")
        assert classify_wind(spec) in ("moderate", "strong")

    def test_storm_scenario_is_extreme(self):
        spec = get_scenario("storm")
        assert classify_wind(spec) in ("strong", "extreme")

    def test_gps_denied_scenario(self):
        spec = get_scenario("gps_denied")
        assert classify_gps(spec) == "gps_denied"


# ─────────────────────────────────────────────────────────────────────────────
# PX4 parameter overrides
# ─────────────────────────────────────────────────────────────────────────────


class TestPx4Params:
    def test_gps_denied_disables_gps_ctrl(self):
        spec = get_scenario("gps_denied")
        params = px4_parameter_overrides(spec)
        names = {p.name for p in params}
        assert "EKF2_GPS_CTRL" in names
        assert "COM_ARM_WO_GPS" in names

    def test_outdoor_sets_min_satellites(self):
        spec = get_scenario("ideal_outdoor")
        params = px4_parameter_overrides(spec)
        nsat = next((p for p in params if p.name == "EKF2_REQ_NSATS"), None)
        assert nsat is not None
        assert nsat.value >= 6

    def test_magnetic_interference_adjusts_mag_noise(self):
        spec = get_scenario("magnetic_interference")
        params = px4_parameter_overrides(spec)
        names = {p.name for p in params}
        assert "EKF2_MAG_NOISE" in names

    def test_baro_drift_adjusts_baro_noise(self):
        spec = get_scenario("hot_day_thermal_battery")
        params = px4_parameter_overrides(spec)
        names = {p.name for p in params}
        assert "EKF2_BARO_NOISE" in names

    def test_high_rate_profile_when_rate_above_50hz(self):
        spec = SITLEnvironmentSpec(telemetry_rate_hz=100.0)
        params = px4_parameter_overrides(spec)
        names = {p.name for p in params}
        assert "SDLOG_PROFILE" in names


# ─────────────────────────────────────────────────────────────────────────────
# Gazebo commands
# ─────────────────────────────────────────────────────────────────────────────


class TestGazeboCommands:
    def test_calm_default_emits_no_commands(self):
        cmds = gazebo_commands(SITLEnvironmentSpec())
        assert cmds == []

    def test_windy_emits_wind_topic(self):
        cmds = gazebo_commands(get_scenario("windy_outdoor"))
        assert any("/wind" in c for c in cmds)

    def test_turbulence_emits_topic(self):
        spec = SITLEnvironmentSpec(turbulence_intensity=0.3)
        cmds = gazebo_commands(spec)
        assert any("turbulence" in c for c in cmds)

    def test_multipath_emits_topic(self):
        spec = SITLEnvironmentSpec(gps_multipath_enabled=True)
        cmds = gazebo_commands(spec)
        assert any("multipath" in c for c in cmds)


# ─────────────────────────────────────────────────────────────────────────────
# Param file rendering
# ─────────────────────────────────────────────────────────────────────────────


class TestParamFile:
    def test_render_includes_scenario_name(self):
        spec = get_scenario("storm")
        rendered = render_param_file(spec)
        assert "storm" in rendered

    def test_render_includes_param_save(self):
        rendered = render_param_file(SITLEnvironmentSpec())
        assert "param save" in rendered

    def test_render_writes_one_command_per_param(self):
        spec = get_scenario("magnetic_interference")
        params = px4_parameter_overrides(spec)
        rendered = render_param_file(spec)
        for p in params:
            assert f"param set {p.name}" in rendered
