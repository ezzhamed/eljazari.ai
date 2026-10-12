#!/usr/bin/env python3
"""Verify the simulation image the way the platform uses it, and report what held.

Two containers, one after the other (the image binds fixed MAVLink ports):

1. MAVSDK inside the container. PX4's startup script must complete, the
   entrypoint's MAVLink patch must be in place, the image's healthcheck command
   must pass, the x500 must take off, hover and land under MAVSDK, and killing
   PX4 must end the container instead of leaving Gazebo running alone. (The
   x500 airframe sets NAV_DLL_ACT=2, so PX4 only reports "Ready for takeoff"
   once a ground station such as MAVSDK is connected; boot is judged by the
   startup script, arming by the flight.)
2. MAVSDK outside the container (PX4_MAVLINK_TARGET_HOST=host.docker.internal).
   The entrypoint must resolve the name to the IP PX4 requires, and this host's
   GazeboService in docker mode must detect the container and receive PX4's
   heartbeat — the platform's own readiness path.

    python scripts/check_docker_sim.py --image eljazari-sim:14529d6 --out <dir>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

API_ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).with_name("sitl_hover_probe.py")
RC_MAVLINK = "/opt/px4/PX4-Autopilot/ROMFS/px4fmu_common/init.d-posix/px4-rc.mavlink"
HEALTHCHECK = "pgrep -x px4 >/dev/null && pgrep -f 'gz-sim-server|gz sim' >/dev/null"
MARKER = "Higher rates for MAVSDK clients"


def docker(*args: str, timeout: float = 120.0, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
    )


def pinned_px4_commit() -> str:
    """The PX4_REF the Dockerfile pins, i.e. the commit behind the WSL evidence."""
    dockerfile = API_ROOT.parents[1] / "infrastructure" / "Dockerfile.sim"
    match = re.search(r"^ARG PX4_REF=([0-9a-f]{40})$", dockerfile.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else ""


def wait_for_log(name: str, pattern: str, timeout_s: float) -> float | None:
    """Seconds until ``pattern`` appears in the container log, or None."""
    started = time.monotonic()
    regex = re.compile(pattern)
    while time.monotonic() - started < timeout_s:
        logs = docker("logs", name, timeout=30)
        if regex.search(logs.stdout + logs.stderr):
            return round(time.monotonic() - started, 1)
        state = docker("inspect", "-f", "{{.State.Running}}", name).stdout.strip()
        if state != "true":
            return None
        time.sleep(2)
    return None


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, passed: bool, **detail: Any) -> None:
        self.checks.append({"check": name, "passed": bool(passed), **detail})
        print(f"[{'PASS' if passed else 'FAIL'}] {name} {json.dumps(detail, default=str)}")

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(item["passed"] for item in self.checks)


def offboard_line(rc_text: str) -> str:
    return next(
        (
            line
            for line in rc_text.splitlines()
            if "$udp_offboard_port_local" in line and "start" in line
        ),
        "",
    )


def local_mavsdk_container(image: str, report: Report, boot_timeout_s: float) -> None:
    name = f"eljazari-sim-check-{uuid.uuid4().hex[:6]}"
    run = docker("run", "-d", "--init", "--name", name, image)
    report.add("container starts", run.returncode == 0, stderr=run.stderr.strip()[-400:])
    if run.returncode != 0:
        return
    try:
        ready = wait_for_log(name, r"Startup script returned successfully", boot_timeout_s)
        report.add("Gazebo up and PX4 startup script completes", ready is not None, seconds=ready)
        if ready is None:
            report.add(
                "boot log tail", False, log=docker("logs", "--tail", "40", name).stderr[-2000:]
            )
            return
        rc_text = docker("exec", name, "cat", RC_MAVLINK).stdout
        line = offboard_line(rc_text)
        report.add(
            "entrypoint patched one onboard link for MAVSDK",
            MARKER in rc_text
            and rc_text.count("$udp_offboard_port_local") >= 1
            and "-t " not in line,
            offboard_link=line.strip(),
            custom_links=rc_text.count("-m custom"),
        )
        health = docker("exec", name, "sh", "-c", HEALTHCHECK)
        report.add("image healthcheck command passes", health.returncode == 0)
        # The runtime image has no .git; PX4 compiles its commit into the binary.
        expected = pinned_px4_commit()
        embedded = docker(
            "exec",
            name,
            "sh",
            "-c",
            f"grep -a -o '{expected}' /opt/px4/PX4-Autopilot/build/px4_sitl_default/bin/px4",
        ).stdout.split()
        report.add(
            "PX4 binary is built from the pinned commit",
            bool(expected) and expected in embedded,
            expected=expected,
        )

        docker("cp", str(PROBE), f"{name}:/tmp/sitl_hover_probe.py", check=True)
        flight = docker(
            "exec",
            name,
            "python3",
            "/tmp/sitl_hover_probe.py",
            "udp://:14540",
            "--altitude-m",
            "5",
            "--hover-s",
            "15",
            timeout=400,
        )
        result_line = next(
            (ln for ln in flight.stdout.splitlines() if ln.startswith("PROBE_RESULT ")), ""
        )
        result = json.loads(result_line.removeprefix("PROBE_RESULT ")) if result_line else {}
        flew = (
            result.get("landed") is True
            and (result.get("hover_alt_rmse_m") or 99) <= 0.5
            and (result.get("hover_horizontal_max_drift_m") or 99) <= 1.0
        )
        report.add(
            "x500 takes off, hovers and lands under MAVSDK in the container",
            flew,
            result=result,
            stderr=flight.stderr.strip()[-600:] if not flew else None,
        )

        started = time.monotonic()
        docker("exec", name, "pkill", "-x", "px4")
        waited = docker("wait", name, timeout=60)
        running = docker("inspect", "-f", "{{.State.Running}}", name).stdout.strip()
        tail = docker("logs", "--tail", "5", name)
        report.add(
            "killing PX4 ends the container",
            running == "false"
            and "a simulation process exited" in tail.stderr
            and waited.stdout.strip() not in {"", "0"},
            exit_code=waited.stdout.strip(),
            seconds=round(time.monotonic() - started, 1),
        )
    finally:
        docker("rm", "-f", name)


async def host_readiness(timeout_s: float) -> tuple[bool, str]:
    for prefix in ("ELJAZARI_", "DRONEPROMPT_"):
        os.environ[f"{prefix}SITL_RUNTIME"] = "docker"
        os.environ[f"{prefix}FORCE_SITL"] = "docker"
        os.environ[f"{prefix}REQUIRE_REAL_SITL"] = "true"
    sys.path.insert(0, str(API_ROOT))
    from services.gazebo_service import GazeboService

    service = GazeboService()
    runtime = service._runtime
    try:
        sim_id = await service.launch_world({}, ready_timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 — reported as the check's result
        return False, f"{runtime.mode}: {exc}"
    try:
        return True, f"{runtime.mode}: {'; '.join(runtime.diagnostics)}"
    finally:
        await service.stop_world(sim_id)


def remote_mavsdk_container(image: str, report: Report, boot_timeout_s: float) -> None:
    name = f"eljazari-sim-check-{uuid.uuid4().hex[:6]}"
    run = docker(
        "run",
        "-d",
        "--init",
        "--name",
        name,
        "-e",
        "PX4_MAVLINK_TARGET_HOST=host.docker.internal",
        image,
    )
    report.add("remote-target container starts", run.returncode == 0, stderr=run.stderr[-400:])
    if run.returncode != 0:
        return
    try:
        resolved = wait_for_log(name, r"resolved host\.docker\.internal to \d+\.\d+\.\d+\.\d+", 60)
        ready = wait_for_log(name, r"Startup script returned successfully", boot_timeout_s)
        line = offboard_line(docker("exec", name, "cat", RC_MAVLINK).stdout)
        report.add(
            "hostname target resolved to the IPv4 literal PX4 requires",
            resolved is not None and re.search(r"-t \d+\.\d+\.\d+\.\d+ ", line) is not None,
            offboard_link=line.strip(),
            px4_ready_s=ready,
        )
        ok, detail = asyncio.run(host_readiness(60.0))
        report.add(
            "GazeboService (docker mode) detects the container and PX4's heartbeat",
            ok,
            detail=detail,
        )
    finally:
        docker("rm", "-f", name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--boot-timeout-s", type=float, default=240.0)
    args = parser.parse_args()
    report = Report()
    inspect = docker("image", "inspect", args.image, "--format", "{{.Id}} {{.Size}}")
    report.add(
        "image present", inspect.returncode == 0, image=args.image, id_size=inspect.stdout.strip()
    )
    if inspect.returncode == 0:
        local_mavsdk_container(args.image, report, args.boot_timeout_s)
        remote_mavsdk_container(args.image, report, args.boot_timeout_s)
    args.out.mkdir(parents=True, exist_ok=True)
    payload = {
        "image": args.image,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "passed": report.passed,
        "checks": report.checks,
    }
    (args.out / "docker_sim_check.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print("PASSED" if report.passed else "FAILED")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
