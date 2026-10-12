"""eljazari.ai Phase 4.2 — MAVLink ↔ WebSocket Bridge

Bridges MAVSDK-Python with the Socket.io server so the WebGCS can:
  • Receive live telemetry from a PX4 drone (real hardware or SITL)
  • Send flight commands: Arm, Takeoff, Land, RTL, E-Stop, Pause
  • Upload MAVLink waypoint missions
  • Set / read PID parameters via MAVLink PARAM_SET/PARAM_REQUEST_READ

Connection modes
  - SITL:  udp://:14540  (default PX4 SITL UDP port)
  - Real:  serial:///dev/ttyUSB0:57600  or  tcp://192.168.1.1:5760

Usage:
    bridge = MAVLinkBridge(project_id="proj-001", system_addr="udp://:14540")
    await bridge.connect()
    await bridge.start_telemetry_loop()   # runs until bridge.stop()
"""

import asyncio
import logging
import math
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from mavsdk import System
    from mavsdk.action import ActionError
    from mavsdk.mission import MissionItem, MissionPlan
    from mavsdk.param import ParamError

    _MAVSDK_AVAILABLE = True
except ImportError:
    _MAVSDK_AVAILABLE = False
    logger.warning("mavsdk not installed — MAVLink bridge in stub mode")

from ws.socket_manager import broadcast_to_project


# ── Constants ─────────────────────────────────────────────────────────────────

TELEMETRY_HZ = 10  # target telemetry publish rate
TELEMETRY_INTERVAL = 1.0 / TELEMETRY_HZ
RECONNECT_DELAY = 3.0  # seconds between reconnect attempts
MAX_RECONNECTS = 20


# ── MAVLinkBridge ─────────────────────────────────────────────────────────────


