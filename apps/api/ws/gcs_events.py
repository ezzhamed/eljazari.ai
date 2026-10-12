"""eljazari.ai — Socket.io handlers for the WebGCS.

`ws/mavlink_bridge.py` has always implemented the ground-station behaviour —
telemetry streaming, flight commands, mission upload, PID parameters — but
nothing imported it, so no handler was ever registered for the `gcs:*` events
the frontend sends. Socket.io drops unknown events silently, so every button on
`/dashboard/gcs` appeared to work and did nothing. This module is the missing
wiring.

Event contract (mirrors apps/web/components/gcs/):

    client -> server
        gcs:start           {}                        begin telemetry streaming
        gcs:stop            {}                        stop it and drop the bridge
        gcs:command         {command}                 arm|takeoff|land|rtl|pause|estop
        gcs:mission_upload  {waypoints:[{lat,lon,alt_m,hover_s}]}
        gcs:set_param       {name, value}
        gcs:get_params      {}

    server -> client (broadcast to the project room)
        mavlink_telemetry   {type, ...}               position/attitude/battery/gps/full
        gcs:event           {severity, message, source}
        gcs:command         {command}                 echo, so every tab logs it
        gcs:params_update   {params: {NAME: value}}

None of these carry a project_id. The socket has already joined a project room,
so the target is resolved from the session and re-authorised on every call —
see `ws.events.current_project`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.settings import settings
from ws.events import current_project
from ws.mavlink_bridge import MAVLinkBridge, get_bridge, remove_bridge
from ws.socket_manager import broadcast_to_project, sio

logger = logging.getLogger(__name__)

# Commands the frontend's MissionControls panel can send, mapped to bridge
# methods. Anything outside this table is rejected rather than dispatched
# dynamically — these arm motors and cut throttle on real hardware.
_COMMANDS: dict[str, str] = {
    "arm": "arm",
    "disarm": "disarm",
    "takeoff": "takeoff",
    "land": "land",
    "rtl": "return_to_launch",
    "pause": "pause_mission",
    "estop": "emergency_stop",
    "start_mission": "start_mission",
}

# One telemetry task per project, not per socket: several tabs can watch the
# same drone, and each would otherwise start its own MAVLink stream.
_telemetry_tasks: dict[str, asyncio.Task] = {}


def _error(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(exc)}


async def _resolve(sid: str) -> str:
    return await current_project(sid)


async def _stop_telemetry(project_id: str) -> None:
    task = _telemetry_tasks.pop(project_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        # CancelledError derives from BaseException, so Exception alone misses it.
        except (asyncio.CancelledError, Exception):
            pass
    await remove_bridge(project_id)


@sio.on("gcs:start")
async def on_gcs_start(sid: str, data: dict | None = None) -> dict:
    """Connect the MAVLink bridge and start streaming telemetry to the room."""
    try:
        project_id = await _resolve(sid)
    except Exception as exc:
        return _error(exc)

    if project_id in _telemetry_tasks and not _telemetry_tasks[project_id].done():
        return {"ok": True, "status": "already_running", "project_id": project_id}

    address = (data or {}).get("address") or settings.gcs_mavlink_address
    bridge: MAVLinkBridge = get_bridge(project_id, address)

    connected = await bridge.connect(timeout_s=settings.gcs_connect_timeout_s)
    if not connected:
        # connect() has already broadcast a gcs:event describing the failure.
        return {"ok": False, "status": "connect_failed", "address": address}

    _telemetry_tasks[project_id] = asyncio.create_task(bridge.start_telemetry_loop())
    logger.info("GCS telemetry started project=%s address=%s", project_id, address)
    return {"ok": True, "status": "started", "address": address}


@sio.on("gcs:stop")
async def on_gcs_stop(sid: str, data: dict | None = None) -> dict:
    try:
        project_id = await _resolve(sid)
    except Exception as exc:
        return _error(exc)
    await _stop_telemetry(project_id)
    logger.info("GCS telemetry stopped project=%s", project_id)
    return {"ok": True, "status": "stopped"}


@sio.on("gcs:command")
async def on_gcs_command(sid: str, data: dict) -> dict:
    """Dispatch a flight command. Rejects anything not in the allow-list."""
    command = str((data or {}).get("command", "")).lower().strip()
    method = _COMMANDS.get(command)
    if not method:
        return {"ok": False, "error": f"unknown command: {command or '(empty)'}"}

    try:
        project_id = await _resolve(sid)
    except Exception as exc:
        return _error(exc)

    bridge = get_bridge(project_id, settings.gcs_mavlink_address)
    try:
        if command == "takeoff":
            altitude = float((data or {}).get("altitude_m", settings.gcs_takeoff_altitude_m))
            result = await bridge.takeoff(altitude)
        else:
            result = await getattr(bridge, method)()
    except Exception as exc:
        logger.exception("GCS command failed project=%s command=%s", project_id, command)
        return _error(exc)

    # Echo to the whole room so every open tab's EventLog records the command,
    # not just the one that sent it.
    await broadcast_to_project(project_id, "gcs:command", {"command": command})
    return result


@sio.on("gcs:mission_upload")
async def on_gcs_mission_upload(sid: str, data: dict) -> dict:
    waypoints = (data or {}).get("waypoints") or []
    if not isinstance(waypoints, list) or not waypoints:
        return {"ok": False, "error": "waypoints required"}

    try:
        project_id = await _resolve(sid)
    except Exception as exc:
        return _error(exc)

    bridge = get_bridge(project_id, settings.gcs_mavlink_address)
    try:
        return await bridge.upload_mission(waypoints)
    except Exception as exc:
        logger.exception("GCS mission upload failed project=%s", project_id)
        return _error(exc)


@sio.on("gcs:set_param")
async def on_gcs_set_param(sid: str, data: dict) -> dict:
    name = str((data or {}).get("name", "")).strip()
    if not name:
        return {"ok": False, "error": "param name required"}
    try:
        value = float((data or {}).get("value"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "param value must be numeric"}

    try:
        project_id = await _resolve(sid)
    except Exception as exc:
        return _error(exc)

    bridge = get_bridge(project_id, settings.gcs_mavlink_address)
    try:
        return await bridge.set_param(name, value)
    except Exception as exc:
        logger.exception("GCS set_param failed project=%s param=%s", project_id, name)
        return _error(exc)


@sio.on("gcs:get_params")
async def on_gcs_get_params(sid: str, data: dict | None = None) -> dict:
    """Read the PID parameter block and push it to the room."""
    try:
        project_id = await _resolve(sid)
    except Exception as exc:
        return _error(exc)

    bridge = get_bridge(project_id, settings.gcs_mavlink_address)
    try:
        result = await bridge.get_pid_params()
    except Exception as exc:
        logger.exception("GCS get_params failed project=%s", project_id)
        return _error(exc)

    await broadcast_to_project(
        project_id, "gcs:params_update", {"params": result.get("params", {})}
    )
    return result
