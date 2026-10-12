"""Flight conformance against the approved code's mission (synthetic flights)."""

from __future__ import annotations

import pytest

from pipelines.flight_conformance import (
    MissionUnreadable,
    assess_flight_conformance,
    read_commanded_mission,
)
from pipelines.mavsdk_templates import render_real_mavsdk_mission
from pipelines.mission_builder import MissionPlanSpec, Waypoint
from pipelines.simulation_acceptance import assess_simulation
from tests.flight_fixtures import flown_mission, mission_code
from tests.test_simulation_acceptance import good_state
from utils.coordinates import PX4_SITL_DEFAULT_HOME

SQUARE = [(20.0, 0.0), (20.0, 20.0), (0.0, 20.0), (0.0, 0.0)]


def _reasons(points, code):
    return " | ".join(assess_flight_conformance(points, code)["reasons"])


def test_the_commanded_mission_is_read_back_as_offsets_from_home():
    mission = read_commanded_mission(mission_code(SQUARE, altitude_m=15.0))
    assert [(round(w.north_m, 2), round(w.east_m, 2)) for w in mission.waypoints] == SQUARE
    assert {w.alt_m for w in mission.waypoints} == {15.0}
    assert mission.takeoff_alt_m == 15.0
    assert mission.return_to_launch is True
    assert mission.geofence_radius_m == 250.0
    hover = read_commanded_mission(mission_code(altitude_m=5.0, hold_s=15.0))
    assert len(hover.waypoints) == 1 and hover.waypoints[0].loiter_s == 15.0


def test_a_disabled_geofence_is_not_read_as_a_limit():
    spec = MissionPlanSpec(
        home=PX4_SITL_DEFAULT_HOME,
        takeoff_altitude_m=5.0,
        waypoints=[Waypoint(name="hover", alt_m=5.0)],
    )
    code = render_real_mavsdk_mission(spec, geofence_radius_m=None)
    assert "if False:" in code
    assert read_commanded_mission(code).geofence_radius_m is None


@pytest.mark.parametrize(
    "edit,fragment",
    [
        (lambda c: c.replace("mission_items = [", "items = ["), "mission_items"),
        (lambda c: c.replace("PLANNED_HOME_LATITUDE_DEG = ", "HOME_LAT = "), "PLANNED_HOME"),
        (
            lambda c: c.replace("await anchor_mission_to_home(drone, mission_items)", "pass"),
            "anchor",
        ),
        (lambda c: c.replace("VehicleAction.NONE", "VehicleAction.LAND"), "vehicle action LAND"),
        (lambda c: c + "\n)", "does not parse"),
    ],
)
def test_code_whose_mission_cannot_be_read_fails_closed(edit, fragment):
    code = edit(mission_code(altitude_m=5.0, hold_s=2.0))
    with pytest.raises(MissionUnreadable, match=fragment):
        read_commanded_mission(code)
    result = assess_flight_conformance(flown_mission(altitude_m=5.0, hold_s=2.0), code)
    assert not result["passed"] and not result["evaluated"]
    assert result["retryable_with_code_change"] is True


def test_positional_mission_items_are_not_guessed():
    code = mission_code().replace("MissionItem(\n        latitude_deg=", "MissionItem(\n        ")
    with pytest.raises(MissionUnreadable, match="keywords"):
        read_commanded_mission(code)


def test_a_hover_and_a_square_flown_as_commanded_pass():
    hover = assess_flight_conformance(
        flown_mission(altitude_m=5.0, hold_s=15.0), mission_code(altitude_m=5.0, hold_s=15.0)
    )
    assert hover["passed"], hover["reasons"]
    assert hover["metrics"]["holds"][0]["held_s"] >= 15.0
    assert hover["metrics"]["horizontal_error_max_m"] < 0.01
    square = assess_flight_conformance(
        flown_mission(SQUARE, altitude_m=15.0), mission_code(SQUARE, altitude_m=15.0)
    )
    assert square["passed"], square["reasons"]
    assert square["metrics"]["altitude_capture_s"] < 3.0
    assert square["metrics"]["landing_error_m"] < 0.01


def test_a_path_other_than_the_commanded_one_fails():
    bigger = [(25.0 * n / 20.0, 25.0 * e / 20.0) for n, e in SQUARE]
    reasons = _reasons(
        flown_mission(bigger, altitude_m=15.0), mission_code(SQUARE, altitude_m=15.0)
    )
    assert "Horizontal tracking" in reasons


def test_one_sample_more_than_a_metre_off_the_path_fails():
    points = flown_mission(SQUARE, altitude_m=15.0)
    middle = next(
        i for i, p in enumerate(points) if p["flight_mode"] == "MISSION" and 9 < p["north_m"] < 11
    )
    points[middle] = {**points[middle], "east_m": points[middle]["east_m"] + 1.2}
    assert "1.20 m from the commanded path" in _reasons(
        points, mission_code(SQUARE, altitude_m=15.0)
    )


