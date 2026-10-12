"""Unit tests for the Simulation & Test Agent (Closed-Loop).

Covers:
  - SemanticConverter  (pure logic — no mocks needed)
  - EvaluationResult   (Pydantic model)
  - CodeFeedback       (Pydantic model)
  - SimulationResult / ClosedLoopResult
  - PX4Service._simulated_telemetry + _parse_waypoints_from_code
  - GazeboService (subprocess mocked)
  - SimulationTestAgent (LLM + Gazebo + PX4 all mocked)

Run:
    cd drone-prompt/apps/api
    pytest tests/test_simulation_agent.py -v
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.test_agent import (
    ClosedLoopResult,
    CodeFeedback,
    EvaluationResult,
    SimulationResult,
    SimulationTestAgent,
)
from pipelines.semantic_converter import (
    SemanticConverter,
    SemanticObservation,
    TelemetryData,
    TelemetryPoint,
)
from services.px4_service import PX4Service
from utils.mavsdk_runtime_adapter import MAVSDKRuntimeAdapter
from utils.sitl_runtime import SITLRuntime, resolve_sitl_runtime, windows_to_wsl_path
from utils.wsl_mission_runner import (
    _build_telemetry_payload,
    _ensure_mission_import_paths,
    _resolve_altitude_m,
    _resolve_horizontal_position,
    _write_telemetry_snapshot,
)
from tests.flight_fixtures import flown_mission, mission_code


@pytest.fixture(autouse=True)
def _disable_forced_real_sitl_by_default(monkeypatch):
    from config.settings import settings

    monkeypatch.delenv("DRONEPROMPT_FORCE_SITL", raising=False)
    monkeypatch.delenv("DRONEPROMPT_REQUIRE_REAL_SITL", raising=False)
    monkeypatch.setattr(settings, "sitl_force_runtime", "", raising=False)
    monkeypatch.setattr(settings, "sitl_require_real", False, raising=False)


def _runtime(
    mode: str = "mock",
    *,
    is_real: bool = False,
    host_platform: str = "Windows",
    **kwargs,
) -> SITLRuntime:
    data = {
        "mode": mode,
        "host_platform": host_platform,
        "is_real": is_real,
        "diagnostics": tuple(kwargs.pop("diagnostics", ())),
        "px4_dir": kwargs.pop("px4_dir", "/opt/px4/PX4-Autopilot"),
        "worlds_path": kwargs.pop("worlds_path", "/opt/ros/humble/share/gazebo_ros/worlds"),
        "wsl_distro": kwargs.pop("wsl_distro", ""),
        "wsl_user": kwargs.pop("wsl_user", ""),
        "wsl_python_bin": kwargs.pop("wsl_python_bin", "python3"),
        "wsl_gz_bin": kwargs.pop("wsl_gz_bin", "gz"),
    }
    data.update(kwargs)
    return SITLRuntime(**data)


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _make_points(n: int = 20, completed: bool = True) -> list[TelemetryPoint]:
    """Build a minimal realistic telemetry sequence."""
    points: list[TelemetryPoint] = []
    for i in range(n):
        t = i * 0.5
        phase = i / n
        alt = 10.0 if 0.1 < phase < 0.9 else max(0.0, 10.0 * (1 - abs(phase - 0.5) * 2))
        north = 50.0 * phase if phase < 0.5 else 50.0 * (1.0 - phase) * 2
        battery = 100.0 - i * 0.5
        points.append(
            TelemetryPoint(
                timestamp_s=round(t, 2),
                north_m=round(north, 2),
                east_m=0.0,
                alt_m=round(max(0.0, alt), 2),
                speed_ms=5.0 if 0.1 < phase < 0.9 else 1.0,
                yaw_deg=0.0,
                battery_percent=round(battery, 1),
                battery_voltage=12.6,
                flight_mode="MISSION" if 0.1 < phase < 0.9 else "AUTO.TAKEOFF",
                armed=True,
                gps_satellites=12,
            )
        )
    if completed:
        points[-1] = points[-1].model_copy(update={"alt_m": 0.0})
    return points


def _make_telemetry(
    sim_id: str = "sim001",
    n_points: int = 20,
    completed: bool = True,
    crash: bool = False,
    geofence: bool = False,
) -> TelemetryData:
    return TelemetryData(
        sim_id=sim_id,
        points=_make_points(n_points, completed),
        duration_s=n_points * 0.5,
        mission_started=True,
        mission_completed=completed,
        crash_detected=crash,
        geofence_violation=geofence,
    )


def _make_agent() -> SimulationTestAgent:
    """Return an agent with all external dependencies mocked."""
    agent = SimulationTestAgent.__new__(SimulationTestAgent)
    agent.eval_llm = MagicMock()
    agent.gazebo = MagicMock()
    agent.px4 = MagicMock()
    agent.converter = SemanticConverter(sample_every=5)
    return agent


# ─────────────────────────────────────────────────────────────────────────────
# TelemetryPoint & TelemetryData models
# ─────────────────────────────────────────────────────────────────────────────


class TestTelemetryModels:
    def test_defaults(self):
        pt = TelemetryPoint()
        assert pt.battery_percent == 100.0
        assert pt.armed is False
        assert pt.motors == []

    def test_telemetry_data_defaults(self):
        td = TelemetryData(sim_id="x")
        assert td.points == []
        assert td.mission_completed is False

    def test_custom_values(self):
        pt = TelemetryPoint(
            timestamp_s=5.0,
            north_m=25.0,
            alt_m=10.0,
            speed_ms=5.5,
            yaw_deg=45.0,
            battery_percent=75.0,
            flight_mode="MISSION",
            armed=True,
            gps_satellites=14,
        )
        assert pt.north_m == 25.0
        assert pt.yaw_deg == 45.0


# ─────────────────────────────────────────────────────────────────────────────
# SemanticConverter — compass and helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestSemanticConverterHelpers:
    def test_compass_north(self):
        assert SemanticConverter._compass_from_yaw(0.0) == "north"
        assert SemanticConverter._compass_from_yaw(360.0) == "north"

    def test_compass_east(self):
        assert SemanticConverter._compass_from_yaw(90.0) == "east"

    def test_compass_south(self):
        assert SemanticConverter._compass_from_yaw(180.0) == "south"

    def test_compass_west(self):
        assert SemanticConverter._compass_from_yaw(270.0) == "west"

    def test_compass_northeast(self):
        assert SemanticConverter._compass_from_yaw(45.0) == "northeast"

    def test_compass_from_ned_north(self):
        # Pure north: atan2(0, 100) = 0° → north
        assert SemanticConverter._compass_from_ned(100.0, 0.0) == "north"

    def test_compass_from_ned_east(self):
        # Pure east: atan2(100, 0) = 90° → east
        assert SemanticConverter._compass_from_ned(0.0, 100.0) == "east"

    def test_altitude_ground(self):
        assert "ground" in SemanticConverter._altitude_description(0.3)

    def test_altitude_normal(self):
        desc = SemanticConverter._altitude_description(15.0)
        assert "15.0" in desc

    def test_altitude_overshoot(self):
        assert "exceeded" in SemanticConverter._altitude_description(150.0)

    def test_battery_full(self):
        assert "excellent" in SemanticConverter._battery_status(95.0)

    def test_battery_low(self):
        # 35% → low (threshold: 30 ≤ x < 50)
        assert "low" in SemanticConverter._battery_status(35.0)

    def test_battery_critical(self):
        # 25% → critical (threshold: 20 ≤ x < 30)
        assert "critical" in SemanticConverter._battery_status(25.0)

    def test_speed_stopped(self):
        assert "stopped" in SemanticConverter._speed_description(0.0)

    def test_speed_over_limit(self):
        assert "excessive" in SemanticConverter._speed_description(20.0)

    def test_speed_normal(self):
        assert "m/s" in SemanticConverter._speed_description(5.0)


# ─────────────────────────────────────────────────────────────────────────────
# SemanticConverter — full conversion
# ─────────────────────────────────────────────────────────────────────────────


class TestSemanticConverterConvert:
    def setup_method(self):
        self.converter = SemanticConverter(sample_every=5)

    def test_empty_telemetry_returns_one_critical(self):
        td = TelemetryData(sim_id="x", points=[], duration_s=0.0)
        obs = self.converter.convert(td)
        assert len(obs) == 1
        assert obs[0].severity == "critical"

    def test_always_includes_first_and_last(self):
        td = _make_telemetry(n_points=12)
        obs = self.converter.convert(td, sample_every=100)  # very sparse
        timestamps = [o.timestamp_s for o in obs]
        # first point ts = 0.0, last point ts = 11 * 0.5 = 5.5 (approx)
        assert min(timestamps) == pytest.approx(0.0, abs=0.1)
        # last point should be present (mission_completed event at duration-0.1)

    def test_observations_sorted_by_timestamp(self):
        td = _make_telemetry(n_points=20, completed=True)
        obs = self.converter.convert(td)
        timestamps = [o.timestamp_s for o in obs]
        assert timestamps == sorted(timestamps)

    def test_crash_event_appended(self):
        td = _make_telemetry(n_points=10, completed=False, crash=True)
        obs = self.converter.convert(td)
        crash_obs = [o for o in obs if "crash" in o.description]
        assert len(crash_obs) == 1
        assert crash_obs[0].severity == "critical"

    def test_geofence_violation_event(self):
        td = _make_telemetry(n_points=10, completed=False, geofence=True)
        obs = self.converter.convert(td)
        geo_obs = [o for o in obs if "geofence" in o.description]
        assert len(geo_obs) == 1

    def test_mission_completed_event(self):
        td = _make_telemetry(n_points=10, completed=True)
        obs = self.converter.convert(td)
        done = [o for o in obs if "completed" in o.description]
        assert len(done) == 1
        assert done[0].severity == "info"

    def test_mission_not_completed_warning(self):
        td = _make_telemetry(n_points=10, completed=False, crash=False)
        obs = self.converter.convert(td)
        not_done = [o for o in obs if "not completed" in o.description]
        assert len(not_done) == 1
        assert not_done[0].severity == "warning"

    def test_low_battery_warning(self):
        points = _make_points(n=20)
        # Inject low-battery point near the middle
        points[10] = points[10].model_copy(update={"battery_percent": 25.0})
        td = TelemetryData(sim_id="x", points=points, duration_s=10.0, mission_completed=False)
        obs = self.converter.convert(td)
        low_bat = [o for o in obs if "low" in o.description and "battery" in o.description]
        assert len(low_bat) >= 1

    def test_movement_observation_contains_position_info(self):
        td = _make_telemetry(n_points=10)
        obs = self.converter.convert(td, sample_every=1)
        movement = [o for o in obs if o.event_type == "movement"]
        assert len(movement) > 0
        # The seconds marker should appear
        assert any("s]" in o.description for o in movement)

    def test_raw_values_populated(self):
        td = _make_telemetry(n_points=10)
        obs = self.converter.convert(td, sample_every=1)
        movement = [o for o in obs if o.event_type == "movement"]
        assert "north_m" in movement[0].raw_values
        assert "battery_pct" in movement[0].raw_values


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic model tests
# ─────────────────────────────────────────────────────────────────────────────


class TestEvaluationResult:
    def test_defaults(self):
        er = EvaluationResult(passed=True)
        assert er.score == 0.0
        assert er.errors == []

    def test_score_bounds(self):
        with pytest.raises(Exception):
            EvaluationResult(passed=True, score=101.0)
        with pytest.raises(Exception):
            EvaluationResult(passed=True, score=-1.0)

    def test_completeness_bounds(self):
        with pytest.raises(Exception):
            EvaluationResult(passed=True, completeness=1.5)

    def test_full_construction(self):
        er = EvaluationResult(
            passed=False,
            score=42.0,
            completeness=0.42,
            reason="wrong path",
            errors=["The drone flew south instead of north"],
            suggestions=["Invert the north_m sign"],
        )
        assert er.errors[0].startswith("The drone")


class TestCodeFeedback:
    def test_defaults(self):
        fb = CodeFeedback(iteration=1)
        assert fb.error_type == "unknown"
        assert fb.confidence == 0.5

    def test_confidence_bounds(self):
        with pytest.raises(Exception):
            CodeFeedback(iteration=1, confidence=1.5)

    def test_custom(self):
        fb = CodeFeedback(
            iteration=2,
            error_type="altitude",
            specific_fixes=["Adjust takeoff altitude"],
            confidence=0.8,
        )
        assert len(fb.specific_fixes) == 1


# ─────────────────────────────────────────────────────────────────────────────
# PX4Service — simulated telemetry & waypoint parsing
# ─────────────────────────────────────────────────────────────────────────────


class TestPX4ServiceSimulated:
    SAMPLE_CODE = """\
