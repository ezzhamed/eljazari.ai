"""Phase 8 tests — NED↔LLA, mission builder, real MAVSDK code template."""

from __future__ import annotations

import ast

import pytest

from pipelines.mavsdk_templates import (
    render_home_position_constants,
    render_real_mavsdk_mission,
)
from pipelines.mission_builder import (
    MissionItemPayload,
    MissionPlanSpec,
    Waypoint,
    build_mission_items,
    render_mission_items_python,
)
from utils.coordinates import (
    LLA,
    NED,
    PX4_SITL_DEFAULT_HOME,
    haversine_distance_m,
    lla_to_ned,
    ned_to_lla,
)


# ─────────────────────────────────────────────────────────────────────────────
# coordinates
# ─────────────────────────────────────────────────────────────────────────────


class TestCoordinates:
    def test_lla_validation_rejects_out_of_range_lat(self):
        with pytest.raises(ValueError):
            LLA(lat_deg=91.0, lon_deg=0.0, alt_m=0.0)

    def test_lla_validation_rejects_out_of_range_lon(self):
        with pytest.raises(ValueError):
            LLA(lat_deg=0.0, lon_deg=181.0, alt_m=0.0)

    def test_ned_zero_offset_returns_home(self):
        result = ned_to_lla(PX4_SITL_DEFAULT_HOME, NED(0, 0, 0))
        assert result.lat_deg == pytest.approx(PX4_SITL_DEFAULT_HOME.lat_deg, abs=1e-9)
        assert result.lon_deg == pytest.approx(PX4_SITL_DEFAULT_HOME.lon_deg, abs=1e-9)

    def test_ned_north_offset_increases_latitude(self):
        result = ned_to_lla(PX4_SITL_DEFAULT_HOME, NED(north_m=100.0, east_m=0.0))
        assert result.lat_deg > PX4_SITL_DEFAULT_HOME.lat_deg
        # 100m north ≈ 0.0009 deg latitude
        dlat = result.lat_deg - PX4_SITL_DEFAULT_HOME.lat_deg
        assert dlat == pytest.approx(0.000898, abs=0.0001)

    def test_ned_east_offset_at_zurich_increases_longitude(self):
        result = ned_to_lla(PX4_SITL_DEFAULT_HOME, NED(north_m=0.0, east_m=100.0))
        assert result.lon_deg > PX4_SITL_DEFAULT_HOME.lon_deg

    def test_round_trip_preserves_position(self):
        """ned_to_lla then lla_to_ned should recover the original offset."""
        original = NED(north_m=237.5, east_m=-118.2, down_m=-12.0)
        lla = ned_to_lla(PX4_SITL_DEFAULT_HOME, original)
        recovered = lla_to_ned(PX4_SITL_DEFAULT_HOME, lla)
        assert recovered.north_m == pytest.approx(original.north_m, abs=0.05)
        assert recovered.east_m == pytest.approx(original.east_m, abs=0.05)
        assert recovered.down_m == pytest.approx(original.down_m, abs=0.05)

    def test_haversine_zero_for_same_point(self):
        d = haversine_distance_m(PX4_SITL_DEFAULT_HOME, PX4_SITL_DEFAULT_HOME)
        assert d == pytest.approx(0.0, abs=0.01)

    def test_spherical_vs_wgs84_close_at_short_range(self):
        offset = NED(north_m=500.0, east_m=500.0)
        spherical = ned_to_lla(PX4_SITL_DEFAULT_HOME, offset, precision="spherical")
        wgs84 = ned_to_lla(PX4_SITL_DEFAULT_HOME, offset, precision="wgs84")
        # Within 500 m the two models should agree to within 3 m worth of
        # lat/lon (≈ 3e-5 deg). Beyond that, prefer wgs84.
        assert abs(spherical.lat_deg - wgs84.lat_deg) < 3e-5
        assert abs(spherical.lon_deg - wgs84.lon_deg) < 3e-5

    def test_pole_raises_on_ned_conversion(self):
        north_pole = LLA(lat_deg=90.0, lon_deg=0.0, alt_m=0.0)
        with pytest.raises(ValueError, match="pole"):
            ned_to_lla(north_pole, NED(north_m=10.0, east_m=10.0))


# ─────────────────────────────────────────────────────────────────────────────
# Mission builder
# ─────────────────────────────────────────────────────────────────────────────