def test_altitude_off_the_command_fails():
    points = [
        {**p, "alt_m": p["alt_m"] + 0.7} if p["flight_mode"] == "MISSION" else p
        for p in flown_mission(altitude_m=10.0, hold_s=5.0)
    ]
    reasons = _reasons(points, mission_code(altitude_m=10.0, hold_s=5.0))
    assert "Altitude RMSE 0.70 m" in reasons
    far = [
        {**p, "alt_m": p["alt_m"] + 1.5} if p["flight_mode"] == "MISSION" else p
        for p in flown_mission(altitude_m=10.0, hold_s=5.0)
    ]
    assert "never came within 0.5 m" in _reasons(far, mission_code(altitude_m=10.0, hold_s=5.0))


def test_a_slow_climb_to_the_commanded_altitude_fails():
    points = flown_mission(altitude_m=10.0, hold_s=20.0)
    start = next(p["timestamp_s"] for p in points if p["flight_mode"] == "MISSION")
    points = [
        {**p, "alt_m": 9.0}
        if p["flight_mode"] == "MISSION" and p["timestamp_s"] - start < 12
        else p
        for p in points
    ]
    reasons = _reasons(points, mission_code(altitude_m=10.0, hold_s=20.0))
    assert "into the mission; the limit is 10 s" in reasons


def test_a_hover_cut_short_fails():
    # Batch 18: MAVSDK reported the mission complete about 2 s into a 15 s hover.
    reasons = _reasons(
        flown_mission(altitude_m=5.0, hold_s=2.0), mission_code(altitude_m=5.0, hold_s=15.0)
    )
    assert "Waypoint 1 was held" in reasons and "commands 15 s" in reasons


def test_a_flight_that_does_not_land_where_it_should_fails():
    code = mission_code(altitude_m=5.0, hold_s=2.0)
    airborne = [p for p in flown_mission(altitude_m=5.0, hold_s=2.0) if p["alt_m"] > 1.0]
    assert "did not land" in _reasons(airborne, code)
    displaced = flown_mission(altitude_m=5.0, hold_s=2.0)
    displaced[-1] = {**displaced[-1], "north_m": 2.0}
    assert "Landed 2.0 m from home" in _reasons(displaced, code)


def test_leaving_the_codes_geofence_fails():
    spec = MissionPlanSpec(
        home=PX4_SITL_DEFAULT_HOME,
        takeoff_altitude_m=15.0,
        waypoints=[
            Waypoint(name=f"c{i}", north_m=n, east_m=e, alt_m=15.0)
            for i, (n, e) in enumerate(SQUARE)
        ],
    )
    code = render_real_mavsdk_mission(spec, geofence_radius_m=20.0)
    assert "beyond the code's 20 m geofence" in _reasons(
        flown_mission(SQUARE, altitude_m=15.0), code
    )


def test_sparse_or_interrupted_telemetry_fails():
    code = mission_code(altitude_m=5.0, hold_s=5.0)
    points = flown_mission(altitude_m=5.0, hold_s=5.0)
    gap = [p for i, p in enumerate(points) if not 40 <= i < 55]
    assert "gap after" in _reasons(gap, code)
    assert "median" in _reasons(points[::3], code)


def test_a_flight_that_never_entered_mission_mode_fails_and_may_be_regenerated():
    points = [{**p, "flight_mode": "HOLD"} for p in flown_mission(altitude_m=5.0, hold_s=2.0)]
    result = assess_flight_conformance(points, mission_code(altitude_m=5.0, hold_s=2.0))
    assert "never reported MISSION mode" in " ".join(result["reasons"])
    assert result["retryable_with_code_change"] is True


def test_a_mission_with_nothing_to_fly_is_named_and_may_be_regenerated():
    """Benchmark run 3, ar-octocopter-cinema #3: one waypoint where takeoff leaves
    the vehicle, no hold; PX4 finished the mission in 65 ms and returned."""
    code = mission_code(altitude_m=10.0, hold_s=0.0)
    points = [
        {**p, "flight_mode": "HOLD" if p["flight_mode"] == "MISSION" else p["flight_mode"]}
        for p in flown_mission(altitude_m=10.0, hold_s=0.0)
    ]
    result = assess_flight_conformance(points, code)
    assert "nothing to fly" in " ".join(result["reasons"])
    assert result["retryable_with_code_change"] is True
    gate = assess_simulation(good_state(generated_code=code, telemetry_data=points))
    assert not gate["passed"] and gate["retryable_with_code_change"] is True


def test_without_approved_code_nothing_passes():
    result = assess_flight_conformance(flown_mission(), None)
    assert not result["passed"] and not result["retryable_with_code_change"]


def test_the_gate_compares_the_flight_with_its_mission():
    passed = assess_simulation(good_state())
    assert passed["passed"], passed["reasons"]
    assert passed["flight_conformance"]["evaluated"] is True
    no_code = assess_simulation(good_state(generated_code=None))
    assert "No approved mission code" in " ".join(no_code["reasons"])
    cut_short = assess_simulation(
        good_state(generated_code=mission_code(altitude_m=5.0, hold_s=15.0))
    )
    assert not cut_short["passed"]
    assert cut_short["retryable_with_code_change"] is False
    # A run that already failed the basic checks is not also judged on its path.
    crashed = assess_simulation(good_state(crash_detected=True))
    assert crashed["flight_conformance"]["evaluated"] is False
