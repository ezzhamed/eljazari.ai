"""Synthetic SITL flights shaped like the mission runner's records.

For gate tests only. The motion is idealised (constant rates, no noise); it is
not flight data. Real flights checked against the same gate are listed in
docs/SIMULATION_ACCEPTANCE.md.
"""

from __future__ import annotations

import math

from pipelines.mavsdk_templates import render_real_mavsdk_mission
from pipelines.mission_builder import MissionPlanSpec, Waypoint
from utils.coordinates import PX4_SITL_DEFAULT_HOME

RATE_HZ = 10.0
CLIMB_M_S = 2.0
CRUISE_M_S = 5.0


def mission_code(
    corners: list[tuple[float, float]] | None = None,
    *,
    altitude_m: float = 5.0,
    hold_s: float = 0.0,
) -> str:
    """The template's code for a hover at home, or a path through ``corners``."""
    corners = corners or [(0.0, 0.0)]
    spec = MissionPlanSpec(
        home=PX4_SITL_DEFAULT_HOME,
        default_speed_m_s=CRUISE_M_S,
        takeoff_altitude_m=altitude_m,
        waypoints=[
            Waypoint(
                name=f"wp{i}",
                north_m=north,
                east_m=east,
                alt_m=altitude_m,
                is_fly_through=False,
                loiter_time_s=hold_s,
            )
            for i, (north, east) in enumerate(corners)
        ],
    )
    return render_real_mavsdk_mission(spec)


def flown_mission(
    corners: list[tuple[float, float]] | None = None,
    *,
    altitude_m: float = 5.0,
    hold_s: float = 0.0,
) -> list[dict]:
    """Samples at 10 Hz: ground, takeoff to 95%, mission, return, landing."""
    corners = corners or [(0.0, 0.0)]
    points: list[dict] = []
    state = {"t": 0.0, "n": 0.0, "e": 0.0, "alt": 0.0}

    def sample(mode: str) -> None:
        points.append(
            {
                "timestamp_s": round(state["t"], 2),
                "north_m": state["n"],
                "east_m": state["e"],
                "alt_m": state["alt"],
                "flight_mode": mode,
            }
        )
        state["t"] += 1.0 / RATE_HZ

    def move(mode: str, north: float, east: float, alt: float) -> None:
        start = dict(state)
        span = max(
            math.hypot(north - start["n"], east - start["e"]) / CRUISE_M_S,
            abs(alt - start["alt"]) / CLIMB_M_S,
        )
        steps = max(1, round(span * RATE_HZ))
        for k in range(1, steps + 1):
            f = k / steps
            state["n"] = start["n"] + f * (north - start["n"])
            state["e"] = start["e"] + f * (east - start["e"])
            state["alt"] = start["alt"] + f * (alt - start["alt"])
            sample(mode)

    for _ in range(int(RATE_HZ)):
        sample("HOLD")
    move("TAKEOFF", 0.0, 0.0, 0.95 * altitude_m)
    for north, east in corners:
        move("MISSION", north, east, altitude_m)
        for _ in range(int(hold_s * RATE_HZ) + 2):
            sample("MISSION")
    move("RETURN_TO_LAUNCH", 0.0, 0.0, altitude_m)
    move("RETURN_TO_LAUNCH", 0.0, 0.0, 0.0)
    return points


def flight(altitude_m: float = 5.0, hold_s: float = 2.0) -> dict:
    """State fields for a hover that flies exactly what its code commands."""
    return {
        "generated_code": mission_code(altitude_m=altitude_m, hold_s=hold_s),
        "telemetry_data": flown_mission(altitude_m=altitude_m, hold_s=hold_s),
    }
