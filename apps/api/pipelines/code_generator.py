"""eljazari.ai — GSCE Code Generator
Implements the full 4-layer GSCE framework for PX4 mission code generation.

GSCE = Guidelines + Skill APIs + Constraints + Examples
Reference: arxiv:2502.12531 — improved success rate from 7.6% → 90.9%
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — GUIDELINES
# Behavioral contract for the LLM: what it is, what rules it must follow.
# ─────────────────────────────────────────────────────────────────────────────
GUIDELINES_PROMPT = """\
You are a code-generation agent specialized in producing flight-control code for PX4-based drones.

## Your strict rules:
- Write Python only, using the MAVSDK library
- Every program must begin with a connection check and arming sequence
- Every program must end with a safe landing sequence
- Do not generate any code that exceeds the defined spatial limits
- Do not generate movement commands without checking the battery state
- Use async/await in every function
- Add comprehensive error handling with a fallback to safe landing
- Add detailed logging for every action
- All coordinates use the NED (North-East-Down) frame
- North = +X, East = +Y, Down = +Z
"""

# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — SKILL APIs   (Phase 8: real MAVSDK only)
# Whitelist of approved MAVSDK calls — LLM must NOT invent helper wrappers.
# Every entry below is a real function in the ``mavsdk`` Python package.
# ─────────────────────────────────────────────────────────────────────────────
SKILL_APIS = """\
## MAVSDK calls available to you (use these directly — no abstractions):

### Connection & startup:
- drone = System()
- await drone.connect(system_address=os.getenv("DRONEPROMPT_MAVSDK_ADDRESS"))
- async for state in drone.core.connection_state(): if state.is_connected: break
- async for health in drone.telemetry.health(): if health.is_global_position_ok and health.is_home_position_ok: break

### Telemetry (async iterators):
- drone.telemetry.battery()               → .remaining_percent, .voltage_v
- drone.telemetry.position_velocity_ned() → .position.north_m / east_m / down_m
- drone.telemetry.attitude_euler()        → .roll_deg, .pitch_deg, .yaw_deg
- drone.telemetry.gps_info()              → .num_satellites, .fix_type
- drone.telemetry.home()                  → .latitude_deg, .longitude_deg
- drone.telemetry.landed_state()          → LandedState.ON_GROUND / IN_AIR
- drone.telemetry.health()                → .is_armable, .is_global_position_ok ...

### Action (one-shot commands):
- await drone.action.arm()
- await drone.action.disarm()
- await drone.action.set_takeoff_altitude(altitude_m: float)
- await drone.action.takeoff()
- await drone.action.land()
- await drone.action.return_to_launch()
- await drone.action.kill()                          # emergency
- await drone.action.set_return_to_launch_altitude(altitude_m: float)

### Mission (recommended for multi-waypoint flights):
- await drone.mission.upload_mission(MissionPlan(mission_items))
- await drone.mission.set_return_to_launch_after_mission(enable: bool)
- await drone.mission.start_mission()
- await drone.mission.pause_mission()
- await drone.mission.clear_mission()
- async for progress in drone.mission.mission_progress(): ...

### Geofence:
- await drone.geofence.upload_geofence(GeofenceData(polygons=[...], circles=[]))

### MissionItem constructor (every field required):
- MissionItem(latitude_deg, longitude_deg, relative_altitude_m, speed_m_s,
              is_fly_through, gimbal_pitch_deg, gimbal_yaw_deg,
              camera_action=MissionItem.CameraAction.NONE,
              loiter_time_s, camera_photo_interval_s,
              acceptance_radius_m, yaw_deg, camera_photo_distance_m,
              vehicle_action=MissionItem.VehicleAction.NONE)

## NOT ALLOWED (legacy skill-API abstractions removed in Phase 8):
✗ drone.fly_to_relative(...)         — build a MissionItem instead
✗ drone.fly_to(...)                  — build a MissionItem instead
✗ drone.set_geofence(radius, alt)    — use drone.geofence.upload_geofence(...)
✗ drone.takeoff(altitude_m=...)      — call set_takeoff_altitude() then takeoff()
✗ drone.emergency_stop()             — use drone.action.kill()
✗ drone.check_pre_flight()           — iterate drone.telemetry.health() instead
✗ Any function that doesn't exist in the mavsdk Python package
"""

# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — CONSTRAINTS  (template — filled at runtime)
# Hard physical/operational limits that cannot be violated.
# ─────────────────────────────────────────────────────────────────────────────
CONSTRAINTS_TEMPLATE = """\
## Strict constraints (must never be exceeded under any circumstance):

