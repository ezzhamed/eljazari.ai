/**
 * @eljazari/gsce-framework
 * Implements the full 4-layer GSCE framework for PX4 mission code generation.
 * GSCE = Guidelines + Skill APIs + Constraints + Examples
 */

import { MissionConstraints } from "@eljazari/shared-types";

export const GUIDELINES_PROMPT = `You are a code-generation agent specialized in producing flight-control code for PX4-based drones.

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
- North = +X, East = +Y, Down = +Z`;

export const SKILL_APIS = `## Available APIs (use only these — do not invent new functions):

### Takeoff & Landing:
- await drone.takeoff(altitude_m: float) -> bool
- await drone.land() -> bool
- await drone.emergency_stop() -> None

### Movement:
- await drone.fly_to(lat: float, lon: float, alt: float, speed: float = 5.0) -> bool
- await drone.fly_to_relative(north_m: float, east_m: float, down_m: float) -> bool
- await drone.set_yaw(heading_deg: float) -> bool
- await drone.orbit(radius_m: float, velocity_ms: float, center_lat: float, center_lon: float) -> bool

### Telemetry & Sensing:
- await drone.get_position() -> Position(lat, lon, alt)
- await drone.get_battery() -> Battery(remaining_percent, voltage)
- await drone.get_attitude() -> Attitude(roll, pitch, yaw)
- await drone.get_gps_info() -> GpsInfo(num_satellites, fix_type)

### Safety:
- await drone.set_geofence(radius_m: float, max_alt_m: float) -> bool
- await drone.set_return_to_launch_alt(alt_m: float) -> bool
- await drone.check_pre_flight() -> PreFlightResult`;

export const CONSTRAINTS_TEMPLATE = `## Strict Constraints (must never be violated under any circumstances):

### Spatial Limits:
- GEOFENCE_RADIUS: {geofence_radius_m} meters from takeoff point
- MAX_ALTITUDE: {max_altitude_m} meters
- MIN_ALTITUDE: 2 meters (never fly below this)

### Operational Limits:
- MAX_SPEED: {max_speed_ms} m/s
- MIN_BATTERY_PERCENT: 20% (must land/return if below this threshold)
- MAX_MISSION_DURATION: {max_duration_s} seconds
- WIND_SPEED_LIMIT: 10 m/s (abort mission if exceeded)

### Safety Rules:
- Verify GPS fix before takeoff (minimum 8 satellites)
- Verify battery is > 50% before takeoff
- Perform a waypoint safety check every 10 seconds
- If connection is lost for > 5 seconds -> automatically Return to Launch`;

export const EXAMPLES = `## Example 1: Simple Photography Mission
Request: "Fly forward 50 meters at 10 meters altitude, capture a photo, then return"
Code:
\`\`\`python
import asyncio
import logging
from mavsdk import System

logger = logging.getLogger(__name__)

async def photo_mission(drone):
    # Pre-flight checks
    pre_flight = await drone.check_pre_flight()
    if not pre_flight.passed:
        logger.error(f"Pre-flight failed: {pre_flight.issues}")
        return False

    battery = await drone.get_battery()
    if battery.remaining_percent < 50:
        logger.error("Battery below 50% — aborting")
        return False

    gps = await drone.get_gps_info()
    if gps.num_satellites < 8:
        logger.error(f"Insufficient GPS satellites: {gps.num_satellites}")
        return False

    try:
        logger.info("Setting geofence: 200m radius, 30m alt")
        await drone.set_geofence(radius_m=200.0, max_alt_m=30.0)

        logger.info("Takeoff to 10m")
        await drone.takeoff(altitude_m=10.0)
        await asyncio.sleep(3)

        logger.info("Flying north 50m")
        await drone.fly_to_relative(north_m=50.0, east_m=0.0, down_m=0.0)

        position = await drone.get_position()
        logger.info(f"Photo captured at: lat={position.lat}, lon={position.lon}, alt={position.alt}")

        logger.info("Returning to home")
        await drone.fly_to_relative(north_m=-50.0, east_m=0.0, down_m=0.0)
        await drone.land()
        logger.info("Mission complete")
        return True

    except Exception as e:
        logger.error(f"Mission error: {e} — initiating safe landing")
        await drone.land()
        return False
\`\`\`

## Example 2: Square Patrol Mission
Request: "Perform a square patrol of 100x100 meters at 15 meters altitude"
Code:
\`\`\`python
import asyncio
import logging
from mavsdk import System

logger = logging.getLogger(__name__)

async def square_patrol(drone):
    pre_flight = await drone.check_pre_flight()
    if not pre_flight.passed:
        logger.error(f"Pre-flight failed: {pre_flight.issues}")
        return False

    battery = await drone.get_battery()
    if battery.remaining_percent < 50:
        logger.error("Low battery — aborting")
        return False

    waypoints = [
        (100.0,   0.0, 0.0),   # Leg 1: north
        (  0.0, 100.0, 0.0),   # Leg 2: east
        (-100.0, 0.0, 0.0),    # Leg 3: south
        (  0.0,-100.0, 0.0),   # Leg 4: west
    ]

    try:
        await drone.set_geofence(radius_m=250.0, max_alt_m=30.0)
        await drone.takeoff(altitude_m=15.0)
        await asyncio.sleep(3)

        for i, (n, e, d) in enumerate(waypoints):
            logger.info(f"Waypoint {i+1}/{len(waypoints)}: N={n} E={e}")
            batt = await drone.get_battery()
            if batt.remaining_percent < 20:
                logger.warning("Low battery — ending patrol early")
                break
            await drone.fly_to_relative(north_m=n, east_m=e, down_m=d)

        await drone.land()
        return True

    except Exception as e:
        logger.error(f"Patrol error: {e} — safe landing")
        await drone.land()
        return False
\`\`\``;