def _square_spec(side_m: float = 100.0, altitude_m: float = 15.0) -> MissionPlanSpec:
    return MissionPlanSpec(
        home=PX4_SITL_DEFAULT_HOME,
        default_speed_m_s=5.0,
        takeoff_altitude_m=altitude_m,
        waypoints=[
            Waypoint(name="north", north_m=side_m, east_m=0, alt_m=altitude_m),
            Waypoint(name="ne", north_m=side_m, east_m=side_m, alt_m=altitude_m),
            Waypoint(name="east", north_m=0, east_m=side_m, alt_m=altitude_m),
            Waypoint(name="home", north_m=0, east_m=0, alt_m=altitude_m),
        ],
    )


class TestMissionBuilder:
    def test_empty_waypoints_rejected(self):
        with pytest.raises(ValueError, match="at least one waypoint"):
            MissionPlanSpec(home=PX4_SITL_DEFAULT_HOME, waypoints=[])

    def test_build_items_count_matches_waypoints(self):
        spec = _square_spec()
        items = build_mission_items(spec)
        assert len(items) == len(spec.waypoints)
        assert all(isinstance(it, MissionItemPayload) for it in items)

    def test_first_waypoint_north_is_north_of_home(self):
        spec = _square_spec()
        items = build_mission_items(spec)
        assert items[0].latitude_deg > PX4_SITL_DEFAULT_HOME.lat_deg

    def test_relative_altitude_propagates(self):
        spec = _square_spec(altitude_m=12.5)
        items = build_mission_items(spec)
        for it in items:
            assert it.relative_altitude_m == pytest.approx(12.5)

    def test_default_speed_used_when_waypoint_speed_none(self):
        spec = MissionPlanSpec(
            home=PX4_SITL_DEFAULT_HOME,
            default_speed_m_s=7.5,
            takeoff_altitude_m=4.0,
            waypoints=[Waypoint(name="a", north_m=10, alt_m=4)],
        )
        items = build_mission_items(spec)
        assert items[0].speed_m_s == pytest.approx(7.5)

    def test_explicit_waypoint_speed_overrides_default(self):
        spec = MissionPlanSpec(
            home=PX4_SITL_DEFAULT_HOME,
            default_speed_m_s=5.0,
            takeoff_altitude_m=4.0,
            waypoints=[Waypoint(name="a", north_m=10, alt_m=4, speed_m_s=12.0)],
        )
        items = build_mission_items(spec)
        assert items[0].speed_m_s == pytest.approx(12.0)

    def test_render_python_emits_valid_syntax(self):
        spec = _square_spec()
        rendered = render_mission_items_python(spec)
        # The block by itself isn't runnable (needs MissionItem in scope), but
        # the syntax must parse.
        try:
            ast.parse(rendered + "\n_x = mission_items")
        except SyntaxError as exc:
            pytest.fail(f"rendered Python has syntax error: {exc}")

    def test_render_includes_one_item_per_waypoint(self):
        spec = _square_spec()
        rendered = render_mission_items_python(spec)
        assert rendered.count("MissionItem(") == len(spec.waypoints)


# ─────────────────────────────────────────────────────────────────────────────
# Real MAVSDK template
# ─────────────────────────────────────────────────────────────────────────────


