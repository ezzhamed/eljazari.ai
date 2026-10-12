"""PX4/Gazebo SITL launch service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import settings
from utils.sitl_runtime import require_real_sitl, resolve_sitl_runtime

logger = logging.getLogger(__name__)

_BASE_MAVLINK_PORT = 14550
_BASE_SITL_PORT = 14540
# SIGTERM leftover simulators, wait up to 10 s for them to exit, then SIGKILL and
# wait again. The [x] patterns keep pgrep/pkill from matching this script itself.
_SITL_PATTERNS = (
    "[g]z sim -r -s --headless-rendering",
    "[b]uild/px4_sitl_default/bin/px4",
    "[b]uild/px4_sitl_default/rootfs/../bin/px4",
)
_SITL_CLEANUP_SCRIPT = (
    "alive() { "
    + " || ".join(f"pgrep -f '{p}' >/dev/null" for p in _SITL_PATTERNS)
    + "; }; "
    + " ".join(f"pkill -f '{p}' || true;" for p in _SITL_PATTERNS)
    + " for i in $(seq 1 50); do alive || exit 0; sleep 0.2; done;"
    + " ".join(f" pkill -9 -f '{p}' || true;" for p in _SITL_PATTERNS)
    + " for i in $(seq 1 25); do alive || exit 0; sleep 0.2; done; exit 1"
)
_SITL_LOG_ROOT = Path(tempfile.gettempdir()) / "droneprompt-sitl-logs"
# One PX4/Gazebo instance per host: ports, the PX4 rootfs and the WSL process
# cleanup are shared, and each launch kills leftover simulators.
_SITL_HOST_LOCK = Path(tempfile.gettempdir()) / "droneprompt-sitl.lock"

# PX4's commander prints this once its health-and-arming checks pass. Waiting for
# it is the difference between a mission that flies and one that is rejected mid
# handshake — see GazeboService._wait_until_ready.
PX4_READY_BANNER = "Ready for takeoff!"

_WORLD_PROFILES: dict[str, str] = {
    "default": "default.sdf",
    "urban": "default.sdf",
    "forest": "forest.sdf",
    "warehouse": "walls.sdf",
    "outdoor": "baylands.sdf",
}


def contains_mavlink_heartbeat(datagram: bytes) -> bool:
    """True if any MAVLink v1/v2 frame in the datagram is a HEARTBEAT (msgid 0).

    PX4 packs several messages into one UDP datagram, so every frame is walked.
    """
    offset = 0
    while offset < len(datagram):
        magic = datagram[offset]
        if magic == 0xFD and offset + 10 <= len(datagram):  # MAVLink 2
            length = datagram[offset + 1]
            if int.from_bytes(datagram[offset + 7 : offset + 10], "little") == 0:
                return True
            signed = datagram[offset + 2] & 0x01
            offset += 12 + length + (13 if signed else 0)
        elif magic == 0xFE and offset + 6 <= len(datagram):  # MAVLink 1
            if datagram[offset + 5] == 0:
                return True
            offset += 8 + datagram[offset + 1]
        else:
            return False
    return False


@dataclass
class SimProcess:
    """Track one Gazebo + PX4 simulation instance."""

    sim_id: str
    mavlink_port: int = _BASE_MAVLINK_PORT
    sitl_port: int = _BASE_SITL_PORT
    world_name: str = "default.sdf"
    config: dict[str, Any] = field(default_factory=dict)
    status: str = "starting"
    runtime_mode: str = "mock"
    artifact_dir: str = ""
    gz_stdout_log: str = ""
    gz_stderr_log: str = ""
    px4_stdout_log: str = ""
    px4_stderr_log: str = ""
    gz_process: asyncio.subprocess.Process | None = None
    px4_process: asyncio.subprocess.Process | None = None
    # Generated aircraft only: verified bundle, spawned instance and attestation.
    flight_bundle: Any = None
    model_instance: str = ""
    model_attestation: dict[str, Any] | None = None
    px4_working_dir: str = ""
    # Docker runtime: the container this run owns (services/docker_sim.py).
    container: str = ""
    # Host-wide simulator ownership, released by stop_world.
    host_lock: Any = None


class GazeboService:
    """Manage Gazebo Harmonic worlds and PX4 SITL instances."""

    def __init__(self) -> None:
        self._sims: dict[str, SimProcess] = {}
        self._next_index = 0
        self._runtime = resolve_sitl_runtime()
        self._is_linux = self._runtime.host_platform == "Linux"

    async def launch_world(
        self,
        drone_config: dict[str, Any],
        world: str | None = None,
        ready_timeout_s: float = 30.0,
    ) -> str:
        """Launch Gazebo + PX4 SITL for one simulation and return its ID."""
        from pipelines.simulation_readiness import generated_model_block_reason

        reason = generated_model_block_reason(drone_config)
        if reason:
            raise ValueError(reason)
        # Re-verify at launch: the readiness check and the spawn must see the same files.
        bundle = self._flight_bundle(drone_config)
        if bundle is not None and not self._runtime.is_real:
            raise ValueError(
                "Generated flight bundles need a real PX4/Gazebo runtime that can spawn and "
                f"attest them; the resolved runtime is '{self._runtime.mode}'."
            )
        # Wait for any other run on this host (other projects, other workers)
        # before touching processes: a launch kills leftover simulators.
        host_lock = await self._acquire_host_lock() if self._runtime.is_real else None
        try:
            return await self._launch_owned_world(
                drone_config, world, ready_timeout_s, bundle, host_lock
            )
        except BaseException:
            self._release_host_lock(host_lock)
            raise

    @staticmethod
    async def _wait_for_heartbeat(port: int, timeout_s: float) -> bool:
        """Listen briefly on MAVSDK's port for a PX4 heartbeat, then release it."""
        loop = asyncio.get_running_loop()
        seen: asyncio.Future[bool] = loop.create_future()

        class Probe(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: Any) -> None:
                if contains_mavlink_heartbeat(data) and not seen.done():
                    seen.set_result(True)

        transport, _ = await loop.create_datagram_endpoint(Probe, local_addr=("0.0.0.0", port))
        try:
            return await asyncio.wait_for(seen, timeout=timeout_s)
        except asyncio.TimeoutError:
            return False
        finally:
            transport.close()

    async def _acquire_host_lock(self) -> Any:
        from filelock import FileLock, Timeout

        lock = FileLock(str(_SITL_HOST_LOCK), thread_local=False)
        try:
            await asyncio.to_thread(lock.acquire, timeout=settings.sitl_lock_timeout_s)
        except Timeout as exc:
            raise RuntimeError(
                "Another PX4/Gazebo simulation is running on this host; waited "
                f"{settings.sitl_lock_timeout_s:.0f}s for it to finish"
            ) from exc
        return lock

    @staticmethod
    def _release_host_lock(lock: Any) -> None:
        if lock is not None and lock.is_locked:
            lock.release()

    async def _launch_owned_world(
        self,
        drone_config: dict[str, Any],
        world: str | None,
        ready_timeout_s: float,
        bundle: Any,
        host_lock: Any,
    ) -> str:
        sim_id = str(uuid.uuid4())[:8]
        # One simulator per host (see _acquire_host_lock), and PX4 instance 0
        # always uses its default ports; striding them per launch made later
        # runs listen where PX4 never sends.
        self._next_index += 1

        sim = SimProcess(
            sim_id=sim_id,
            mavlink_port=_BASE_MAVLINK_PORT,
            sitl_port=_BASE_SITL_PORT,
            world_name=self._select_world(drone_config, world),
            config=drone_config,
            runtime_mode=self._runtime.mode,
            flight_bundle=bundle,
            host_lock=host_lock,
        )
        self._prepare_log_paths(sim)
        self._sims[sim_id] = sim
        try:
            if self._runtime.mode == "wsl" and len(self._sims) == 1:
                await self._cleanup_wsl_sitl_processes()

            if self._runtime.mode == "wsl":
                await self._prepare_wsl_px4_rootfs(sim)

            if self._runtime.is_real:
                if self._runtime.mode == "docker":
                    # One container per run, owned by this service: Gazebo starts
                    # inside it; spawn, PX4 and attestation then run exactly as
                    # under WSL, through docker exec (services/docker_sim.py).
                    from services import docker_sim

                    await docker_sim.remove_stale()
                    sim.container = await docker_sim.start(
                        sim.sim_id,
                        self._world_name_for_px4(sim),
                        bundle.bundle_dir if bundle is not None else None,
                    )
                    if bundle is not None:
                        await self._spawn_generated_model(sim)
                    await self._start_px4_sitl(sim)
                    await self._wait_until_ready(sim, timeout_s=ready_timeout_s)
                    if bundle is not None:
                        await self._attest_px4_params(sim)
                elif bundle is not None:
                    # A failure here reaches the handler below, which stops the
                    # run: a half-attested generated aircraft is never left up.
                    await self._start_gazebo(sim)
                    await self._spawn_generated_model(sim)
                    await self._start_px4_sitl(sim)
                    await self._wait_until_ready(sim, timeout_s=ready_timeout_s)
                    await self._attest_px4_params(sim)
                else:
                    await self._start_gazebo(sim)
                    if self._runtime.mode == "wsl":
                        # Give Gazebo time to advertise the world before PX4 starts waiting on it.
                        await asyncio.sleep(3.0)
                    await self._start_px4_sitl(sim)
                    await self._wait_until_ready(sim, timeout_s=ready_timeout_s)
            else:
                if require_real_sitl():
                    self._sims.pop(sim_id, None)
                    details = (
                        "; ".join(self._runtime.diagnostics) or "No diagnostic detail was provided."
                    )
                    raise RuntimeError(
                        "Real PX4/Gazebo SITL is required, but the resolved runtime is "
                        f"'{self._runtime.mode}'. {details}"
                    )
                logger.warning(
                    "Real SITL unavailable - using headless simulation mode",
                    extra={
                        "runtime": self._runtime.mode,
                        "platform": self._runtime.host_platform,
                        "sim_id": sim_id,
                        "diagnostics": list(self._runtime.diagnostics),
                    },
                )
                sim.status = "running"

            logger.info(
                "Simulation world launched",
                extra={
                    "sim_id": sim_id,
                    "world": sim.world_name,
                    "mavlink_port": sim.mavlink_port,
                    "runtime": self._runtime.mode,
                    "headless": not self._runtime.is_real,
                    "artifact_dir": sim.artifact_dir,
                },
            )
        except BaseException:
            # Whatever failed after registration: kill what started, drop the
            # entry and give the host back (stop_world is idempotent).
            await self.stop_world(sim_id)
            raise
        return sim_id

    async def stop_world(self, sim_id: str) -> None:
        """Terminate Gazebo and PX4 SITL for the given simulation."""
        sim = self._sims.pop(sim_id, None)
        if sim is None:
            return

        try:
            if self._runtime.is_real:
                await self._kill_process(sim.gz_process, "Gazebo")
                await self._kill_process(sim.px4_process, "PX4 SITL")
                if self._runtime.mode == "wsl":
                    await self._cleanup_wsl_sitl_processes()
                if self._runtime.mode == "docker":
                    from services import docker_sim

                    await docker_sim.stop(sim_id)
        finally:
            self._release_host_lock(sim.host_lock)

        logger.info("Simulation stopped", extra={"sim_id": sim_id})

    async def is_running(self, sim_id: str) -> bool:
        """Return True if the simulation is alive."""
        sim = self._sims.get(sim_id)
        if sim is None:
            return False
        if not self._runtime.is_real:
            return sim.status == "running"
        if self._runtime.mode == "docker":
            # Gazebo is the container's main process: the container stops with it.
            from services import docker_sim

            px4_alive = sim.px4_process is not None and sim.px4_process.returncode is None
            return px4_alive and await docker_sim.is_running(sim_id)
        gz_alive = sim.gz_process is not None and sim.gz_process.returncode is None
        px4_alive = sim.px4_process is not None and sim.px4_process.returncode is None
        return gz_alive and px4_alive

    def get_mavlink_port(self, sim_id: str) -> int:
        """Return the MAVLink UDP port assigned to this simulation."""
        sim = self._sims.get(sim_id)
        if sim is None:
            raise KeyError(f"Unknown sim_id: {sim_id}")
        return sim.mavlink_port

    def get_mavsdk_address(self, sim_id: str) -> str:
        """Return the preferred MAVSDK address for this simulation."""
        sim = self._sims.get(sim_id)
        if sim is None:
            raise KeyError(f"Unknown sim_id: {sim_id}")
        return f"udpin://0.0.0.0:{sim.sitl_port}"

    def get_sim_status(self, sim_id: str) -> str:
        sim = self._sims.get(sim_id)
        return sim.status if sim else "unknown"

    def get_sim_artifacts(self, sim_id: str) -> dict[str, str]:
        sim = self._sims.get(sim_id)
        if sim is None:
            raise KeyError(f"Unknown sim_id: {sim_id}")
        artifacts = {
            "artifact_dir": sim.artifact_dir,
            "gz_stdout": sim.gz_stdout_log,
            "gz_stderr": sim.gz_stderr_log,
            "px4_stdout": sim.px4_stdout_log,
            "px4_stderr": sim.px4_stderr_log,
        }
        if sim.px4_working_dir:
            # PX4 writes its ULog under <working dir>/log/ for generated runs.
            artifacts["px4_working_dir"] = sim.px4_working_dir
        return artifacts

    def get_model_attestation(self, sim_id: str) -> dict[str, Any] | None:
        """What the simulator actually loaded; None for reference-model runs."""
        sim = self._sims.get(sim_id)
        if sim is None or sim.model_attestation is None:
            return None
        return json.loads(json.dumps(sim.model_attestation))

    # PX4 shell commands a fault-injection check may run inside a live run.
    FAULT_COMMANDS = frozenset({"failure", "param", "mavlink"})

    def active_sim_ids(self) -> list[str]:
        """The runs this service currently owns (one per host)."""
        return list(self._sims)

    async def px4_command(
        self, sim_id: str, command: str, *args: str, timeout_s: float = 20.0
    ) -> tuple[int, str]:
        """Run ``px4-<command> <args>`` against this run's PX4.

        For fault-injection checks (scripts/check_sitl_faults.py); no API route
        reaches it, and only :data:`FAULT_COMMANDS` are allowed.
        """
        if command not in self.FAULT_COMMANDS:
            raise ValueError(f"PX4 command {command!r} is not allowed")
        client = f"{self._runtime.px4_dir.rstrip('/')}/build/px4_sitl_default/bin/px4-{command}"
        return await self._run_in_sim(sim_id, shlex.join([client, *args]), timeout_s)

    async def kill_px4(self, sim_id: str) -> tuple[int, str]:
        """SIGKILL this run's PX4 mid-flight, as a crash would (fault-injection checks)."""
        # The trailing space matches the daemon, not the px4-<command> clients.
        return await self._run_in_sim(sim_id, "pkill -9 -f '[b]in/px4 '", 20.0)

    async def _run_in_sim(self, sim_id: str, script: str, timeout_s: float) -> tuple[int, str]:
        sim = self._sims.get(sim_id)
        if sim is None:
            raise KeyError(f"No running simulation {sim_id}")
        if self._runtime.mode == "docker":
            from services.docker_sim import exec_prefix

            prefix = exec_prefix(sim.container, stdin=True)
        elif self._runtime.mode == "wsl":
            prefix = self._runtime.wsl_prefix()
        else:
            prefix = []
        # Over stdin: wsl.exe mangles quoted arguments (see _cleanup_wsl_sitl_processes).
        proc = await asyncio.create_subprocess_exec(
            *prefix,
            "bash",
            "-s",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(script.encode()), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "timed out"
        return proc.returncode or 0, out.decode("utf-8", errors="replace")

    def get_available_worlds(self) -> list[str]:
        return list(_WORLD_PROFILES.keys())

    def active_sim_count(self) -> int:
        return len(self._sims)

    def runtime_summary(self) -> dict[str, Any]:
        return {
            "mode": self._runtime.mode,
            "is_real": self._runtime.is_real,
            "host_platform": self._runtime.host_platform,
            "diagnostics": list(self._runtime.diagnostics),
            "wsl_distro": self._runtime.wsl_distro,
        }

    async def _start_gazebo(self, sim: SimProcess) -> None:
        if self._runtime.mode == "wsl":
            sim.gz_process = await self._create_wsl_gazebo_process(sim)
            return

        worlds_path = Path(self._runtime.worlds_path)
        world_file = worlds_path / sim.world_name
        cmd = ["gz", "sim", "-r", "-s", "--headless-rendering", str(world_file)]
        env = {
            **self._gz_runtime_env(),
            "GZ_PARTITION": self._gz_partition(sim),
            "QT_QPA_PLATFORM": "offscreen",
            "LIBGL_ALWAYS_SOFTWARE": "1",
        }
        sim.gz_process = await self._create_subprocess(
            cmd,
            env=env,
            stdout_log_path=sim.gz_stdout_log,
            stderr_log_path=sim.gz_stderr_log,
        )

    async def _start_px4_sitl(self, sim: SimProcess) -> None:
        if self._runtime.mode in ("wsl", "docker"):
            sim.px4_process = await self._create_wsl_px4_process(sim)
            return

        px4_dir = Path(self._runtime.px4_dir)
        rootfs_dir = px4_dir / "build/px4_sitl_default/rootfs"
        px4_bin = px4_dir / "build/px4_sitl_default/bin/px4"
        cwd = str(rootfs_dir)
        cmd = [
            str(px4_bin),
            "-d",
            str(px4_dir / "ROMFS/px4fmu_common"),
            "-s",
            "etc/init.d-posix/rcS",
        ]
        env = {
            **self._gz_runtime_env(),
            "PX4_SIM_HOSTNAME": "localhost",
            "MAVLINK_UDP_PORT": str(sim.mavlink_port),
            "PX4_GZ_WORLD": self._world_name_for_px4(sim),
            "PX4_GZ_STANDALONE": "1",
            "GZ_PARTITION": self._gz_partition(sim),
        }
        if sim.flight_bundle is not None:
            cwd = self._px4_working_dir(sim)
            Path(cwd).mkdir(parents=True, exist_ok=True)
            env.update(self._generated_px4_env(sim))
        else:
            env["PX4_GZ_MODEL"] = self._model_name(sim)
            env["PX4_SIM_MODEL"] = self._px4_sim_model(sim)
        sim.px4_process = await self._create_subprocess(
            cmd,
            cwd=cwd,
            env=env,
            stdout_log_path=sim.px4_stdout_log,
            stderr_log_path=sim.px4_stderr_log,
        )

    def _px4_working_dir(self, sim: SimProcess) -> str:
        """Fresh PX4 working directory per generated run.

        PX4 persists ``param set`` values in ``parameters.bson`` of its working
        directory. A per-run directory keeps one design's geometry and gains
        from leaking into the next run or into reference x500 runs, and keeps
        the run's ULog next to nothing else.
        """
        if not sim.px4_working_dir:
            base = (
                "/tmp/droneprompt-px4"
                if self._runtime.mode in ("wsl", "docker")
                else str(Path(tempfile.gettempdir()) / "droneprompt-px4")
            )
            sim.px4_working_dir = f"{base}/{sim.sim_id}"
        return sim.px4_working_dir

    @staticmethod
    def _generated_px4_env(sim: SimProcess) -> dict[str, str]:
        bundle = sim.flight_bundle
        env = {
            "PX4_SYS_AUTOSTART": str(bundle.px4_base_autostart),
            "PX4_GZ_MODEL_NAME": sim.model_instance,
        }
        for name, value in bundle.px4_params.items():
            env[f"PX4_PARAM_{name}"] = str(value)
        return env

    @staticmethod
    def _flight_bundle(config: dict[str, Any]):
        manifest = config.get("flight_model_manifest_path")
        if not manifest:
            return None
        from pipelines.flight_model import verify_flight_bundle

        return verify_flight_bundle(manifest, str(config.get("resolved_drone_sha256") or ""))

    def _runtime_path(self, path: str) -> str:
        if self._runtime.mode == "wsl":
            from utils.sitl_runtime import windows_to_wsl_path

            return windows_to_wsl_path(path)
        if self._runtime.mode == "docker":
            from services.docker_sim import BUNDLE_MOUNT

            # The run's flight bundle is mounted read-only at /bundle.
            return f"{BUNDLE_MOUNT}/{Path(path).name}"
        return path

    def _shell_prefix(self, sim: SimProcess) -> list[str]:
        """Where a bash command for this run executes: WSL or the run's container."""
        if self._runtime.mode == "docker":
            from services.docker_sim import exec_prefix

            return exec_prefix(sim.container)
        return self._runtime.wsl_prefix()

    async def _gz_command(
        self,
        args: list[str],
        timeout_s: float = 20.0,
        partition: str | None = None,
        sim: SimProcess | None = None,
    ) -> tuple[int, str]:
        """Run a ``gz`` CLI command where the simulator runs; return (code, output)."""
        if self._runtime.mode in ("wsl", "docker"):
            bash_cmd = " ".join(
                [
                    f"export GZ_PARTITION={shlex.quote(partition)};" if partition else "",
                    f"cd {shlex.quote(self._rootfs_dir())} &&",
                    '{ if [ -f "./gz_env.sh" ]; then . ./gz_env.sh; fi; } >/dev/null 2>&1;',
                    "exec",
                    shlex.join(
                        [self._runtime.wsl_gz_bin if self._runtime.mode == "wsl" else "gz", *args]
                    ),
                ]
            )
            prefix = (
                self._shell_prefix(sim)
                if sim is not None and self._runtime.mode == "docker"
                else self._runtime.wsl_prefix()
            )
            cmd = prefix + ["bash", "-lc", bash_cmd]
            env = None
        else:
            cmd = ["gz", *args]
            env = {**os.environ, **self._gz_runtime_env()}
            if partition:
                env["GZ_PARTITION"] = partition
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, ""
        return proc.returncode or 0, out.decode("utf-8", errors="replace").replace("\x00", "")

    async def _spawn_generated_model(self, sim: SimProcess, timeout_s: float = 40.0) -> None:
        """Spawn the bundle's SDF, then attest what Gazebo actually loaded."""
        from pipelines.flight_model import attest_spawned_model, parse_gz_model_links

        bundle = sim.flight_bundle
        world = self._world_name_for_px4(sim)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            _, out = await self._gz_command(
                ["service", "-i", "--service", f"/world/{world}/scene/info"],
                timeout_s=10,
                partition=self._gz_partition(sim),
                sim=sim,
            )
            if "Service providers" in out:
                break
            if loop.time() > deadline or (
                sim.gz_process is not None and sim.gz_process.returncode is not None
            ):
                raise RuntimeError(
                    f"Gazebo world '{world}' did not become ready. Log: {sim.gz_stderr_log}"
                )
            await asyncio.sleep(1.0)

        sim.model_instance = f"{bundle.model_name}_0"
        sdf_path = self._runtime_path(bundle.sdf_path)
        request = f'sdf_filename: "{sdf_path}", name: "{sim.model_instance}", allow_renaming: false'
        _, out = await self._gz_command(
            [
                "service",
                "-s",
                f"/world/{world}/create",
                "--reqtype",
                "gz.msgs.EntityFactory",
                "--reptype",
                "gz.msgs.Boolean",
                "--timeout",
                "5000",
                "--req",
                request,
            ],
            partition=self._gz_partition(sim),
            sim=sim,
        )
        if "data: true" not in out:
            raise RuntimeError(f"Gazebo refused to spawn the generated model: {out.strip()[:300]}")

        report = None
        while loop.time() <= deadline:
            _, out = await self._gz_command(
                ["model", "-m", sim.model_instance, "-l"],
                partition=self._gz_partition(sim),
                sim=sim,
            )
            report = parse_gz_model_links(out)
            if report.links:
                break
            await asyncio.sleep(0.5)
        sim.model_attestation = attest_spawned_model(
            bundle, sim.model_instance, report or parse_gz_model_links("")
        )
        logger.info(
            "Generated model spawned and attested",
            extra={
                "sim_id": sim.sim_id,
                "model_instance": sim.model_instance,
                "bundle_sha256": bundle.bundle_sha256,
                "simulated_total_mass_kg": sim.model_attestation["simulated_total_mass_kg"],
            },
        )

    async def _attest_px4_params(self, sim: SimProcess, timeout_s: float = 30.0) -> None:
        """Read back every design parameter from the running PX4 instance."""
        from pipelines.flight_model import compare_px4_params, parse_px4_param_show

        bundle = sim.flight_bundle
        expected = {**bundle.px4_params, "SYS_AUTOSTART": bundle.px4_base_autostart}
        client = f"{self._runtime.px4_dir.rstrip('/')}/build/px4_sitl_default/bin/px4-param"
        script = "; ".join(
            f"{shlex.quote(client)} show {shlex.quote(name)}" for name in sorted(expected)
        )
        if self._runtime.mode in ("wsl", "docker"):
            cmd = self._shell_prefix(sim) + ["bash", "-lc", script]
        else:
            cmd = ["bash", "-c", script]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        mismatches = ["not queried"]
        while loop.time() <= deadline:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                out = b""
            observed = parse_px4_param_show(out.decode("utf-8", errors="replace"))
            mismatches = compare_px4_params(expected, observed)
            if not mismatches:
                break
            await asyncio.sleep(1.0)
        if mismatches:
            raise RuntimeError(
                "PX4 is not running the generated airframe parameters: " + "; ".join(mismatches[:5])
            )
        sim.model_attestation = {
            **(sim.model_attestation or {}),
            "px4_autostart": bundle.px4_base_autostart,
            "px4_params_checked": len(expected),
            "px4_working_dir": sim.px4_working_dir,
        }

    async def _create_wsl_gazebo_process(
        self, sim: SimProcess
    ) -> asyncio.subprocess.Process | None:
        world_file = f"{self._runtime.worlds_path.rstrip('/')}/{sim.world_name}"
        rootfs_dir = self._rootfs_dir()
        bash_cmd = " ".join(
            [
                'export XDG_RUNTIME_DIR="/run/user/$(id -u)";',
                'mkdir -p "$XDG_RUNTIME_DIR";',
                'chmod 700 "$XDG_RUNTIME_DIR" || true;',
                "export QT_QPA_PLATFORM=offscreen;",
                "export LIBGL_ALWAYS_SOFTWARE=1;",
                f"export GZ_PARTITION={shlex.quote(self._gz_partition(sim))};",
                f"cd {shlex.quote(rootfs_dir)} &&",
                'if [ -f "./gz_env.sh" ]; then . ./gz_env.sh; fi &&',
                "exec",
                shlex.quote(self._runtime.wsl_gz_bin),
                "sim",
                "-r",
                "-s",
                "--headless-rendering",
                shlex.quote(world_file),
            ]
        )
        return await self._create_subprocess(
            self._runtime.wsl_prefix() + ["bash", "-lc", bash_cmd],
            stdout_log_path=sim.gz_stdout_log,
            stderr_log_path=sim.gz_stderr_log,
        )

    async def _create_wsl_px4_process(self, sim: SimProcess) -> asyncio.subprocess.Process | None:
        px4_dir = self._runtime.px4_dir.rstrip("/")
        rootfs_dir = self._rootfs_dir()
        romfs_dir = f"{px4_dir}/ROMFS/px4fmu_common"
        common = [
            "export PX4_SIM_HOSTNAME=localhost;",
            f"export MAVLINK_UDP_PORT={sim.mavlink_port};",
            f"export DRONEPROMPT_MAVSDK_PORT_REMOTE={sim.sitl_port};",
            f"export PX4_GZ_WORLD={shlex.quote(self._world_name_for_px4(sim))};",
            "export PX4_GZ_STANDALONE=1;",
            f"export GZ_PARTITION={shlex.quote(self._gz_partition(sim))};",
        ]
        if sim.flight_bundle is not None:
            workdir = self._px4_working_dir(sim)
            exports = [
                f"export {name}={shlex.quote(value)};"
                for name, value in self._generated_px4_env(sim).items()
            ]
            bash_cmd = " ".join(
                [
                    *common,
                    *exports,
                    f"cd {shlex.quote(rootfs_dir)} &&",
                    'if [ -f "./gz_env.sh" ]; then . ./gz_env.sh; fi &&',
                    f"mkdir -p {shlex.quote(workdir)} && cd {shlex.quote(workdir)} &&",
                    "exec",
                    shlex.quote(f"{px4_dir}/build/px4_sitl_default/bin/px4"),
                    "-d",
                    shlex.quote(romfs_dir),
                    "-s",
                    "etc/init.d-posix/rcS",
                ]
            )
            return await self._create_subprocess(
                self._shell_prefix(sim) + ["bash", "-lc", bash_cmd],
                stdout_log_path=sim.px4_stdout_log,
                stderr_log_path=sim.px4_stderr_log,
            )
        model = self._model_name(sim)
        bash_cmd = " ".join(
            [
                *common,
                f"export PX4_GZ_MODEL={shlex.quote(model)};",
                f"export PX4_SIM_MODEL={shlex.quote(self._px4_sim_model(sim))};",
                f"cd {shlex.quote(rootfs_dir)} &&",
                'if [ -f "./gz_env.sh" ]; then . ./gz_env.sh; fi &&',
                "exec",
                "../bin/px4",
                "-d",
                shlex.quote(romfs_dir),
                "-s",
                "etc/init.d-posix/rcS",
            ]
        )
        return await self._create_subprocess(
            self._shell_prefix(sim) + ["bash", "-lc", bash_cmd],
            stdout_log_path=sim.px4_stdout_log,
            stderr_log_path=sim.px4_stderr_log,
        )

    async def _prepare_wsl_px4_rootfs(self, sim: SimProcess) -> None:
        """Tune PX4's own MAVSDK link; utils/px4_mavlink_patch.py explains why.

        The Docker simulator runs the same module from its entrypoint, so both
        runtimes share one link contract.
        """
        sdk_host = settings.sitl_wsl_mavlink_target_host.strip() or "127.0.0.1"
        px4_dir = self._runtime.px4_dir.rstrip("/")
        source_path = f"{px4_dir}/ROMFS/px4fmu_common/init.d-posix/px4-rc.mavlink"
        target_path = f"{self._rootfs_dir()}/etc/init.d-posix/px4-rc.mavlink"
        module = Path(__file__).resolve().parents[1] / "utils" / "px4_mavlink_patch.py"
        # Windows paths need translating to WSL's /mnt/<drive> view; POSIX ones
        # pass through unchanged.
        drive, tail = os.path.splitdrive(str(module))
        module_path = (
            f"/mnt/{drive[0].lower()}{tail.replace(os.sep, '/')}"
            if drive
            else str(module).replace(os.sep, "/")
        )
        proc = await self._create_subprocess(
            self._runtime.wsl_prefix()
            + [
                self._runtime.wsl_python_bin,
                module_path,
                "--source",
                source_path,
                "--target",
                target_path,
                "--sdk-host",
                sdk_host,
                "--git-repo",
                px4_dir,
            ],
            stdout_log_path=sim.px4_stdout_log,
            stderr_log_path=sim.px4_stderr_log,
        )
        if proc is None:
            logger.warning(
                "Could not prepare WSL PX4 MAVLink runtime script",
                extra={"sim_id": sim.sim_id, "target_path": target_path},
            )
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(
                "Timed out while patching WSL PX4 MAVLink runtime script",
                extra={"sim_id": sim.sim_id, "target_path": target_path},
            )
            return
        if proc.returncode not in (0, None):
            logger.warning(
                "WSL PX4 MAVLink runtime patch exited non-zero",
                extra={
                    "sim_id": sim.sim_id,
                    "target_path": target_path,
                    "returncode": proc.returncode,
                    "px4_stderr_log": sim.px4_stderr_log,
                },
            )

    async def _create_subprocess(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdout_log_path: str | None = None,
        stderr_log_path: str | None = None,
    ) -> asyncio.subprocess.Process | None:
        merged_env = None
        if env:
            import os

            merged_env = {**os.environ, **env}
        stdout_handle = None
        stderr_handle = None
        try:
            if stdout_log_path:
                Path(stdout_log_path).parent.mkdir(parents=True, exist_ok=True)
                stdout_handle = open(stdout_log_path, "ab")
            if stderr_log_path:
                Path(stderr_log_path).parent.mkdir(parents=True, exist_ok=True)
                stderr_handle = open(stderr_log_path, "ab")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout_handle or asyncio.subprocess.DEVNULL,
                stderr=stderr_handle or asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=merged_env,
            )
            return proc
        except FileNotFoundError as exc:
            logger.warning("SITL dependency missing", extra={"cmd": cmd[:3], "error": str(exc)})
        except Exception as exc:
            logger.error("Failed to start SITL process", extra={"cmd": cmd[:3], "error": str(exc)})
        finally:
            if stdout_handle is not None:
                stdout_handle.close()
            if stderr_handle is not None:
                stderr_handle.close()
        return None

    async def _cleanup_wsl_sitl_processes(self) -> None:
        """Stop leftover simulators and wait until they are gone.

        pkill only sends the signal. A Gazebo server takes seconds to exit, and a
        new one started meanwhile shares its world name: a hexacopter was spawned
        into the old world and never left the ground while PX4 drove the new one.
        """
        try:
            # On stdin, not as an argument: wsl.exe re-parses its command line and
            # mangled this script into a bash syntax error, so it killed nothing
            # and every run's Gazebo server stayed behind, orphaned, at full CPU.
            proc = await asyncio.create_subprocess_exec(
                *self._runtime.wsl_prefix(),
                "bash",
                "-s",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(
                proc.communicate(_SITL_CLEANUP_SCRIPT.encode("utf-8")), timeout=25.0
            )
            if proc.returncode:
                logger.warning(
                    "WSL SITL cleanup failed (exit %s): %s",
                    proc.returncode,
                    err.decode("utf-8", errors="replace")[-300:],
                )
        except Exception:
            logger.debug("Best-effort WSL SITL cleanup failed", exc_info=True)

    async def _wait_until_ready(self, sim: SimProcess, timeout_s: float = 30.0) -> None:
        """Wait until PX4 will actually accept commands, not merely until it exists.

        This used to return as soon as both processes had a live PID, which is
        true within milliseconds of spawning them. The mission then connected to
        a PX4 that was still bringing up its EKF and printing

            Preflight Fail: ekf2 missing data
            Preflight Fail: system power unavailable

        so arming came back COMMAND_DENIED, or the mission upload raced the
        mission manager and came back PROTOCOL_ERROR. Which of the two failed
        varied between otherwise identical runs — the signature of a race.

        PX4 announces readiness on its console, but that banner cannot be waited
        for here: one of its own preflight gates is "No connection to the GCS",
        so PX4 stays un-ready until a client connects — and the only client is
        the mission, which runs after this function returns. Blocking on the
        banner deadlocks; measured across runs, `partner IP` and
        `Ready for takeoff!` appear together or not at all.

        So this reports the banner when it is already present and moves on
        otherwise. The actual waiting belongs after the connection exists, in
        the mission's own arm retry.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s

        while loop.time() < deadline:
            if await self.is_running(sim.sim_id):
                sim.status = "running"
                logger.info(
                    "SITL processes are up",
                    extra={
                        "sim_id": sim.sim_id,
                        "runtime": self._runtime.mode,
                        # Usually false at this point — PX4 withholds readiness
                        # until the mission connects and satisfies its GCS check.
                        "px4_ready_banner": self._px4_reports_ready(sim),
                    },
                )
                return
            await asyncio.sleep(1.0)

        logger.warning(
            "Simulation not ready within timeout",
            extra={
                "sim_id": sim.sim_id,
                "runtime": self._runtime.mode,
                "px4_stderr_log": sim.px4_stderr_log,
                "gz_stderr_log": sim.gz_stderr_log,
            },
        )
        # Check whether the processes actually died (common when PX4/Gazebo
        # binaries are missing or WSL cannot start them).  Proceeding with
        # status="running" when both are dead produces cascading timeouts in
        # PX4Service.execute_mission that make the pipeline appear frozen.
        gz_alive = sim.gz_process is not None and sim.gz_process.returncode is None
        px4_alive = sim.px4_process is not None and sim.px4_process.returncode is None
        if not gz_alive and not px4_alive:
            sim.status = "failed"
            await self._kill_process(sim.gz_process, "Gazebo")
            await self._kill_process(sim.px4_process, "PX4 SITL")
            if self._runtime.mode == "wsl":
                await self._cleanup_wsl_sitl_processes()
            self._sims.pop(sim.sim_id, None)
            raise RuntimeError(
                "PX4/Gazebo SITL processes exited before becoming ready. "
                f"PX4 log: {sim.px4_stderr_log}; Gazebo log: {sim.gz_stderr_log}"
            )
        if require_real_sitl():
            sim.status = "failed"
            await self._kill_process(sim.gz_process, "Gazebo")
            await self._kill_process(sim.px4_process, "PX4 SITL")
            if self._runtime.mode == "wsl":
                await self._cleanup_wsl_sitl_processes()
            self._sims.pop(sim.sim_id, None)
            raise RuntimeError(
                "Real PX4/Gazebo SITL did not become ready before the timeout. "
                f"PX4 log: {sim.px4_stderr_log}; Gazebo log: {sim.gz_stderr_log}"
            )
        logger.warning(
            "SITL processes alive but not ready — continuing in best-effort mode",
            extra={"sim_id": sim.sim_id, "gz_alive": gz_alive, "px4_alive": px4_alive},
        )
        sim.status = "running"

    @staticmethod
    def _px4_reports_ready(sim: SimProcess) -> bool:
        """True once PX4's console announces it will accept an arm command.

        PX4 prints ``Ready for takeoff!`` from the commander module when its
        health-and-arming checks pass. The banner carries ANSI colour codes, so
        match on the text alone.
        """
        for path_str in (sim.px4_stdout_log, sim.px4_stderr_log):
            if not path_str:
                continue
            try:
                text = Path(path_str).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if PX4_READY_BANNER in text:
                return True
        return False

    @staticmethod
    def _prepare_log_paths(sim: SimProcess) -> None:
        artifact_dir = _SITL_LOG_ROOT / sim.sim_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        sim.artifact_dir = str(artifact_dir)
        sim.gz_stdout_log = str(artifact_dir / "gazebo.stdout.log")
        sim.gz_stderr_log = str(artifact_dir / "gazebo.stderr.log")
        sim.px4_stdout_log = str(artifact_dir / "px4.stdout.log")
        sim.px4_stderr_log = str(artifact_dir / "px4.stderr.log")

    def _rootfs_dir(self) -> str:
        return f"{self._runtime.px4_dir.rstrip('/')}/build/px4_sitl_default/rootfs"

    def _model_name(self, sim: SimProcess) -> str:
        model = str(sim.config.get("model", "x500")).strip() or "x500"
        return model[3:] if model.startswith("gz_") else model

    def _px4_sim_model(self, sim: SimProcess) -> str:
        model = self._model_name(sim)
        return model if model.startswith("gz_") else f"gz_{model}"

    @staticmethod
    def _world_name_for_px4(sim: SimProcess) -> str:
        return Path(sim.world_name).stem

    @staticmethod
    def _gz_partition(sim: SimProcess) -> str:
        """A gz-transport partition of this run's own: Gazebo, gz and PX4 share it,
        so a server left over from another run cannot answer them."""
        return f"eljazari_{sim.sim_id}"

    def _gz_runtime_env(self) -> dict[str, str]:
        px4_dir = self._runtime.px4_dir.rstrip("/")
        return {
            "PX4_GZ_MODELS": f"{px4_dir}/Tools/simulation/gz/models",
            "PX4_GZ_WORLDS": self._runtime.worlds_path.rstrip("/"),
            "PX4_GZ_PLUGINS": f"{px4_dir}/build/px4_sitl_default/src/modules/simulation/gz_plugins",
            "PX4_GZ_SERVER_CONFIG": f"{px4_dir}/src/modules/simulation/gz_bridge/server.config",
            "GZ_SIM_RESOURCE_PATH": self._gz_resource_path(),
            "GZ_SIM_SYSTEM_PLUGIN_PATH": f"{px4_dir}/build/px4_sitl_default/src/modules/simulation/gz_plugins",
            "GZ_SIM_SERVER_CONFIG_PATH": (
                f"{px4_dir}/src/modules/simulation/gz_bridge/server.config"
            ),
        }

    def _gz_resource_path(self) -> str:
        worlds_path = self._runtime.worlds_path.rstrip("/")
        models_path = f"{self._runtime.px4_dir.rstrip('/')}/Tools/simulation/gz/models"
        return ":".join(part for part in [worlds_path, models_path] if part)

    @staticmethod
    async def _kill_process(
        proc: asyncio.subprocess.Process | None,
        name: str,
    ) -> None:
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                logger.warning("%s process did not exit after kill; abandoning", name)
        logger.debug("%s process terminated", name)

    @staticmethod
    def _select_world(drone_config: dict[str, Any], world: str | None) -> str:
        if world and world in _WORLD_PROFILES:
            return _WORLD_PROFILES[world]
        env = drone_config.get("environment", "default")
        return _WORLD_PROFILES.get(str(env), _WORLD_PROFILES["default"])
