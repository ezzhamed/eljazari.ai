"""Run a generated mission and collect telemetry from inside WSL."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import json
import logging
import math
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

try:
    from . import mission_sandbox
    from .mavsdk_runtime_adapter import MAVSDKRuntimeAdapter
except ImportError:
    import mission_sandbox
    from mavsdk_runtime_adapter import MAVSDKRuntimeAdapter

# 10 Hz. The viewer follows the aircraft live, and 2 Hz reads as teleporting
# between positions rather than flying. PX4 can stream faster still, but every
# sample crosses a WSL pipe and a WebSocket, and 10 Hz is where the motion stops
# looking sampled without paying for rates nobody can see.
_TELEMETRY_HZ = 10.0
_TELEMETRY_INTERVAL = 1.0 / _TELEMETRY_HZ
_TELEMETRY_SNAPSHOT_INTERVAL_S = 5.0
#: Marks a live telemetry sample on stdout, so the reader never has to
#: guess whether a line is JSON or a log message.
_LIVE_POINT_PREFIX = "@@PT "
_MISSION_SETTLE_S = 8.0
# A sample is recorded only while the position stream is live. Repeating the last
# values every 0.1 s once showed a vehicle holding at 9.95 m for five minutes
# after its MAVLink link had been cut.
_STALE_POSITION_S = 1.0
# No position update for this long means the link is lost: the run ends.
_LINK_LOST_S = 15.0
# PX4 caps tilt at MPC_TILTMAX_AIR (45 degrees by default; generated designs keep
# the default, though PX4 accepts up to 89). An airborne vehicle past this has
# lost control: in SITL a quadcopter with one motor stopped rolled past
# 130 degrees and fell from 10 m, while PX4 kept reporting MISSION for 280 s.
_LOSS_OF_CONTROL_TILT_DEG = 70.0
# PX4 limits commanded descent to MPC_Z_VEL_MAX_DN (1.5 m/s by default, 4 m/s at
# most), and 60 recorded SITL flights never exceeded 1.55 m/s. A hexacopter with one motor
# stopped stayed within 19 degrees of level and hit the ground at 8.1 m/s.
_LOSS_OF_CONTROL_DESCENT_M_S = 4.0
# Samples kept after a loss of control, to record the fall, before the run ends.
_AFTER_LOSS_OF_CONTROL_S = 5.0
_API_ROOT = Path(__file__).resolve().parents[1]
# "wsl" when launched from a Windows API through wsl.exe, "native" when the
# API itself runs on Linux. Only the provenance label depends on it.
_RUNTIME_MODE = "wsl"
# How the generated mission ran: in the namespace sandbox, or (development
# override only) in this process, where it could also reach the telemetry.
_MISSION_ISOLATION = "none"
# The sandboxed mission's last ERROR log line, reported with its exit code: why
# it failed, in its own words (PX4's «Geofence violation», a failsafe mode).
# Untrusted text; it only explains, it decides nothing.
_last_mission_error = ""
# This runner is also copied alone to /opt/eljazari/utils in the sim image.
_REPO_ROOT = (
    _API_ROOT.parent.parent
    if _API_ROOT.name == "api" and _API_ROOT.parent.name == "apps"
    else _API_ROOT
)


async def _close_async_iterator(iterator: Any) -> None:
    close_fn = getattr(iterator, "aclose", None)
    if not callable(close_fn):
        return
    try:
        result = close_fn()
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass


async def _close_mavsdk_system(system: Any) -> None:
    close_fn = getattr(system, "close", None)
    if not callable(close_fn):
        return
    try:
        result = close_fn()
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass


async def _pump_stream(
    stream_factory: Any,
    latest: dict[str, Any],
    key: str,
    warnings: list[str],
    received: dict[str, float] | None = None,
) -> None:
    iterator = stream_factory().__aiter__()
    try:
        while True:
            latest[key] = await iterator.__anext__()
            if received is not None:
                received[key] = time.monotonic()
    except asyncio.CancelledError:
        raise
    except StopAsyncIteration:
        return
    except Exception as exc:
        warnings.append(f"{key} stream failed: {type(exc).__name__}: {exc}")
    finally:
        await _close_async_iterator(iterator)


def _extract_local_position(position_velocity_ned: Any) -> tuple[float, float]:
    position = getattr(position_velocity_ned, "position", None)
    if position is None:
        return 0.0, 0.0
    return (
        float(getattr(position, "north_m", 0.0)),
        float(getattr(position, "east_m", 0.0)),
    )


def _ned_from_gps(
    home_lat_deg: float,
    home_lon_deg: float,
    lat_deg: float,
    lon_deg: float,
) -> tuple[float, float]:
    north_m = (lat_deg - home_lat_deg) * 111_111.0
    avg_lat_rad = math.radians((home_lat_deg + lat_deg) / 2.0)
    east_m = (lon_deg - home_lon_deg) * 111_111.0 * math.cos(avg_lat_rad)
    return north_m, east_m


def _extract_odometry_position(odometry: Any) -> tuple[float, float, float]:
    position = getattr(odometry, "position_body", None)
    if position is None:
        return 0.0, 0.0, 0.0
    return (
        float(getattr(position, "x_m", 0.0)),
        float(getattr(position, "y_m", 0.0)),
        -float(getattr(position, "z_m", 0.0)),
    )


def _resolve_horizontal_position(
    north_m: float,
    east_m: float,
    odometry: Any,
) -> tuple[float, float]:
    odom_north_m, odom_east_m, _ = _extract_odometry_position(odometry)
    primary_span_m = math.hypot(north_m, east_m)
    odom_span_m = math.hypot(odom_north_m, odom_east_m)
    if odom_span_m > max(0.1, primary_span_m + 0.25):
        return odom_north_m, odom_east_m
    return north_m, east_m


def _extract_monotonic_altitude(altitude: Any) -> float | None:
    for attr in ("altitude_monotonic_m", "monotonic_altitude_m"):
        if hasattr(altitude, attr):
            return float(getattr(altitude, attr))
    return None


_FLAT_ALTITUDE_M = 0.5


def _resolve_altitude_m(
    rel_alt_m: float,
    position_velocity_ned: Any,
    altitude: Any,
    odometry: Any,
    *,
    in_air: bool | None,
) -> float:
    """Height above home, as PX4's estimator reports it to the mission.

    The other streams only stand in for a position stream that stays flat; they
    are never combined with a valid one. `altitude_monotonic_m` is the raw
    barometric altitude (PX4's simulated barometer has about 0.25 m of noise), so
    taking the highest of all sources turned noise spikes into altitude peaks.
    """
    alt_m = max(0.0, float(rel_alt_m))
    if alt_m < _FLAT_ALTITUDE_M:
        position = getattr(position_velocity_ned, "position", None)
        local_alt_m = -float(getattr(position, "down_m", 0.0)) if position is not None else 0.0
        _odom_north_m, _odom_east_m, odom_alt_m = _extract_odometry_position(odometry)
        alt_m = max(alt_m, local_alt_m, odom_alt_m)
        monotonic_alt_m = _extract_monotonic_altitude(altitude)
        if alt_m < _FLAT_ALTITUDE_M and monotonic_alt_m is not None:
            alt_m = max(alt_m, monotonic_alt_m)

    if in_air is False and alt_m < 1.0:
        return 0.0
    return alt_m


def _battery_percent(remaining_percent: float) -> float:
    value = float(remaining_percent)
    return value if value > 1.0 else value * 100.0


def _infer_telemetry_source(points: list[dict[str, Any]]) -> str:
    if not points:
        return f"real_{_RUNTIME_MODE}_sitl_empty"

    peak_alt_m = max(max(0.0, float(point.get("alt_m", 0.0) or 0.0)) for point in points)
    north_values = [float(point.get("north_m", 0.0) or 0.0) for point in points]
    east_values = [float(point.get("east_m", 0.0) or 0.0) for point in points]
    horizontal_span_m = math.hypot(
        max(north_values) - min(north_values),
        max(east_values) - min(east_values),
    )
    has_nonzero_gps = any(
        abs(float(point.get("lat", 0.0) or 0.0)) > 1e-6
        or abs(float(point.get("lon", 0.0) or 0.0)) > 1e-6
        for point in points
    )

    if peak_alt_m >= 0.8 or horizontal_span_m >= 2.0 or has_nonzero_gps:
        return f"real_{_RUNTIME_MODE}_sitl"
    return f"real_{_RUNTIME_MODE}_sitl_partial"


def _loss_of_control(
    points: list[dict[str, Any]], *, airborne: bool = False
) -> dict[str, float] | None:
    """The first sample where a vehicle that has flown tilted or fell beyond control."""
    for point in points:
        airborne = airborne or point["alt_m"] >= 1.0
        # Absent from some records; a missing value is no evidence of either.
        tilt = max(abs(point.get("roll_deg") or 0.0), abs(point.get("pitch_deg") or 0.0))
        descent = point.get("vel_down_ms") or 0.0
        if airborne and (
            tilt > _LOSS_OF_CONTROL_TILT_DEG or descent > _LOSS_OF_CONTROL_DESCENT_M_S
        ):
            return {
                "at_s": point["timestamp_s"],
                "tilt_deg": round(tilt, 1),
                "descent_m_s": round(descent, 2),
                "alt_m": round(point["alt_m"], 2),
            }
    return None


def _describe_loss_of_control(event: dict[str, float]) -> str:
    return (
        f"Loss of control at {event['at_s']:.1f} s: tilted {event['tilt_deg']:.0f} degrees, "
        f"descending {event['descent_m_s']:.1f} m/s at {event['alt_m']:.1f} m"
    )


def _build_telemetry_payload(
    *,
    sim_id: str,
    points: list[dict[str, Any]],
    elapsed_s: float,
    mission_done: bool,
    warnings: list[str],
    home_lat: float | None,
    home_lon: float | None,
    home_alt_m: float | None,
) -> dict[str, Any]:
    completed = mission_done and len(points) > 3 and points[-1]["alt_m"] < 1.0
    crashed = (
        any(point["alt_m"] < -0.5 for point in points[5:]) or _loss_of_control(points) is not None
    )
    telemetry_source = _infer_telemetry_source(points)
    warning_items = [str(item) for item in warnings if str(item).strip()]
    if telemetry_source.endswith("_partial") and not any(
        "incomplete" in item for item in warning_items
    ):
        warning_items.append(
            "Telemetry captured a real run but movement/altitude evidence is incomplete."
        )
    if telemetry_source.endswith("_empty") and not any(
        "No telemetry points" in item for item in warning_items
    ):
        warning_items.append(
            f"No telemetry points were captured from the real {_RUNTIME_MODE} SITL run."
        )
    return {
        "sim_id": sim_id,
        "duration_s": round(elapsed_s, 2),
        "mission_started": len(points) > 3,
        "mission_completed": completed,
        "crash_detected": crashed,
        "home_lat": home_lat or 0.0,
        "home_lon": home_lon or 0.0,
        "home_alt_m": home_alt_m or 0.0,
        "runtime_mode": _RUNTIME_MODE,
        "mission_isolation": _MISSION_ISOLATION,
        "telemetry_source": telemetry_source,
        "warnings": warning_items,
        "points": points,
    }


def _write_telemetry_snapshot(output_path: Path, payload: dict[str, Any]) -> None:
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(output_path)


def _ensure_mission_import_paths(mission_path: Path) -> None:
    """Let /tmp mission files import eljazari.ai helper modules inside WSL."""
    candidates = [mission_path.parent, _API_ROOT, _REPO_ROOT]
    for path in candidates:
        text = str(path)
        if text and text not in sys.path:
            sys.path.insert(0, text)
    return "real_wsl_sitl_partial"


def _normalize_mavsdk_address(address: str) -> str:
    text = (address or "").strip()
    if text.startswith("udp://:"):
        return f"udpin://0.0.0.0:{text.rsplit(':', 1)[-1]}"
    if text.startswith("udp://0.0.0.0:"):
        return "udpin://" + text[len("udp://") :]
    return text


def _candidate_mavsdk_addresses(preferred_address: str, port: int) -> list[str]:
    candidates = [
        _normalize_mavsdk_address(preferred_address),
        f"udpout://127.0.0.1:{port + 30}",
        f"udpin://0.0.0.0:{port}",
        f"udpout://127.0.0.1:{port + 4020}",
        "udpin://0.0.0.0:14540",
    ]
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        unique.append(candidate)
        seen.add(candidate)
    return unique


async def _wait_until_connected(system: Any, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    iterator = system.core.connection_state().__aiter__()
    try:
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                state = await asyncio.wait_for(iterator.__anext__(), timeout=min(2.0, remaining))
            except asyncio.TimeoutError:
                continue
            if getattr(state, "is_connected", False):
                return
    finally:
        await _close_async_iterator(iterator)
    raise TimeoutError("Timed out waiting for MAVSDK connection state")


async def _configure_telemetry_rates(system: Any) -> None:
    rate_calls = [
        ("set_rate_position", 10.0),
        ("set_rate_position_velocity_ned", 10.0),
        ("set_rate_altitude", 10.0),
        ("set_rate_odometry", 10.0),
        ("set_rate_velocity_ned", 10.0),
        ("set_rate_attitude_euler", 10.0),
        ("set_rate_battery", 2.0),
        ("set_rate_flight_mode", 4.0),
        ("set_rate_in_air", 4.0),
        ("set_rate_gps_info", 2.0),
    ]
    for method_name, rate_hz in rate_calls:
        method = getattr(system.telemetry, method_name, None)
        if not callable(method):
            continue
        try:
            await method(rate_hz)
        except Exception:
            pass


def _mavsdk_server_binary() -> str:
    import mavsdk

    return str((Path(mavsdk.__file__).resolve().parent / "bin" / "mavsdk_server"))


async def _terminate_subprocess(proc: asyncio.subprocess.Process | None) -> tuple[str, str]:
    if proc is None:
        return "", ""
    if proc.returncode is None:
        proc.kill()
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except Exception:
        return "", ""
    return (
        (stdout or b"").decode("utf-8", errors="replace"),
        (stderr or b"").decode("utf-8", errors="replace"),
    )


def _grpc_port(port: int) -> int:
    return 50051 + max(0, port - 14550)


async def _run_sandboxed_mission(
    mission_path: Path, expected_sha256: str, grpc_port: int, timeout_s: float
) -> int:
    """Run the mission in the namespace sandbox; return its exit code.

    The sandbox reaches mavsdk_server only through a relayed Unix socket. Its
    output is logged to stderr with a prefix, never to stdout, so it cannot
    inject live telemetry lines.
    """
    import shutil
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="droneprompt-sandbox-"))
    relay = None
    proc = None
    try:
        layout = mission_sandbox.prepare(workdir, mission_path, expected_sha256)
        relay = await mission_sandbox.serve_grpc_relay(
            layout.socket_path(grpc_port), "127.0.0.1", grpc_port
        )
        argv = mission_sandbox.write_launch(layout, grpc_port=grpc_port, timeout_s=timeout_s)
        proc = await mission_sandbox.start(argv)

        async def forward() -> None:
            global _last_mission_error
            assert proc.stdout is not None
            while line := await proc.stdout.readline():
                text = line.decode("utf-8", "replace").rstrip()
                print("[mission] " + text, file=sys.stderr)
                if " ERROR " in text:
                    _last_mission_error = text.split(" ERROR ", 1)[1][:200]

        await asyncio.wait_for(asyncio.gather(forward(), proc.wait()), timeout=timeout_s + 30)
        if proc.returncode == 3:
            raise RuntimeError("Sandbox refused the mission: fingerprint mismatch")
        return proc.returncode
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        if relay is not None:
            relay.close()
        shutil.rmtree(workdir, ignore_errors=True)


async def _connect_system(
    preferred_address: str,
    port: int,
    *,
    timeout_s: float = 80.0,
) -> tuple[Any, str, list[str], asyncio.subprocess.Process]:
    from mavsdk import System

    warnings: list[str] = []
    addresses = _candidate_mavsdk_addresses(preferred_address, port)
    per_attempt_timeout = max(15.0, timeout_s / max(len(addresses), 1))
    last_error: Exception | None = None
    grpc_port = _grpc_port(port)
    server_bin = _mavsdk_server_binary()

    for address in addresses:
        server_proc: asyncio.subprocess.Process | None = None
        system = System(mavsdk_server_address="127.0.0.1", port=grpc_port)
        try:
            server_proc = await asyncio.create_subprocess_exec(
                server_bin,
                "-p",
                str(grpc_port),
                address,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(1.0)
            if server_proc.returncode not in (None, 0):
                stdout_text, stderr_text = await _terminate_subprocess(server_proc)
                raise RuntimeError(
                    f"mavsdk_server exited early for {address}: "
                    f"{stderr_text.strip() or stdout_text.strip() or 'no output'}"
                )
            await asyncio.wait_for(
                system.connect(),
                timeout=per_attempt_timeout,
            )
            await _wait_until_connected(system, timeout_s=per_attempt_timeout)
            return system, address, warnings, server_proc
        except Exception as exc:
            last_error = exc
            warnings.append(
                f"MAVSDK connect attempt failed for {address}: {type(exc).__name__}: {exc}"
            )
            await _close_mavsdk_system(system)
            stdout_text, stderr_text = await _terminate_subprocess(server_proc)
            if stderr_text.strip() or stdout_text.strip():
                warnings.append(
                    f"mavsdk_server output for {address}: "
                    f"{stderr_text.strip() or stdout_text.strip()}"
                )

    if last_error is None:
        raise TimeoutError("No MAVSDK addresses were available to try")
    attempts_summary = " | ".join(warnings[-4:])
    raise TimeoutError(
        "Could not connect to MAVSDK using any known address: "
        f"{', '.join(addresses)}. Attempts: {attempts_summary}"
    ) from last_error


async def _collect_telemetry(
    system: Any,
    sim_id: str,
    duration_s: float,
    mission_done: asyncio.Event,
    output_path: Path | None = None,
) -> dict[str, Any]:
    points: list[dict[str, Any]] = []
    warnings: list[str] = []
    latest: dict[str, Any] = {}
    received: dict[str, float] = {}
    link_lost_after_s: float | None = None
    loss_of_control: dict[str, float] | None = None
    start = time.monotonic()
    deadline = start + duration_s
    next_snapshot_at = start
    stop_deadline: float | None = None
    peak_alt_m = 0.0
    home_lat: float | None = None
    home_lon: float | None = None
    home_alt_m: float | None = None
    stream_tasks = [
        asyncio.create_task(
            _pump_stream(system.telemetry.position, latest, "position", warnings, received)
        ),
        asyncio.create_task(
            _pump_stream(
                system.telemetry.position_velocity_ned, latest, "position_velocity_ned", warnings
            )
        ),
        asyncio.create_task(_pump_stream(system.telemetry.altitude, latest, "altitude", warnings)),
        asyncio.create_task(_pump_stream(system.telemetry.odometry, latest, "odometry", warnings)),
        asyncio.create_task(
            _pump_stream(system.telemetry.velocity_ned, latest, "velocity_ned", warnings)
        ),
        asyncio.create_task(
            _pump_stream(system.telemetry.attitude_euler, latest, "attitude_euler", warnings)
        ),
        asyncio.create_task(_pump_stream(system.telemetry.battery, latest, "battery", warnings)),
        asyncio.create_task(_pump_stream(system.telemetry.gps_info, latest, "gps_info", warnings)),
        asyncio.create_task(
            _pump_stream(system.telemetry.flight_mode, latest, "flight_mode", warnings)
        ),
        asyncio.create_task(_pump_stream(system.telemetry.in_air, latest, "in_air", warnings)),
    ]

    try:
        while time.monotonic() < deadline:
            timestamp_s = time.monotonic() - start
            pos = latest.get("position")
            local = latest.get("position_velocity_ned")
            altitude = latest.get("altitude")
            odometry = latest.get("odometry")
            vel = latest.get("velocity_ned")
            att = latest.get("attitude_euler")
            bat = latest.get("battery")
            gps = latest.get("gps_info")
            mode = latest.get("flight_mode")
            in_air = latest.get("in_air")

            if any(
                item is None
                for item in (pos, local, altitude, odometry, vel, att, bat, gps, mode, in_air)
            ):
                now = time.monotonic()
                if output_path is not None and now >= next_snapshot_at:
                    _write_telemetry_snapshot(
                        output_path,
                        _build_telemetry_payload(
                            sim_id=sim_id,
                            points=points,
                            elapsed_s=now - start,
                            mission_done=mission_done.is_set(),
                            warnings=warnings,
                            home_lat=home_lat,
                            home_lon=home_lon,
                            home_alt_m=home_alt_m,
                        ),
                    )
                    next_snapshot_at = now + _TELEMETRY_SNAPSHOT_INTERVAL_S
                await asyncio.sleep(0.1)
                continue

            position_age_s = time.monotonic() - received.get("position", start)
            if position_age_s > _STALE_POSITION_S:
                if position_age_s > _LINK_LOST_S:
                    link_lost_after_s = round(timestamp_s - position_age_s, 1)
                    warnings.append(
                        f"Telemetry link lost: no position update for {position_age_s:.0f} s "
                        f"after {link_lost_after_s:.1f} s"
                    )
                    break
                await asyncio.sleep(_TELEMETRY_INTERVAL)
                continue

            north_m, east_m = _extract_local_position(local)
            north_m, east_m = _resolve_horizontal_position(north_m, east_m, odometry)
            lat_deg = float(getattr(pos, "latitude_deg", 0.0))
            lon_deg = float(getattr(pos, "longitude_deg", 0.0))
            rel_alt_m = float(getattr(pos, "relative_altitude_m", 0.0))
            alt_m = _resolve_altitude_m(
                rel_alt_m,
                local,
                altitude,
                odometry,
                in_air=bool(in_air),
            )
            abs_alt_m = float(getattr(pos, "absolute_altitude_m", 0.0))
            if home_lat is None and (abs(lat_deg) > 1e-6 or abs(lon_deg) > 1e-6):
                home_lat = lat_deg
                home_lon = lon_deg
                home_alt_m = abs_alt_m - rel_alt_m
            if (
                abs(north_m) < 1e-3
                and abs(east_m) < 1e-3
                and home_lat is not None
                and home_lon is not None
            ):
                north_m, east_m = _ned_from_gps(home_lat, home_lon, lat_deg, lon_deg)
            speed = math.sqrt(vel.north_m_s**2 + vel.east_m_s**2)
            points.append(
                {
                    "timestamp_s": round(timestamp_s, 2),
                    "north_m": north_m,
                    "east_m": east_m,
                    "lat": lat_deg,
                    "lon": lon_deg,
                    "alt_m": alt_m,
                    "vel_north_ms": vel.north_m_s,
                    "vel_east_ms": vel.east_m_s,
                    "vel_down_ms": vel.down_m_s,
                    "speed_ms": round(speed, 2),
                    "roll_deg": att.roll_deg,
                    "pitch_deg": att.pitch_deg,
                    "yaw_deg": att.yaw_deg,
                    "battery_percent": _battery_percent(getattr(bat, "remaining_percent", 0.0)),
                    "battery_voltage": bat.voltage_v,
                    "flight_mode": str(mode),
                    "armed": True,
                    "gps_satellites": gps.num_satellites,
                }
            )
            peak_alt_m = max(peak_alt_m, max(0.0, alt_m))
            _emit_live_point(points[-1])
            now = time.monotonic()
            if output_path is not None and now >= next_snapshot_at:
                _write_telemetry_snapshot(
                    output_path,
                    _build_telemetry_payload(
                        sim_id=sim_id,
                        points=points,
                        elapsed_s=now - start,
                        mission_done=mission_done.is_set(),
                        warnings=warnings,
                        home_lat=home_lat,
                        home_lon=home_lon,
                        home_alt_m=home_alt_m,
                    ),
                )
                next_snapshot_at = now + _TELEMETRY_SNAPSHOT_INTERVAL_S
            if loss_of_control is None:
                loss_of_control = _loss_of_control(points[-1:], airborne=peak_alt_m >= 1.0)
                if loss_of_control is not None:
                    warnings.append(_describe_loss_of_control(loss_of_control))
            elif timestamp_s - loss_of_control["at_s"] >= _AFTER_LOSS_OF_CONTROL_S:
                break
            if mission_done.is_set():
                stop_deadline = stop_deadline or (time.monotonic() + _MISSION_SETTLE_S)
                if peak_alt_m >= 0.5 and alt_m <= 0.5 and len(points) >= 6:
                    break
                if stop_deadline is not None and time.monotonic() >= stop_deadline:
                    break
            await asyncio.sleep(_TELEMETRY_INTERVAL)
    finally:
        for task in stream_tasks:
            task.cancel()
        await asyncio.gather(*stream_tasks, return_exceptions=True)

    elapsed = time.monotonic() - start
    payload = _build_telemetry_payload(
        sim_id=sim_id,
        points=points,
        elapsed_s=elapsed,
        mission_done=mission_done.is_set(),
        warnings=warnings,
        home_lat=home_lat,
        home_lon=home_lon,
        home_alt_m=home_alt_m,
    )
    payload["telemetry_link_lost_after_s"] = link_lost_after_s
    payload["loss_of_control"] = loss_of_control
    if output_path is not None:
        _write_telemetry_snapshot(output_path, payload)
    return payload


def _load_mission_module(mission_path: Path) -> Any:
    _ensure_mission_import_paths(mission_path)
    module_name = f"droneprompt_mission_{mission_path.stem}_{abs(hash(str(mission_path)))}"
    spec = importlib.util.spec_from_file_location(module_name, mission_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load mission module from {mission_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _invoke_callable(func: Any, *args: Any) -> int:
    result = func(*args)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        return 0
    if isinstance(result, bool):
        return 0 if result else 1
    if isinstance(result, int):
        return result
    return 0 if bool(result) else 1


def _wire_module_safety_helpers(module: Any) -> None:
    if not hasattr(module, "return_to_launch"):

        async def _return_to_launch(drone: Any) -> Any:
            return await drone.return_to_launch()

        module.return_to_launch = _return_to_launch

    if not hasattr(module, "emergency_stop"):

        async def _emergency_stop(drone: Any) -> Any:
            return await drone.emergency_stop()

        module.emergency_stop = _emergency_stop


async def _run_mission(mission_path: Path, system: Any, timeout_s: float) -> int:
    async def invoke() -> int:
        module = _load_mission_module(mission_path)
        _wire_module_safety_helpers(module)
        run_mission_fn = getattr(module, "run_mission", None)
        if callable(run_mission_fn):
            adapter = MAVSDKRuntimeAdapter(system)
            return await _invoke_callable(run_mission_fn, adapter)

        main_fn = getattr(module, "main", None)
        if callable(main_fn):
            return await _invoke_callable(main_fn)

        raise RuntimeError(
            "Mission module does not expose an async main() or run_mission() entrypoint"
        )

    return await asyncio.wait_for(invoke(), timeout=timeout_s)


async def _main(args: argparse.Namespace) -> None:
    global _MISSION_ISOLATION
    if args.isolation == "sandbox":
        reason = mission_sandbox.unsupported_reason()
        if reason:
            raise RuntimeError(
                f"Mission sandbox unavailable; refusing to run generated code. {reason}"
            )
        if not args.mission_sha256:
            raise RuntimeError("A mission fingerprint is required to run in the sandbox")
        _MISSION_ISOLATION = mission_sandbox.ISOLATION_LABEL
    system, connected_address, connect_warnings, server_proc = await _connect_system(
        args.mission_address,
        args.port,
    )
    os.environ["DRONEPROMPT_MAVSDK_ADDRESS"] = connected_address
    await _configure_telemetry_rates(system)

    mission_done = asyncio.Event()

    async def run_and_signal() -> int:
        try:
            if args.isolation == "sandbox":
                return await _run_sandboxed_mission(
                    Path(args.mission), args.mission_sha256, _grpc_port(args.port), args.timeout
                )
            return await _run_mission(Path(args.mission), system, args.timeout)
        finally:
            mission_done.set()

    mission_task = asyncio.create_task(run_and_signal())
    telemetry_task = asyncio.create_task(
        _collect_telemetry(
            system,
            args.sim_id,
            args.timeout,
            mission_done,
            output_path=Path(args.output),
        )
    )

    def stop_mission_when_collection_ends_early(task: asyncio.Task) -> None:
        # A lost link or a lost vehicle: nothing the mission does matters any
        # more, so do not wait out its timeout.
        if task.cancelled() or task.exception() is not None:
            return
        result = task.result()
        if result.get("telemetry_link_lost_after_s") is not None or result.get("loss_of_control"):
            mission_task.cancel()

    telemetry_task.add_done_callback(stop_mission_when_collection_ends_early)
    results = await asyncio.gather(mission_task, telemetry_task, return_exceptions=True)

    try:
        mission_result = results[0]
        telemetry = results[1]
        if isinstance(telemetry, BaseException):
            raise telemetry

        mission_error: str | None = None
        lost_after = telemetry.get("telemetry_link_lost_after_s")
        lost_control = telemetry.get("loss_of_control")
        if lost_control:
            mission_error = f"{_describe_loss_of_control(lost_control)}; the mission was stopped"
        elif lost_after is not None:
            mission_error = f"Telemetry link lost after {lost_after:.1f} s; the mission was stopped"
        elif isinstance(mission_result, BaseException):
            mission_error = f"{type(mission_result).__name__}: {mission_result}"
        elif mission_result not in (0, None):
            mission_error = f"Mission execution returned exit code {mission_result}"
            if _last_mission_error:
                mission_error += f" ({_last_mission_error})"

        if mission_error:
            if not telemetry.get("points"):
                raise RuntimeError(mission_error)
            telemetry["mission_completed"] = False
            telemetry["mission_error"] = mission_error
            warnings = [str(item) for item in telemetry.get("warnings", []) if str(item).strip()]
            warnings.append(mission_error)
            telemetry["warnings"] = warnings
            print(f"mission_warning: {mission_error}", file=sys.stderr)

        if connect_warnings:
            warnings = [str(item) for item in telemetry.get("warnings", []) if str(item).strip()]
            warnings.extend(connect_warnings)
            telemetry["warnings"] = warnings

        output_path = Path(args.output)
        output_path.write_text(json.dumps(telemetry, ensure_ascii=False), encoding="utf-8")
    finally:
        await _close_mavsdk_system(system)
        await _terminate_subprocess(server_proc)


def _emit_live_point(point: dict) -> None:
    """Publish one telemetry sample to stdout for the live viewer.

    The mission's own JSON payload is still written at the end, and this stream
    is deliberately additive: a dropped or malformed line costs a frame of
    animation, never the flight record. The prefix lets the reader pick these
    out of ordinary log output without parsing everything.
    """
    try:
        line = f"{_LIVE_POINT_PREFIX}{json.dumps(point, ensure_ascii=False)}"
        print(line, flush=True)
    except Exception:  # noqa: BLE001 - telemetry must never break the mission
        pass


def _configure_logging() -> None:
    """Send this process's logs — and the mission's — to stdout.

    Nothing configured logging here before, so Python's last-resort handler
    swallowed every logger.info() the mission emitted and only surfaced
    warnings and errors. helper.stdout.log came back empty on every run, which
    left a failed flight with no explanation beyond a single stderr line.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--sim-id", required=True)
    parser.add_argument("--timeout", required=True, type=float)
    parser.add_argument("--mission-address", default="udp://:14540")
    parser.add_argument("--runtime-mode", choices=("wsl", "native", "docker"), default="wsl")
    parser.add_argument("--isolation", choices=("sandbox", "none"), default="sandbox")
    parser.add_argument("--mission-sha256", default="")
    args = parser.parse_args()
    global _RUNTIME_MODE
    _RUNTIME_MODE = args.runtime_mode
    _configure_logging()
    # mavsdk installs uvloop's event-loop policy on import when uvloop is
    # present (the API venv has it). A loop created before that import can no
    # longer spawn mavsdk_server, so settle the policy before the loop exists.
    import mavsdk  # noqa: F401

    asyncio.run(_main(args))
    return 0


if __name__ == "__main__":
    exit_code = 0
    try:
        exit_code = main()
    except Exception:
        traceback.print_exc()
        exit_code = 1
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os._exit(exit_code)