class TestRealMavsdkTemplate:
    def test_full_render_parses_as_python(self):
        spec = _square_spec()
        code = render_real_mavsdk_mission(spec)
        try:
            ast.parse(code)
        except SyntaxError as exc:
            pytest.fail(f"rendered MAVSDK mission has syntax error: {exc}")

    def test_template_imports_real_mavsdk_modules(self):
        spec = _square_spec()
        code = render_real_mavsdk_mission(spec)
        assert "from mavsdk import System" in code
        assert "from mavsdk.action import" in code
        assert "from mavsdk.mission import" in code
        assert "from mavsdk.telemetry import" in code

    def test_template_has_no_skill_api_calls(self):
        """The hallmark of Phase 8: the legacy skill-API abstractions must
        not appear in newly generated mission code."""
        spec = _square_spec()
        code = render_real_mavsdk_mission(spec)
        forbidden = [
            "drone.fly_to_relative(",
            "drone.fly_to(",
            "drone.set_geofence(",
            "drone.check_pre_flight(",
            "drone.takeoff(altitude_m",
            "drone.emergency_stop(",
        ]
        for f in forbidden:
            assert f not in code, f"Legacy skill-API call leaked into template: {f!r}"

    def test_template_uses_real_mavsdk_action_and_mission_calls(self):
        spec = _square_spec()
        code = render_real_mavsdk_mission(spec)
        for required in [
            "await drone.connect(system_address=MAVSDK_ADDRESS)",
            "drone.core.connection_state()",
            "drone.telemetry.health()",
            "drone.telemetry.battery()",
            "drone.action.arm()",
            "drone.action.set_takeoff_altitude(",
            "drone.action.takeoff()",
            "drone.mission.upload_mission(",
            "drone.mission.start_mission()",
            "drone.mission.mission_progress()",
            "drone.action.return_to_launch()",
        ]:
            assert required in code, f"Missing real MAVSDK call: {required!r}"

    def test_template_reads_mavsdk_address_from_env(self):
        spec = _square_spec()
        code = render_real_mavsdk_mission(spec)
        assert 'os.getenv("DRONEPROMPT_MAVSDK_ADDRESS"' in code

    def test_geofence_block_emitted_when_radius_set(self):
        spec = _square_spec()
        with_fence = render_real_mavsdk_mission(spec, geofence_radius_m=250.0)
        # Geofence is enforced via PX4 params (GF_*), not a polygon upload.
        assert "upload_circular_geofence(drone, radius_m=250" in with_fence
        assert "GF_MAX_HOR_DIST" in with_fence

    def test_geofence_block_skipped_when_radius_none(self):
        spec = _square_spec()
        without_fence = render_real_mavsdk_mission(spec, geofence_radius_m=None)
        # The helper function is still defined (it's part of the helpers
        # block), but the *call* should not happen when radius is None.
        assert "if False:" in without_fence  # the guard literally renders False

    def test_home_position_constants_block(self):
        block = render_home_position_constants(PX4_SITL_DEFAULT_HOME)
        assert "HOME_LATITUDE_DEG" in block
        assert "HOME_LONGITUDE_DEG" in block
        assert "HOME_ALTITUDE_M" in block

    def test_template_passes_safety_layer_static_check(self):
        """Generated code must satisfy the SafetyLayer AST whitelist."""
        from agents.safety_layer import SafetyLayer

        spec = _square_spec()
        code = render_real_mavsdk_mission(spec)
        layer = SafetyLayer(max_altitude=120.0, max_speed=15.0)
        report = layer.analyze(code)
        # Allow warnings, but the static AST whitelist (SR001) must pass.
        sr001 = [i for i in report.issues if i.rule_id == "SR001"]
        assert not sr001, "Real MAVSDK template emits an unapproved function: " + ", ".join(
            f"{i.description}" for i in sr001
        )


@pytest.mark.asyncio
async def test_waypoints_are_reanchored_on_the_vehicles_actual_home():
    """PX4 v1.17's default world sits ~50 m from the nominal home; offsets must survive."""
    import logging
    import math
    from types import SimpleNamespace

    spec = MissionPlanSpec(
        home=PX4_SITL_DEFAULT_HOME,
        waypoints=[
            Waypoint(name="hover", alt_m=5.0),
            Waypoint(name="north", north_m=20.0, east_m=-10.0, alt_m=5.0),
        ],
        takeoff_altitude_m=5.0,
    )
    code = render_real_mavsdk_mission(spec)
    tree = ast.parse(code)
    helper = next(n for n in tree.body if getattr(n, "name", "") == "anchor_mission_to_home")
    constants = {
        t.targets[0].id: ast.literal_eval(t.value)
        for t in tree.body
        if isinstance(t, ast.Assign)
        and isinstance(t.targets[0], ast.Name)
        and t.targets[0].id.startswith("PLANNED_HOME")
    }
    assert constants["PLANNED_HOME_LATITUDE_DEG"] == pytest.approx(PX4_SITL_DEFAULT_HOME.lat_deg)
    namespace = {"math": math, "logger": logging.getLogger("t"), "System": object, **constants}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "helper", "exec"), namespace)

    actual = LLA(lat_deg=47.397971057728974, lon_deg=8.546163739800146, alt_m=0.0)

    async def home():
        yield SimpleNamespace(latitude_deg=actual.lat_deg, longitude_deg=actual.lon_deg)

    items = [
        SimpleNamespace(latitude_deg=i.latitude_deg, longitude_deg=i.longitude_deg)
        for i in build_mission_items(spec)
    ]
    drone = SimpleNamespace(telemetry=SimpleNamespace(home=home))
    await namespace["anchor_mission_to_home"](drone, items)
    first = LLA(lat_deg=items[0].latitude_deg, lon_deg=items[0].longitude_deg, alt_m=0.0)
    second = LLA(lat_deg=items[1].latitude_deg, lon_deg=items[1].longitude_deg, alt_m=0.0)
    assert haversine_distance_m(first, actual) < 0.01
    offset = lla_to_ned(actual, second)
    assert offset.north_m == pytest.approx(20.0, abs=0.05)
    assert offset.east_m == pytest.approx(-10.0, abs=0.05)
    assert "await anchor_mission_to_home(drone, mission_items)" in code


