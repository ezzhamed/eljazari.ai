"""Fly a generated multirotor in real PX4/Gazebo SITL through the project's own path.

Builds a resolved spec from catalog parts (or loads one), writes its flight
bundle, and runs the same SimulationTestAgent.run_single call the graph uses:
Gazebo spawn and attestation, PX4 parameter readback, mission execution,
telemetry and the deterministic acceptance gate. No LLM is involved; the
mission is the code agent's deterministic template: a stationary hover, or a
square with --square-m.

Run from apps/api, for example:

    python scripts/check_generated_model_sitl.py --payload-g 125 --payload-g 400

The result is SITL evidence for a catalog-estimate model only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
os.environ.setdefault("DRONEPROMPT_REQUIRE_REAL_SITL", "true")

REFERENCE_PARTS = {
    # A documented combination: T-MOTOR publishes MN3110 KV700 with the P12*4 at
    # 14.8 V. SITL flies documented propulsion data only.
    "motor": "tmotor_mn3110_700kv",
    "propeller": "tmotor_p12_4",
    "battery": "tattu_1500_4s_75c",
    "esc": "holybro_tekko32_f3_4in1",
    "flight_controller": "holybro_durandal_h7",
    "frame": "holybro_x500_v2",
}


# Documented hexa and octo airframes (T-MOTOR tests at 22.2 V on Tarot frames).
MULTIROTOR_PARTS = {
    "hexacopter": {
        "motor": "tmotor_mn3508_380kv",
        "propeller": "tmotor_p13_44",
        "battery": "tattu_plus_10000_6s_25c",
        "esc": "holybro_tekko32_f3_4in1",
        "flight_controller": "holybro_durandal_h7",
        "frame": "tarot_680_pro_hex",
    },
    "octocopter": {
        "motor": "tmotor_mn4014_400kv",
        "propeller": "tmotor_p15_5",
        "battery": "tattu_plus_10000_6s_25c",
        "esc": "holybro_tekko32_f3_4in1",
        "flight_controller": "holybro_durandal_h7",
        "frame": "tarot_x8_tl8x000",
    },
}
_MOTORS = {"quadcopter": 4, "hexacopter": 6, "octocopter": 8}


def reference_spec(payload_g: float, topology: str = "quadcopter"):
    from agents.component_models import SelectedComponents
    from agents.design_models import (
        BatterySpec,
        DroneDesignTree,
        ElectronicsSpec,
        FrameSpec,
        PayloadSpec,
    )
    from pipelines.resolved_drone import resolve_drone
    from services.component_catalog import get_catalog

    catalog = get_catalog()
    chosen = REFERENCE_PARTS if topology == "quadcopter" else MULTIROTOR_PARTS[topology]
    count = _MOTORS[topology]
    kinds = {kind: getattr(catalog, kind)(part) for kind, part in chosen.items()}
    if count != 4:
        # A four-in-one ESC cannot drive six or eight motors: one per arm.
        kinds["esc"] = next(e for e in catalog.escs if e.channels == 1)
    parts = catalog.with_propulsion_test(SelectedComponents(**kinds, motor_count=count))
    design = DroneDesignTree(
        topology=topology,
        frame=FrameSpec(type="X", arm_length_mm=parts.frame.arm_length_mm),
        propulsion=[],
        battery=BatterySpec(type="LiPo", cells=4, capacity_mah=1500, weight_g=190.0),
        payload=PayloadSpec(max_weight_g=payload_g, mount_type="bottom"),
        electronics=ElectronicsSpec(flight_controller="PX4", gps=True, compass=True),
    )
    design.payload.installed_weight_g = payload_g
    return resolve_drone(design, parts)


def hover_code(altitude_m: float, hover_s: float) -> tuple[str, str]:
    from agents.code_agent import CodeGenerationAgent

    prompt = (
        f"In simulation, take off to {altitude_m:g} meters, hover for {hover_s:g} seconds and land."
    )
    agent = CodeGenerationAgent.__new__(CodeGenerationAgent)
    limits = dict(
        max_altitude_m=altitude_m, max_speed_ms=3, max_duration_s=120, geofence_radius_m=30
    )
    plan = agent._normalize_plan(prompt, limits, {})
    return prompt, agent._build_code(plan)


def square_code(side_m: float, altitude_m: float) -> tuple[str, str]:
    """The code agent's no-LLM path for a square: plan, render, post-process."""
    from agents.code_agent import CodeGenerationAgent

    prompt = (
        f"In simulation, take off to {altitude_m:g} meters, fly a {side_m:g} meter square and land."
    )
    agent = CodeGenerationAgent.__new__(CodeGenerationAgent)
    limits = dict(
        max_altitude_m=altitude_m, max_speed_ms=5, max_duration_s=180, geofence_radius_m=100
    )
    code = agent._fallback_result(prompt, limits, "")["code"]
    return prompt, agent._rewrite_to_real_mavsdk(
        code, prompt, enforce_geometry=False, max_speed_m_s=5.0
    )