async def run(drone):
    await drone.takeoff(altitude_m=15.0)
    await drone.fly_to_relative(north_m=100.0, east_m=0.0, down_m=0.0)
    await drone.fly_to_relative(north_m=0.0, east_m=100.0, down_m=0.0)
    await drone.fly_to_relative(north_m=-100.0, east_m=0.0, down_m=0.0)
    await drone.fly_to_relative(north_m=0.0, east_m=-100.0, down_m=0.0)
    await drone.land()
"""

    def test_parse_waypoints_from_code(self):
        wps = PX4Service._parse_waypoints_from_code(self.SAMPLE_CODE)
        assert len(wps) == 4
        # First waypoint: north=100, east=0
        assert wps[0][0] == pytest.approx(100.0)
        assert wps[0][1] == pytest.approx(0.0)
        assert wps[0][2] == pytest.approx(15.0)
        # Last waypoint: cumulative north=0, east=0
        assert wps[3][0] == pytest.approx(0.0)
        assert wps[3][1] == pytest.approx(0.0)

    def test_parse_waypoints_empty_code(self):
        wps = PX4Service._parse_waypoints_from_code("async def run(): pass")
        assert wps == []

    def test_parse_waypoints_multiline_calls(self):
        code = """
async def run(drone):
    await drone.takeoff(altitude_m=10.0)
    await drone.fly_to_relative(
        north_m=10.0,
        east_m=20.0,
        down_m=0.0
    )
"""
        wps = PX4Service._parse_waypoints_from_code(code)
        assert len(wps) == 1
        assert wps[0] == (10.0, 20.0, 10.0)

    def test_parse_waypoints_negative_numbers(self):
        code = """
async def run(drone):
    await drone.takeoff(altitude_m=10.0)
    await drone.fly_to_relative(north_m=-15.5, east_m=-5.0, down_m=-2.0)
"""
        wps = PX4Service._parse_waypoints_from_code(code)
        assert len(wps) == 1
        assert wps[0] == (-15.5, -5.0, 12.0)

    def test_parse_waypoints_positional(self):
        code = """
async def run(drone):
    await drone.takeoff(10.0)
    await drone.fly_to_relative(10.0, 20.0, 0.0)
"""
        wps = PX4Service._parse_waypoints_from_code(code)
        assert len(wps) == 1
        assert wps[0] == (10.0, 20.0, 10.0)

    def test_parse_waypoints_invalid_code(self):
        # Invalid python code triggering syntax error (will use regex fallback)
        code = """
async def run(drone)
    await drone.takeoff(altitude_m=10.0)
    await drone.fly_to_relative(north_m=10.0, east_m=20.0, down_m=0.0)
