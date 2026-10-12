"""Tests for the Phase 8.5 post-LLM code rewriter.

Closes the gap surfaced by the live e2e run: the LLM emitted
``drone.fly_to_relative(north_m=40.0, ...)`` for a "6 m north" prompt.
The rewriter must:

  1. AST-detect every legacy skill-API call
  2. Extract the implicit waypoints
  3. Reject / clamp plans whose distance exceeds the prompt budget
  4. Re-emit the mission as real MAVSDK so it runs on a real Pixhawk
"""

from __future__ import annotations

import pytest

from pipelines.code_post_processor import (
    ExtractedPlan,
    extract_max_distance_from_prompt,
    enforce_mission_geometry,
    extract_plan_from_legacy_code,
    rewrite_to_real_mavsdk,
    validate_plan_distance,
)
from pipelines.mission_builder import Waypoint


# ─────────────────────────────────────────────────────────────────────────────
# Distance extraction from the prompt
# ─────────────────────────────────────────────────────────────────────────────


class TestPromptDistanceExtraction:
    def test_simple_metres(self):
        assert extract_max_distance_from_prompt("fly 6m north") == 6.0

    def test_takes_max_of_multiple_distances(self):
        d = extract_max_distance_from_prompt("Takeoff to 4m, fly 6m north, then land safely.")
        assert d == 6.0

    def test_handles_square_pattern(self):
        d = extract_max_distance_from_prompt("square 100m, altitude 15m")
        assert d == 100.0

    def test_returns_none_when_no_distance(self):
        assert extract_max_distance_from_prompt("Hover for a while") is None

    def test_handles_decimal(self):
        assert extract_max_distance_from_prompt("fly 12.5 m east") == 12.5


# ─────────────────────────────────────────────────────────────────────────────
# Legacy-call extraction
# ─────────────────────────────────────────────────────────────────────────────

LEGACY_CODE = """\
async def run_mission(drone):
    await drone.set_geofence(radius_m=250.0, max_alt_m=4.0)
    await drone.takeoff(altitude_m=4.0)
    await drone.fly_to_relative(north_m=40.0, east_m=0.0, down_m=0.0)
    await drone.fly_to_relative(north_m=-40.0, east_m=0.0, down_m=0.0)
    await drone.land()
"""


REAL_MAVSDK_CODE = """\
import asyncio
from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan

async def run_mission(drone):
    await drone.action.set_takeoff_altitude(4.0)
    await drone.action.arm()
    await drone.action.takeoff()
    items = [MissionItem(latitude_deg=47.0, longitude_deg=8.5, relative_altitude_m=4.0,
                         speed_m_s=5.0, is_fly_through=True,
                         gimbal_pitch_deg=0.0, gimbal_yaw_deg=0.0,
                         camera_action=MissionItem.CameraAction.NONE,
                         loiter_time_s=0.0, camera_photo_interval_s=0.0,
                         acceptance_radius_m=1.0, yaw_deg=float("nan"),
                         camera_photo_distance_m=float("nan"),
                         vehicle_action=MissionItem.VehicleAction.NONE)]
    await drone.mission.upload_mission(MissionPlan(items))
    await drone.mission.start_mission()
"""