### Spatial limits:
- GEOFENCE_RADIUS: {geofence_radius_m} m from the launch point
- MAX_ALTITUDE: {max_altitude_m} m
- MIN_ALTITUDE: 2 m (never fly below this)

### Operational limits:
- MAX_SPEED: {max_speed_ms} m/s
- MIN_BATTERY_PERCENT: 20% (must return when this limit is reached)
- MAX_MISSION_DURATION: {max_duration_s} seconds
- WIND_SPEED_LIMIT: 10 m/s (abort the mission if exceeded)

### Safety rules:
- Check the GPS fix before takeoff (minimum 8 satellites)
- Check that the battery is > 50% before takeoff
- Add a waypoint safety check every 10 seconds
- If the connection drops for more than 5 seconds → automatic Return to Launch
"""

# ─────────────────────────────────────────────────────────────────────────────
# Layer 4 — EXAMPLES   (Phase 8: real MAVSDK only)
# In-context reference missions in the canonical "Mission Plan" pattern.
# Convert any NED waypoint to absolute lat/lon using the host_position_ok home.
# ─────────────────────────────────────────────────────────────────────────────
EXAMPLES = """\
## Example 1 — Photo run: takeoff, fly 50 m north, return, land

```python
import asyncio
import logging
import math
import os

from mavsdk import System
from mavsdk.action import ActionError
from mavsdk.mission import MissionItem, MissionPlan
from mavsdk.telemetry import FixType, LandedState

logger = logging.getLogger(__name__)
MAVSDK_ADDRESS = os.getenv("DRONEPROMPT_MAVSDK_ADDRESS", "udpin://0.0.0.0:14540")

EARTH_RADIUS_M = 6_371_000.0


def ned_to_lla(home_lat_deg, home_lon_deg, north_m, east_m):
    dlat = math.degrees(north_m / EARTH_RADIUS_M)
    dlon = math.degrees(
        east_m / (EARTH_RADIUS_M * math.cos(math.radians(home_lat_deg)))
    )
    return home_lat_deg + dlat, home_lon_deg + dlon


async def main():
    logging.basicConfig(level=logging.INFO)
    drone = System()
    await drone.connect(system_address=MAVSDK_ADDRESS)

    async for state in drone.core.connection_state():
        if state.is_connected:
            break
    async for health in drone.telemetry.health():
        if health.is_global_position_ok and health.is_home_position_ok:
            break
    async for gps in drone.telemetry.gps_info():
        fix_name = getattr(getattr(gps, "fix_type", None), "name", "")
        fix_ok = ("3D" in fix_name) or ("DGPS" in fix_name) or ("RTK" in fix_name)
        if fix_ok and gps.num_satellites >= 6:
            break
    async for battery in drone.telemetry.battery():
        if (battery.remaining_percent or 0) < 0.5:
            raise RuntimeError("Battery too low for takeoff")
        break
    async for home in drone.telemetry.home():
        home_lat, home_lon = home.latitude_deg, home.longitude_deg
        break

    target_lat, target_lon = ned_to_lla(home_lat, home_lon, 50.0, 0.0)
    items = [
        MissionItem(
            latitude_deg=target_lat,
            longitude_deg=target_lon,
            relative_altitude_m=10.0,
            speed_m_s=5.0,
            is_fly_through=True,
            gimbal_pitch_deg=-30.0,
            gimbal_yaw_deg=0.0,
            camera_action=MissionItem.CameraAction.TAKE_PHOTO,
            loiter_time_s=2.0,
            camera_photo_interval_s=0.0,
            acceptance_radius_m=1.0,
            yaw_deg=float("nan"),
            camera_photo_distance_m=float("nan"),
            vehicle_action=MissionItem.VehicleAction.NONE,
        ),
    ]
    await drone.action.set_takeoff_altitude(10.0)
    await drone.mission.set_return_to_launch_after_mission(True)
    await drone.mission.upload_mission(MissionPlan(items))

    try:
        await drone.action.arm()
        await drone.action.takeoff()
    except ActionError as exc:
        logger.error("takeoff_failed: %s", exc); return False

    await drone.mission.start_mission()
    async for p in drone.mission.mission_progress():
        if p.current >= p.total:
            break
    await drone.action.return_to_launch()
    async for state in drone.telemetry.landed_state():
        if state == LandedState.ON_GROUND:
            break
    return True


if __name__ == "__main__":
    import sys
    sys.exit(0 if asyncio.run(main()) else 1)
```