"""
        wps = PX4Service._parse_waypoints_from_code(code)
        assert len(wps) == 1
        assert wps[0] == (10.0, 20.0, 10.0)

    def test_simulated_telemetry_returns_telemetry_data(self):
        td = PX4Service._simulated_telemetry("sim1", self.SAMPLE_CODE, duration_s=30.0)
        assert isinstance(td, TelemetryData)
        assert len(td.points) > 0
        assert td.sim_id == "sim1"

    def test_simulated_telemetry_points_are_non_negative_alt(self):
        td = PX4Service._simulated_telemetry("sim1", self.SAMPLE_CODE, duration_s=20.0)
        assert all(p.alt_m >= 0.0 for p in td.points)

    def test_simulated_telemetry_takeoff_altitude(self):
        """Verifies that takeoff altitude parsed from code is used."""
        td = PX4Service._simulated_telemetry("sim1", self.SAMPLE_CODE, duration_s=60.0)
        max_alt = max(p.alt_m for p in td.points)
        # Should reach close to takeoff_alt=15.0
        assert max_alt >= 10.0  # allows some simulation imprecision

    def test_estimated_duration_uses_mission_target_hint(self):
        code = "MISSION_TARGET_DURATION_S = 1200.0\n" + self.SAMPLE_CODE
        duration = PX4Service._estimate_sim_duration(code)
        assert duration >= 1200.0

    def test_simulated_telemetry_battery_drains(self):
        td = PX4Service._simulated_telemetry("sim1", self.SAMPLE_CODE, duration_s=60.0)
        assert td.points[0].battery_percent > td.points[-1].battery_percent

    def test_simulated_telemetry_no_code_uses_default(self):
        td = PX4Service._simulated_telemetry("sim1", "async def empty(): pass", duration_s=15.0)
        assert len(td.points) > 0

    def test_write_temp_code(self):
        import os

        path = PX4Service._write_temp_code("print('hello')")
        assert os.path.exists(path)
        with open(path) as f:
            assert "hello" in f.read()
        os.unlink(path)

    def test_prepare_runtime_code_wraps_literal_connect_address(self):
        code = (
            "import asyncio\n"
            "from mavsdk import System\n\n"
            "async def main():\n"
            "    drone = System()\n"
            '    await drone.connect(system_address="udp://:14540")\n'
        )
        prepared = PX4Service._prepare_runtime_code(code)

        assert "import os\n" in prepared
        assert 'os.getenv("DRONEPROMPT_MAVSDK_ADDRESS", "udp://:14540")' in prepared

    def test_prepare_runtime_code_keeps_existing_env_aware_code(self):
        code = (
            "import os\n"
            "async def main():\n"
            '    address = os.getenv("DRONEPROMPT_MAVSDK_ADDRESS", "udp://:14540")\n'
        )
        assert PX4Service._prepare_runtime_code(code) == code

    def test_mavsdk_address_uses_udpin_scheme(self):
        assert PX4Service._mavsdk_address(14550) == "udpin://0.0.0.0:14550"

    @pytest.mark.asyncio
    async def test_execute_mission_uses_simulated_fallback_when_runtime_is_mock(self):
        expected = TelemetryData(sim_id="sim-win")
        with (
            patch("services.px4_service.resolve_sitl_runtime", return_value=_runtime("mock")),
            patch.object(PX4Service, "_simulated_telemetry", return_value=expected) as sim_mock,
            patch.object(PX4Service, "_run_code_subprocess", new=AsyncMock()) as run_mock,
            patch.object(
                PX4Service, "_collect_telemetry_stream", new=AsyncMock()
            ) as telemetry_mock,
        ):
            service = PX4Service()
            result = await service.execute_mission("async def run(): pass", "sim-win")

        assert result.sim_id == "sim-win"
        assert result.telemetry_source == "simulated_fallback"
        assert result.runtime_mode == "mock"
        assert "Real SITL runtime is unavailable" in result.mission_error
        sim_mock.assert_called_once()
        run_mock.assert_not_awaited()
        telemetry_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_mission_uses_wsl_runner_when_runtime_is_wsl(self):
        # WSL telemetry must be non-empty for execute_mission to return it
        # directly; an empty payload would (correctly) trigger the synthetic
        # fallback. The test's intent is the success path, so seed one point.
        expected = TelemetryData(
            sim_id="sim-wsl",
            telemetry_source="real_wsl_sitl",
            mission_started=True,
            mission_completed=True,
            points=[TelemetryPoint(timestamp_s=0.0, alt_m=5.0)],
        )
        with (
            patch(
                "services.px4_service.resolve_sitl_runtime",
                return_value=_runtime("wsl", is_real=True, wsl_distro="Ubuntu"),
            ),
            patch.object(
                PX4Service, "_execute_wsl_mission", new=AsyncMock(return_value=expected)
            ) as wsl_mock,
            patch.object(PX4Service, "_simulated_telemetry") as sim_mock,
            patch.object(PX4Service, "_run_code_subprocess", new=AsyncMock()) as run_mock,
        ):
            service = PX4Service()
            result = await service.execute_mission("async def run(): pass", "sim-wsl")

        assert result is expected
        wsl_mock.assert_awaited_once()
        sim_mock.assert_not_called()
        run_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_mission_does_not_synthesize_fallback_when_real_wsl_is_required(self):
        expected = TelemetryData(
            sim_id="sim-wsl",
            runtime_mode="wsl",
            telemetry_source="real_wsl_sitl_empty",
            mission_error="No real telemetry was produced.",
        )
        with (
            patch(
                "services.px4_service.resolve_sitl_runtime",
                return_value=_runtime("wsl", is_real=True, wsl_distro="Ubuntu"),
            ),
            patch("services.px4_service.require_real_sitl", return_value=True),
            patch.object(
                PX4Service, "_execute_wsl_mission", new=AsyncMock(return_value=expected)
            ) as wsl_mock,
            patch.object(PX4Service, "_simulated_telemetry") as sim_mock,
        ):
            service = PX4Service()
            result = await service.execute_mission("async def run(): pass", "sim-wsl")

        assert result is expected
        wsl_mock.assert_awaited_once()
        sim_mock.assert_not_called()

    def test_telemetry_from_payload_preserves_runtime_metadata(self):
        payload = {
            "sim_id": "sim-wsl",
            "duration_s": 12.5,
            "mission_started": True,
            "mission_completed": False,
            "runtime_mode": "wsl",
            "telemetry_source": "real_wsl_sitl_partial",
            "mission_error": "Mission execution returned exit code 1",
            "warnings": ["partial telemetry"],
            "home_lat": 47.0,
            "home_lon": 8.0,
            "home_alt_m": 500.0,
            "points": [
                {
                    "timestamp_s": 0.0,
                    "north_m": 0.0,
                    "east_m": 0.0,
                    "alt_m": 0.2,
                    "lat": 47.0,
                    "lon": 8.0,
                }
            ],
        }

        result = PX4Service._telemetry_from_payload(
            payload,
            "fallback-sim-id",
            artifacts={"telemetry_payload": "/tmp/test.json"},
            extra_warnings=["runner exited non-zero"],
        )

        assert result.sim_id == "sim-wsl"
        assert result.runtime_mode == "wsl"
        assert result.telemetry_source == "real_wsl_sitl_partial"
        assert result.mission_error == "Mission execution returned exit code 1"
        assert result.home_lat == pytest.approx(47.0)
        assert result.artifacts["telemetry_payload"] == "/tmp/test.json"
        assert "partial telemetry" in result.warnings
        assert "runner exited non-zero" in result.warnings


class TestWslTelemetryFusion:
    def test_resolve_altitude_uses_odometry_when_position_is_flat(self):
        local = SimpleNamespace(position=SimpleNamespace(north_m=0.0, east_m=0.0, down_m=-0.02))
        altitude = SimpleNamespace(altitude_monotonic_m=5.1)
        odometry = SimpleNamespace(position_body=SimpleNamespace(x_m=0.0, y_m=0.0, z_m=-5.0))

        alt_m = _resolve_altitude_m(
            0.02,
            local,
            altitude,
            odometry,
            in_air=True,
        )

        # The estimator's odometry, not the raw barometer.
        assert alt_m == pytest.approx(5.0)

    def test_resolve_altitude_falls_back_to_barometer_only_when_estimator_is_flat(self):
        local = SimpleNamespace(position=SimpleNamespace(north_m=0.0, east_m=0.0, down_m=0.0))
        altitude = SimpleNamespace(altitude_monotonic_m=5.1)
        odometry = SimpleNamespace(position_body=SimpleNamespace(x_m=0.0, y_m=0.0, z_m=0.0))

        assert _resolve_altitude_m(0.0, local, altitude, odometry, in_air=True) == pytest.approx(
            5.1
        )

    def test_resolve_altitude_ignores_higher_sources_when_position_is_valid(self):
        # Hexacopter SITL: one sample at 6.1 m between 5.3 m and 5.5 m, 0.1 s apart
        # each way. The highest-of-all-sources rule reported it as the flight's peak.
        local = SimpleNamespace(position=SimpleNamespace(north_m=0.0, east_m=0.0, down_m=-5.6))
        altitude = SimpleNamespace(altitude_monotonic_m=6.1)
        odometry = SimpleNamespace(position_body=SimpleNamespace(x_m=0.0, y_m=0.0, z_m=-5.6))

        alt_m = _resolve_altitude_m(5.3, local, altitude, odometry, in_air=True)

        assert alt_m == pytest.approx(5.3)

    def test_resolve_altitude_is_zero_on_the_ground(self):
        local = SimpleNamespace(position=SimpleNamespace(north_m=0.0, east_m=0.0, down_m=-0.3))
        altitude = SimpleNamespace(altitude_monotonic_m=0.6)
        odometry = SimpleNamespace(position_body=SimpleNamespace(x_m=0.0, y_m=0.0, z_m=-0.3))

        assert _resolve_altitude_m(0.2, local, altitude, odometry, in_air=False) == 0.0

    def test_resolve_horizontal_position_prefers_odometry_when_primary_is_flat(self):
        odometry = SimpleNamespace(position_body=SimpleNamespace(x_m=10.0, y_m=5.0, z_m=-5.0))

        north_m, east_m = _resolve_horizontal_position(0.0, 0.0, odometry)

        assert north_m == pytest.approx(10.0)
        assert east_m == pytest.approx(5.0)

    def test_live_telemetry_snapshot_is_written_atomically(self, tmp_path):
        output_path = tmp_path / "telemetry.json"
        payload = _build_telemetry_payload(
            sim_id="sim-live",
            points=[{"timestamp_s": 1.0, "north_m": 0.0, "east_m": 0.0, "alt_m": 1.2}],
            elapsed_s=1.0,
            mission_done=False,
            warnings=[],
            home_lat=47.0,
            home_lon=8.0,
            home_alt_m=500.0,
        )

        _write_telemetry_snapshot(output_path, payload)

        saved = json.loads(output_path.read_text(encoding="utf-8"))
        assert saved["sim_id"] == "sim-live"
        assert saved["telemetry_source"] == "real_wsl_sitl"
        assert len(saved["points"]) == 1

    def test_empty_live_snapshot_preserves_timeout_diagnostics(self):
        payload = _build_telemetry_payload(
            sim_id="sim-empty",
            points=[],
            elapsed_s=5.0,
            mission_done=False,
            warnings=[],
            home_lat=None,
            home_lon=None,
            home_alt_m=None,
        )

        assert payload["telemetry_source"] == "real_wsl_sitl_empty"
        assert payload["mission_completed"] is False
        assert any("No telemetry points" in item for item in payload["warnings"])

    def test_wsl_runner_adds_import_paths_for_tmp_mission(self, tmp_path, monkeypatch):
        import sys
        from utils import wsl_mission_runner

        mission_path = tmp_path / "mission.py"
        monkeypatch.setattr(wsl_mission_runner, "_API_ROOT", tmp_path / "apps" / "api")
        monkeypatch.setattr(wsl_mission_runner, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(sys, "path", ["sentinel"])

        _ensure_mission_import_paths(mission_path)

        assert str(mission_path.parent) in sys.path
        assert str(tmp_path / "apps" / "api") in sys.path
        assert str(tmp_path) in sys.path


# ─────────────────────────────────────────────────────────────────────────────
# SITL runtime resolver
# ─────────────────────────────────────────────────────────────────────────────


class TestSITLRuntimeResolver:
    def test_windows_path_to_wsl_path(self):
        assert windows_to_wsl_path(r"C:\Users\demouser\demo.py") == "/mnt/c/Users/demouser/demo.py"

    @pytest.mark.asyncio
    async def test_runtime_adapter_normalizes_mavsdk_battery_fraction(self):
        async def battery_stream():
            yield SimpleNamespace(remaining_percent=0.75, voltage_v=15.2)

        system = SimpleNamespace(
            action=SimpleNamespace(),
            core=SimpleNamespace(),
            telemetry=SimpleNamespace(battery=battery_stream),
        )
        adapter = MAVSDKRuntimeAdapter(system)

        battery = await adapter.get_battery()

        assert battery.remaining_percent == pytest.approx(75.0)

    def test_resolve_runtime_selects_configured_wsl_distro(self):
        from config.settings import settings

        with (
            patch.object(settings, "sitl_runtime", "auto"),
            patch.object(settings, "sitl_wsl_distro", "Ubuntu-22.04"),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value=r"C:\Windows\System32\wsl.exe"),
            patch("utils.sitl_runtime.subprocess.run") as run_mock,
        ):
            run_mock.side_effect = [
                MagicMock(returncode=0, stdout="Ubuntu-22.04\n", stderr=""),
                MagicMock(returncode=0, stdout="DRONEPROMPT_WSL_OK", stderr=""),
            ]
            runtime = resolve_sitl_runtime()

        assert runtime.mode == "wsl"
        assert runtime.is_real is True
        assert runtime.wsl_distro == "Ubuntu-22.04"

    def test_resolve_runtime_falls_back_to_mock_when_only_docker_desktop_exists(self):
        from config.settings import settings

        with (
            patch.object(settings, "sitl_runtime", "auto"),
            patch.object(settings, "sitl_wsl_distro", ""),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value=r"C:\Windows\System32\wsl.exe"),
            patch("utils.sitl_runtime.subprocess.run") as run_mock,
        ):
            run_mock.return_value = MagicMock(
                returncode=0,
                stdout="docker-desktop\ndocker-desktop-data\n",
                stderr="",
            )
            runtime = resolve_sitl_runtime()

        assert runtime.mode == "mock"
        assert runtime.is_real is False
        assert any("No usable WSL distro" in item for item in runtime.diagnostics)

    def test_resolve_runtime_falls_back_to_mock_when_wsl_listing_is_denied(
        self, tmp_path, monkeypatch
    ):
        from config.settings import settings

        monkeypatch.setenv("DRONEPROMPT_SITL_CACHE", str(tmp_path / "missing.json"))
        monkeypatch.delenv("DRONEPROMPT_FORCE_SITL", raising=False)
        with (
            patch.object(settings, "sitl_runtime", "auto"),
            patch.object(settings, "sitl_wsl_distro", ""),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value=r"C:\Windows\System32\wsl.exe"),
            patch("utils.sitl_runtime.subprocess.run") as run_mock,
        ):
            run_mock.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Access is denied.",
            )
            runtime = resolve_sitl_runtime()

        assert runtime.mode == "mock"
        assert runtime.is_real is False
        assert all(item != "Access is denied." for item in [runtime.wsl_distro])
        assert any("Access is denied." in item for item in runtime.diagnostics)

    def test_configured_wsl_distro_is_not_trusted_when_listing_is_denied(
        self, tmp_path, monkeypatch
    ):
        from config.settings import settings

        monkeypatch.setenv("DRONEPROMPT_SITL_CACHE", str(tmp_path / "missing.json"))
        monkeypatch.delenv("DRONEPROMPT_FORCE_SITL", raising=False)
        with (
            patch.object(settings, "sitl_runtime", "wsl"),
            patch.object(settings, "sitl_wsl_distro", "Ubuntu"),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value=r"C:\Windows\System32\wsl.exe"),
            patch("utils.sitl_runtime.subprocess.run") as run_mock,
        ):
            run_mock.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Access is denied.",
            )
            runtime = resolve_sitl_runtime()

        assert runtime.mode == "mock"
        assert runtime.is_real is False
        assert runtime.wsl_distro == ""
        assert any("Access is denied." in item for item in runtime.diagnostics)

    def test_force_sitl_wsl_skips_listing_but_probes_execution(self, monkeypatch):
        from config.settings import settings

        monkeypatch.setenv("DRONEPROMPT_FORCE_SITL", "wsl")
        # `shutil.which` must be patched: on a Linux CI runner `wsl.exe` is
        # not on PATH, so `_probe_wsl_execution` returns early without ever
        # calling `subprocess.run` — making `run_mock.assert_called_once()`
        # fail with "Called 0 times". Patching it keeps the test
        # host-OS-independent, matching every sibling test in this class.
        with (
            patch.object(settings, "sitl_runtime", "auto"),
            patch.object(settings, "sitl_wsl_distro", "Ubuntu-22.04"),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value=r"C:\Windows\System32\wsl.exe"),
            patch("utils.sitl_runtime.subprocess.run") as run_mock,
        ):
            run_mock.return_value = MagicMock(returncode=0, stdout="DRONEPROMPT_WSL_OK", stderr="")
            runtime = resolve_sitl_runtime()

        run_mock.assert_called_once()
        assert runtime.mode == "wsl"
        assert runtime.is_real is True
        assert runtime.wsl_distro == "Ubuntu-22.04"

    def test_force_sitl_wsl_falls_back_when_execution_is_denied(self, monkeypatch):
        from config.settings import settings

        monkeypatch.setenv("DRONEPROMPT_FORCE_SITL", "wsl")
        with (
            patch.object(settings, "sitl_runtime", "auto"),
            patch.object(settings, "sitl_wsl_distro", "Ubuntu-22.04"),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value=r"C:\Windows\System32\wsl.exe"),
            patch("utils.sitl_runtime.subprocess.run") as run_mock,
        ):
            run_mock.return_value = MagicMock(returncode=1, stdout="", stderr="Access is denied.")
            runtime = resolve_sitl_runtime()

        assert runtime.mode == "mock"
        assert runtime.is_real is False
        assert any("could not execute distro" in item for item in runtime.diagnostics)

    def test_force_sitl_wsl_raises_when_real_is_required_and_execution_is_denied(self, monkeypatch):
        from config.settings import settings

        monkeypatch.setenv("DRONEPROMPT_FORCE_SITL", "wsl")
        monkeypatch.setenv("DRONEPROMPT_REQUIRE_REAL_SITL", "true")
        with (
            patch.object(settings, "sitl_runtime", "auto"),
            patch.object(settings, "sitl_wsl_distro", "Ubuntu-22.04"),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value=r"C:\Windows\System32\wsl.exe"),
            patch("utils.sitl_runtime.subprocess.run") as run_mock,
        ):
            run_mock.return_value = MagicMock(returncode=1, stdout="", stderr="Access is denied.")
            with pytest.raises(RuntimeError, match="Real PX4/Gazebo SITL was required"):
                resolve_sitl_runtime()

    def test_resolve_runtime_uses_cached_wsl_when_listing_is_denied_but_execution_works(
        self, tmp_path, monkeypatch
    ):
        from config.settings import settings

        cache_path = tmp_path / "sitl_runtime.json"
        cache_path.write_text(json.dumps({"distros": ["Ubuntu-22.04"]}), encoding="utf-8")
        monkeypatch.delenv("DRONEPROMPT_FORCE_SITL", raising=False)
        monkeypatch.setenv("DRONEPROMPT_SITL_CACHE", str(cache_path))

        with (
            patch.object(settings, "sitl_runtime", "auto"),
            patch.object(settings, "sitl_wsl_distro", ""),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value=r"C:\Windows\System32\wsl.exe"),
            patch("utils.sitl_runtime.subprocess.run") as run_mock,
        ):
            run_mock.side_effect = [
                MagicMock(returncode=1, stdout="", stderr="Access is denied."),
                MagicMock(returncode=0, stdout="DRONEPROMPT_WSL_OK", stderr=""),
            ]
            runtime = resolve_sitl_runtime()

        assert runtime.mode == "wsl"
        assert runtime.is_real is True
        assert runtime.wsl_distro == "Ubuntu-22.04"
        assert any("Using cached WSL distro" in item for item in runtime.diagnostics)

    def test_cached_wsl_falls_back_when_execution_is_denied(self, tmp_path, monkeypatch):
        from config.settings import settings

        cache_path = tmp_path / "sitl_runtime.json"
        cache_path.write_text(json.dumps({"distros": ["Ubuntu-22.04"]}), encoding="utf-8")
        monkeypatch.delenv("DRONEPROMPT_FORCE_SITL", raising=False)
        monkeypatch.setenv("DRONEPROMPT_SITL_CACHE", str(cache_path))

        with (
            patch.object(settings, "sitl_runtime", "auto"),
            patch.object(settings, "sitl_wsl_distro", ""),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value=r"C:\Windows\System32\wsl.exe"),
            patch("utils.sitl_runtime.subprocess.run") as run_mock,
        ):
            # The exec probe retries a few times (a cold distro may boot slowly);
            # every attempt here is denied, so it must still fall back to mock.
            run_mock.return_value = MagicMock(returncode=1, stdout="", stderr="Access is denied.")
            runtime = resolve_sitl_runtime()

        assert runtime.mode == "mock"
        assert runtime.is_real is False
        assert any("could not execute distro" in item for item in runtime.diagnostics)

    def test_docker_runtime_falls_back_to_mock_when_the_image_is_missing(self, monkeypatch):
        """Docker mode starts one container per run; without the image it is not real."""
        from config.settings import settings

        monkeypatch.delenv("DRONEPROMPT_SKIP_DOCKER_PROBE", raising=False)
        monkeypatch.delenv("ELJAZARI_SKIP_DOCKER_PROBE", raising=False)
        with (
            patch.object(settings, "sitl_runtime", "docker"),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value="docker.exe"),
            patch("utils.sitl_runtime.subprocess.run") as run_mock,
        ):
            run_mock.return_value = MagicMock(returncode=1, stdout="", stderr="No such image")
            runtime = resolve_sitl_runtime()

        assert runtime.mode == "mock"
        assert runtime.is_real is False
        assert any("No such image" in item for item in runtime.diagnostics)

    def test_docker_runtime_is_real_when_the_image_exists(self, monkeypatch):
        from config.settings import settings

        monkeypatch.delenv("DRONEPROMPT_SKIP_DOCKER_PROBE", raising=False)
        monkeypatch.delenv("ELJAZARI_SKIP_DOCKER_PROBE", raising=False)
        with (
            patch.object(settings, "sitl_runtime", "docker"),
            patch.object(settings, "sitl_docker_image", "eljazari-sim:test"),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value="docker.exe"),
            patch("utils.sitl_runtime.subprocess.run") as run_mock,
        ):
            run_mock.return_value = MagicMock(returncode=0, stdout="sha256:abc", stderr="")
            runtime = resolve_sitl_runtime()

        assert runtime.mode == "docker"
        assert runtime.is_real is True
        assert runtime.px4_dir == "/opt/px4/PX4-Autopilot"
        assert any("eljazari-sim:test" in item for item in runtime.diagnostics)
        assert run_mock.call_args.args[0][:3] == ["docker", "image", "inspect"]

    def test_docker_runtime_skip_probe_env_bypasses_container_check(self, monkeypatch):
        from config.settings import settings

        monkeypatch.setenv("DRONEPROMPT_SKIP_DOCKER_PROBE", "1")
        with (
            patch.object(settings, "sitl_runtime", "docker"),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
        ):
            runtime = resolve_sitl_runtime()

        assert runtime.mode == "docker"
        assert runtime.is_real is True

    def test_docker_runtime_falls_back_when_docker_cli_missing(self, monkeypatch):
        from config.settings import settings

        monkeypatch.delenv("DRONEPROMPT_SKIP_DOCKER_PROBE", raising=False)
        monkeypatch.delenv("ELJAZARI_SKIP_DOCKER_PROBE", raising=False)
        with (
            patch.object(settings, "sitl_runtime", "docker"),
            patch("utils.sitl_runtime.platform.system", return_value="Windows"),
            patch("utils.sitl_runtime.shutil.which", return_value=None),
        ):
            runtime = resolve_sitl_runtime()

        assert runtime.mode == "mock"
        assert runtime.is_real is False
        assert any("docker CLI" in item for item in runtime.diagnostics)


# ─────────────────────────────────────────────────────────────────────────────
# GazeboService — port allocation & state tracking
# ─────────────────────────────────────────────────────────────────────────────


class TestGazeboService:
    @pytest.mark.asyncio
    async def test_every_launch_uses_px4_instance_zero_ports(self):
        """One simulator per host; PX4 instance 0 always sends to 14540/14550."""
        from services.gazebo_service import GazeboService

        with patch("services.gazebo_service.resolve_sitl_runtime", return_value=_runtime("mock")):
            svc = GazeboService()
        first = await svc.launch_world({})
        second = await svc.launch_world({})
        assert svc.get_mavlink_port(first) == svc.get_mavlink_port(second) == 14550
        assert svc.get_mavsdk_address(second) == "udpin://0.0.0.0:14540"

    def test_select_world_default(self):
        from services.gazebo_service import GazeboService

        assert GazeboService._select_world({}, None) == "default.sdf"

    def test_select_world_by_environment(self):
        from services.gazebo_service import GazeboService

        assert GazeboService._select_world({"environment": "urban"}, None) == "default.sdf"

    def test_select_world_explicit(self):
        from services.gazebo_service import GazeboService

        assert GazeboService._select_world({}, "forest") == "forest.sdf"

    def test_select_world_unknown_falls_back(self):
        from services.gazebo_service import GazeboService

        assert GazeboService._select_world({"environment": "mars"}, None) == "default.sdf"

    def test_px4_sim_model_prefixes_gz(self):
        from services.gazebo_service import GazeboService, SimProcess

        with patch("services.gazebo_service.resolve_sitl_runtime", return_value=_runtime("mock")):
            svc = GazeboService()

        sim = SimProcess(sim_id="sim001", config={"model": "x500"})
        assert svc._px4_sim_model(sim) == "gz_x500"

    def test_px4_world_name_uses_sdf_stem(self):
        from services.gazebo_service import GazeboService, SimProcess

        sim = SimProcess(sim_id="sim001", world_name="default.sdf")
        assert GazeboService._world_name_for_px4(sim) == "default"

    def test_prepare_log_paths_populates_artifact_logs(self):
        from services.gazebo_service import GazeboService, SimProcess

        sim = SimProcess(sim_id="sim001")
        GazeboService._prepare_log_paths(sim)

        assert sim.artifact_dir
        assert sim.gz_stdout_log.endswith("gazebo.stdout.log")
        assert sim.gz_stderr_log.endswith("gazebo.stderr.log")
        assert sim.px4_stdout_log.endswith("px4.stdout.log")
        assert sim.px4_stderr_log.endswith("px4.stderr.log")

    def test_get_available_worlds(self):
        from services.gazebo_service import GazeboService

        with patch("services.gazebo_service.resolve_sitl_runtime", return_value=_runtime("mock")):
            svc = GazeboService()
        worlds = svc.get_available_worlds()
        assert "default" in worlds
        assert "urban" in worlds
        assert "forest" in worlds

    @pytest.mark.asyncio
    async def test_launch_and_stop_headless(self):
        from services.gazebo_service import GazeboService

        with patch("services.gazebo_service.resolve_sitl_runtime", return_value=_runtime("mock")):
            svc = GazeboService()

            sim_id = await svc.launch_world({"environment": "default"})
        assert isinstance(sim_id, str)
        assert len(sim_id) == 8
        assert await svc.is_running(sim_id)
        assert svc.active_sim_count() == 1

        await svc.stop_world(sim_id)
        assert not await svc.is_running(sim_id)
        assert svc.active_sim_count() == 0

    @pytest.mark.asyncio
    async def test_get_mavlink_port(self):
        from services.gazebo_service import GazeboService

        with patch("services.gazebo_service.resolve_sitl_runtime", return_value=_runtime("mock")):
            svc = GazeboService()
            sim_id = await svc.launch_world({})
        port = svc.get_mavlink_port(sim_id)
        assert port >= 14550
        await svc.stop_world(sim_id)

    @pytest.mark.asyncio
    async def test_get_mavsdk_address(self):
        from services.gazebo_service import GazeboService

        with patch("services.gazebo_service.resolve_sitl_runtime", return_value=_runtime("mock")):
            svc = GazeboService()
            sim_id = await svc.launch_world({})
        address = svc.get_mavsdk_address(sim_id)
        assert address == "udpin://0.0.0.0:14540"
        await svc.stop_world(sim_id)

    @pytest.mark.asyncio
    async def test_get_mavlink_port_unknown_raises(self):
        from services.gazebo_service import GazeboService

        with patch("services.gazebo_service.resolve_sitl_runtime", return_value=_runtime("mock")):
            svc = GazeboService()
        with pytest.raises(KeyError):
            svc.get_mavlink_port("nonexistent")
        with pytest.raises(KeyError):
            svc.get_mavsdk_address("nonexistent")

    @pytest.mark.asyncio
    async def test_mock_sims_are_tracked_separately_on_fixed_ports(self):
        # Real simulators are serialized per host (F10): PX4 instance 0 always
        # uses its default ports, so per-launch port striding was never real.
        from services.gazebo_service import GazeboService

        with patch("services.gazebo_service.resolve_sitl_runtime", return_value=_runtime("mock")):
            svc = GazeboService()

            id1 = await svc.launch_world({})
            id2 = await svc.launch_world({})
        assert id1 != id2
        assert svc.get_mavlink_port(id1) == svc.get_mavlink_port(id2) == 14550
        assert svc.active_sim_count() == 2

        await svc.stop_world(id1)
        await svc.stop_world(id2)

    @pytest.mark.asyncio
    async def test_launch_world_uses_real_runtime_when_wsl_is_available(self):
        from services.gazebo_service import GazeboService

        runtime = _runtime("wsl", is_real=True, wsl_distro="Ubuntu")
        with (
            patch("services.gazebo_service.resolve_sitl_runtime", return_value=runtime),
            patch.object(GazeboService, "_start_gazebo", new=AsyncMock()) as gz_mock,
            patch.object(GazeboService, "_prepare_wsl_px4_rootfs", new=AsyncMock()) as prep_mock,
            patch.object(GazeboService, "_start_px4_sitl", new=AsyncMock()) as px4_mock,
            patch.object(GazeboService, "_wait_until_ready", new=AsyncMock()) as wait_mock,
        ):
            svc = GazeboService()
            sim_id = await svc.launch_world({"environment": "default"})

        assert isinstance(sim_id, str)
        gz_mock.assert_awaited_once()
        prep_mock.assert_awaited_once()
        px4_mock.assert_awaited_once()
        wait_mock.assert_awaited_once()

    def test_runtime_summary_includes_artifacts_lookup_when_running(self):
        from services.gazebo_service import GazeboService, SimProcess

        with patch("services.gazebo_service.resolve_sitl_runtime", return_value=_runtime("mock")):
            svc = GazeboService()

        sim = SimProcess(sim_id="sim001")
        GazeboService._prepare_log_paths(sim)
        svc._sims[sim.sim_id] = sim

        artifacts = svc.get_sim_artifacts("sim001")
        assert artifacts["px4_stderr"].endswith("px4.stderr.log")
        assert artifacts["gz_stdout"].endswith("gazebo.stdout.log")


# ─────────────────────────────────────────────────────────────────────────────
# SimulationTestAgent._parse_evaluation
# ─────────────────────────────────────────────────────────────────────────────


class TestParseEvaluation:
    def setup_method(self):
        self.agent = _make_agent()

    def test_valid_json_block(self):
        raw = '```json\n{"passed": true, "score": 85, "completeness": 0.85, "reason": "ok", "errors": [], "suggestions": []}\n```'
        er = self.agent._parse_evaluation(raw)
        assert er.passed is True
        assert er.score == 85.0
        assert er.completeness == pytest.approx(0.85)

    def test_raw_json_without_backticks(self):
        raw = '{"passed": false, "score": 40, "completeness": 0.4, "reason": "failed", "errors": ["altitude wrong"], "suggestions": []}'
        er = self.agent._parse_evaluation(raw)
        assert er.passed is False
        assert er.errors == ["altitude wrong"]

    def test_no_json_fallback_to_text_detection(self):
        er = self.agent._parse_evaluation("the drone completed the mission passed=true")
        assert er.passed is True

    def test_no_json_failed_fallback(self):
        er = self.agent._parse_evaluation("the drone failed to complete the mission")
        assert er.passed is False

    def test_malformed_json_fallback(self):
        er = self.agent._parse_evaluation("```json\n{bad json\n```")
        assert isinstance(er, EvaluationResult)
        assert er.raw_llm_response != ""

    def test_raw_response_preserved(self):
        raw = '{"passed": true, "score": 90, "completeness": 0.9, "reason": "r", "errors": [], "suggestions": []}'
        er = self.agent._parse_evaluation(raw)
        assert er.raw_llm_response == raw


# ─────────────────────────────────────────────────────────────────────────────
# SimulationTestAgent._classify_error_type
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyErrorType:
    def test_altitude_arabic(self):
        assert SimulationTestAgent._classify_error_type(["altitude wrong"]) == "altitude"

    def test_altitude_english(self):
        assert SimulationTestAgent._classify_error_type(["altitude exceeded"]) == "altitude"

    def test_speed(self):
        assert SimulationTestAgent._classify_error_type(["speed excessive"]) == "speed"

    def test_path(self):
        assert SimulationTestAgent._classify_error_type(["path incorrect"]) == "path"

    def test_battery(self):
        assert SimulationTestAgent._classify_error_type(["battery depleted"]) == "battery"

    def test_geofence(self):
        assert SimulationTestAgent._classify_error_type(["geofence violation"]) == "geofence"

    def test_landing(self):
        assert SimulationTestAgent._classify_error_type(["landing incomplete"]) == "mission_logic"

    def test_unknown(self):
        assert SimulationTestAgent._classify_error_type(["unspecified error"]) == "unknown"

    def test_empty(self):
        assert SimulationTestAgent._classify_error_type([]) == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# SimulationTestAgent._build_fix_suggestions
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildFixSuggestions:
    def test_altitude_error_generates_fix(self):
        fixes = SimulationTestAgent._build_fix_suggestions(["altitude wrong"], [])
        assert any("takeoff" in f or "altitude" in f for f in fixes)

    def test_battery_error_generates_fix(self):
        fixes = SimulationTestAgent._build_fix_suggestions(["battery low"], [])
        assert any("battery" in f for f in fixes)

    def test_landing_error_generates_fix(self):
        fixes = SimulationTestAgent._build_fix_suggestions(["landing missing"], [])
        assert any("land" in f for f in fixes)

    def test_llm_suggestions_included(self):
        fixes = SimulationTestAgent._build_fix_suggestions(
            [], ["Adjust takeoff altitude", "Add battery check"]
        )
        assert "Adjust takeoff altitude" in fixes

    def test_max_five_fixes(self):
        errors = ["wrong path", "altitude", "speed", "battery", "landing incomplete"]
        fixes = SimulationTestAgent._build_fix_suggestions(errors, [])
        assert len(fixes) <= 5

    def test_deduplication(self):
        # Same error twice should yield same fix once
        fixes = SimulationTestAgent._build_fix_suggestions(["altitude wrong", "altitude wrong"], [])
        assert len(fixes) == len(set(fixes))


# ─────────────────────────────────────────────────────────────────────────────
# SimulationTestAgent._feedback_to_prompt
# ─────────────────────────────────────────────────────────────────────────────


class TestFeedbackToPrompt:
    def test_contains_iteration(self):
        fb = CodeFeedback(
            iteration=3,
            error_type="path",
            description="wrong path",
            specific_fixes=["Invert north_m"],
        )
        prompt = SimulationTestAgent._feedback_to_prompt(fb)
        assert "3" in prompt
        assert "Invert north_m" in prompt

    def test_contains_error_type(self):
        fb = CodeFeedback(iteration=1, error_type="altitude")
        prompt = SimulationTestAgent._feedback_to_prompt(fb)
        assert "altitude" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# SimulationTestAgent integration (mocked Gazebo + PX4 + LLM)
# ─────────────────────────────────────────────────────────────────────────────

PASS_EVAL_JSON = json.dumps(
    {
        "passed": True,
        "score": 90,
        "completeness": 0.9,
        "reason": "Mission completed successfully",
        "errors": [],
        "suggestions": [],
    }
)

FAIL_EVAL_JSON = json.dumps(
    {
        "passed": False,
        "score": 45,
        "completeness": 0.4,
        "reason": "wrong path",
        "errors": ["The drone flew south instead of north"],
        "suggestions": ["Invert the north_m sign in fly_to_relative"],
    }
)


# The template's code for a 10 m hover, and a synthetic flight that follows it:
# the gate compares every flight with its code's mission.
HOVER_CODE = mission_code(altitude_m=10.0, hold_s=2.0)


def _flown_points() -> list[TelemetryPoint]:
    return [TelemetryPoint(**p, armed=True) for p in flown_mission(altitude_m=10.0, hold_s=2.0)]


def _agent_with_mocks(
    eval_response: str = PASS_EVAL_JSON,
    mission_completed: bool = True,
) -> SimulationTestAgent:
    agent = _make_agent()
    # Mock Gazebo
    agent.gazebo.launch_world = AsyncMock(return_value="simABC")
    agent.gazebo.stop_world = AsyncMock()
    agent.gazebo.get_mavlink_port = MagicMock(return_value=14550)
    agent.gazebo.get_mavsdk_address = MagicMock(return_value="udpin://0.0.0.0:14540")
    # Mock PX4
    # Synthetic fixture declaring a successful backend source for positive-path tests.
    telemetry = _make_telemetry(completed=mission_completed).model_copy(
        update={
            "runtime_mode": "wsl",
            "telemetry_source": "real_wsl_sitl",
            "mission_isolation": "linux_namespaces",
        }
    )
    if mission_completed:
        telemetry = telemetry.model_copy(update={"points": _flown_points()})
    agent.px4.execute_mission = AsyncMock(return_value=telemetry)
    # Mock LLM
    agent.eval_llm.ainvoke = AsyncMock(return_value=f"```json\n{eval_response}\n```")
    return agent


class TestSimulationTestAgentIntegration:
    @pytest.mark.asyncio
    async def test_run_simulation_returns_result(self):
        agent = _agent_with_mocks()
        result = await agent.run_simulation("code here", {}, iteration=1)
        assert isinstance(result, SimulationResult)
        assert result.sim_id == "simABC"
        assert len(result.observations) > 0
        agent.gazebo.stop_world.assert_called_once_with("simABC")

    @pytest.mark.asyncio
    async def test_run_simulation_uses_generated_mission_timeout(self):
        agent = _agent_with_mocks()
        code = "MISSION_TARGET_DURATION_S = 1200.0\nasync def run_mission(drone): return True\n"
        await agent.run_simulation(code, {}, iteration=1)

        kwargs = agent.px4.execute_mission.await_args.kwargs
        # 1200s target + 60s headroom = 1260s, capped at MISSION_TIMEOUT_MAX_S = 600.0
        assert kwargs["timeout_s"] == pytest.approx(600.0)

    @pytest.mark.asyncio
    async def test_run_single_returns_telemetry_metadata(self):
        agent = _agent_with_mocks(mission_completed=False)
        telemetry = _make_telemetry(completed=False).model_copy(
            update={
                "runtime_mode": "wsl",
                "telemetry_source": "real_wsl_sitl_partial",
                "mission_error": "TimeoutError",
                "warnings": ["mission timeout"],
                "artifacts": {"telemetry_payload": "/tmp/payload.json"},
            }
        )
        agent.px4.execute_mission = AsyncMock(return_value=telemetry)

        result = await agent.run_single("async def run_mission(drone): return False")

        assert result["mission_completed"] is False
        assert result["telemetry_source"] == "real_wsl_sitl_partial"
        assert result["mission_error"] == "TimeoutError"
        assert result["warnings"] == ["mission timeout"]
        assert result["artifacts"]["telemetry_payload"] == "/tmp/payload.json"

    @pytest.mark.asyncio
    async def test_evaluate_mission_passes(self):
        agent = _agent_with_mocks(eval_response=PASS_EVAL_JSON)
        td = _make_telemetry()
        obs = agent.converter.convert(td)
        ev = await agent.evaluate_mission(obs, "Fly 50m north")
        assert ev.passed is True
        assert ev.score == 90.0

    @pytest.mark.asyncio
    async def test_evaluate_mission_fails(self):
        agent = _agent_with_mocks(eval_response=FAIL_EVAL_JSON)
        td = _make_telemetry(completed=False)
        obs = agent.converter.convert(td)
        ev = await agent.evaluate_mission(obs, "Fly 50m north")
        assert ev.passed is False
        assert "north" in " ".join(ev.errors).lower()

    @pytest.mark.asyncio
    async def test_generate_feedback_altitude(self):
        agent = _make_agent()
        ev = EvaluationResult(passed=False, score=30.0, errors=["altitude wrong"], suggestions=[])
        fb = await agent.generate_feedback(ev, iteration=2)
        assert fb.error_type == "altitude"
        assert fb.iteration == 2
        assert len(fb.specific_fixes) >= 1

    @pytest.mark.asyncio
    async def test_closed_loop_single_pass_success(self):
        agent = _agent_with_mocks(eval_response=PASS_EVAL_JSON, mission_completed=True)
        result = await agent.run_closed_loop(
            initial_code_str=HOVER_CODE,
            drone_config={},
            mission_description="Fly 50m north",
            max_iterations=5,
        )
        assert isinstance(result, ClosedLoopResult)
        assert result.final_passed is True
        assert result.total_iterations == 1  # succeeded on first try
        assert "success" in result.convergence_reason

    @pytest.mark.asyncio
    async def test_closed_loop_no_generator_stops_after_fail(self):
        agent = _agent_with_mocks(eval_response=FAIL_EVAL_JSON, mission_completed=False)
        result = await agent.run_closed_loop(
            initial_code_str="code",
            drone_config={},
            mission_description="Fly 50m north",
            code_generator_fn=None,
            max_iterations=5,
        )
        assert result.total_iterations == 1
        assert result.final_passed is False
        assert "single-iteration" in result.convergence_reason

    @pytest.mark.asyncio
    async def test_closed_loop_with_generator_retries(self):
        # First call fails, second passes
        call_count = {"n": 0}
        eval_responses = [FAIL_EVAL_JSON, PASS_EVAL_JSON]

        async def fake_eval(prompt: str) -> str:
            resp = f"```json\n{eval_responses[min(call_count['n'], 1)]}\n```"
            call_count["n"] += 1
            return resp

        agent = _agent_with_mocks()
        agent.eval_llm.ainvoke = fake_eval

        async def gen_fn(mission: str, feedback: str) -> str:
            return HOVER_CODE

        result = await agent.run_closed_loop(
            initial_code_str=HOVER_CODE,
            drone_config={},
            mission_description="Fly 50m north",
            code_generator_fn=gen_fn,
            max_iterations=3,
        )
        assert result.total_iterations == 2
        assert result.final_passed is True

    @pytest.mark.asyncio
    async def test_closed_loop_best_iteration_tracked(self):
        scores = [20.0, 55.0, 80.0]
        call_count = {"n": 0}

        async def fake_eval(prompt: str) -> str:
            idx = min(call_count["n"], len(scores) - 1)
            resp = json.dumps(
                {
                    "passed": False,
                    "score": scores[idx],
                    "completeness": scores[idx] / 100,
                    "reason": "test",
                    "errors": ["path"],
                    "suggestions": [],
                }
            )
            call_count["n"] += 1
            return f"```json\n{resp}\n```"

        agent = _agent_with_mocks()
        agent.eval_llm.ainvoke = fake_eval

        async def gen_fn(mission: str, feedback: str) -> str:
            return HOVER_CODE

        result = await agent.run_closed_loop(
            initial_code_str=HOVER_CODE,
            drone_config={},
            mission_description="Mission",
            code_generator_fn=gen_fn,
            max_iterations=3,
        )
        assert result.best_iteration == 3  # highest score at iteration 3

    @pytest.mark.asyncio
    async def test_closed_loop_generator_failure_stops(self):
        agent = _agent_with_mocks(eval_response=FAIL_EVAL_JSON)

        async def failing_gen(mission: str, feedback: str) -> str:
            raise RuntimeError("LLM unavailable")

        result = await agent.run_closed_loop(
            initial_code_str="code",
            drone_config={},
            mission_description="Mission",
            code_generator_fn=failing_gen,
            max_iterations=5,
        )
        assert result.total_iterations == 1
        assert "failed" in result.convergence_reason

    @pytest.mark.asyncio
    async def test_generate_semantic_observations_delegates_to_converter(self):
        agent = _make_agent()
        td = _make_telemetry()
        obs = await agent.generate_semantic_observations(td)
        assert isinstance(obs, list)
        assert len(obs) > 0
        assert all(isinstance(o, SemanticObservation) for o in obs)


def test_migrate_telemetry_fields():
    from routers.simulation import _migrate_telemetry_fields

    # Normal case: has battery_percent, doesn't have battery_pct
    payload = {"battery_percent": 85.5, "alt": 10.0}
    migrated = _migrate_telemetry_fields(payload)
    assert migrated["battery_pct"] == 85.5
    assert migrated["battery_percent"] == 85.5

    # Case: already has battery_pct
    payload = {"battery_percent": 85.5, "battery_pct": 90.0}
    migrated = _migrate_telemetry_fields(payload)
    assert migrated["battery_pct"] == 90.0

    # Case: missing battery_percent
    payload = {"alt": 10.0}
    migrated = _migrate_telemetry_fields(payload)
    assert "battery_pct" not in migrated


# ─────────────────────────────────────────────────────────────────────────────
# WSL PX4 rootfs patch
# ─────────────────────────────────────────────────────────────────────────────


# Trimmed to the structure the patch anchors on, in upstream's own wording.
# PX4 aims the offboard link at 14540 because that is where MAVSDK listens.
_PRISTINE_RC_MAVLINK = """#!/bin/sh
# GCS link
udp_gcs_port_local=$((18570+px4_instance))
udp_gcs_port_remote=$((14550+px4_instance))

