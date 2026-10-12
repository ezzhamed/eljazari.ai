"""Simulation containers the API owns, one per run, for the docker SITL runtime.

The WSL runtime drives Gazebo, PX4 and the mission runner through ``wsl.exe``;
this does the same through ``docker exec`` on a container started for the run:

* the container starts only Gazebo (``SIM_MODE=gazebo``) with the run's flight
  bundle mounted read-only at ``/bundle``;
* the service spawns the aircraft, starts PX4, attests the model and reads back
  PX4's parameters inside it, exactly as under WSL;
* the mission runs in the Linux namespace sandbox inside the same container,
  whose seccomp profile allows the sandbox's namespace calls and nothing more
  (infrastructure/seccomp/sim-sandbox.json).

GazeboService and PX4Service share the run's container through this registry.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config.paths import PROJECT_ROOT
from config.settings import settings

logger = logging.getLogger(__name__)

CONTAINER_PX4_DIR = "/opt/px4/PX4-Autopilot"
CONTAINER_WORLDS = f"{CONTAINER_PX4_DIR}/Tools/simulation/gz/worlds"
CONTAINER_RUNNER = "/opt/eljazari/utils/wsl_mission_runner.py"
CONTAINER_PYTHON = "python3"
BUNDLE_MOUNT = "/bundle"
ROLE_LABEL = "ai.eljazari.role=px4-sitl"
_REPO_ROOT = PROJECT_ROOT

_containers: dict[str, str] = {}


def container_name(sim_id: str) -> str:
    return f"eljazari-sim-{sim_id}"


def register(sim_id: str, container: str) -> None:
    _containers[sim_id] = container


def lookup(sim_id: str) -> str | None:
    return _containers.get(sim_id)


def forget(sim_id: str) -> None:
    _containers.pop(sim_id, None)


def exec_prefix(container: str, *, stdin: bool = False) -> list[str]:
    return ["docker", "exec", *(["-i"] if stdin else []), container]


def seccomp_profile() -> Path:
    configured = (settings.sitl_docker_seccomp_profile or "").strip()
    path = (
        Path(configured) if configured else _REPO_ROOT / "infrastructure/seccomp/sim-sandbox.json"
    )
    if not path.is_file():
        raise RuntimeError(f"Simulation seccomp profile not found: {path}")
    return path


def run_arguments(sim_id: str, world: str, bundle_dir: str | None) -> list[str]:
    """``docker run`` arguments for one run's Gazebo-only simulation container."""
    # No --rm: a container that exits keeps its logs until stop() or remove_stale()
    # removes it, so a failed start can say why.
    arguments = [
        "docker",
        "run",
        "-d",
        "--init",
        "--name",
        container_name(sim_id),
        "--label",
        ROLE_LABEL,
        # Namespace calls for the mission sandbox; still no CAP_SYS_ADMIN.
        "--security-opt",
        f"seccomp={seccomp_profile()}",
        "-e",
        "SIM_MODE=gazebo",
        "-e",
        f"SIM_WORLD={world}",
        "-e",
        f"GZ_PARTITION=eljazari_{sim_id}",
    ]
    if bundle_dir:
        arguments += ["-v", f"{Path(bundle_dir).resolve()}:{BUNDLE_MOUNT}:ro"]
    return [*arguments, settings.sitl_docker_image]


async def _docker(*args: str, timeout_s: float = 60.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "timed out"
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


async def start(sim_id: str, world: str, bundle_dir: str | None) -> str:
    """Start the run's container and wait until its Gazebo world is up."""
    name = container_name(sim_id)
    code, out = await _docker(*run_arguments(sim_id, world, bundle_dir)[1:], timeout_s=120)
    if code != 0:
        raise RuntimeError(f"docker run failed for the simulation container: {out[-400:]}")
    register(sim_id, name)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 120
    while loop.time() < deadline:
        code, out = await _docker("logs", name, timeout_s=20)
        if "gazebo ready" in out:
            return name
        state = (await _docker("inspect", "-f", "{{.State.Running}}", name))[1]
        if state != "true":
            break
        await asyncio.sleep(1.0)
    _, logs = await _docker("logs", "--tail", "30", name, timeout_s=20)
    await stop(sim_id)
    raise RuntimeError(f"Simulation container did not bring Gazebo up: {logs[-600:]}")


async def is_running(sim_id: str) -> bool:
    name = lookup(sim_id)
    if name is None:
        return False
    return (await _docker("inspect", "-f", "{{.State.Running}}", name))[1] == "true"


async def remove_stale() -> None:
    """Remove simulation containers a crashed run left behind.

    Called under the one-simulator-per-host lock, so any labelled container that
    is still here belongs to no live run.
    """
    code, out = await _docker("ps", "-aq", "--filter", f"label={ROLE_LABEL}", timeout_s=20)
    stale = out.split() if code == 0 else []
    if stale:
        logger.warning("Removing %d leftover simulation container(s)", len(stale))
        await _docker("rm", "-f", *stale, timeout_s=60)
    _containers.clear()


async def stop(sim_id: str) -> None:
    name = lookup(sim_id) or container_name(sim_id)
    code, out = await _docker("rm", "-f", name, timeout_s=60)
    if code != 0 and "No such container" not in out:
        logger.warning("Could not remove simulation container %s: %s", name, out[-200:])
    forget(sim_id)
