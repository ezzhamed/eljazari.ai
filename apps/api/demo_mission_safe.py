# Safety helper: normalize MAVSDK battery units
def _battery_remaining_pct(battery):
    """Return battery as percent whether MAVSDK reports 0..1 or 0..100."""
    value = float(battery.remaining_percent)
    return value * 100.0 if value <= 1.0 else value


# Safety helper: concrete RTL action wrapper
async def return_to_launch(drone):
    """Invoke MAVSDK RTL, with adapter fallback for generated wrappers."""
    try:
        await drone.action.return_to_launch()
    except AttributeError:
        await drone.return_to_launch()


# Safety helper: concrete emergency-stop action wrapper
async def emergency_stop(drone):
    """Invoke MAVSDK kill, with adapter fallback for generated wrappers."""
    try:
        await drone.action.kill()
    except AttributeError:
        await drone.emergency_stop()


# [IP001] Safety Injection: Battery Monitor
async def _battery_monitor(drone):
    """RTL when battery drops below safe threshold."""
    async for battery in drone.telemetry.battery():
        battery_pct = _battery_remaining_pct(battery)
        if battery_pct < 20.0:
            import structlog as _log

            _log.get_logger(__name__).warning("safety.low_battery_rtl", level=battery_pct)
            await return_to_launch(drone)
            break


# ── [IP002] Safety Injection: Connection Watchdog ─────────────────────────
async def _connection_watchdog(drone):
    """Initiate Return to Launch (RTL) if connection is lost.

    NOTE: Onboard PX4 datalink-loss failsafe (e.g. COM_RC_LOSS_T -> RTL) is
    the primary protection. This companion-script watchdog is a secondary
    protection layer.
    """
    async for state in drone.core.connection_state():
        if not state.is_connected:
            import structlog as _log

            _log.get_logger(__name__).warning("safety.connection_lost_rtl")
            await return_to_launch(drone)
            break


# ─────────────────────────────────────────────────────────────────────────────


# [IP003] Safety Injection: Geofence Enforcer
async def _geofence_enforcer(drone, max_radius_m=500.0):
    """RTL when drone exits geofence."""
    import math as _math

    home_n, home_e = 0.0, 0.0
    async for pos in drone.telemetry.position_velocity_ned():
        n = pos.position.north_m
        e = pos.position.east_m
        if _math.hypot(n - home_n, e - home_e) > max_radius_m:
            import structlog as _log

            _log.get_logger(__name__).warning("safety.geofence_breach_rtl", north=n, east=e)
            await return_to_launch(drone)
            break


# ── [IP004] Safety Injection: Emergency Stop Handler ─────────────────────
import signal as _signal


def _emergency_signal_handler(signum, frame):
    import structlog as _log

    _log.get_logger(__name__).critical("safety.emergency_signal_received", signal=signum)
    raise SystemExit(1)


_signal.signal(_signal.SIGTERM, _emergency_signal_handler)
_signal.signal(_signal.SIGINT, _emergency_signal_handler)
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import logging
import math
import os

from mavsdk import System
from mavsdk.action import ActionError
from mavsdk.mission import MissionItem, MissionPlan
from mavsdk.telemetry import LandedState

logger = logging.getLogger(__name__)
MAVSDK_ADDRESS = os.getenv("DRONEPROMPT_MAVSDK_ADDRESS", "udpin://0.0.0.0:14540")

EARTH_RADIUS_M = 6_371_000.0


def ned_to_lla(home_lat_deg, home_lon_deg, north_m, east_m):
    dlat = math.degrees(north_m / EARTH_RADIUS_M)
    dlon = math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(home_lat_deg))))
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
        if (battery.remaining_percent or 0) < 0.2:
            raise RuntimeError("Battery too low for takeoff")
        break
    async for home in drone.telemetry.home():
        home_lat, home_lon = home.latitude_deg, home.longitude_deg
        break

    square_waypoints_ned = [
        (100.0, 0.0),  # north corner
        (100.0, 100.0),  # NE corner
        (0.0, 100.0),  # east corner
        (0.0, 0.0),  # home column
    ]
    items = []
    for north_m, east_m in square_waypoints_ned:
        lat, lon = ned_to_lla(home_lat, home_lon, north_m, east_m)
        items.append(
            MissionItem(
                latitude_deg=lat,
                longitude_deg=lon,
                relative_altitude_m=30.0,
                speed_m_s=27.0,
                is_fly_through=True,
                gimbal_pitch_deg=-45.0,
                gimbal_yaw_deg=0.0,
                camera_action=MissionItem.CameraAction.NONE,
                loiter_time_s=0.0,
                camera_photo_interval_s=0.0,
                acceptance_radius_m=2.0,
                yaw_deg=float("nan"),
                camera_photo_distance_m=float("nan"),
                vehicle_action=MissionItem.VehicleAction.NONE,
            )
        )
    await drone.mission.set_return_to_launch_after_mission(True)
    await drone.mission.upload_mission(MissionPlan(items))

    try:
        await drone.action.arm()
        await drone.action.takeoff()
    except ActionError as exc:
        logger.error("takeoff_failed: %s", exc)
        return False

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