class TestExtractPlan:
    def test_extracts_takeoff_altitude(self):
        plan = extract_plan_from_legacy_code(LEGACY_CODE)
        assert plan.takeoff_altitude_m == pytest.approx(4.0)

    def test_extracts_geofence_radius(self):
        plan = extract_plan_from_legacy_code(LEGACY_CODE)
        assert plan.geofence_radius_m == pytest.approx(250.0)

    def test_accumulates_relative_waypoints(self):
        plan = extract_plan_from_legacy_code(LEGACY_CODE)
        # First fly_to_relative: (40, 0); second: (-40, 0) → returns to home.
        # Then the rewriter auto-appends a return_home waypoint, but extraction
        # itself stops at the 2 explicit legs.
        assert len(plan.waypoints) >= 2
        assert plan.waypoints[0].north_m == pytest.approx(40.0)
        assert plan.waypoints[1].north_m == pytest.approx(0.0)

    def test_real_mavsdk_code_has_no_legacy_flags(self):
        plan = extract_plan_from_legacy_code(REAL_MAVSDK_CODE)
        assert plan.has_legacy_calls is False
        assert plan.waypoints == []

    def test_handles_syntax_error_gracefully(self):
        plan = extract_plan_from_legacy_code("def broken(:")
        assert plan.has_legacy_calls is False

    def test_records_legacy_call_names(self):
        plan = extract_plan_from_legacy_code(LEGACY_CODE)
        assert "fly_to_relative" in plan.legacy_call_names
        assert "set_geofence" in plan.legacy_call_names
        assert "takeoff" in plan.legacy_call_names


# ─────────────────────────────────────────────────────────────────────────────
# Distance validation
# ─────────────────────────────────────────────────────────────────────────────


class TestDistanceValidation:
    def test_within_budget_passes(self):
        plan = ExtractedPlan(
            takeoff_altitude_m=4.0,
            waypoints=[Waypoint(name="a", north_m=5.0, alt_m=4.0)],
        )
        ok, reason = validate_plan_distance(plan, "fly 6m north")
        assert ok is True
        assert reason is None

    def test_over_budget_rejected(self):
        plan = ExtractedPlan(
            takeoff_altitude_m=4.0,
            waypoints=[Waypoint(name="a", north_m=40.0, alt_m=4.0)],
        )
        ok, reason = validate_plan_distance(plan, "fly 6m north")
        assert ok is False
        assert "exceeds prompt budget" in (reason or "")

    def test_no_distance_in_prompt_accepts_anything(self):
        plan = ExtractedPlan(
            takeoff_altitude_m=4.0,
            waypoints=[Waypoint(name="a", north_m=999.0, alt_m=4.0)],
        )
        ok, reason = validate_plan_distance(plan, "fly somewhere")
        assert ok is True

    def test_2x_tolerance_factor_default(self):
        # Prompt says 10m, plan goes to 19m — within 2× tolerance.
        plan = ExtractedPlan(
            takeoff_altitude_m=4.0,
            waypoints=[Waypoint(name="a", north_m=19.0, alt_m=4.0)],
        )
        ok, _ = validate_plan_distance(plan, "fly 10m north")
        assert ok is True

    def test_custom_tolerance(self):
        plan = ExtractedPlan(
            takeoff_altitude_m=4.0,
            waypoints=[Waypoint(name="a", north_m=15.0, alt_m=4.0)],
        )
        ok, _ = validate_plan_distance(plan, "fly 10m", tolerance_factor=1.2)
        assert ok is False


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end rewriter
# ─────────────────────────────────────────────────────────────────────────────