HOLD_BAND_M = 0.2


def hover_metrics(points: list[dict], altitude_m: float) -> dict:
    """Stability once settled: the span from the first to the last sample within
    HOLD_BAND_M of the target. The climb before it and the descent after it are
    not part of the hold, and any excursion within the span counts."""
    near = [i for i, p in enumerate(points) if abs(p["alt_m"] - altitude_m) <= HOLD_BAND_M]
    if len(near) < 5:
        return {"hold_samples": len(near)}
    hold = points[near[0] : near[-1] + 1]
    alts = [p["alt_m"] for p in hold]
    drift = [
        math.hypot(p["north_m"] - hold[0]["north_m"], p["east_m"] - hold[0]["east_m"]) for p in hold
    ]
    return {
        "hold_samples": len(hold),
        "hold_duration_s": round(hold[-1]["timestamp_s"] - hold[0]["timestamp_s"], 2),
        "altitude_rmse_m": round(
            math.sqrt(statistics.fmean((a - altitude_m) ** 2 for a in alts)), 3
        ),
        "horizontal_drift_max_m": round(max(drift), 3),
        "roll_std_deg": round(statistics.pstdev(p["roll_deg"] for p in hold), 3),
        "pitch_std_deg": round(statistics.pstdev(p["pitch_deg"] for p in hold), 3),
        "tilt_max_deg": round(max(max(abs(p["roll_deg"]), abs(p["pitch_deg"])) for p in hold), 3),
        "peak_alt_m": round(max(p["alt_m"] for p in points), 3),
        "final_alt_m": round(points[-1]["alt_m"], 3),
    }


_ULOG_PROBE = """
import glob, json, statistics, sys
from pyulog import ULog
logs = sorted(glob.glob(sys.argv[1] + "/log/*/*.ulg"))
if not logs:
    print(json.dumps({"error": "no ulog"})); sys.exit()
log = ULog(logs[-1], ["hover_thrust_estimate", "actuator_motors"])
out = {"ulog": logs[-1]}
for d in log.data_list:
    if d.name == "hover_thrust_estimate":
        valid = [h for h, v in zip(d.data["hover_thrust"], d.data["valid"]) if v]
        out["hover_thrust_estimate_median"] = float(statistics.median(valid)) if valid else None
    if d.name == "actuator_motors":
        count = int(sys.argv[2])
        cols = [d.data[f"control[{i}]"] for i in range(count)]
        mean = [sum(c[j] for c in cols) / count for j in range(len(cols[0]))]
        airborne = [m for m in mean if m > 0.05]
        out["motor_command_median_airborne"] = float(statistics.median(airborne)) if airborne else None
print(json.dumps(out))
"""