## Example 2 — 100 × 100 m square patrol at 15 m

```python
# Same boilerplate as Example 1; the mission body just adds 4 waypoints.
square_waypoints_ned = [
    ( 100.0,   0.0),   # north corner
    ( 100.0, 100.0),   # NE corner
    (   0.0, 100.0),   # east corner
    (   0.0,   0.0),   # home column
]
items = []
for north_m, east_m in square_waypoints_ned:
    lat, lon = ned_to_lla(home_lat, home_lon, north_m, east_m)
    items.append(MissionItem(
        latitude_deg=lat, longitude_deg=lon, relative_altitude_m=15.0,
        speed_m_s=6.0, is_fly_through=True,
        gimbal_pitch_deg=-45.0, gimbal_yaw_deg=0.0,
        camera_action=MissionItem.CameraAction.NONE,
        loiter_time_s=0.0, camera_photo_interval_s=0.0,
        acceptance_radius_m=2.0, yaw_deg=float("nan"),
        camera_photo_distance_m=float("nan"),
        vehicle_action=MissionItem.VehicleAction.NONE,
    ))
await drone.mission.upload_mission(MissionPlan(items))
await drone.action.arm()
await drone.action.takeoff()
await drone.mission.start_mission()
```
"""

# ─────────────────────────────────────────────────────────────────────────────
# Structured output request appended at end of prompt
# ─────────────────────────────────────────────────────────────────────────────
OUTPUT_FORMAT = """\
## Required output format:
After the code, add a JSON section between ```json and ``` containing:
```json
{
  "mission_summary": "Brief mission description",
  "estimated_duration_s": <float>,
  "estimated_distance_m": <float>,
  "waypoints": [{"name": "...", "north_m": 0, "east_m": 0, "alt_m": 0, "action": "..."}],
  "safety_checks": ["...", "..."],
  "constraints_applied": ["...", "..."],
  "confidence_score": <0.0-1.0>
}
```
"""


class GCSECodeGenerator:
    """Builds GSCE-structured prompts and extracts code from LLM responses."""

    def __init__(self, llm: Any) -> None:
        self.llm = llm

    async def generate(
        self,
        mission_description: str,
        design_tree: dict[str, Any],
        constraints: dict[str, Any],
        feedback: str = "",
    ) -> tuple[str, str]:
        """
        Returns (code: str, metadata_json: str).
        metadata_json may be empty string if LLM didn't produce it.
        """
        prompt = self._build_prompt(mission_description, constraints, feedback)
        logger.info("GSCE prompt built", extra={"prompt_len": len(prompt)})
        raw_response = await self.llm.ainvoke(prompt)
        code = self._extract_code(raw_response)
        metadata_json = self._extract_metadata(raw_response)
        return code, metadata_json

    # ── Prompt construction ──────────────────────────────────────────────────

    def _build_prompt(
        self,
        mission: str,
        constraints: dict[str, Any],
        feedback: str,
    ) -> str:
        constraints_section = CONSTRAINTS_TEMPLATE.format(
            geofence_radius_m=constraints.get("geofence_radius_m", 500),
            max_altitude_m=constraints.get("max_altitude_m", 120),
            max_speed_ms=constraints.get("max_speed_ms", 15),
            max_duration_s=constraints.get("max_duration_s", 600),
        )
        feedback_section = f"\n## Notes on the previous attempt:\n{feedback}\n" if feedback else ""

        return (
            f"{GUIDELINES_PROMPT}\n\n"
            f"{SKILL_APIS}\n\n"
            f"{constraints_section}\n\n"
            f"{EXAMPLES}\n\n"
            f"{OUTPUT_FORMAT}\n"
            f"{feedback_section}"
            f"## Requested mission:\n{mission}\n\n"
            "Write the complete code:"
        )

    # ── Response extraction ──────────────────────────────────────────────────

    def _extract_code(self, response: str) -> str:
        """Extract first Python code block from response."""
        if "```python" in response:
            parts = response.split("```python")
            if len(parts) > 1:
                return parts[1].split("```")[0].strip()
        if "```" in response:
            parts = response.split("```")
            if len(parts) > 1:
                return parts[1].strip()
        return response.strip()

    def _extract_metadata(self, response: str) -> str:
        """Extract JSON metadata block from response (may be empty)."""
        if "```json" in response:
            parts = response.split("```json")
            if len(parts) > 1:
                return parts[1].split("```")[0].strip()
        return ""