# API/Offboard link
udp_offboard_port_local=$((14580+px4_instance))
udp_offboard_port_remote=$((14540+px4_instance))

udp_onboard_payload_port_local=$((14280+px4_instance))
udp_onboard_payload_port_remote=$((14030+px4_instance))

mavlink start -x -u $udp_gcs_port_local -r 4000000 -f
mavlink stream -r 50 -s POSITION_TARGET_LOCAL_NED -u $udp_gcs_port_local

mavlink start -x -u $udp_offboard_port_local -r 4000000 -f -m onboard -o $udp_offboard_port_remote

mavlink start -x -u $udp_onboard_payload_port_local -r 4000 -f -m onboard -o $udp_onboard_payload_port_remote
"""


class TestWslMavlinkRootfsPatch:
    """_prepare_wsl_px4_rootfs() must leave exactly one link on MAVSDK's port.

    This is the only automated guard on that file. Nothing in CI runs SITL, and
    the two bugs it covers both shipped green:

      * a second `-m custom` link was injected alongside PX4's own, so two
        channels answered one client and the sequenced mission upload handshake
        collapsed with PROTOCOL_ERROR;
      * moving PX4's link off 14540 to fix that left only the custom link, whose
        stream set starts EMPTY — no SYS_STATUS, so MAVSDK never saw sensor
        health and every mission died in preflight instead.

    Both are invisible to lint, types and unit tests of anything else.
    """

    @staticmethod
    async def _apply(tmp_path, *, mavlink_target_host="127.0.0.1"):
        """Run the real patch against a pristine file and return the result."""
        import services.gazebo_service as gz_mod
        from services.gazebo_service import GazeboService, SimProcess

        px4_dir = tmp_path / "PX4-Autopilot"
        source = px4_dir / "ROMFS/px4fmu_common/init.d-posix/px4-rc.mavlink"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(_PRISTINE_RC_MAVLINK, encoding="utf-8")

        target = px4_dir / "build/px4_sitl_default/rootfs/etc/init.d-posix/px4-rc.mavlink"
        target.parent.mkdir(parents=True, exist_ok=True)

        with patch(
            "services.gazebo_service.resolve_sitl_runtime",
            return_value=_runtime(
                "wsl",
                px4_dir=str(px4_dir).replace("\\", "/"),
                wsl_distro="Ubuntu",  # wsl_prefix() refuses to build without one
                wsl_user="droneprompt",
            ),
        ):
            svc = GazeboService()

        # The patch module runs under WSL's interpreter; run the same module here
        # with the same arguments, so the assertions cover what actually ships.
        async def run_locally(cmd, **_kwargs):
            from utils import px4_mavlink_patch

            index = next(i for i, part in enumerate(cmd) if part.endswith("px4_mavlink_patch.py"))
            px4_mavlink_patch.main(cmd[index + 1 :])
            proc = MagicMock()
            proc.wait = AsyncMock(return_value=0)
            proc.returncode = 0
            return proc

        svc._create_subprocess = run_locally
        with patch.object(gz_mod.settings, "sitl_wsl_mavlink_target_host", mavlink_target_host):
            await svc._prepare_wsl_px4_rootfs(SimProcess(sim_id="test"))
        return target.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_exactly_one_link_targets_the_mavsdk_port(self, tmp_path):
        text = await self._apply(tmp_path)
        starts = [
            ln
            for ln in text.splitlines()
            if ln.startswith("mavlink start") and "$udp_offboard_port_remote" in ln
        ]
        assert len(starts) == 1, f"expected one link on the MAVSDK port, got:\n{starts}"
        # -m onboard carries SYS_STATUS / GPS_RAW_INT / HOME_POSITION, which
        # MAVSDK reads health and GPS fix from. -m custom starts empty.
        assert "-m onboard" in starts[0]

    @pytest.mark.asyncio
    async def test_px4_keeps_14540_and_gains_no_second_link(self, tmp_path):
        text = await self._apply(tmp_path)
        assert "udp_offboard_port_remote=$((14540+px4_instance))" in text
        assert "udp_sdk_port" not in text, "a second SDK link was re-introduced"
        assert "-m custom" not in text

    @pytest.mark.asyncio
    async def test_high_rate_streams_attach_to_that_link(self, tmp_path):
        text = await self._apply(tmp_path)
        for msg in ("LOCAL_POSITION_NED", "GLOBAL_POSITION_INT", "ATTITUDE", "BATTERY_STATUS"):
            assert f"-s {msg} -u $udp_offboard_port_local" in text, f"{msg} stream missing"

    @pytest.mark.asyncio
    async def test_patch_is_idempotent(self, tmp_path):
        first = await self._apply(tmp_path)
        second = await self._apply(tmp_path)
        assert first == second
        assert second.count("Higher rates for MAVSDK clients") == 1

    @pytest.mark.asyncio
    async def test_remote_host_is_addressed_on_the_same_link(self, tmp_path):
        """A client outside WSL needs -t, but still only one channel."""
        text = await self._apply(tmp_path, mavlink_target_host="192.168.1.50")
        starts = [
            ln
            for ln in text.splitlines()
            if ln.startswith("mavlink start") and "$udp_offboard_port_remote" in ln
        ]
        assert len(starts) == 1
        assert "-t 192.168.1.50" in starts[0]


@pytest.mark.asyncio
async def test_native_runtime_uses_the_single_connection_runner(tmp_path, monkeypatch):
    """Native (API on Linux) must not start a second MAVSDK client on the mission's port."""
    import sys

    from utils import wsl_mission_runner

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    runtime = _runtime("native", is_real=True, host_platform="Linux")
    with patch("services.px4_service.resolve_sitl_runtime", return_value=runtime):
        service = PX4Service()
    seen = {}

    async def fake_exec(*cmd, **kwargs):
        seen["cmd"] = list(cmd)
        output = Path(cmd[cmd.index("--output") + 1])
        monkeypatch.setattr(wsl_mission_runner, "_RUNTIME_MODE", "native")
        payload = wsl_mission_runner._build_telemetry_payload(
            sim_id="sim-native",
            points=[
                {"timestamp_s": t, "north_m": 0.0, "east_m": 0.0, "alt_m": a}
                for t, a in ((0.0, 0.0), (1.0, 5.0), (2.0, 5.0), (3.0, 0.2))
            ],
            elapsed_s=3.0,
            mission_done=True,
            warnings=[],
            home_lat=47.0,
            home_lon=8.0,
            home_alt_m=0.0,
        )
        output.write_text(json.dumps(payload), encoding="utf-8")
        proc = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.stdout = proc.stderr = None
        return proc

    with (
        patch("asyncio.create_subprocess_exec", new=fake_exec),
        patch.object(PX4Service, "_stream_mission_output", new=AsyncMock(return_value=("", ""))),
        patch.object(PX4Service, "_collect_telemetry_stream", new=AsyncMock()) as collector,
        patch.object(PX4Service, "_run_code_subprocess", new=AsyncMock()) as direct,
    ):
        result = await service.execute_mission("async def run(): pass", "sim-native")
    collector.assert_not_awaited()
    direct.assert_not_awaited()
    assert seen["cmd"][:2] == [sys.executable, seen["cmd"][1]]
    assert seen["cmd"][1].endswith("wsl_mission_runner.py")
    assert seen["cmd"][seen["cmd"].index("--runtime-mode") + 1] == "native"
    assert result.runtime_mode == "native"
    assert result.telemetry_source == "real_native_sitl"
    assert result.mission_completed is True
    assert not list(tmp_path.glob("droneprompt_telemetry_*.json"))


