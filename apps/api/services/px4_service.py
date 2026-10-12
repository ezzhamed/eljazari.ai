"""PX4 mission execution and telemetry collection service."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from collections.abc import Callable
from typing import Any

from pipelines.semantic_converter import TelemetryData, TelemetryPoint
from utils.sitl_runtime import require_real_sitl, resolve_sitl_runtime, windows_to_wsl_path

logger = logging.getLogger(__name__)
WSL_RUNNER_BOOTSTRAP_HEADROOM_S = 45.0

_MAVSDK_ADDR_TMPL = "udpin://0.0.0.0:{port}"
_TELEMETRY_HZ = 2.0
_TELEMETRY_INTERVAL = 1.0 / _TELEMETRY_HZ
_WSL_ARTIFACT_ROOT = Path(tempfile.gettempdir()) / "droneprompt-wsl-artifacts"
_UNSANDBOXED_OVERRIDE_ENV = "DRONEPROMPT_ALLOW_UNSANDBOXED_MISSIONS"
# The native runner gets only these variables from the API process; service
# secrets in its environment must not reach generated code or its helpers.
_RUNNER_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "XDG_RUNTIME_DIR")


def mission_isolation_mode() -> str:
    """Return "sandbox" unless a development-only override allows "none"."""
    from config.settings import settings

    override = os.getenv(_UNSANDBOXED_OVERRIDE_ENV, "").strip().lower() in {"1", "true", "yes"}
    return "none" if override and not settings.is_production else "sandbox"


class PX4Service:
    """Execute a mission and collect MAVSDK telemetry when available."""

    def __init__(self) -> None:
        self._runtime = resolve_sitl_runtime()

    async def execute_mission(
        self,
        code: str,
        sim_id: str,
        mavlink_port: int = 14550,
        mavsdk_address: str | None = None,
        timeout_s: float = 180.0,
        on_point: Callable[[dict], None] | None = None,
        expected_code_sha256: str | None = None,
    ) -> TelemetryData:
        if expected_code_sha256 is not None and (
            hashlib.sha256(code.encode("utf-8")).hexdigest() != expected_code_sha256
        ):
            raise ValueError("Mission code does not match its approved fingerprint")
        logger.info(
            "Executing mission",
            extra={
                "sim_id": sim_id,
                "port": mavlink_port,
                "timeout_s": timeout_s,
                "runtime": self._runtime.mode,
            },
        )
        if not self._runtime.is_real:
            if require_real_sitl():
                details = (
                    "; ".join(self._runtime.diagnostics) or "No diagnostic detail was provided."
                )
                raise RuntimeError(
                    "Real PX4/Gazebo SITL is required, but the resolved runtime is "
                    f"'{self._runtime.mode}'. {details}"
                )
            logger.info(
                "Real SITL unavailable - using simulated telemetry fallback",
                extra={
                    "sim_id": sim_id,
                    "runtime": self._runtime.mode,
                    "diagnostics": list(self._runtime.diagnostics),
                },
            )
            telemetry = self._fallback_telemetry(
                sim_id,
                code,
                reason="Real SITL runtime is unavailable on this host.",
                warnings=list(self._runtime.diagnostics),
            )
            await self._stream_fallback_points(telemetry.points, on_point)
            return telemetry

        runtime_code = self._prepare_runtime_code(code)
        code_path = self._write_temp_code(runtime_code)
        try:
            if self._runtime.mode in ("wsl", "native", "docker"):
                # One runner process owns the vehicle link and collects telemetry;
                # the mission runs in its namespace sandbox. The direct path below
                # starts a second MAVSDK client on the same UDP port, which receives
                # only part of the stream (flight modes advance, altitude stays 0).
                telemetry = await self._execute_wsl_mission(
                    code_path=code_path,
                    sim_id=sim_id,
                    mavlink_port=mavlink_port,
                    mavsdk_address=mavsdk_address,
                    timeout_s=timeout_s,
                    on_point=on_point,
                )
                if telemetry is not None and telemetry.points:
                    return telemetry

                if require_real_sitl():
                    if telemetry is not None:
                        return telemetry
                    return self._wsl_failure_telemetry(
                        sim_id,
                        runtime_mode=self._runtime.mode,
                        mission_error="WSL mission runner did not return a telemetry payload.",
                        warnings=[
                            "Real PX4/Gazebo SITL was required, so synthetic telemetry fallback was disabled."
                        ],
                    )

                logger.warning(
                    "WSL mission failed to produce telemetry. Falling back to synthetic mock telemetry.",
                    extra={
                        "sim_id": sim_id,
                        "mission_error": getattr(telemetry, "mission_error", "unknown")
                        if telemetry
                        else "None",
                    },
                )
                fallback = self._fallback_telemetry(
                    sim_id,
                    code,
                    reason=getattr(
                        telemetry,
                        "mission_error",
                        "WSL mission runner did not return a telemetry payload.",
                    )
                    if telemetry
                    else "WSL mission runner did not return a telemetry payload.",
                    warnings=getattr(telemetry, "warnings", []) if telemetry else [],
                )
                await self._stream_fallback_points(fallback.points, on_point)
                return fallback

            if mission_isolation_mode() == "sandbox":
                # This path runs generated code in the API process with the
                # service's environment and files. Only the namespace sandbox
                # in the Linux runner (native, WSL and Docker runtimes) may execute it.
                return TelemetryData(
                    sim_id=sim_id,
                    runtime_mode=self._runtime.mode,
                    telemetry_source="none",
                    mission_error=(
                        f"The {self._runtime.mode} runtime cannot sandbox generated mission "
                        "code, so it was not executed. Use the native, WSL or Docker runtime."
                    ),
                )

            mission_done = asyncio.Event()

            async def _run_mission_and_signal() -> int:
                try:
                    return await self._run_code_subprocess(code_path, timeout_s)
                finally:
                    mission_done.set()

            mission_task = asyncio.create_task(_run_mission_and_signal())
            telemetry_task = asyncio.create_task(
                self._collect_telemetry_stream(
                    sim_id,
                    mavlink_port,
                    timeout_s,
                    mavsdk_address=mavsdk_address,
                    mission_done=mission_done,
                )
            )
            results = await asyncio.gather(mission_task, telemetry_task, return_exceptions=True)
            telemetry = results[1]
            if isinstance(telemetry, BaseException):
                if require_real_sitl():
                    raise RuntimeError(f"Live telemetry stream failed: {telemetry}") from telemetry
                logger.warning(
                    "Telemetry stream failed - using simulated telemetry",
                    extra={"error": str(telemetry)},
                )
                return self._fallback_telemetry(
                    sim_id,
                    code,
                    reason=f"Live telemetry stream failed: {telemetry}",
                )
            return telemetry  # type: ignore[return-value]
        except Exception as exc:
            logger.error("execute_mission error", extra={"error": str(exc)})
            if require_real_sitl():
                raise RuntimeError(f"Real PX4/Gazebo SITL mission execution failed: {exc}") from exc
            return self._fallback_telemetry(
                sim_id,
                code,
                reason=f"execute_mission error: {exc}",
            )
        finally:
            try:
                os.unlink(code_path)
            except OSError:
                pass

    #: Marks a live telemetry sample on the runner's stdout. Must match
    #: wsl_mission_runner._LIVE_POINT_PREFIX.
    _LIVE_POINT_PREFIX = "@@PT "

    async def _stream_mission_output(
        self,
        proc: asyncio.subprocess.Process,
        on_point: Callable[[dict], None] | None,
    ) -> tuple[str, str]:
        """Drain the runner's pipes while it runs, forwarding telemetry as it lands.

        Both pipes have to be drained concurrently. Reading one to completion
        first deadlocks the moment the other fills its buffer, and at 10 Hz the
        runner produces enough stdout to hit that.

        Every line is still collected and returned, so the artifacts and the
        failure paths keep the full output they had before; the callback is
        purely additive.
        """

        async def drain(stream: asyncio.StreamReader | None, live: bool) -> str:
            if stream is None:
                return ""
            chunks: list[str] = []
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace")
                chunks.append(line)
                if not (live and on_point):
                    continue
                stripped = line.strip()
                if not stripped.startswith(self._LIVE_POINT_PREFIX):
                    continue
                try:
                    point = json.loads(stripped[len(self._LIVE_POINT_PREFIX) :])
                except (ValueError, TypeError):
                    continue  # a truncated line costs one frame, nothing more
                try:
                    on_point(point)
                except Exception:  # noqa: BLE001 - a viewer must not fail a flight
                    logger.debug("live telemetry callback raised", exc_info=True)
            return "".join(chunks)

        stdout_text, stderr_text = await asyncio.gather(
            drain(proc.stdout, live=True),
            drain(proc.stderr, live=False),
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("Process did not exit after pipe drain; killed")
        return stdout_text, stderr_text

    async def _execute_wsl_mission(
        self,
        *,
        code_path: str,
        sim_id: str,
        mavlink_port: int,
        mavsdk_address: str | None,
        timeout_s: float,
        on_point: Callable[[dict], None] | None = None,
    ) -> TelemetryData | None:
        await self._cleanup_wsl_mission_processes()
        # Fingerprint the exact bytes the runner will load; the sandbox checks it again.
        mission_sha256 = hashlib.sha256(Path(code_path).read_bytes()).hexdigest()

        helper_path = Path(__file__).resolve().parents[1] / "utils" / "wsl_mission_runner.py"
        mode = self._runtime.mode
        # Docker: the runner and its sandbox run inside the run's own container.
        prefix: list[str] | None = None
        if mode == "docker":
            from services import docker_sim

            container = docker_sim.lookup(sim_id)
            if container is None:
                return self._wsl_failure_telemetry(
                    sim_id,
                    runtime_mode=mode,
                    mission_error="No simulation container is registered for this run.",
                    warnings=["The docker runtime starts one container per run in launch_world."],
                )
            prefix = docker_sim.exec_prefix(container)
        artifact_dir = self._artifact_dir(sim_id)
        if mode == "native":
            wsl_mission_path = code_path
            output_path = str(
                Path(tempfile.gettempdir()) / f"droneprompt_telemetry_{uuid.uuid4().hex}.json"
            )
        else:
            wsl_mission_path = f"/tmp/droneprompt_mission_{uuid.uuid4().hex}.py"
            output_path = f"/tmp/droneprompt_wsl_telemetry_{uuid.uuid4().hex}.json"
            if not await self._copy_file_to_wsl(code_path, wsl_mission_path, prefix=prefix):
                return self._wsl_failure_telemetry(
                    sim_id,
                    mission_error="Failed to copy the mission file into WSL.",
                    warnings=["WSL mission bootstrap failed before execution started."],
                    artifacts=self._persist_wsl_artifacts(
                        artifact_dir,
                        payload_text=None,
                        stdout_text="",
                        stderr_text="",
                    ),
                )
        mission_address = (mavsdk_address or "").strip() or os.getenv(
            "DRONEPROMPT_MAVSDK_ADDRESS", "udp://:14540"
        )

        if mode == "native":
            runner = [sys.executable, str(helper_path)]
        elif prefix is not None:
            from services.docker_sim import CONTAINER_PYTHON, CONTAINER_RUNNER

            runner = prefix + [CONTAINER_PYTHON, CONTAINER_RUNNER]
        else:
            runner = self._runtime.wsl_prefix() + [
                self._runtime.wsl_python_bin,
                windows_to_wsl_path(str(helper_path)),
            ]
        command = runner + [
            "--mission",
            wsl_mission_path,
            "--output",
            output_path,
            "--port",
            str(mavlink_port),
            "--sim-id",
            sim_id,
            "--timeout",
            str(timeout_s),
            "--mission-address",
            mission_address,
            "--runtime-mode",
            mode,
            "--isolation",
            mission_isolation_mode(),
            "--mission-sha256",
            mission_sha256,
        ]
        runner_env = (
            {key: os.environ[key] for key in _RUNNER_ENV_ALLOWLIST if key in os.environ}
            if mode == "native"
            else None
        )

        proc: asyncio.subprocess.Process | None = None
        stdout_text = ""
        stderr_text = ""
        payload_text: str | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=runner_env,
            )
            try:
                # Read stdout as it arrives rather than waiting for exit.
                # communicate() only returns once the process is gone, which is
                # why telemetry could only ever be delivered as one burst after
                # the flight had finished.
                stdout_text, stderr_text = await asyncio.wait_for(
                    self._stream_mission_output(proc, on_point),
                    timeout=timeout_s + WSL_RUNNER_BOOTSTRAP_HEADROOM_S,
                )
            except asyncio.TimeoutError:
                payload_text = await self._read_wsl_text_file(output_path, prefix=prefix)
                if proc.returncode is None:
                    proc.kill()
                    try:
                        stdout_bytes, stderr_bytes = await asyncio.wait_for(
                            proc.communicate(), timeout=10.0
                        )
                    except asyncio.TimeoutError:
                        stdout_bytes, stderr_bytes = b"", b""
                        logger.warning("proc.communicate() timed out after kill")
                    stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace")
                    stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace")
                if payload_text:
                    logger.warning(
                        "WSL mission runner timed out after writing telemetry payload",
                        extra={"sim_id": sim_id},
                    )
                    payload = json.loads(payload_text)
                    artifacts = self._persist_wsl_artifacts(
                        artifact_dir,
                        payload_text=payload_text,
                        stdout_text=stdout_text,
                        stderr_text=stderr_text,
                    )
                    return self._telemetry_from_payload(
                        payload,
                        sim_id,
                        artifacts=artifacts,
                        extra_warnings=[
                            "WSL mission runner timed out after writing telemetry payload."
                        ],
                    )
                artifacts = self._persist_wsl_artifacts(
                    artifact_dir,
                    payload_text=None,
                    stdout_text=stdout_text,
                    stderr_text=stderr_text,
                )
                return self._wsl_failure_telemetry(
                    sim_id,
                    runtime_mode=mode,
                    mission_error="WSL mission runner timed out before producing telemetry.",
                    warnings=[
                        "The real WSL runtime launched but no telemetry payload was written."
                    ],
                    artifacts=artifacts,
                )
            payload_text = await self._read_wsl_text_file(output_path, prefix=prefix)
            artifacts = self._persist_wsl_artifacts(
                artifact_dir,
                payload_text=payload_text,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            )
            if proc.returncode not in (0, None):
                if payload_text:
                    logger.warning(
                        "WSL mission runner returned non-zero status with telemetry payload",
                        extra={
                            "sim_id": sim_id,
                            "returncode": proc.returncode,
                        },
                    )
                    payload = json.loads(payload_text)
                    warning = (
                        f"WSL mission runner returned exit code {proc.returncode} "
                        "but still produced telemetry."
                    )
                    return self._telemetry_from_payload(
                        payload,
                        sim_id,
                        artifacts=artifacts,
                        extra_warnings=[warning],
                    )
                logger.warning(
                    "WSL mission runner failed",
                    extra={
                        "sim_id": sim_id,
                        "returncode": proc.returncode,
                        "stderr_tail": stderr_text[-300:],
                    },
                )
                return self._wsl_failure_telemetry(
                    sim_id,
                    runtime_mode=mode,
                    mission_error=f"WSL mission runner failed with exit code {proc.returncode}.",
                    warnings=[
                        stderr_text[-500:]
                        if stderr_text.strip()
                        else "No stderr output was captured."
                    ],
                    artifacts=artifacts,
                )

            if not payload_text:
                return self._wsl_failure_telemetry(
                    sim_id,
                    runtime_mode=mode,
                    mission_error="WSL mission runner exited without writing a telemetry payload.",
                    warnings=[
                        stderr_text[-500:]
                        if stderr_text.strip()
                        else "No stderr output was captured."
                    ],
                    artifacts=artifacts,
                )
            payload = json.loads(payload_text)
            return self._telemetry_from_payload(payload, sim_id, artifacts=artifacts)
        except Exception as exc:
            artifacts = self._persist_wsl_artifacts(
                artifact_dir,
                payload_text=payload_text,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            )
            logger.warning(
                "WSL mission execution failed",
                extra={
                    "sim_id": sim_id,
                    "error": str(exc),
                    "stderr_tail": stderr_text[-300:],
                },
            )
            return self._wsl_failure_telemetry(
                sim_id,
                runtime_mode=mode,
                mission_error=f"WSL mission execution failed: {exc}",
                warnings=[stderr_text[-500:] if stderr_text.strip() else str(exc)],
                artifacts=artifacts,
            )
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except (asyncio.TimeoutError, Exception):
                    logger.debug(
                        "Failed to terminate WSL mission runner process during cleanup",
                        exc_info=True,
                    )
            await self._remove_wsl_file(output_path, prefix=prefix)
            await self._remove_wsl_file(wsl_mission_path, prefix=prefix)
            await self._cleanup_wsl_mission_processes()

    @staticmethod
    async def _run_code_subprocess(code_path: str, timeout_s: float) -> int:
        """Run mission file in a subprocess and return its exit code."""
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                code_path,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
                if proc.returncode not in (None, 0):
                    logger.debug("Mission subprocess exited with code %s", proc.returncode)
                return proc.returncode or 0
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning("Mission subprocess timed out")
                return -1
        except Exception as exc:
            logger.warning("Could not run mission subprocess", extra={"error": str(exc)})
            return -1
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except (asyncio.TimeoutError, Exception):
                    logger.debug("Failed to terminate subprocess during cleanup", exc_info=True)

    async def _collect_telemetry_stream(
        self,
        sim_id: str,
        mavlink_port: int,
        duration_s: float,
        *,
        mavsdk_address: str | None = None,
        mission_done: asyncio.Event | None = None,
    ) -> TelemetryData:
        """Connect to MAVSDK and stream telemetry for the requested duration.

        When *mission_done* is provided and becomes set, the collector drains
        telemetry for a short grace period (5 s) to capture landing data, then
        exits early instead of running until the full *duration_s* deadline.
        """
        # Grace period after mission_done fires — captures the landing.
        _POST_MISSION_GRACE_S = 5.0

        try:
            from mavsdk import System  # type: ignore
        except ImportError:
            raise RuntimeError("MAVSDK not installed")

        system = System()
        address = self._mavsdk_address(mavlink_port, explicit_address=mavsdk_address)
        try:
            await asyncio.wait_for(system.connect(system_address=address), timeout=10.0)
        except (asyncio.TimeoutError, Exception) as exc:
            raise RuntimeError(
                f"MAVSDK connection failed for address {address} (port hint {mavlink_port}): {exc}"
            ) from exc

        points: list[TelemetryPoint] = []
        start = time.monotonic()
        deadline = start + duration_s
        # Once the mission subprocess exits, shorten the deadline to now + grace.
        mission_end_seen = False

        pos_it = system.telemetry.position().__aiter__()
        local_it = system.telemetry.position_velocity_ned().__aiter__()
        vel_it = system.telemetry.velocity_ned().__aiter__()
        att_it = system.telemetry.attitude_euler().__aiter__()
        bat_it = system.telemetry.battery().__aiter__()
        gps_it = system.telemetry.gps_info().__aiter__()
        mode_it = system.telemetry.flight_mode().__aiter__()
        iterators = [pos_it, local_it, vel_it, att_it, bat_it, gps_it, mode_it]

        try:
            while time.monotonic() < deadline:
                # Check if mission has finished and shorten deadline.
                if not mission_end_seen and mission_done is not None and mission_done.is_set():
                    mission_end_seen = True
                    grace_deadline = time.monotonic() + _POST_MISSION_GRACE_S
                    deadline = min(deadline, grace_deadline)
                    logger.info(
                        "Mission done signal received; draining telemetry for %.0fs",
                        _POST_MISSION_GRACE_S,
                    )
                timestamp_s = time.monotonic() - start
                try:
                    pos = await asyncio.wait_for(pos_it.__anext__(), timeout=2.0)
                    local = await asyncio.wait_for(local_it.__anext__(), timeout=2.0)
                    vel = await asyncio.wait_for(vel_it.__anext__(), timeout=2.0)
                    att = await asyncio.wait_for(att_it.__anext__(), timeout=2.0)
                    bat = await asyncio.wait_for(bat_it.__anext__(), timeout=2.0)
                    gps = await asyncio.wait_for(gps_it.__anext__(), timeout=2.0)
                    mode = await asyncio.wait_for(mode_it.__anext__(), timeout=2.0)
                except (StopAsyncIteration, asyncio.TimeoutError):
                    break

                north_m, east_m = self._extract_local_position(local)
                alt_m = self._resolve_altitude_m(pos.relative_altitude_m, local)
                speed = math.sqrt(vel.north_m_s**2 + vel.east_m_s**2)
                points.append(
                    TelemetryPoint(
                        timestamp_s=round(timestamp_s, 2),
                        north_m=north_m,
                        east_m=east_m,
                        lat=pos.latitude_deg,
                        lon=pos.longitude_deg,
                        alt_m=alt_m,
                        vel_north_ms=vel.north_m_s,
                        vel_east_ms=vel.east_m_s,
                        vel_down_ms=vel.down_m_s,
                        speed_ms=round(speed, 2),
                        roll_deg=att.roll_deg,
                        pitch_deg=att.pitch_deg,
                        yaw_deg=att.yaw_deg,
                        battery_percent=self._battery_percent(
                            getattr(bat, "remaining_percent", 0.0)
                        ),
                        battery_voltage=bat.voltage_v,
                        flight_mode=str(mode),
                        armed=True,
                        gps_satellites=gps.num_satellites,
                    )
                )
                await asyncio.sleep(_TELEMETRY_INTERVAL)
        finally:
            await asyncio.gather(
                *(self._close_async_iterator(iterator) for iterator in iterators),
                return_exceptions=True,
            )
            await self._close_mavsdk_system(system)

        elapsed = time.monotonic() - start
        completed = len(points) > 3 and points[-1].alt_m < 1.0
        crashed = any(point.alt_m < -0.5 for point in points[5:])

        return TelemetryData(
            sim_id=sim_id,
            points=points,
            duration_s=round(elapsed, 2),
            mission_started=len(points) > 3,
            mission_completed=completed,
            crash_detected=crashed,
        )

    @staticmethod
    async def _close_async_iterator(iterator: Any) -> None:
        close_fn = getattr(iterator, "aclose", None)
        if not callable(close_fn):
            return
        try:
            result = close_fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.debug("Failed to close async iterator during cleanup", exc_info=True)

    @staticmethod
    async def _close_mavsdk_system(system: Any) -> None:
        close_fn = getattr(system, "close", None)
        if not callable(close_fn):
            return
        try:
            result = close_fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.debug("Failed to close MAVSDK system connection", exc_info=True)

    @staticmethod
    def _mavsdk_address(mavlink_port: int, explicit_address: str | None = None) -> str:
        candidate = (explicit_address or "").strip()
        if candidate:
            return candidate
        return _MAVSDK_ADDR_TMPL.format(port=mavlink_port)

    @staticmethod
    def _extract_local_position(position_velocity_ned: Any) -> tuple[float, float]:
        position = getattr(position_velocity_ned, "position", None)
        if position is None:
            return 0.0, 0.0
        return (
            float(getattr(position, "north_m", 0.0)),
            float(getattr(position, "east_m", 0.0)),
        )

    @staticmethod
    def _resolve_altitude_m(rel_alt_m: float, position_velocity_ned: Any) -> float:
        position = getattr(position_velocity_ned, "position", None)
        if position is None:
            return rel_alt_m
        local_alt_m = -float(getattr(position, "down_m", 0.0))
        if abs(local_alt_m) > abs(rel_alt_m) + 0.25:
            return local_alt_m
        return rel_alt_m

    @staticmethod
    def _battery_percent(remaining_percent: float) -> float:
        value = float(remaining_percent)
        return value if value > 1.0 else value * 100.0

    @staticmethod
    def _telemetry_from_payload(
        payload: dict[str, Any],
        sim_id: str,
        *,
        artifacts: dict[str, str] | None = None,
        extra_warnings: list[str] | None = None,
    ) -> TelemetryData:
        points = [
            TelemetryPoint.model_validate(item)
            for item in payload.get("points", [])
            if isinstance(item, dict)
        ]
        warnings = [str(item).strip() for item in payload.get("warnings", []) if str(item).strip()]
        warnings.extend(
            warning.strip() for warning in (extra_warnings or []) if warning and warning.strip()
        )
        telemetry_source = str(payload.get("telemetry_source") or "").strip()
        runtime_mode = str(payload.get("runtime_mode") or "wsl")
        if not telemetry_source:
            telemetry_source = PX4Service._infer_telemetry_source(points, runtime_mode=runtime_mode)
        return TelemetryData(
            sim_id=str(payload.get("sim_id") or sim_id),
            points=points,
            duration_s=float(payload.get("duration_s", 0.0)),
            mission_started=bool(payload.get("mission_started", False)),
            mission_completed=bool(payload.get("mission_completed", False)),
            crash_detected=bool(payload.get("crash_detected", False)),
            geofence_violation=bool(payload.get("geofence_violation", False)),
            home_lat=float(payload.get("home_lat", 0.0) or 0.0),
            home_lon=float(payload.get("home_lon", 0.0) or 0.0),
            home_alt_m=float(payload.get("home_alt_m", 0.0) or 0.0),
            runtime_mode=runtime_mode,
            telemetry_source=telemetry_source,
            mission_isolation=str(payload.get("mission_isolation") or "unknown"),
            mission_error=str(payload.get("mission_error") or ""),
            warnings=warnings,
            artifacts=artifacts or {},
        )

    def _fallback_telemetry(
        self,
        sim_id: str,
        code: str,
        *,
        reason: str,
        warnings: list[str] | None = None,
    ) -> TelemetryData:
        telemetry = self._simulated_telemetry(
            sim_id,
            code,
            duration_s=self._estimate_sim_duration(code),
        )
        warning_items = [
            item.strip() for item in [*(warnings or []), reason] if item and item.strip()
        ]
        return telemetry.model_copy(
            update={
                "runtime_mode": self._runtime.mode,
                "telemetry_source": "simulated_fallback",
                "mission_error": reason,
                "warnings": warning_items,
            }
        )

    @staticmethod
    async def _stream_fallback_points(
        points: list[TelemetryPoint],
        on_point: Callable[[dict], None] | None,
    ) -> None:
        """Stream a subset of synthetic points to on_point so the live UI animates."""
        if not on_point or not points:
            return
        sample_step = max(1, len(points) // 35)
        for p in points[::sample_step]:
            raw_p = p.model_dump() if hasattr(p, "model_dump") else vars(p)
            on_point(raw_p)
            await asyncio.sleep(0.04)

    @staticmethod
    def _wsl_failure_telemetry(
        sim_id: str,
        *,
        runtime_mode: str = "wsl",
        mission_error: str,
        warnings: list[str] | None = None,
        artifacts: dict[str, str] | None = None,
    ) -> TelemetryData:
        warning_items = [item.strip() for item in (warnings or []) if item and item.strip()]
        if mission_error.strip():
            warning_items.insert(0, mission_error.strip())
        return TelemetryData(
            sim_id=sim_id,
            mission_started=False,
            mission_completed=False,
            crash_detected=False,
            runtime_mode=runtime_mode,
            telemetry_source=f"real_{runtime_mode}_sitl_empty",
            mission_error=mission_error,
            warnings=warning_items,
            artifacts=artifacts or {},
        )

    @staticmethod
    def _infer_telemetry_source(
        points: list[TelemetryPoint],
        *,
        runtime_mode: str,
    ) -> str:
        if not points:
            return f"{runtime_mode}_empty"

        peak_alt_m = max(max(0.0, point.alt_m) for point in points)
        north_values = [point.north_m for point in points]
        east_values = [point.east_m for point in points]
        horizontal_span_m = math.hypot(
            max(north_values) - min(north_values),
            max(east_values) - min(east_values),
        )
        has_nonzero_gps = any(abs(point.lat) > 1e-6 or abs(point.lon) > 1e-6 for point in points)

        if peak_alt_m >= 0.8 or horizontal_span_m >= 2.0 or has_nonzero_gps:
            return f"real_{runtime_mode}_sitl"
        return f"real_{runtime_mode}_sitl_partial"

    @staticmethod
    def _artifact_dir(sim_id: str) -> Path:
        path = _WSL_ARTIFACT_ROOT / f"{sim_id}-{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _persist_wsl_artifacts(
        artifact_dir: Path,
        *,
        payload_text: str | None,
        stdout_text: str,
        stderr_text: str,
    ) -> dict[str, str]:
        artifacts: dict[str, str] = {}

        stdout_path = artifact_dir / "helper.stdout.log"
        stdout_path.write_text(stdout_text or "", encoding="utf-8")
        artifacts["helper_stdout"] = str(stdout_path)

        stderr_path = artifact_dir / "helper.stderr.log"
        stderr_path.write_text(stderr_text or "", encoding="utf-8")
        artifacts["helper_stderr"] = str(stderr_path)

        if payload_text:
            payload_path = artifact_dir / "telemetry.json"
            payload_path.write_text(payload_text, encoding="utf-8")
            artifacts["telemetry_payload"] = str(payload_path)

        return artifacts

    @staticmethod
    def _simulated_telemetry(
        sim_id: str,
        code: str,
        duration_s: float = 30.0,
    ) -> TelemetryData:
        """Generate plausible telemetry from mission code when SITL is unavailable."""
        waypoints = PX4Service._parse_waypoints_from_code(code)
        if not waypoints:
            waypoints = [(0.0, 0.0, 10.0)]

        takeoff_alt = PX4Service._extract_takeoff_altitude(code)
        duration_s = max(
            duration_s, PX4Service._estimate_sim_duration(code, waypoints, takeoff_alt)
        )

        points: list[TelemetryPoint] = []
        north = east = alt = 0.0
        battery = 100.0
        timestamp_s = 0.0
        dt = _TELEMETRY_INTERVAL
        wp_idx = 0
        speed = 5.0

        while timestamp_s < duration_s:
            if timestamp_s < 3.0:
                phase = "AUTO.TAKEOFF"
                alt = min(takeoff_alt, alt + takeoff_alt / 3.0 * dt)
                velocity = takeoff_alt / 3.0
            elif wp_idx < len(waypoints):
                phase = "MISSION"
                tgt_north, tgt_east, tgt_alt = waypoints[wp_idx]
                delta_north = tgt_north - north
                delta_east = tgt_east - east
                delta_alt = tgt_alt - alt
                horizontal = math.sqrt(delta_north**2 + delta_east**2)
                if horizontal > speed * dt:
                    ratio = (speed * dt) / horizontal
                    north += delta_north * ratio
                    east += delta_east * ratio
                    alt += math.copysign(min(abs(delta_alt), 0.5), delta_alt)
                    velocity = speed
                else:
                    north, east, alt = tgt_north, tgt_east, tgt_alt
                    wp_idx += 1
                    velocity = 0.0
            else:
                phase = "AUTO.LAND"
                alt = max(0.0, alt - 0.5)
                velocity = 0.5
                if alt <= 0.0:
                    break

            battery -= 100.0 / (duration_s / dt * 20)
            yaw = (
                math.degrees(math.atan2(east, north)) % 360
                if (abs(north) + abs(east)) > 0.5
                else 0.0
            )
            points.append(
                TelemetryPoint(
                    timestamp_s=round(timestamp_s, 2),
                    north_m=round(north, 3),
                    east_m=round(east, 3),
                    alt_m=round(max(0.0, alt), 3),
                    speed_ms=round(velocity, 2),
                    yaw_deg=round(yaw, 1),
                    battery_percent=round(max(0.0, battery), 1),
                    battery_voltage=round(12.6 * battery / 100, 2),
                    flight_mode=phase,
                    armed=True,
                    gps_satellites=12,
                )
            )
            timestamp_s = round(timestamp_s + dt, 3)

        completed = len(points) > 5 and points[-1].alt_m < 1.0
        return TelemetryData(
            sim_id=sim_id,
            points=points,
            duration_s=timestamp_s,
            mission_started=len(points) > 3,
            mission_completed=completed,
            crash_detected=False,
        )

    @staticmethod
    def _parse_waypoints_from_code(code: str) -> list[tuple[float, float, float]]:
        """Extract cumulative absolute NED waypoints from fly_to_relative() calls using AST, with regex fallback."""
        takeoff_alt = PX4Service._extract_takeoff_altitude(code)
        try:
            tree = ast.parse(code)

            class WaypointExtractor(ast.NodeVisitor):
                def __init__(self, start_alt: float):
                    self.waypoints = []
                    self.cumulative_north = 0.0
                    self.cumulative_east = 0.0
                    self.current_alt = start_alt

                def _eval_node(self, node):
                    if isinstance(node, ast.Constant):
                        return node.value
                    elif isinstance(node, ast.UnaryOp) and isinstance(
                        node.op, (ast.USub, ast.UAdd)
                    ):
                        val = self._eval_node(node.operand)
                        if val is not None:
                            return -val if isinstance(node.op, ast.USub) else val
                    return None

                def visit_Call(self, node):
                    func_name = ""
                    if isinstance(node.func, ast.Name):
                        func_name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        func_name = node.func.attr

                    if func_name == "fly_to_relative":
                        kwargs = {}
                        for kw in node.keywords:
                            if kw.arg in ("north_m", "east_m", "down_m"):
                                val = self._eval_node(kw.value)
                                if val is not None:
                                    kwargs[kw.arg] = float(val)

                        pos_args = ["north_m", "east_m", "down_m"]
                        for idx, arg_node in enumerate(node.args):
                            if idx < len(pos_args):
                                val = self._eval_node(arg_node)
                                if val is not None:
                                    kwargs[pos_args[idx]] = float(val)

                        north = kwargs.get("north_m", 0.0)
                        east = kwargs.get("east_m", 0.0)
                        down = kwargs.get("down_m", 0.0)
                        self.cumulative_north += north
                        self.cumulative_east += east
                        if down != 0.0:
                            self.current_alt = max(2.0, self.current_alt - down)
                        self.waypoints.append(
                            (self.cumulative_north, self.cumulative_east, self.current_alt)
                        )
                    self.generic_visit(node)

            extractor = WaypointExtractor(takeoff_alt)
            extractor.visit(tree)
            return extractor.waypoints
        except Exception:
            # Fallback to regex-based extraction
            pattern = (
                r"fly_to_relative\s*\(\s*"
                r"north_m\s*=\s*([\-\d.]+)\s*,\s*"
                r"east_m\s*=\s*([\-\d.]+)\s*,\s*"
                r"down_m\s*=\s*([\-\d.]+)"
            )
            waypoints: list[tuple[float, float, float]] = []
            cumulative_north = 0.0
            cumulative_east = 0.0
            current_alt = takeoff_alt

            for north_delta, east_delta, down_delta in re.findall(pattern, code):
                north = float(north_delta)
                east = float(east_delta)
                down = float(down_delta)
                cumulative_north += north
                cumulative_east += east
                if down != 0.0:
                    current_alt = max(2.0, current_alt - down)
                waypoints.append((cumulative_north, cumulative_east, current_alt))

            return waypoints

    @staticmethod
    def _extract_takeoff_altitude(code: str) -> float:
        match = re.search(r"takeoff\s*\(\s*altitude_m\s*=\s*([\d.]+)", code)
        return float(match.group(1)) if match else 10.0

    @staticmethod
    def _estimate_sim_duration(
        code: str,
        waypoints: list[tuple[float, float, float]] | None = None,
        takeoff_alt: float | None = None,
    ) -> float:
        duration_match = re.search(r"MISSION_TARGET_DURATION_S\s*=\s*([\d.]+)", code)
        if duration_match:
            buffered = float(duration_match.group(1)) + 25.0
            return max(30.0, buffered)

        speed = 5.0
        takeoff_alt = (
            takeoff_alt if takeoff_alt is not None else PX4Service._extract_takeoff_altitude(code)
        )
        waypoints = (
            waypoints if waypoints is not None else PX4Service._parse_waypoints_from_code(code)
        )

        path_distance = 0.0
        prev_north = prev_east = 0.0
        for north, east, _alt in waypoints:
            path_distance += math.hypot(north - prev_north, east - prev_east)
            prev_north, prev_east = north, east
        path_distance += math.hypot(prev_north, prev_east)

        sleep_time = sum(float(match) for match in re.findall(r"asyncio\.sleep\(([\d.]+)\)", code))
        travel_time = path_distance / speed if speed > 0 else 0.0
        ascent_and_landing = (takeoff_alt / 2.0) + max(6.0, takeoff_alt / 2.0)
        return max(30.0, round(travel_time + sleep_time + ascent_and_landing + 5.0, 1))

    @staticmethod
    def _write_temp_code(code: str) -> str:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".py",
            prefix="drone_mission_",
            delete=False,
        ) as handle:
            handle.write(code)
            return handle.name

    @staticmethod
    def _prepare_runtime_code(code: str) -> str:
        if "DRONEPROMPT_MAVSDK_ADDRESS" in code:
            return code

        pattern = re.compile(
            r"(?P<indent>^[ \t]*)await\s+(?P<drone>\w+)\.connect\(\s*"
            r'system_address\s*=\s*(?P<quote>["\'])(?P<address>[^"\']+)(?P=quote)\s*\)',
            re.MULTILINE,
        )

        def replace(match: re.Match[str]) -> str:
            indent = match.group("indent")
            drone = match.group("drone")
            quote = match.group("quote")
            address = match.group("address")
            return (
                f"{indent}await {drone}.connect("
                f'system_address=os.getenv("DRONEPROMPT_MAVSDK_ADDRESS", '
                f"{quote}{address}{quote}))"
            )

        prepared, count = pattern.subn(replace, code, count=1)
        if count == 0:
            return code
        return PX4Service._ensure_os_import(prepared)

    @staticmethod
    def _ensure_os_import(code: str) -> str:
        if re.search(r"^\s*import os\s*$", code, re.MULTILINE):
            return code

        lines = code.splitlines(keepends=True)
        insert_at = 0
        if lines and lines[0].startswith("#!"):
            insert_at = 1
        while insert_at < len(lines) and lines[insert_at].startswith("from __future__ import"):
            insert_at += 1
        lines.insert(insert_at, "import os\n")
        return "".join(lines)

    async def _cleanup_wsl_mission_processes(self) -> None:
        if self._runtime.mode != "wsl":
            return

        bash_cmd = " ".join(
            [
                'pkill -f "wsl_mission_runner.py" || true;',
                'pkill -f "mission_sandbox_entry.py" || true;',
                'pkill -f "mavsdk/bin/mavsdk_server" || true;',
            ]
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._runtime.wsl_prefix(),
                "bash",
                "-lc",
                bash_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except Exception:
            logger.debug("Best-effort WSL mission cleanup failed", exc_info=True)

    async def _read_wsl_text_file(self, path: str, prefix: list[str] | None = None) -> str | None:
        if self._runtime.mode == "native":
            try:
                return Path(path).read_text(encoding="utf-8").strip() or None
            except OSError:
                return None
        if prefix is None and self._runtime.mode != "wsl":
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                *(prefix or self._runtime.wsl_prefix()),
                "cat",
                path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        text = (stdout or b"").decode("utf-8", errors="replace").strip()
        return text or None

    async def _remove_wsl_file(self, path: str, prefix: list[str] | None = None) -> None:
        if self._runtime.mode == "native":
            # The mission file is the caller's code_path; only remove our output.
            if Path(path).name.startswith("droneprompt_telemetry_"):
                Path(path).unlink(missing_ok=True)
            return
        if prefix is None and self._runtime.mode != "wsl":
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                *(prefix or self._runtime.wsl_prefix()),
                "rm",
                "-f",
                path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except Exception:
            logger.debug("Best-effort WSL file cleanup failed", exc_info=True)

    async def _copy_file_to_wsl(
        self, local_path: str, wsl_path: str, prefix: list[str] | None = None
    ) -> bool:
        if prefix is not None:
            # A container cannot read a host path: stream the file in on stdin.
            try:
                proc = await asyncio.create_subprocess_exec(
                    *prefix[:2],
                    "-i",
                    *prefix[2:],
                    "sh",
                    "-c",
                    'cat > "$1"',
                    "sh",
                    wsl_path,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(
                    proc.communicate(Path(local_path).read_bytes()), timeout=20.0
                )
            except Exception:
                return False
            return proc.returncode == 0
        if self._runtime.mode != "wsl":
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._runtime.wsl_prefix(),
                "cp",
                windows_to_wsl_path(local_path),
                wsl_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except Exception:
            return False
        return proc.returncode == 0