def _progress_helper(settle_s: float, start_timeout_s: float = 30.0):
    """Execute the rendered run_mission_progress against scripted streams."""
    import logging
    from types import SimpleNamespace

    spec = MissionPlanSpec(
        home=PX4_SITL_DEFAULT_HOME, waypoints=[Waypoint(name="hover", alt_m=5.0)]
    )
    tree = ast.parse(render_real_mavsdk_mission(spec))
    helper = next(n for n in tree.body if getattr(n, "name", "") == "run_mission_progress")
    namespace = {
        "asyncio": __import__("asyncio"),
        "logger": logging.getLogger("t"),
        "System": object,
        "FlightMode": SimpleNamespace(MISSION="MISSION"),
        "MISSION_SETTLE_S": settle_s,
        "MISSION_START_TIMEOUT_S": start_timeout_s,
    }
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "helper", "exec"), namespace)

    def drone(progress_events, mode_events, status_events=()):
        import asyncio

        def stream(events):
            async def gen():
                start = asyncio.get_event_loop().time()
                for at, value in events:
                    await asyncio.sleep(max(0.0, at - (asyncio.get_event_loop().time() - start)))
                    yield value
                await asyncio.sleep(3600)

            return gen

        progress = [(t, SimpleNamespace(current=c, total=n)) for t, c, n in progress_events]
        status = [(t, SimpleNamespace(type="WARNING", text=text)) for t, text in status_events]
        return SimpleNamespace(
            mission=SimpleNamespace(mission_progress=stream(progress)),
            telemetry=SimpleNamespace(flight_mode=stream(mode_events), status_text=stream(status)),
        )

    return namespace["run_mission_progress"], drone


@pytest.mark.asyncio
async def test_transient_progress_at_mission_start_does_not_end_the_mission():
    """Replays the SITL race: current == total for 0.3 s right after the start."""
    import asyncio

    run, drone = _progress_helper(settle_s=0.6)
    vehicle = drone(
        [(0.3, 1, 1), (0.6, 0, 1), (2.0, 1, 1)],
        [(0.0, "HOLD"), (1.0, "MISSION"), (2.2, "RETURN_TO_LAUNCH")],
    )
    start = asyncio.get_event_loop().time()
    assert await asyncio.wait_for(run(vehicle, total_items=1), timeout=5) == "complete"
    assert asyncio.get_event_loop().time() - start >= 2.2


@pytest.mark.asyncio
async def test_leaving_mission_mode_before_the_end_is_a_failure():
    import asyncio

    run, drone = _progress_helper(settle_s=0.6)
    vehicle = drone([(0.2, 0, 1)], [(0.0, "HOLD"), (0.5, "MISSION"), (1.0, "RETURN_TO_LAUNCH")])
    assert await asyncio.wait_for(run(vehicle, total_items=1), timeout=5) == "left"


@pytest.mark.asyncio
async def test_mission_too_short_to_see_in_mission_mode_completes_once_settled():
    import asyncio

    run, drone = _progress_helper(settle_s=0.6)
    vehicle = drone([(0.3, 1, 1)], [(0.0, "HOLD")])
    assert await asyncio.wait_for(run(vehicle, total_items=1), timeout=5) == "complete"


@pytest.mark.asyncio
async def test_a_mission_px4_never_starts_ends_instead_of_waiting_forever(caplog):
    """SITL: waypoints beyond a 10 m geofence; PX4 held, the script waited 330 s."""
    import asyncio

    run, drone = _progress_helper(settle_s=0.6, start_timeout_s=1.0)
    vehicle = drone([(0.2, 0, 4)], [(0.0, "HOLD")], [(0.3, "Geofence violation")])
    with caplog.at_level("ERROR"):
        assert await asyncio.wait_for(run(vehicle, total_items=4), timeout=5) == "not_started"
    assert "Geofence violation" in caplog.text


def test_after_px4_takes_over_the_mission_code_does_not_override_it():
    """SITL: an empty battery made PX4 land; the code's return climbed to 26 m."""
    code = render_real_mavsdk_mission(
        MissionPlanSpec(home=PX4_SITL_DEFAULT_HOME, waypoints=[Waypoint(name="h", alt_m=5.0)])
    )
    body = code[code.index("async def run_mission(") :]
    left = body[body.index('if outcome == "left":') : body.index('if outcome != "complete":')]
    assert "return_to_launch" not in left and "land()" not in left
    assert "wait_until_landed" in left
    not_started = body[body.index('if outcome != "complete":') :].split("return False")[0]
    assert "return_to_launch()" in not_started
