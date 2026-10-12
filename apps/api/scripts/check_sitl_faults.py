"""Inject faults into real PX4/Gazebo SITL and check that nothing passes.

The engineering review's negative protocol (section 6.4): under a fault the
correct outcome is a rejection or an understood failure, not a flight that
recovers at any cost. Each scenario flies the code agent's own 20 m square at
10 m with a generated aircraft, through the same SimulationTestAgent.run_single
call as the graph. It injects one fault a few seconds into the mission and
records what PX4 did, the acceptance verdict and whether a simulator process
survived the run.

======== ===================================================================
motor    ``failure motor off -i 1``, with SYS_FAILURE_EN and CA_FAILURE_MODE=1 set
         at runtime: PX4 applies an injected motor stop only when
         CA_FAILURE_MODE > 0, and its default 0 flew a full mission regardless
battery  ``failure battery off``: the simulated battery reads empty
gps      ``param set EKF2_GPS_CTRL 0``: the estimator stops fusing GPS. The
         ``failure gps`` injection has no handler under Gazebo in this PX4.
link     ``mavlink stop-all``: the vehicle's MAVLink link to the runner ends
px4      SIGKILL of the PX4 daemon
geofence the code's geofence (10 m) is smaller than its square (20 m)
======== ===================================================================

A scenario passes when the gate rejects the run with at least one reason and no
Gazebo or PX4 process is left. Run from apps/api, for example:

    python scripts/check_sitl_faults.py --scenario motor --scenario battery
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
os.environ.setdefault("DRONEPROMPT_REQUIRE_REAL_SITL", "true")

from scripts.check_generated_model_sitl import reference_spec, square_code  # noqa: E402

ENABLE = ("param", "set", "SYS_FAILURE_EN", "1")
SCENARIOS: dict[str, list[tuple[str, ...]]] = {
    "motor": [
        ENABLE,
        ("param", "set", "CA_FAILURE_MODE", "1"),
        ("failure", "motor", "off", "-i", "1"),
    ],
    "battery": [ENABLE, ("failure", "battery", "off")],
    "gps": [("param", "set", "EKF2_GPS_CTRL", "0")],
    "link": [("mavlink", "stop-all")],
    "px4": [("kill",)],
    "geofence": [],
}
SQUARE_M = 20.0
ALTITUDE_M = 10.0
SMALL_GEOFENCE_M = 10.0


def _modes(points: list[dict]) -> list[list]:
    """Flight-mode sequence as [mode, first time seen]."""
    sequence: list[list] = []
    for p in points:
        mode = str(p.get("flight_mode") or "")
        if not sequence or sequence[-1][0] != mode:
            sequence.append([mode, p.get("timestamp_s")])
    return sequence


async def _leftovers(runtime: str) -> dict:
    """Simulator processes or containers still alive after the run."""
    from utils.sitl_runtime import resolve_sitl_runtime

    if runtime == "docker":
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-q", "--filter", "name=eljazari-sim-", stdout=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        return {"containers": len(out.split())}
    prefix = resolve_sitl_runtime().wsl_prefix() if runtime == "wsl" else []
    proc = await asyncio.create_subprocess_exec(
        *prefix,
        "bash",
        "-s",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    script = "echo $(pgrep -fc '[g]z sim') $(pgrep -fc '[b]in/px4 ')"
    out, _ = await proc.communicate(script.encode())
    gz, px4 = (out.decode().split() + ["?", "?"])[:2]
    return {"gz": gz, "px4": px4}


async def run_scenario(name: str, spec, out_dir: Path, delay_s: float) -> dict:
    from agents.test_agent import SimulationTestAgent
    from pipelines.flight_model import verify_flight_bundle, write_flight_model
    from pipelines.simulation_acceptance import assess_simulation

    manifest = write_flight_model(spec, out_dir)
    bundle = verify_flight_bundle(manifest, spec.sha256)
    prompt, code = square_code(SQUARE_M, ALTITUDE_M)
    if name == "geofence":
        code, count = re.subn(
            r"upload_circular_geofence\(drone, radius_m=[0-9.]+\)",
            f"upload_circular_geofence(drone, radius_m={SMALL_GEOFENCE_M:.2f})",
            code,
        )
        assert count == 1, "the template's geofence call was not found"

    agent = SimulationTestAgent()
    loop = asyncio.get_running_loop()
    injection: dict = {"steps": []}

    async def inject() -> None:
        await asyncio.sleep(delay_s)
        sim_ids = agent.gazebo.active_sim_ids()
        if not sim_ids:
            injection["error"] = "no running simulation to inject into"
            return
        for step in SCENARIOS[name]:
            if step == ("kill",):
                exit_code, output = await agent.gazebo.kill_px4(sim_ids[0])
            else:
                exit_code, output = await agent.gazebo.px4_command(sim_ids[0], *step)
            injection["steps"].append(
                {"command": " ".join(step), "exit": exit_code, "output": output.strip()[-300:]}
            )
        injection["done_at_wall_s"] = round(time.monotonic() - started, 1)

    def on_point(point: dict) -> None:
        mode = str(point.get("flight_mode") or "")
        if SCENARIOS[name] and "task" not in injection and mode.endswith("MISSION"):
            injection["mission_seen_at_s"] = point.get("timestamp_s")
            injection["task"] = loop.create_task(inject())

    started = time.monotonic()
    result = await agent.run_single(
        code=code,
        mission_description=prompt,
        on_point=on_point,
        drone_config={
            "resolved_drone_sha256": spec.sha256,
            "flight_model_manifest_path": str(manifest),
        },
    )
    task = injection.pop("task", None)
    if task is not None:
        await asyncio.wait({task}, timeout=30)
    state = {
        "simulation_status": "complete",
        "runtime_mode": result["runtime_mode"],
        "telemetry_source": result["telemetry_source"],
        "mission_started": result["mission_started"],
        "mission_completed": result["mission_completed"],
        "mission_error": result["mission_error"],
        "crash_detected": result["crash_detected"],
        "geofence_violation": result["geofence_violation"],
        "telemetry_data": result["telemetry"],
        "resolved_drone_sha256": spec.sha256,
        "flight_model_bundle_sha256": bundle.bundle_sha256,
        "simulation_model_attestation": result["model_attestation"],
        "mission_isolation": result["mission_isolation"],
        "resolved_drone_spec": spec.model_dump(mode="json"),
        "user_prompt": prompt,
        "generated_code": code,
    }
    acceptance = assess_simulation(state)
    points = result["telemetry"]
    leftovers = await _leftovers(result["runtime_mode"])
    injected = name == "geofence" or (
        bool(injection["steps"]) and all(step["exit"] == 0 for step in injection["steps"])
    )
    clean = all(str(v) == "0" for v in leftovers.values())
    return {
        "scenario": name,
        "topology": spec.topology,
        "injection": injection,
        "injected": injected,
        "flight_modes": _modes(points),
        "final_alt_m": round(points[-1]["alt_m"], 2) if points else None,
        "telemetry_points": len(points),
        "telemetry_source": result["telemetry_source"],
        "mission_completed": result["mission_completed"],
        "mission_error": result["mission_error"],
        "crash_detected": result["crash_detected"],
        "geofence_violation": result["geofence_violation"],
        "acceptance_passed": acceptance["passed"],
        "reasons": acceptance["reasons"],
        "leftovers": leftovers,
        "wall_time_s": round(time.monotonic() - started, 1),
        # Correct outcome: injected, rejected with a stated reason, nothing left running.
        "ok": injected and not acceptance["passed"] and bool(acceptance["reasons"]) and clean,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", action="append", choices=sorted(SCENARIOS))
    parser.add_argument("--topology", default="quadcopter")
    parser.add_argument("--payload-g", type=float, default=125.0)
    parser.add_argument("--delay-s", type=float, default=4.0, help="seconds into the mission")
    parser.add_argument(
        "--out", default=str(Path(tempfile.gettempdir()) / "droneprompt-fault-check")
    )
    args = parser.parse_args()
    spec = reference_spec(args.payload_g, args.topology)
    reports = []
    for name in args.scenario or sorted(SCENARIOS):
        report = await run_scenario(name, spec, Path(args.out) / name, args.delay_s)
        reports.append(report)
        print(json.dumps(report, default=str), flush=True)
    out = Path(args.out) / "fault_check_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(reports, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"REPORT {out}")
    return 0 if all(r["ok"] for r in reports) else 1


if __name__ == "__main__":
    try:
        # mavsdk installs uvloop's policy on import; settle it before the loop exists.
        import mavsdk  # noqa: F401
    except ImportError:
        pass
    raise SystemExit(asyncio.run(main()))