class MAVLinkBridge:
    """Singleton bridge per project — connects to one PX4 instance.

    Lifecycle:
        bridge = MAVLinkBridge(project_id, system_addr)
        await bridge.connect()
        task  = asyncio.create_task(bridge.start_telemetry_loop())
        ...
        await bridge.stop()
        task.cancel()
    """

    def __init__(
        self,
        project_id: str,
        system_addr: str = "udp://:14540",
    ) -> None:
        self.project_id = project_id
        self.system_addr = system_addr
        self._drone: Optional[Any] = None
        self._running = False
        self._connected = False

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self, timeout_s: float = 30.0) -> bool:
        """Connect to the PX4 instance.  Returns True on success."""
        if not _MAVSDK_AVAILABLE:
            logger.info("[MAVLink] stub mode — no MAVSDK")
            self._connected = True
            return True

        self._drone = System()
        try:
            await asyncio.wait_for(
                self._drone.connect(system_address=self.system_addr),
                timeout=timeout_s,
            )
            # Wait for heartbeat
            async for state in self._drone.core.connection_state():
                if state.is_connected:
                    self._connected = True
                    logger.info("[MAVLink] connected to %s", self.system_addr)
                    await self._broadcast_event("info", "MAVLink connected", "mavlink")
                    return True
                break
        except asyncio.TimeoutError:
            logger.warning("[MAVLink] timeout connecting to %s", self.system_addr)
        except Exception as exc:
            logger.error("[MAVLink] connection error: %s", exc)

        self._connected = False
        await self._broadcast_event(
            "error", f"MAVLink connect failed: {self.system_addr}", "mavlink"
        )
        return False

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        logger.info("[MAVLink] bridge stopped for project %s", self.project_id)

    # ── Telemetry loop ────────────────────────────────────────────────────────

    async def start_telemetry_loop(self) -> None:
        """Continuously publish telemetry to the project's Socket.io room."""
        self._running = True
        reconnects = 0

        while self._running:
            if not self._connected:
                if reconnects >= MAX_RECONNECTS:
                    logger.error("[MAVLink] max reconnects reached, stopping")
                    break
                await asyncio.sleep(RECONNECT_DELAY)
                await self.connect()
                reconnects += 1
                continue

            reconnects = 0

            if not _MAVSDK_AVAILABLE:
                # Stub: emit synthetic telemetry for frontend dev
                await self._emit_stub_telemetry()
                await asyncio.sleep(TELEMETRY_INTERVAL)
                continue

            try:
                await self._run_telemetry_tasks()
            except Exception as exc:
                logger.warning("[MAVLink] telemetry loop error: %s", exc)
                self._connected = False

    async def _run_telemetry_tasks(self) -> None:
        """Subscribe to all telemetry streams concurrently."""
        await asyncio.gather(
            self._stream_position(),
            self._stream_attitude(),
            self._stream_battery(),
            self._stream_gps(),
            self._stream_flight_mode(),
            return_exceptions=True,
        )

    async def _stream_position(self) -> None:
        async for pos in self._drone.telemetry.position():
            if not self._running:
                break
            await broadcast_to_project(
                self.project_id,
                "mavlink_telemetry",
                {
                    "type": "position",
                    "lat": pos.latitude_deg,
                    "lon": pos.longitude_deg,
                    "alt_m": pos.relative_altitude_m,
                    "abs_alt_m": pos.absolute_altitude_m,
                },
            )

    async def _stream_attitude(self) -> None:
        async for att in self._drone.telemetry.attitude_euler():
            if not self._running:
                break
            await broadcast_to_project(
                self.project_id,
                "mavlink_telemetry",
                {
                    "type": "attitude",
                    "roll_deg": att.roll_deg,
                    "pitch_deg": att.pitch_deg,
                    "yaw_deg": att.yaw_deg,
                },
            )

    async def _stream_battery(self) -> None:
        async for bat in self._drone.telemetry.battery():
            if not self._running:
                break
            await broadcast_to_project(
                self.project_id,
                "mavlink_telemetry",
                {
                    "type": "battery",
                    "voltage_v": bat.voltage_v,
                    "remaining_pct": round(bat.remaining_percent * 100, 1),
                },
            )

    async def _stream_gps(self) -> None:
        async for gps in self._drone.telemetry.gps_info():
            if not self._running:
                break
            await broadcast_to_project(
                self.project_id,
                "mavlink_telemetry",
                {
                    "type": "gps",
                    "num_satellites": gps.num_satellites,
                    "fix_type": str(gps.fix_type),
                },
            )

    async def _stream_flight_mode(self) -> None:
        async for mode in self._drone.telemetry.flight_mode():
            if not self._running:
                break
            await broadcast_to_project(
                self.project_id,
                "mavlink_telemetry",
                {
                    "type": "flight_mode",
                    "flight_mode": str(mode),
                },
            )

    # ── Flight commands ───────────────────────────────────────────────────────

    async def arm(self) -> dict[str, Any]:
        return await self._run_command("arm", self._drone.action.arm if _MAVSDK_AVAILABLE else None)

    async def disarm(self) -> dict[str, Any]:
        return await self._run_command(
            "disarm", self._drone.action.disarm if _MAVSDK_AVAILABLE else None
        )

    async def takeoff(self, altitude_m: float = 10.0) -> dict[str, Any]:
        if _MAVSDK_AVAILABLE and self._drone:
            try:
                await self._drone.action.set_takeoff_altitude(altitude_m)
                await self._drone.action.takeoff()
                return {"ok": True, "command": "takeoff", "altitude_m": altitude_m}
            except ActionError as e:
                return {"ok": False, "command": "takeoff", "error": str(e)}
        return {"ok": True, "command": "takeoff", "stub": True}

    async def land(self) -> dict[str, Any]:
        return await self._run_command(
            "land", self._drone.action.land if _MAVSDK_AVAILABLE else None
        )

    async def return_to_launch(self) -> dict[str, Any]:
        return await self._run_command(
            "rtl", self._drone.action.return_to_launch if _MAVSDK_AVAILABLE else None
        )

    async def emergency_stop(self) -> dict[str, Any]:
        return await self._run_command(
            "estop", self._drone.action.kill if _MAVSDK_AVAILABLE else None
        )

    async def pause_mission(self) -> dict[str, Any]:
        return await self._run_command(
            "pause", self._drone.mission.pause_mission if _MAVSDK_AVAILABLE else None
        )

    async def _run_command(self, name: str, fn: Any) -> dict[str, Any]:
        if not _MAVSDK_AVAILABLE or fn is None:
            await self._broadcast_event("cmd", f"[stub] {name.upper()} sent", "mavlink")
            return {"ok": True, "command": name, "stub": True}
        try:
            await fn()
            await self._broadcast_event("cmd", f"{name.upper()} command sent", "mavlink")
            return {"ok": True, "command": name}
        except Exception as exc:
            err = str(exc)
            await self._broadcast_event("error", f"{name.upper()} failed: {err}", "mavlink")
            return {"ok": False, "command": name, "error": err}

    # ── Mission upload ────────────────────────────────────────────────────────

    async def upload_mission(self, waypoints: list[dict[str, Any]]) -> dict[str, Any]:
        """Upload a list of waypoint dicts {lat, lon, alt_m, speed_ms} to the drone."""
        if not _MAVSDK_AVAILABLE or not self._drone:
            await self._broadcast_event(
                "info", f"[stub] Mission uploaded: {len(waypoints)} waypoints", "mavlink"
            )
            return {"ok": True, "waypoints": len(waypoints), "stub": True}

        try:
            items = []
            for i, wp in enumerate(waypoints):
                items.append(
                    MissionItem(
                        latitude_deg=wp["lat"],
                        longitude_deg=wp["lon"],
                        relative_altitude_m=wp.get("alt_m", 20.0),
                        speed_m_s=wp.get("speed_ms", 5.0),
                        is_fly_through=True,
                        gimbal_pitch_deg=float("nan"),
                        gimbal_yaw_deg=float("nan"),
                        camera_action=MissionItem.CameraAction.NONE,
                        loiter_time_s=wp.get("hover_s", 0.0),
                        camera_photo_interval_time_s=float("nan"),
                        acceptance_radius_m=1.0,
                        yaw_deg=float("nan"),
                        camera_photo_distance_m=float("nan"),
                        vehicle_action=MissionItem.VehicleAction.NONE,
                    )
                )

            plan = MissionPlan(items)
            await self._drone.mission.upload_mission(plan)
            await self._broadcast_event(
                "info", f"Mission uploaded: {len(items)} waypoints", "mavlink"
            )
            return {"ok": True, "waypoints": len(items)}
        except Exception as exc:
            err = str(exc)
            await self._broadcast_event("error", f"Mission upload failed: {err}", "mavlink")
            return {"ok": False, "error": err}

    async def start_mission(self) -> dict[str, Any]:
        return await self._run_command(
            "start_mission", self._drone.mission.start_mission if _MAVSDK_AVAILABLE else None
        )

    # ── PID parameters ────────────────────────────────────────────────────────

    async def set_param(self, name: str, value: float) -> dict[str, Any]:
        """Set a PX4 parameter by name (e.g. MC_ROLL_P = 6.5)."""
        if not _MAVSDK_AVAILABLE or not self._drone:
            await self._broadcast_event("info", f"[stub] SET {name}={value}", "mavlink")
            return {"ok": True, "param": name, "value": value, "stub": True}
        try:
            await self._drone.param.set_param_float(name, float(value))
            await self._broadcast_event("info", f"Param {name}={value}", "mavlink")
            return {"ok": True, "param": name, "value": value}
        except ParamError as exc:
            return {"ok": False, "param": name, "error": str(exc)}

    async def get_param(self, name: str) -> dict[str, Any]:
        """Read a PX4 parameter."""
        if not _MAVSDK_AVAILABLE or not self._drone:
            return {"ok": True, "param": name, "value": 0.0, "stub": True}
        try:
            value = await self._drone.param.get_param_float(name)
            return {"ok": True, "param": name, "value": value}
        except ParamError as exc:
            return {"ok": False, "param": name, "error": str(exc)}

    async def get_pid_params(self) -> dict[str, Any]:
        """Fetch all major PID parameters at once."""
        pid_names = [
            # Roll
            "MC_ROLL_P",
            "MC_ROLLRATE_P",
            "MC_ROLLRATE_I",
            "MC_ROLLRATE_D",
            # Pitch
            "MC_PITCH_P",
            "MC_PITCHRATE_P",
            "MC_PITCHRATE_I",
            "MC_PITCHRATE_D",
            # Yaw
            "MC_YAW_P",
            "MC_YAWRATE_P",
            "MC_YAWRATE_I",
            "MC_YAWRATE_D",
            # Throttle
            "MPC_THR_HOVER",
        ]
        results: dict[str, float] = {}
        for name in pid_names:
            res = await self.get_param(name)
            if res["ok"]:
                results[name] = res["value"]
        return {"ok": True, "params": results}

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _broadcast_event(self, severity: str, message: str, source: str = "mavlink") -> None:
        await broadcast_to_project(
            self.project_id,
            "gcs:event",
            {
                "severity": severity,
                "message": message,
                "source": source,
            },
        )

    async def _emit_stub_telemetry(self) -> None:
        """Emit synthetic telemetry when MAVSDK is not available (dev mode).

        Every sample carries ``synthetic: True`` so the ground station can say it
        is a made-up orbit; it used to look exactly like a live vehicle.
        """
        import time

        t = time.time()
        # Circular orbit around home
        radius, alt = 50.0, 20.0
        lat = 24.7136 + (radius / 111111) * math.sin(t * 0.1)
        lon = 46.6753 + (radius / 111111) * math.cos(t * 0.1)

        await broadcast_to_project(
            self.project_id,
            "mavlink_telemetry",
            {
                "type": "full",
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "alt_m": round(alt + 2 * math.sin(t * 0.3), 2),
                "north_m": round(radius * math.sin(t * 0.1), 2),
                "east_m": round(radius * math.cos(t * 0.1), 2),
                "roll_deg": round(5 * math.sin(t * 0.5), 2),
                "pitch_deg": round(3 * math.cos(t * 0.4), 2),
                "yaw_deg": round(
                    (math.degrees(math.atan2(math.cos(t * 0.1), -math.sin(t * 0.1))) + 360) % 360, 1
                ),
                "speed_ms": round(5.0 + math.sin(t * 0.2), 2),
                "battery_pct": max(20.0, 100.0 - (t % 600) / 6.0),
                "voltage_v": round(16.8 - (t % 600) / 600 * 3.0, 2),
                "satellites": 14,
                "hdop": 0.8,
                "fix_type": "FIX_3D",
                "flight_mode": "AUTO_MISSION",
                "armed": True,
                "synthetic": True,
            },
        )


# ── Bridge registry ───────────────────────────────────────────────────────────

_bridges: dict[str, MAVLinkBridge] = {}


def get_bridge(project_id: str, system_addr: str = "udp://:14540") -> MAVLinkBridge:
    """Get or create a MAVLinkBridge for a project."""
    if project_id not in _bridges:
        _bridges[project_id] = MAVLinkBridge(project_id, system_addr)
    return _bridges[project_id]


async def remove_bridge(project_id: str) -> None:
    """Stop and remove the bridge for a project."""
    if project_id in _bridges:
        await _bridges[project_id].stop()
        del _bridges[project_id]