def test_mavlink_patch_is_shared_idempotent_and_readdresses_the_same_link():
    """Docker patches the ROMFS file in place on every start; a new host must win."""
    from utils.px4_mavlink_patch import MARKER, patch

    first = patch(_PRISTINE_RC_MAVLINK, "172.18.0.5")
    again = patch(first, "172.18.0.5")
    moved = patch(first, "10.0.0.9")
    local = patch(moved, "127.0.0.1")
    assert again == first and first.count(MARKER) == 1
    for text, host in ((first, "172.18.0.5"), (moved, "10.0.0.9"), (local, None)):
        starts = [
            ln
            for ln in text.splitlines()
            if ln.startswith("mavlink start") and "$udp_offboard_port_remote" in ln
        ]
        assert len(starts) == 1 and "-m onboard" in starts[0] and "-m custom" not in text
        assert starts[0].count("-t ") == (1 if host else 0)
        if host:
            assert f"-t {host} " in starts[0]


@pytest.mark.asyncio
async def test_runner_records_only_live_samples_and_ends_on_a_lost_link(monkeypatch, capsys):
    """SITL: with MAVLink cut, stale values were recorded for five minutes."""
    import asyncio

    from utils import wsl_mission_runner as runner

    monkeypatch.setattr(runner, "_STALE_POSITION_S", 0.2)
    monkeypatch.setattr(runner, "_LINK_LOST_S", 0.6)
    monkeypatch.setattr(runner, "_TELEMETRY_INTERVAL", 0.02)

    def stream(value, repeats=1, every=0.0):
        def factory():
            async def gen():
                for _ in range(repeats):
                    yield value
                    await asyncio.sleep(every)
                await asyncio.sleep(3600)

            return gen()

        return factory

    position = SimpleNamespace(
        latitude_deg=47.4, longitude_deg=8.5, relative_altitude_m=5.0, absolute_altitude_m=5.0
    )
    telemetry = SimpleNamespace(
        # Ten updates 0.05 s apart, then the link goes quiet.
        position=stream(position, repeats=10, every=0.05),
        position_velocity_ned=stream(
            SimpleNamespace(position=SimpleNamespace(north_m=0.0, east_m=0.0, down_m=-5.0))
        ),
        altitude=stream(SimpleNamespace()),
        odometry=stream(SimpleNamespace(position_body=SimpleNamespace(x_m=0.0, y_m=0.0, z_m=-5.0))),
        velocity_ned=stream(SimpleNamespace(north_m_s=0.0, east_m_s=0.0, down_m_s=0.0)),
        attitude_euler=stream(SimpleNamespace(roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0)),
        battery=stream(SimpleNamespace(remaining_percent=0.9, voltage_v=16.0)),
        gps_info=stream(SimpleNamespace(num_satellites=10)),
        flight_mode=stream("MISSION"),
        in_air=stream(True),
    )
    payload = await asyncio.wait_for(
        runner._collect_telemetry(SimpleNamespace(telemetry=telemetry), "s", 30.0, asyncio.Event()),
        timeout=5,
    )
    lost_after = payload["telemetry_link_lost_after_s"]
    assert lost_after is not None
    assert any("Telemetry link lost" in w for w in payload["warnings"])
    # Nothing is recorded once the position stream has been silent past the limit.
    assert payload["points"][-1]["timestamp_s"] <= lost_after + 0.2 + 0.05