class TestRewriter:
    def test_legacy_code_gets_rewritten(self):
        new_code, plan = rewrite_to_real_mavsdk(LEGACY_CODE)
        assert plan.has_legacy_calls is True
        assert new_code != LEGACY_CODE
        # Output must import real MAVSDK
        assert "from mavsdk import System" in new_code
        assert "MissionItem(" in new_code
        # Legacy patterns must NOT appear in the rewritten body
        assert "drone.fly_to_relative(" not in new_code
        assert "drone.set_geofence(" not in new_code

    def test_real_mavsdk_code_unchanged(self):
        new_code, plan = rewrite_to_real_mavsdk(REAL_MAVSDK_CODE)
        assert plan.has_legacy_calls is False
        assert new_code == REAL_MAVSDK_CODE

    def test_idempotent_on_rewritten_code(self):
        once, _ = rewrite_to_real_mavsdk(LEGACY_CODE)
        twice, _ = rewrite_to_real_mavsdk(once)
        assert once == twice

    def test_distance_violation_clamps_waypoints(self):
        """Live-run regression: prompt says 6m, LLM emits 40m → clamp to 6m."""
        new_code, plan = rewrite_to_real_mavsdk(
            LEGACY_CODE,
            mission_prompt="Indoor inspection. Fly 6m north then land.",
        )
        # After clamping, the furthest waypoint must be ≤ 6m.
        max_dist = max(max(abs(wp.north_m), abs(wp.east_m)) for wp in plan.waypoints)
        assert max_dist <= 6.0 + 0.01, f"Rewriter failed to clamp: furthest waypoint at {max_dist}m"

    def test_distance_within_budget_preserved(self):
        small_legacy = """\
async def run_mission(drone):
    await drone.takeoff(altitude_m=4.0)
    await drone.fly_to_relative(north_m=5.0, east_m=0.0, down_m=0.0)
    await drone.land()
"""
        new_code, plan = rewrite_to_real_mavsdk(
            small_legacy,
            mission_prompt="fly 6m north",
        )
        # 5m ≤ 6m × 2.0 — no clamping.
        max_dist = max(max(abs(wp.north_m), abs(wp.east_m)) for wp in plan.waypoints)
        assert max_dist == pytest.approx(5.0, abs=0.01)

    def test_rewritten_code_passes_safety_layer(self):
        """Rewritten mission must pass the SafetyLayer's whitelist check."""
        from agents.safety_layer import SafetyLayer

        new_code, plan = rewrite_to_real_mavsdk(LEGACY_CODE)
        assert plan.has_legacy_calls
        layer = SafetyLayer(max_altitude=120.0, max_speed=15.0)
        report = layer.analyze(new_code)
        sr001 = [i for i in report.issues if i.rule_id == "SR001"]
        assert not sr001, "Rewritten code emits unapproved functions: " + ", ".join(
            i.description for i in sr001
        )

    def test_no_waypoints_skips_rewrite(self):
        """If the LLM somehow emits legacy calls but no extractable waypoints,
        return the code unchanged rather than producing an empty mission."""
        weird = """\
async def run_mission(drone):
    await drone.check_pre_flight()
    await drone.land()
"""
        new_code, plan = rewrite_to_real_mavsdk(weird)
        # check_pre_flight is legacy but yields no waypoints — leave alone.
        assert plan.has_legacy_calls
        assert new_code == weird

    def test_extracts_legacy_call_count(self):
        new_code, plan = rewrite_to_real_mavsdk(LEGACY_CODE)
        # The rewriter logs which legacy calls it found.
        assert len(plan.legacy_call_names) >= 4  # geofence + takeoff + 2× fly_to_relative


# ─────────────────────────────────────────────────────────────────────────────
# Geometry enforcement — the model must not author coordinates
# ─────────────────────────────────────────────────────────────────────────────

# What the LLM actually emitted for "a 30 m square" in September 2026: corners
# roughly a kilometre apart, which tripped the geofence with auto_fixable=false
# and sent the pipeline round the regeneration loop eleven times.
REAL_MAVSDK_WITH_BAD_COORDS = """\
import asyncio
from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan


async def run():
    drone = System()
    await drone.connect(system_address="udp://:14540")

    mission_items = [
        MissionItem(47.3977508, 8.5456073, 12.0, 5.0, True,
                    float('nan'), float('nan'),
                    MissionItem.CameraAction.NONE, float('nan'), float('nan'),
                    5.0, float('nan'), float('nan'),
                    MissionItem.VehicleAction.NONE),
        MissionItem(47.4067508, 8.5586073, 12.0, 5.0, True,
                    float('nan'), float('nan'),
                    MissionItem.CameraAction.NONE, float('nan'), float('nan'),
                    5.0, float('nan'), float('nan'),
                    MissionItem.VehicleAction.NONE),
    ]

    await drone.mission.upload_mission(MissionPlan(mission_items))
    print("mission uploaded")
"""

SQUARE_PROMPT = "A quadcopter that takes off to 12m, flies a 30m square, and lands"


