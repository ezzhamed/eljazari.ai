"""Trusted bootstrap for one generated mission inside the Linux sandbox.

Runs after the sandbox has dropped every capability. It lowers resource limits,
checks the mission file against the approved fingerprint, points MAVSDK at the
relayed Unix socket and only then imports the untrusted mission.

Exit codes: 0 mission succeeded, 1 mission reported failure, 2 mission raised
or timed out, 3 fingerprint mismatch.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import inspect
import logging
import resource
import sys
import traceback
from pathlib import Path
from typing import Any

MiB = 1024 * 1024
_LIMITS = {
    resource.RLIMIT_AS: 4096 * MiB,
    resource.RLIMIT_FSIZE: 64 * MiB,
    resource.RLIMIT_NOFILE: 256,
    resource.RLIMIT_NPROC: 128,
    resource.RLIMIT_CORE: 0,
}


def _apply_limits(cpu_s: int) -> None:
    for kind, value in {**_LIMITS, resource.RLIMIT_CPU: cpu_s}.items():
        _, hard = resource.getrlimit(kind)
        cap = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(kind, (cap, cap))


async def _invoke(func: Any, *args: Any) -> int:
    result = func(*args)
    if inspect.isawaitable(result):
        result = await result
    if result is None or result is True:
        return 0
    if result is False:
        return 1
    if isinstance(result, int):
        return result
    return 0 if result else 1


async def _run(mission: Path, system_cls: Any, timeout_s: float) -> int:
    from mavsdk_runtime_adapter import MAVSDKRuntimeAdapter

    spec = importlib.util.spec_from_file_location("droneprompt_mission", mission)
    if spec is None or spec.loader is None:
        raise RuntimeError("Mission module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "return_to_launch"):

        async def _return_to_launch(drone: Any) -> Any:
            return await drone.return_to_launch()

        setattr(module, "return_to_launch", _return_to_launch)
    if not hasattr(module, "emergency_stop"):

        async def _emergency_stop(drone: Any) -> Any:
            return await drone.emergency_stop()

        setattr(module, "emergency_stop", _emergency_stop)

    async def invoke() -> int:
        run_mission = getattr(module, "run_mission", None)
        if callable(run_mission):
            system = system_cls()
            await system.connect()
            return await _invoke(run_mission, MAVSDKRuntimeAdapter(system))
        main = getattr(module, "main", None)
        if callable(main):
            return await _invoke(main)
        raise RuntimeError("Mission exposes neither run_mission() nor main()")

    return await asyncio.wait_for(invoke(), timeout=timeout_s)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mission", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--grpc-address", required=True)
    parser.add_argument("--grpc-port", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    _apply_limits(int(args.timeout) + 60)

    mission = Path(args.mission)
    if hashlib.sha256(mission.read_bytes()).hexdigest() != args.sha256:
        print("sandbox: mission fingerprint mismatch; refusing to execute", file=sys.stderr)
        return 3
    sys.path.insert(0, str(mission.parent))
    # Without a handler, the mission's logger.info() calls vanish and a failed
    # flight has no explanation. The runner relays these lines, prefixed.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )

    # Importing mavsdk may install uvloop's policy; do it before any loop exists.
    import mavsdk

    address, port = args.grpc_address, args.grpc_port

    class SandboxSystem(mavsdk.System):  # type: ignore[misc, valid-type]
        """Every System in the mission uses the relayed mavsdk_server socket."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            super().__init__(mavsdk_server_address=address, port=port)

        async def connect(self, system_address: str | None = None) -> None:
            await super().connect()

    mavsdk.System = SandboxSystem
    try:
        return asyncio.run(_run(mission, SandboxSystem, args.timeout))
    except Exception:  # noqa: BLE001 - reported through the exit code
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