export const OUTPUT_FORMAT = `## Required Output Format:
After the code, append a JSON block wrapped in \`\`\`json and \`\`\` containing:
\`\`\`json
{
  "mission_summary": "Brief description of the mission",
  "estimated_duration_s": <float>,
  "estimated_distance_m": <float>,
  "waypoints": [{"name": "...", "north_m": 0, "east_m": 0, "alt_m": 0, "action": "..."}],
  "safety_checks": ["...", "..."],
  "constraints_applied": ["...", "..."],
  "confidence_score": <0.0-1.0>
}
\`\`\``;

/**
 * Constructs the full 4-layer GSCE prompt.
 */
export function buildPrompt(
  missionDescription: string,
  constraints: Partial<MissionConstraints> & { max_duration_s?: number },
  feedback: string = ""
): string {
  const geofence = constraints.geofence_radius_m ?? 500;
  const maxAlt = constraints.max_altitude_m ?? 120;
  const maxSpeed = constraints.max_speed_ms ?? 15;
  const maxDuration = constraints.max_duration_s ?? 600;

  const constraintsSection = CONSTRAINTS_TEMPLATE
    .replace("{geofence_radius_m}", geofence.toString())
    .replace("{max_altitude_m}", maxAlt.toString())
    .replace("{max_speed_ms}", maxSpeed.toString())
    .replace("{max_duration_s}", maxDuration.toString());

  const feedbackSection = feedback
    ? `\n## Feedback on previous attempt:\n${feedback}\n`
    : "";

  return `${GUIDELINES_PROMPT}\n\n` +
    `${SKILL_APIS}\n\n` +
    `${constraintsSection}\n\n` +
    `${EXAMPLES}\n\n` +
    `${OUTPUT_FORMAT}\n` +
    `${feedbackSection}` +
    `## Requested Mission:\n${missionDescription}\n\n` +
    `Write the complete code:`;
}

/**
 * Helper to extract Python code block from LLM response.
 */
export function extractCode(response: string): string {
  if (response.includes("```python")) {
    const parts = response.split("```python");
    if (parts.length > 1) {
      return parts[1].split("```")[0].trim();
    }
  }
  if (response.includes("```")) {
    const parts = response.split("```");
    if (parts.length > 1) {
      return parts[1].trim();
    }
  }
  return response.trim();
}

/**
 * Helper to extract JSON metadata block from LLM response.
 */
export function extractMetadata(response: string): string {
  if (response.includes("```json")) {
    const parts = response.split("```json");
    if (parts.length > 1) {
      return parts[1].split("```")[0].trim();
    }
  }
  return "";
}

/**
 * @deprecated Use plain functions buildPrompt, extractCode, extractMetadata directly.
 */
export class GCSEPromptBuilder {
  public static buildPrompt(
    missionDescription: string,
    constraints: Partial<MissionConstraints> & { max_duration_s?: number },
    feedback: string = ""
  ): string {
    return buildPrompt(missionDescription, constraints, feedback);
  }

  public static extractCode(response: string): string {
    return extractCode(response);
  }

  public static extractMetadata(response: string): string {
    return extractMetadata(response);
  }
}
