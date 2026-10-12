#!/usr/bin/env python3
"""
eljazari.ai — Interactive/Automated Multi-Agent Command Line Interface (CLI)

Enables end-to-end execution of the multi-agent pipeline from the terminal,
bypassing the web service dependencies (PostgreSQL, Celery, Redis, Next.js).
Uses LangGraph MemorySaver for in-memory checkpointing.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Inject current directory to Python Path to ensure absolute imports function correctly
API_DIR = Path(__file__).parent.resolve()
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

# Standalone CLI: no database, and no secret to configure. Models, Ollama URL and
# RAG mode are left to `.env` and the settings defaults. An environment variable
# set here would outrank `.env`, so the CLI asked for models the Ollama bootstrap
# had never pulled.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("API_SECRET_KEY", "cli-default-secret-key-32-chars-minimum!!")

from agents.orchestrator import Orchestrator
from config.settings import settings
from graph.state import initial_state
from graph.workflow import workflow  # Sync dev workflow with MemorySaver

SEP = "=" * 80
LINE = "-" * 80


def print_header(title: str):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(f"{SEP}\n")


def format_design_tree(design: dict) -> str:
    """Pretty-format the drone design tree for CLI view."""
    lines = []
    lines.append(f"Topology  : {design.get('topology', 'N/A')}")
    lines.append(f"Use Case  : {design.get('use_case', 'N/A')}")

    frame = design.get("frame", {})
    lines.append(
        f"Frame     : {frame.get('type', 'N/A')}-frame / {frame.get('arm_length_mm', 'N/A')}mm arm / {frame.get('material', 'N/A')}"
    )

    battery = design.get("battery", {})
    lines.append(
        f"Battery   : {battery.get('cells', 'N/A')}S / {battery.get('capacity_mah', 'N/A')}mAh / {battery.get('weight_g', 'N/A')}g"
    )

    prop = design.get("propulsion", [])
    if prop:
        lines.append(f"Motors    : {prop[0].get('motor', 'N/A')} (x{len(prop)})")
        lines.append(f"Props     : {prop[0].get('propeller', 'N/A')}")
    else:
        lines.append("Motors    : None")

    el = design.get("electronics", {})
    lines.append(f"FC / GPS  : {el.get('flight_controller', 'N/A')} (GPS: {el.get('gps', False)})")

    pay = design.get("payload", {})
    lines.append(f"Payload   : {pay.get('installed_weight_g', pay.get('max_weight_g', 0))}g")
    return "\n".join("    " + line for line in lines)


async def run_cli_pipeline(args: argparse.Namespace) -> int:
    t_start = time.monotonic()

    prompt = args.prompt.strip()
    if not prompt:
        print("Error: Prompt cannot be empty.")
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Configure safety limits and default models via env / settings overrides
    if args.design_model:
        os.environ["OLLAMA_DESIGN_MODEL"] = args.design_model
    if args.code_model:
        os.environ["OLLAMA_CODE_MODEL"] = args.code_model
    if args.eval_model:
        os.environ["OLLAMA_EVAL_MODEL"] = args.eval_model
    if args.max_speed:
        os.environ["MAX_SPEED_MS"] = str(args.max_speed)
    if args.max_weight:
        os.environ["MAX_WEIGHT_LIMIT"] = str(args.max_weight)

    # Reload/apply settings to make sure downstream agents read overrides
    settings.ollama_design_model = os.getenv("OLLAMA_DESIGN_MODEL", settings.ollama_design_model)
    settings.ollama_code_model = os.getenv("OLLAMA_CODE_MODEL", settings.ollama_code_model)
    settings.ollama_eval_model = os.getenv("OLLAMA_EVAL_MODEL", settings.ollama_eval_model)
    settings.max_speed_ms = float(os.getenv("MAX_SPEED_MS", settings.max_speed_ms))

    # Initialize Orchestrator. We force use_postgres=False to run in memory
    orch = Orchestrator(use_postgres=False)

    project_id = "cli-" + os.urandom(4).hex()
    config = {"configurable": {"thread_id": project_id}}

    print_header(f"DRONEPROMPT CLI — Project ID: {project_id}")
    print("User Prompt:")
    print(f'  "{prompt}"')
    print(f"\nOutput Directory: {output_dir.absolute()}")
    print(f"Auto-Approve    : {args.auto_approve}")
    print("Models Used     :")
    print(f"  • Design: {settings.ollama_design_model}")
    print(f"  • Code  : {settings.ollama_code_model}")
    print(f"  • Eval  : {settings.ollama_eval_model}")
    print(LINE)

    # 1. Start pipeline execution
    print("\n[PHASE] Starting pipeline execution...")

    # We run the graph stream directly
    state = initial_state(prompt, project_id, max_iterations=args.max_sim_iter)

    # In-memory graph runner
    graph = workflow

    async def process_updates(stream):
        async for raw_event in stream:
            node_name, node_updates = next(iter(raw_event.items()))
            print(f"  [OK] Completed node: {node_name}")

            # Print feedback/warnings from the nodes if available
            if node_name == "parse_prompt":
                print("       Prompt parsed successfully.")
            elif node_name == "generate_design":
                if "design_tree" in node_updates:
                    print("       Design specification generated.")
            elif node_name == "select_components":
                if "selected_components" in node_updates:
                    c = node_updates["selected_components"]
                    print(
                        f"       Components chosen: Motor = {c.get('motor', {}).get('model_name', 'N/A')}, Battery = {c.get('battery', {}).get('model_name', 'N/A')}"
                    )
            elif node_name == "convert_to_urdf":
                print("       URDF files generated successfully.")
            elif node_name == "generate_code":
                print("       PX4 MAVSDK Mission code written.")
            elif node_name == "safety_check":
                passed = node_updates.get("safety_passed", False)
                issues = node_updates.get("safety_issues", [])
                if passed:
                    print("       Safety check PASSED.")
                else:
                    print(f"       Safety check FAILED with {len(issues)} issues:")
                    for iss in issues:
                        print(f"         - {iss}")
            elif node_name == "run_simulation":
                comp = node_updates.get("mission_completed", False)
                crash = node_updates.get("crash_detected", False)
                print(
                    f"       Simulation finished. Mission Completed: {comp}, Crash Detected: {crash}"
                )
            elif node_name == "evaluate_simulation":
                print(
                    f"       Evaluation Score: {node_updates.get('simulation_score')}/100. Passed: {node_updates.get('simulation_success')}"
                )
            elif node_name == "generate_monitor_report":
                print("       Flight performance monitor analysis compiled.")

    # Start initial stream
    stream = graph.astream(state, config=config, stream_mode="updates")
    await process_updates(stream)

    # Check loop until completion
    while True:
        current = await graph.aget_state(config)
        if not current.next:
            break

        interrupted_node = list(current.next)[0]
        print(f"\n{LINE}")
        print(f"[GATEWAY] Human Review Gate Interrupted: {interrupted_node}")
        print(LINE)

        if args.auto_approve:
            print("Auto-Approving phase automatically...")
            await orch.approve(project_id, approved=True)
            stream = graph.astream(None, config=config, stream_mode="updates")
            await process_updates(stream)
            continue

        # Interactive review prompts
        if interrupted_node == "human_review_design":
            design_tree = current.values.get("design_tree") or {}
            perf = current.values.get("performance_estimate") or {}
            print("\nGenerated Design Tree for Review:")
            print(format_design_tree(design_tree))
            print("\nEstimated Performance:")
            print(f"  • Total Weight  : {perf.get('total_weight_g', 0.0)} g")
            print(f"  • Hover Time    : {round(perf.get('hover_time_s', 0.0) / 60.0, 1)} min")
            print(f"  • Max Speed     : {perf.get('max_speed_ms', 0.0)} m/s")

            while True:
                choice = (
                    input(
                        "\nApprove design? [y = approve, n = reject, m = modify with feedback, q = quit]: "
                    )
                    .strip()
                    .lower()
                )
                if choice == "y":
                    await orch.approve(project_id, phase="design", approved=True)
                    print("[CLI] Design approved. Resuming pipeline...")
                    break
                elif choice == "n":
                    await orch.reject(
                        project_id, phase="design", feedback="Design rejected by CLI user."
                    )
                    print("[CLI] Design rejected. Re-running design stage...")
                    break
                elif choice == "m":
                    fb = input("Enter your modification instructions: ").strip()
                    await orch.reject(project_id, phase="design", feedback=fb)
                    print(f'[CLI] Submitting feedback: "{fb}". Regenerating...')
                    break
                elif choice == "q":
                    print("[CLI] Exiting pipeline.")
                    return 0
                else:
                    print("Invalid choice. Please enter y, n, m, or q.")

        elif interrupted_node == "human_review_code":
            code = current.values.get("generated_code") or ""
            print("\nGenerated Mission Code Preview (First 40 lines):")
            lines = code.splitlines()
            for idx, line in enumerate(lines[:40], 1):
                print(f"  {idx:02d} | {line}")
            if len(lines) > 40:
                print(f"  ... ({len(lines) - 40} more lines)")

            while True:
                choice = (
                    input(
                        "\nApprove mission code? [y = approve, n = reject, m = modify with feedback, q = quit]: "
                    )
                    .strip()
                    .lower()
                )
                if choice == "y":
                    await orch.approve(project_id, phase="code", approved=True)
                    print("[CLI] Mission code approved. Resuming pipeline...")
                    break
                elif choice == "n":
                    await orch.reject(
                        project_id, phase="code", feedback="Code rejected by CLI user."
                    )
                    print("[CLI] Code rejected. Re-running code generation stage...")
                    break
                elif choice == "m":
                    fb = input("Enter your modifications instructions for the code: ").strip()
                    await orch.reject(project_id, phase="code", feedback=fb)
                    print(f'[CLI] Submitting feedback: "{fb}". Regenerating code...')
                    break
                elif choice == "q":
                    print("[CLI] Exiting pipeline.")
                    return 0
                else:
                    print("Invalid choice. Please enter y, n, m, or q.")

        # Resume the stream
        stream = graph.astream(None, config=config, stream_mode="updates")
        await process_updates(stream)

    # 3. Export all results to output_dir
    final_state = await graph.aget_state(config)
    values = final_state.values

    # Save files
    # Design Spec
    design_file = output_dir / "design_spec.json"
    design_tree = values.get("design_tree") or {}
    design_file.write_text(json.dumps(design_tree, indent=2), encoding="utf-8")

    # URDF
    urdf_content = ""
    urdf_path_in_state = values.get("urdf_path")
    if urdf_path_in_state and Path(urdf_path_in_state).exists():
        urdf_content = Path(urdf_path_in_state).read_text(encoding="utf-8")
        (output_dir / "model.urdf").write_text(urdf_content, encoding="utf-8")
        print(f"  [EXPORT] 3D Model Saved -> {output_dir / 'model.urdf'}")

    # Code
    code_content = values.get("generated_code") or ""
    if code_content:
        (output_dir / "mission_safe.py").write_text(code_content, encoding="utf-8")
        print(f"  [EXPORT] Injected Code Saved -> {output_dir / 'mission_safe.py'}")

    # Deliverables (BOM, Assembly Guide, checklist, report)
    # We call Phase 14 deliverables generator manually if we have final state
    report_payload = values.get("monitor_report") or {}
    try:
        from pipelines.engineering_deliverables import (
            ComponentLine,
            DeliverableBundle,
            render_all_deliverables,
        )

        frame = design_tree.get("frame") or {}
        battery = design_tree.get("battery") or {}
        topology = design_tree.get("topology", "quadcopter")

        components = []
        cells = int(battery.get("cells") or 4)
        capacity = float(battery.get("capacity_mah") or 2200)
        components.append(
            ComponentLine(
                category="Frame",
                part_name=f"{topology} {frame.get('type', 'X')}-frame ({frame.get('arm_length_mm', 150)}mm arms)",
                qty=1,
                unit_price_usd=45.0,
                weight_g=float(frame.get("weight_g") or 150.0),
            )
        )
        components.append(
            ComponentLine(
                category="Battery",
                part_name=f"LiPo {cells}S {capacity:.0f}mAh ({battery.get('weight_g', 220)}g)",
                qty=1,
                unit_price_usd=35.0,
                weight_g=float(battery.get("weight_g") or 220.0),
            )
        )

        perf = values.get("performance_estimate") or {}
        hover_time_s = perf.get("hover_time_s")
        hover_endurance_min = float(hover_time_s) / 60.0 if hover_time_s else 8.0
        max_speed_ms = float(perf.get("max_speed_ms") or 25.0)

        bundle = DeliverableBundle(
            project_id=project_id,
            project_name=f"Drone-{project_id[:8]}",
            mission_prompt=prompt,
            frame_type=str(frame.get("type") or "X"),
            arm_length_mm=float(frame.get("arm_length_mm") or 150),
            total_weight_g=float(perf.get("total_weight_g") or 780.0),
            battery_summary=f"{cells}S {capacity:.0f}mAh LiPo",
            components=components,
            flight_quality_score=float(report_payload.get("overall_score") or 90.0),
            flight_quality_grade=str(report_payload.get("grade") or "A"),
            hover_endurance_min=hover_endurance_min,
            max_speed_ms=max_speed_ms,
            urdf_path="model.urdf",
            mission_code_path="mission_safe.py",
        )

        deliverables = render_all_deliverables(bundle)
        for name, content in deliverables.items():
            (output_dir / name).write_text(content, encoding="utf-8")
            print(f"  [EXPORT] Deliverable Saved  -> {output_dir / name}")

    except Exception as e:
        print(f"  [WARNING] Could not export engineering deliverables: {e}")

    # Write JSON results summary
    summary = {
        "project_id": project_id,
        "prompt": prompt,
        "duration_seconds": round(time.monotonic() - t_start, 1),
        "success": not bool(values.get("errors")),
        "design": design_tree,
        "performance": values.get("performance_estimate"),
        "safety_passed": values.get("safety_passed"),
        "safety_report": values.get("safety_report"),
        "errors": values.get("errors", []),
    }
    (output_dir / "pipeline_results.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print_header("PIPELINE COMPLETE")
    print(f"Total Duration: {summary['duration_seconds']} seconds")
    print(f"Outputs written to: {output_dir.absolute()}")
    print(f"Run status: {'SUCCESS' if summary['success'] else 'FAILED'}")
    return 0 if summary["success"] else 1


def main():
    parser = argparse.ArgumentParser(
        description="eljazari.ai CLI — Design, code, verify, and simulate drones from natural language prompts."
    )
    parser.add_argument(
        "-p",
        "--prompt",
        type=str,
        help="Natural language prompt describing the drone design and flight mission specifications.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=str,
        default="cli_runs/latest",
        help="Directory path where generated files and deliverables will be saved.",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Enable automatic approval for human review (HITL) gateways.",
    )
    parser.add_argument(
        "--design-model",
        type=str,
        help="Specify the Ollama model for the Design phase (e.g. gemma3:4b).",
    )
    parser.add_argument(
        "--code-model",
        type=str,
        help="Specify the Ollama model for Code generation (e.g. qwen2.5-coder:7b).",
    )
    parser.add_argument(
        "--eval-model",
        type=str,
        help="Specify the Ollama model for Simulation evaluation (e.g. llama3.2:3b).",
    )
    parser.add_argument(
        "--max-speed", type=float, help="Specify maximum allowed airspeed (m/s) override."
    )
    parser.add_argument(
        "--max-weight", type=float, help="Specify maximum allowed takeoff mass (g) override."
    )
    parser.add_argument(
        "--max-sim-iter",
        type=int,
        default=3,
        help="Maximum loop iterations in the closed-loop simulation validation phase.",
    )

    args = parser.parse_args()

    # Interactive prompt request if not provided on the command line
    if not args.prompt:
        try:
            args.prompt = input("Enter your drone design and mission prompt: ").strip()
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(1)

    if not args.prompt:
        print("Error: Prompt cannot be empty.")
        sys.exit(1)

    try:
        rc = asyncio.run(run_cli_pipeline(args))
        sys.exit(rc)
    except KeyboardInterrupt:
        print("\nPipeline execution cancelled.")
        sys.exit(1)


if __name__ == "__main__":
    main()