async def ulog_hover_evidence(working_dir: str, motor_count: int = 4) -> dict:
    """PX4's own hover-thrust estimate and motor commands from the run's ULog."""
    from utils.sitl_runtime import resolve_sitl_runtime

    runtime = resolve_sitl_runtime()
    if runtime.mode == "docker":
        # The run's container, and the ULog in it, is removed when the run stops.
        return {"error": "ULog is not kept: the run's container was removed"}
    prefix = runtime.wsl_prefix() if runtime.mode == "wsl" else []
    proc = await asyncio.create_subprocess_exec(
        *prefix,
        "python3",
        "-c",
        _ULOG_PROBE,
        working_dir,
        str(motor_count),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    try:
        return json.loads(out.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": (err or out).decode(errors="replace")[-300:]}


async def fly(
    spec, out_dir: Path, altitude_m: float, hover_s: float, square_m: float | None = None
) -> dict:
    from agents.test_agent import SimulationTestAgent
    from pipelines.flight_model import verify_flight_bundle, write_flight_model
    from pipelines.simulation_acceptance import assess_simulation

    manifest = write_flight_model(spec, out_dir)
    bundle = verify_flight_bundle(manifest, spec.sha256)
    prompt, code = (
        square_code(square_m, altitude_m) if square_m else hover_code(altitude_m, hover_s)
    )
    started = time.monotonic()
    result = await SimulationTestAgent().run_single(
        code=code,
        mission_description=prompt,
        drone_config={
            "resolved_drone_sha256": spec.sha256,
            "flight_model_manifest_path": str(manifest),
        },
    )
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
        # What the gate compares the flight with: the design and the request.
        "resolved_drone_spec": spec.model_dump(mode="json"),
        "user_prompt": prompt,
        # The gate compares the flight with this code's mission.
        "generated_code": code,
    }
    model = json.loads((manifest.parent / "flight_model.json").read_text(encoding="utf-8"))
    (manifest.parent / "last_flight_telemetry.json").write_text(
        json.dumps(result["telemetry"]), encoding="utf-8"
    )
    attestation = result["model_attestation"] or {}
    ulog = (
        await ulog_hover_evidence(attestation["px4_working_dir"], spec.components.motor_count)
        if attestation.get("px4_working_dir")
        else {"error": "no PX4 working directory"}
    )
    return {
        "topology": spec.topology,
        "motor_count": spec.components.motor_count,
        "propulsion_source": json.loads(manifest.read_text(encoding="utf-8"))["propulsion_source"],
        "payload_g": spec.payload_g,
        "resolved_spec_sha256": spec.sha256,
        "bundle_sha256": bundle.bundle_sha256,
        "expected_total_mass_kg": bundle.total_mass_kg,
        "hover_thrust_fraction": round(model["hover_thrust_fraction"], 4),
        "px4_mpc_thr_hover": model["px4_params"]["MPC_THR_HOVER"],
        "rate_gain_scale": model["rate_gain_scale"],
        "attestation": result["model_attestation"],
        "runtime_mode": result["runtime_mode"],
        "telemetry_source": result["telemetry_source"],
        "telemetry_points": len(result["telemetry"]),
        "mission_started": result["mission_started"],
        "mission_completed": result["mission_completed"],
        "mission_error": result["mission_error"],
        "crash_detected": result["crash_detected"],
        "mission_isolation": result["mission_isolation"],
        "hover": hover_metrics(result["telemetry"], altitude_m),
        "px4_ulog": ulog,
        "acceptance": assess_simulation(state),
        "wall_time_s": round(time.monotonic() - started, 1),
        "artifacts": result["artifacts"],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--payload-g", type=float, action="append", help="repeatable")
    parser.add_argument("--resolved-spec", help="resolved_drone.json from a project instead")
    parser.add_argument("--topology", choices=sorted(_MOTORS), default="quadcopter")
    parser.add_argument("--altitude-m", type=float, default=5.0)
    parser.add_argument("--hover-s", type=float, default=15.0)
    parser.add_argument("--square-m", type=float, help="fly a square of this side instead")
    parser.add_argument(
        "--out", default=str(Path(tempfile.gettempdir()) / "droneprompt-flight-check")
    )
    args = parser.parse_args()
    out_dir = Path(args.out)
    if args.resolved_spec:
        from pipelines.resolved_drone import ResolvedDroneSpec

        data = json.loads(Path(args.resolved_spec).read_text(encoding="utf-8"))
        specs = [ResolvedDroneSpec.model_validate(data.get("spec", data))]
    else:
        specs = [reference_spec(p, args.topology) for p in (args.payload_g or [125.0])]
    reports = []
    for spec in specs:
        report = await fly(spec, out_dir, args.altitude_m, args.hover_s, args.square_m)
        reports.append(report)
        print(json.dumps(report, indent=2, default=str), flush=True)
    summary = out_dir / "flight_check_report.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(reports, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"REPORT {summary}")
    return 0 if all(r["acceptance"]["passed"] for r in reports) else 1


if __name__ == "__main__":
    try:
        # mavsdk installs uvloop's policy on import; a loop created before that
        # can no longer spawn subprocesses, so settle the policy first.
        import mavsdk  # noqa: F401
    except ImportError:
        pass
    raise SystemExit(asyncio.run(main()))