def test_runner_flags_a_tilt_beyond_control_once_airborne():
    from utils.wsl_mission_runner import _build_telemetry_payload, _loss_of_control

    def sample(t, alt, roll):
        return {
            "timestamp_s": t,
            "north_m": 0.0,
            "east_m": 0.0,
            "alt_m": alt,
            "roll_deg": roll,
            "pitch_deg": 0.0,
        }

    # SITL, motor 1 stopped at 10 m: level, then 47.6, -67.8 and 136.8 degrees falling.
    flight = [
        sample(t, alt, roll)
        for t, alt, roll in [
            (0.0, 0.0, 0.0),
            (1.0, 9.95, 0.5),
            (2.0, 9.78, 47.6),
            (3.0, 7.54, -67.8),
            (4.0, 2.68, 136.8),
            (5.0, 0.43, -149.2),
        ]
    ]
    assert _loss_of_control(flight) == {
        "at_s": 4.0,
        "tilt_deg": 136.8,
        "descent_m_s": 0.0,
        "alt_m": 2.68,
    }
    # SITL hexacopter, motor 1 stopped: within 19 degrees of level, falling at 8 m/s.
    upright_fall = [
        {**sample(0.0, 9.95, 1.0), "vel_down_ms": 0.0},
        {**sample(0.5, 8.88, -10.6), "vel_down_ms": 3.04},
        {**sample(1.0, 6.62, -4.1), "vel_down_ms": 5.34},
    ]
    assert _loss_of_control(upright_fall)["at_s"] == 1.0
    # Past 70 degrees only counts once the vehicle has flown.
    assert _loss_of_control([sample(0.0, 0.0, 90.0)]) is None
    assert _loss_of_control([sample(0.0, 0.3, 90.0)], airborne=True)["tilt_deg"] == 90.0
    payload = _build_telemetry_payload(
        sim_id="s",
        points=flight,
        elapsed_s=5.0,
        mission_done=False,
        warnings=[],
        home_lat=None,
        home_lon=None,
        home_alt_m=None,
    )
    assert payload["crash_detected"] is True