def _legs_and_span(code: str) -> tuple[list[float], float]:
    """Measure the emitted waypoints in metres."""
    import math
    import re

    lats = [float(v) for v in re.findall(r"latitude_deg=([-\d.]+)", code)]
    lons = [float(v) for v in re.findall(r"longitude_deg=([-\d.]+)", code)]
    r_earth = 6371000.0
    cos_lat = math.cos(math.radians(lats[0]))

    def dist(i: int, j: int) -> float:
        dn = math.radians(lats[j] - lats[i]) * r_earth
        de = math.radians(lons[j] - lons[i]) * r_earth * cos_lat
        return math.hypot(dn, de)

    legs = [dist(i, i + 1) for i in range(len(lats) - 1)]
    span = max(dist(0, k) for k in range(len(lats)))
    return legs, span


class TestGeometryEnforcement:
    def test_replaces_kilometre_wide_square_with_a_30m_one(self):
        new_code, replaced = enforce_mission_geometry(REAL_MAVSDK_WITH_BAD_COORDS, SQUARE_PROMPT)
        assert replaced
        legs, span = _legs_and_span(new_code)
        assert len(legs) == 3  # 4 corners
        for leg in legs:
            assert leg == pytest.approx(30.0, abs=0.5)
        # The diagonal of a 30 m square, not a kilometre.
        assert span == pytest.approx(30.0 * 2**0.5, abs=1.0)

    def test_altitude_comes_from_the_prompt(self):
        new_code, replaced = enforce_mission_geometry(REAL_MAVSDK_WITH_BAD_COORDS, SQUARE_PROMPT)
        assert replaced
        assert "relative_altitude_m=12.00" in new_code
        assert "relative_altitude_m=10.00" not in new_code

    def test_output_is_valid_python_and_keeps_the_llm_logic(self):
        new_code, _ = enforce_mission_geometry(REAL_MAVSDK_WITH_BAD_COORDS, SQUARE_PROMPT)
        compile(new_code, "<generated>", "exec")
        # Everything the model wrote around the waypoints must survive.
        assert 'print("mission uploaded")' in new_code
        assert "upload_mission(MissionPlan(mission_items))" in new_code
        assert 'system_address="udp://:14540"' in new_code
        # ...and none of its coordinates may.
        assert "8.5586073" not in new_code

    def test_indentation_is_preserved(self):
        new_code, _ = enforce_mission_geometry(REAL_MAVSDK_WITH_BAD_COORDS, SQUARE_PROMPT)
        assert "\n    mission_items = [" in new_code

    def test_prompt_without_geometry_leaves_code_alone(self):
        code, replaced = enforce_mission_geometry(
            REAL_MAVSDK_WITH_BAD_COORDS, "take some nice photos"
        )
        assert not replaced
        assert code == REAL_MAVSDK_WITH_BAD_COORDS

    def test_code_without_mission_items_is_untouched(self):
        plain = "async def run(drone):\n    await drone.action.takeoff()\n"
        code, replaced = enforce_mission_geometry(plain, SQUARE_PROMPT)
        assert not replaced
        assert code == plain

    def test_unparseable_code_is_untouched(self):
        broken = "def run(:\n    mission_items = [\n"
        code, replaced = enforce_mission_geometry(broken, SQUARE_PROMPT)
        assert not replaced
        assert code == broken

    def test_square_closes_back_to_its_start(self):
        """A square's last corner must sit on top of its first."""
        import re

        new_code, _ = enforce_mission_geometry(REAL_MAVSDK_WITH_BAD_COORDS, SQUARE_PROMPT)
        lats = [float(v) for v in re.findall(r"latitude_deg=([-\d.]+)", new_code)]
        lons = [float(v) for v in re.findall(r"longitude_deg=([-\d.]+)", new_code)]
        # (30,0) (30,30) (0,30) (0,0): the fourth corner is home.
        assert lats[3] == pytest.approx(lats[0] - 0.00027, abs=0.0001)
        assert lons[3] == pytest.approx(lons[0], abs=0.00001)
