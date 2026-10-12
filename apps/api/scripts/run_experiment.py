"""
eljazari.ai — Full End-to-End Experiment Script
================================================
Prompt → Design (Ollama) → URDF → Code (Ollama) → Safety → Simulation → Evaluation → Report

This script runs the complete pipeline programmatically, logging every stage,
and produces a JSON experiment log + HTML report.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# Add the api directory to path
API_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API_DIR))


def _has_usable_wsl_distro() -> bool:
    if sys.platform != "win32" or not shutil.which("wsl.exe"):
        return False
    try:
        proc = subprocess.run(
            ["wsl.exe", "-l", "-q"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return False
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).replace("\x00", "")
    distros = [line.strip(" *\r\n\t") for line in output.splitlines()]
    usable = [
        name
        for name in distros
        if name
        and name.upper() != "NAME"
        and name.lower() not in {"docker-desktop", "docker-desktop-data"}
    ]
    return bool(usable)


def _should_use_selector_policy() -> bool:
    # Read directly rather than via config.env_names: this runs at module level,
    # before the deferred import block below that sets up config.* imports.
    runtime = (
        (os.getenv("ELJAZARI_SITL_RUNTIME") or os.getenv("DRONEPROMPT_SITL_RUNTIME") or "")
        .strip()
        .lower()
    )
    if runtime in {"wsl", "native"}:
        return False
    if runtime == "mock":
        return True
    return not _has_usable_wsl_distro()


# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if _should_use_selector_policy():
        selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if selector_policy is not None:
            asyncio.set_event_loop_policy(selector_policy())

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://drone:drone@127.0.0.1:5432/droneprompt")

from config.settings import settings
from utils.sitl_runtime import resolve_sitl_runtime


def _sitl_runtime_summary(runtime) -> dict:
    """Return a JSON-safe summary for the resolved SITL runtime."""
    return {
        "mode": getattr(runtime, "mode", "unknown"),
        "is_real": bool(getattr(runtime, "is_real", False)),
        "host_platform": getattr(runtime, "host_platform", ""),
        "px4_dir": getattr(runtime, "px4_dir", ""),
        "worlds_path": getattr(runtime, "worlds_path", ""),
        "wsl_distro": getattr(runtime, "wsl_distro", ""),
        "wsl_user": getattr(runtime, "wsl_user", ""),
        "wsl_python_bin": getattr(runtime, "wsl_python_bin", ""),
        "wsl_gz_bin": getattr(runtime, "wsl_gz_bin", ""),
        "diagnostics": list(getattr(runtime, "diagnostics", ()) or ()),
    }


# ─────────────────────────────────────────────────────────────────
# Experiment Configuration
# ─────────────────────────────────────────────────────────────────

EXPERIMENT_PROMPT = "quadcopter photography drone, altitude 15 m, square path 100x100 m, flight time 20 minutes, aerial camera"
EXPERIMENT_NAME = "experiment-001-full-pipeline"
OUTPUT_DIR = API_DIR.parent.parent / "experiment-results"


class ExperimentLogger:
    """Tracks every step of the pipeline with timing and outputs."""

    def __init__(self, name: str, prompt: str):
        self.name = name
        self.prompt = prompt
        self.project_id = str(uuid.uuid4())
        self.start_time = time.time()
        self.stages: list[dict] = []
        self.current_stage = None

    def begin_stage(self, name: str, description: str = ""):
        self.current_stage = {
            "name": name,
            "description": description,
            "start_time": time.time(),
            "status": "running",
            "output": None,
            "error": None,
            "duration_s": 0,
        }
        print(f"\n{'=' * 60}")
        print(f"▶ Stage: {name}")
        print(f"  {description}")
        print(f"{'=' * 60}")

    def end_stage(self, output: dict | str | None = None, status: str = "success"):
        if self.current_stage:
            self.current_stage["duration_s"] = round(
                time.time() - self.current_stage["start_time"], 2
            )
            self.current_stage["status"] = status
            self.current_stage["output"] = output
            self.stages.append(self.current_stage)
            marker = "OK" if status == "success" else "SKIPPED" if status == "skipped" else "FAILED"
            print(
                f"  [{marker}] {self.current_stage['name']} finished in {self.current_stage['duration_s']}s"
            )
            self.current_stage = None

    def fail_stage(self, error: str):
        if self.current_stage:
            self.current_stage["duration_s"] = round(
                time.time() - self.current_stage["start_time"], 2
            )
            self.current_stage["status"] = "failed"
            self.current_stage["error"] = error
            self.stages.append(self.current_stage)
            print(f"  ❌ {self.current_stage['name']} FAILED: {error}")
            self.current_stage = None

    def to_dict(self) -> dict:
        return {
            "experiment": self.name,
            "prompt": self.prompt,
            "project_id": self.project_id,
            "total_duration_s": round(time.time() - self.start_time, 2),
            "timestamp": datetime.now().isoformat(),
            "ollama_models": {
                "design": settings.ollama_design_model,
                "code": settings.ollama_code_model,
                "eval": settings.ollama_eval_model,
            },
            "stages": self.stages,
        }


async def run_experiment():
    """Execute the full eljazari.ai pipeline and log everything."""

    logger = ExperimentLogger(EXPERIMENT_NAME, EXPERIMENT_PROMPT)

    print("\n" + "█" * 60)
    print("█  eljazari.ai — Full End-to-End Experiment")
    print("█  Prompt → Design → Code → Simulation → Report")
    print(f"█  Using Ollama LLMs: {settings.ollama_design_model}, {settings.ollama_code_model}")
    print("█" * 60)
    print(f"\n📝 Prompt: {EXPERIMENT_PROMPT}")
    print(f"🔑 Project ID: {logger.project_id}")

    # ── Stage 0: Verify Ollama ──────────────────────────────────
    logger.begin_stage("ollama_check", "Verify Ollama is running and models are available")
    try:
        from services.llm_service import LLMService

        llm = LLMService()
        healthy = await llm.health_check()
        models = await llm.list_models() if healthy else []
        logger.end_stage(
            {
                "healthy": healthy,
                "models": models,
                "base_url": settings.ollama_base_url,
            }
        )
    except Exception as e:
        logger.fail_stage(str(e))
        print("⚠️  Ollama not available — experiment cannot continue")
        return logger.to_dict()

    # ── Stage 1: Parse Prompt ───────────────────────────────────
    logger.begin_stage("parse_prompt", "Analyze and normalize the prompt")
    try:
        from graph.nodes import parse_prompt
        from graph.state import initial_state

        state = initial_state(EXPERIMENT_PROMPT, logger.project_id, max_iterations=3)
        result = await parse_prompt(state)
        state.update(result)
        logger.end_stage(result)
        print(f"  Language: {result.get('design_params', {}).get('language', '?')}")
    except Exception as e:
        logger.fail_stage(str(e))

    # ── Stage 2: Generate Design (Ollama LLM) ───────────────────
    logger.begin_stage(
        "generate_design",
        f"Generate drone design tree using Ollama ({settings.ollama_design_model})",
    )
    try:
        from graph.nodes import generate_design

        result = await generate_design(state)
        state.update(result)

        design_tree = result.get("design_tree", {})
        perf = result.get("performance_estimate", {})

        logger.end_stage(
            {
                "design_tree": design_tree,
                "performance_estimate": perf,
                "design_status": result.get("design_status"),
                "design_generation": result.get("design_generation", {}),
                "generation_method": [log_entry for log_entry in result.get("agent_logs", [])],
            }
        )

        if design_tree:
            topo = design_tree.get("topology", "unknown")
            motors = len(design_tree.get("propulsion", []))
            print(f"  🛩️  Topology: {topo} ({motors} motors)")
            print(
                f"  ⚡ Performance: hover={perf.get('hover_time_s', '?')}s, speed={perf.get('max_speed_ms', '?')}m/s"
            )
            print(
                f"  ⚖️  Weight: {perf.get('total_weight_g', '?')}g, T/W={perf.get('thrust_to_weight_ratio', '?')}"
            )
    except Exception as e:
        logger.fail_stage(str(e))

    # ── Stage 3: HITL — Auto-approve design ─────────────────────
    logger.begin_stage("approve_design", "Auto-approve design (HITL simulation)")
    try:
        state["design_status"] = "approved"
        state["human_approval_needed"] = False
        state["human_feedback"] = ""
        logger.end_stage({"action": "approved", "reason": "auto-approved for experiment"})
    except Exception as e:
        logger.fail_stage(str(e))

    # ── Stage 4: Convert to URDF ────────────────────────────────
    logger.begin_stage("convert_to_urdf", "Convert JSON design tree → URDF/SDF files")
    try:
        from graph.nodes import convert_to_urdf

        result = await convert_to_urdf(state)
        state.update(result)
        logger.end_stage(
            {
                "urdf_path": result.get("urdf_path"),
                "sdf_path": result.get("sdf_path"),
                "conversion_status": result.get("conversion_status"),
            }
        )
        print(f"  📄 URDF: {result.get('urdf_path', 'N/A')}")
    except Exception as e:
        logger.fail_stage(str(e))

    # ── Stage 5: Generate Code (Ollama LLM) ─────────────────────
    logger.begin_stage(
        "generate_code",
        f"Generate PX4/MAVSDK mission code using Ollama ({settings.ollama_code_model})",
    )
    try:
        from graph.nodes import generate_code

        result = await generate_code(state)
        state.update(result)

        code = result.get("generated_code", "")
        logger.end_stage(
            {
                "code_length": len(code),
                "code_preview": code[:500] if code else "",
                "code_status": result.get("code_status"),
                "gsce_config": result.get("gsce_config"),
            }
        )
        print(f"  💻 Code generated: {len(code)} characters")
        if code:
            lines = code.strip().split("\n")
            print(f"  📋 Lines: {len(lines)}")
            # Show first few lines
            for line in lines[:5]:
                print(f"     {line}")
            print("     ...")
    except Exception as e:
        logger.fail_stage(str(e))

    # ── Stage 6: Safety Check ───────────────────────────────────
    logger.begin_stage(
        "safety_check", "Run SafetyLayer: AST analysis + reachability + protocol injection"
    )
    try:
        from graph.nodes import safety_check

        result = await safety_check(state)
        state.update(result)

        safety_report = result.get("safety_report", {})
        logger.end_stage(
            {
                "safety_passed": result.get("safety_passed"),
                "safety_issues": result.get("safety_issues", []),
                "risk_level": safety_report.get("risk_level", "unknown"),
                "issues_count": len(safety_report.get("issues", [])),
            },
            status="success" if result.get("safety_passed") else "failed",
        )
        print(f"  🛡️ Safety: {'PASSED ✅' if result.get('safety_passed') else 'FAILED ❌'}")
        print(f"  ⚠️ Risk Level: {safety_report.get('risk_level', 'unknown')}")
        for issue in result.get("safety_issues", [])[:3]:
            print(f"     • {issue}")
    except Exception as e:
        logger.fail_stage(str(e))

    # ── Stage 7: HITL — Auto-approve code ───────────────────────
    logger.begin_stage("approve_code", "Approve code only when SafetyLayer passes")
    try:
        if state.get("safety_passed"):
            state["code_status"] = "approved"
            state["human_approval_needed"] = False
            state["human_feedback"] = ""
            logger.end_stage({"action": "approved", "reason": "safety passed"})
        else:
            state["code_status"] = "rejected"
            state["human_approval_needed"] = True
            state["human_feedback"] = state.get("code_feedback") or "Safety check failed"
            logger.end_stage(
                {
                    "action": "blocked",
                    "reason": "safety_failed",
                    "code_feedback": state.get("human_feedback"),
                },
                status="failed",
            )
            print("  Code approval blocked because SafetyLayer failed.")
    except Exception as e:
        logger.fail_stage(str(e))

    # ── Stage 8: Run Simulation ─────────────────────────────────
    sitl_runtime = resolve_sitl_runtime()
    runtime_label = f"{sitl_runtime.mode}; real={sitl_runtime.is_real}"
    logger.begin_stage("run_simulation", f"Execute mission code in simulation ({runtime_label})")
    try:
        if state.get("code_status") != "approved":
            state.update(
                {
                    "simulation_status": "skipped",
                    "simulation_success": False,
                    "simulation_score": None,
                    "evaluation_status": "skipped",
                    "evaluation_skip_reason": f"code_status={state.get('code_status')}",
                    "telemetry_data": [],
                    "semantic_observations": [],
                }
            )
            logger.end_stage(
                {
                    "simulation_status": "skipped",
                    "reason": f"code_status={state.get('code_status')}",
                    "runtime": _sitl_runtime_summary(sitl_runtime),
                },
                status="skipped",
            )
            print(f"  Simulation skipped because code_status={state.get('code_status')}.")
        else:
            from graph.nodes import run_simulation

            result = await run_simulation(state)
            state.update(result)

            telemetry = result.get("telemetry_data", [])
            observations = result.get("semantic_observations", [])
            sim_status = result.get("simulation_status")

            logger.end_stage(
                {
                    "simulation_status": sim_status,
                    "telemetry_points": len(telemetry) if isinstance(telemetry, list) else 0,
                    "semantic_observations_count": len(observations),
                    "semantic_observations_preview": observations[:5] if observations else [],
                    "duration_s": result.get("duration_s"),
                    "mission_started": result.get("mission_started"),
                    "mission_completed": result.get("mission_completed"),
                    "telemetry_source": result.get("telemetry_source"),
                    "mission_error": result.get("mission_error"),
                    "warnings": result.get("simulation_warnings", []),
                    "artifacts": result.get("simulation_artifacts", {}),
                    "runtime": _sitl_runtime_summary(sitl_runtime),
                },
                status="success" if sim_status == "complete" else "failed",
            )
            print(f"  🎯 Simulation: {sim_status or 'unknown'}")
            print(f"  🧭 Mission completed: {result.get('mission_completed', False)}")
            print(f"  🛰️ Telemetry source: {result.get('telemetry_source', 'unknown')}")
            if result.get("mission_error"):
                print(f"  ⚠️ Mission error: {result.get('mission_error')}")
            print(f"  📊 Telemetry points: {len(telemetry) if isinstance(telemetry, list) else 0}")
            print(f"  📝 Semantic observations: {len(observations)}")
            for obs in (observations or [])[:3]:
                print(f"     • {obs}")
    except Exception as e:
        logger.fail_stage(str(e))

    # ── Stage 9: Evaluate Simulation (Ollama LLM) ──────────────
    logger.begin_stage(
        "evaluate_simulation",
        f"LLM evaluation of mission results using Ollama ({settings.ollama_eval_model})",
    )
    try:
        if state.get("simulation_status") != "complete":
            skip_reason = f"simulation_status={state.get('simulation_status')}"
            state.update(
                {
                    "simulation_success": False,
                    "simulation_score": None,
                    "evaluation_status": "skipped",
                    "evaluation_skip_reason": skip_reason,
                }
            )
            logger.end_stage(
                {
                    "simulation_success": False,
                    "simulation_score": None,
                    "evaluation_status": "skipped",
                    "evaluation_skip_reason": skip_reason,
                    "reason": skip_reason,
                },
                status="skipped",
            )
            print(
                f"  Evaluation skipped because simulation_status={state.get('simulation_status')}."
            )
        else:
            from graph.nodes import evaluate_simulation

            result = await evaluate_simulation(state)
            state.update(result)
            evaluation_status = result.get("evaluation_status")
            status_map = {
                "skipped": "skipped",
                "completed_with_failure": "success",
                "completed": "success",
                None: "success",
            }
            stage_status = status_map.get(evaluation_status, "failed")

            logger.end_stage(
                {
                    "simulation_success": result.get("simulation_success"),
                    "simulation_score": result.get("simulation_score"),
                    "evaluation_status": evaluation_status,
                    "evaluation_skip_reason": result.get("evaluation_skip_reason"),
                    "code_feedback": result.get("code_feedback"),
                },
                status=stage_status,
            )
            score_value = result.get("simulation_score")
            score_display = "—" if score_value is None else score_value
            print(f"  📈 Score: {score_display}")
            print(f"  ✅ Passed: {result.get('simulation_success', False)}")
    except Exception as e:
        logger.fail_stage(str(e))

    # ── Stage 10: Generate Monitor Report ───────────────────────
    logger.begin_stage("generate_monitor_report", "Generate comprehensive performance report")
    try:
        from graph.nodes import generate_monitor_report

        result = await generate_monitor_report(state)
        state.update(result)

        report = result.get("monitor_report", {})
        recommendations = result.get("recommendations", [])

        logger.end_stage(
            {
                "overall_score": report.get("overall_score"),
                "recommendations_count": len(recommendations),
                "recommendations": recommendations[:5],
                "report_keys": list(report.keys()) if isinstance(report, dict) else [],
            }
        )
        print(f"  📊 Overall Score: {report.get('overall_score', 'N/A')}")
        print(f"  💡 Recommendations: {len(recommendations)}")
        for rec in recommendations[:3]:
            print(f"     • {rec}")
    except Exception as e:
        logger.fail_stage(str(e))

    # ── Save Results ────────────────────────────────────────────
    experiment_data = logger.to_dict()
    runtime_summary = _sitl_runtime_summary(
        sitl_runtime if "sitl_runtime" in locals() else resolve_sitl_runtime()
    )
    experiment_data["final_status"] = {
        "safety_passed": bool(state.get("safety_passed")),
        "code_status": state.get("code_status"),
        "simulation_status": state.get("simulation_status"),
        "simulation_success": bool(state.get("simulation_success")),
        "simulation_score": state.get("simulation_score"),
        "evaluation_status": state.get("evaluation_status"),
        "evaluation_skip_reason": state.get("evaluation_skip_reason"),
        "mission_completed": bool(state.get("mission_completed")),
        "telemetry_source": state.get("telemetry_source"),
        "mission_error": state.get("mission_error"),
        "simulation_warnings": state.get("simulation_warnings", []),
        "sitl_runtime": runtime_summary,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Save JSON
    json_path = OUTPUT_DIR / f"{EXPERIMENT_NAME}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(experiment_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n📁 JSON saved: {json_path}")

    # Save generated code separately
    code_content = state.get("generated_code", "")
    if code_content:
        code_path = OUTPUT_DIR / f"{EXPERIMENT_NAME}-mission-code.py"
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(code_content)
        print(f"📁 Mission code saved: {code_path}")

    # Generate HTML report
    html_path = OUTPUT_DIR / f"{EXPERIMENT_NAME}-report.html"
    generate_html_report(experiment_data, state, html_path)
    print(f"📁 HTML report saved: {html_path}")

    # Generate Markdown report
    md_path = OUTPUT_DIR / f"{EXPERIMENT_NAME}-summary.md"
    generate_markdown_report(experiment_data, state, md_path)
    print(f"📁 Markdown report saved: {md_path}")

    print("\n" + "█" * 60)
    print("█  EXPERIMENT COMPLETE!")
    print(f"█  Total Duration: {experiment_data['total_duration_s']}s")
    print(f"█  Stages: {len(experiment_data['stages'])}")
    passed = sum(1 for s in experiment_data["stages"] if s["status"] == "success")
    failed = sum(1 for s in experiment_data["stages"] if s["status"] == "failed")
    skipped = sum(1 for s in experiment_data["stages"] if s["status"] == "skipped")
    print(f"█  Passed: {passed} | Failed: {failed} | Skipped: {skipped}")
    print(f"█  Safety Passed: {experiment_data['final_status']['safety_passed']}")
    print(f"█  Code Status: {experiment_data['final_status']['code_status']}")
    print(f"█  Mission Completed: {experiment_data['final_status']['mission_completed']}")
    print(f"█  Simulation Success: {experiment_data['final_status']['simulation_success']}")
    print("█" * 60)

    return experiment_data


def _score_display(score: object) -> str:
    if score is None:
        return "—"
    if isinstance(score, int | float):
        return f"{float(score):.1f}"
    return str(score)


def _dict_or_empty(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list_or_empty(value: object) -> list:
    return value if isinstance(value, list) else []


def generate_markdown_report(data: dict, state: dict, output_path: Path) -> None:
    """Generate a compact Markdown report with relative artifact links."""
    final_status = data.get("final_status", {})
    monitor = (
        state.get("monitor_report", {}) if isinstance(state.get("monitor_report"), dict) else {}
    )
    design_generation = _dict_or_empty(state.get("design_generation"))
    design_diagnostics = _dict_or_empty(design_generation.get("diagnostics"))
    stages = data.get("stages", [])

    lines = [
        f"# Experiment Report — {data.get('experiment', EXPERIMENT_NAME)}",
        "",
        f"- Date: `{data.get('timestamp', '')}`",
        f"- Project ID: `{data.get('project_id', '')}`",
        f"- Total duration: `{data.get('total_duration_s', 0)}s`",
        f"- Evaluation status: `{final_status.get('evaluation_status') or 'unknown'}`",
        f"- Simulation score: `{_score_display(final_status.get('simulation_score'))}`",
        f"- Monitor score: `{_score_display(monitor.get('overall_score'))}`",
        "",
        "## Pipeline Stages",
        "",
        "| Stage | Status | Duration |",
        "| --- | --- | ---: |",
    ]

    for stage in stages:
        lines.append(
            f"| `{stage.get('name', '')}` | `{stage.get('status', '')}` | `{stage.get('duration_s', 0)}s` |"
        )

    lines.extend(
        [
            "",
            "## Execution Summary",
            "",
            f"- Safety: `{final_status.get('safety_passed')}`",
            f"- Code status: `{final_status.get('code_status') or 'unknown'}`",
            f"- Simulation status: `{final_status.get('simulation_status') or 'unknown'}`",
            f"- Mission completed: `{final_status.get('mission_completed')}`",
            f"- Telemetry source: `{final_status.get('telemetry_source') or 'unknown'}`",
        ]
    )

    if final_status.get("mission_error"):
        lines.append(f"- Mission error: `{final_status.get('mission_error')}`")
    if final_status.get("evaluation_skip_reason"):
        lines.append(f"- Evaluation skip reason: `{final_status.get('evaluation_skip_reason')}`")

    if isinstance(design_diagnostics, dict) and design_diagnostics:
        lines.extend(
            [
                "",
                "## Design Generation Time",
                "",
                "| Metric | Value |",
                "| --- | ---: |",
            ]
        )
        for key, value in sorted(design_diagnostics.items()):
            lines.append(f"| `{key}` | `{value}` |")

    lines.extend(
        [
            "",
            "## Output Files",
            "",
            f"- [JSON]({EXPERIMENT_NAME}.json)",
            f"- [HTML]({EXPERIMENT_NAME}-report.html)",
            f"- [Mission Code]({EXPERIMENT_NAME}-mission-code.py)",
        ]
    )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_html_report(data: dict, state: dict, output_path: Path):
    """Generate a beautiful HTML report documenting the experiment."""

    stages_html = ""
    for i, stage in enumerate(data.get("stages", []), 1):
        if stage["status"] == "success":
            status_icon = "✅"
            status_class = "success"
        elif stage["status"] == "skipped":
            status_icon = "⏭️"
            status_class = "skipped"
        else:
            status_icon = "❌"
            status_class = "failed"

        output_html = ""
        if stage.get("output"):
            output_json = json.dumps(stage["output"], ensure_ascii=False, indent=2, default=str)
            # Truncate very long outputs
            if len(output_json) > 3000:
                output_json = output_json[:3000] + "\n... (truncated)"
            output_html = (
                f'<pre class="output-block"><code>{_html_escape(output_json)}</code></pre>'
            )

        if stage.get("error"):
            output_html += f'<div class="error-block">❌ {_html_escape(stage["error"])}</div>'

        stages_html += f"""
        <div class="stage-card {status_class}">
            <div class="stage-header">
                <span class="stage-number">{i}</span>
                <span class="stage-name">{stage["name"]}</span>
                <span class="stage-status">{status_icon}</span>
                <span class="stage-duration">{stage["duration_s"]}s</span>
            </div>
            <div class="stage-description">{stage.get("description", "")}</div>
            <div class="stage-output">{output_html}</div>
        </div>
        """

    # Code section
    code_content = state.get("generated_code", "No code generated")

    # Design tree section
    design_tree = _dict_or_empty(state.get("design_tree"))
    design_json = (
        json.dumps(design_tree, ensure_ascii=False, indent=2, default=str)
        if design_tree
        else "No design generated"
    )

    # Performance
    perf = _dict_or_empty(state.get("performance_estimate"))

    # Safety report
    safety = _dict_or_empty(state.get("safety_report"))

    # Monitor report
    monitor = _dict_or_empty(state.get("monitor_report"))

    frame = _dict_or_empty(design_tree.get("frame"))
    battery = _dict_or_empty(design_tree.get("battery"))
    propulsion = _list_or_empty(design_tree.get("propulsion"))

    html = f"""<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>eljazari.ai — Full Pipeline Experiment Report</title>
    <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@300;400;600;700;900&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg: #0a0e17;
            --surface: #111827;
            --surface-2: #1a2332;
            --border: #1e293b;
            --text: #e2e8f0;
            --text-muted: #94a3b8;
            --primary: #3b82f6;
            --primary-glow: rgba(59,130,246,0.3);
            --success: #10b981;
            --success-glow: rgba(16,185,129,0.2);
            --warning: #f59e0b;
            --danger: #ef4444;
            --accent: #8b5cf6;
        }}
        
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        
        body {{
            font-family: 'Cairo', sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.8;
            min-height: 100vh;
        }}
        
        .hero {{
            background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 50%, #0f172a 100%);
            padding: 60px 20px;
            text-align: center;
            position: relative;
            overflow: hidden;
        }}
        
        .hero::before {{
            content: '';
            position: absolute;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background: radial-gradient(circle at 50% 50%, var(--primary-glow) 0%, transparent 50%);
            animation: pulse 4s ease-in-out infinite;
        }}
        
        @keyframes pulse {{ 
            0%, 100% {{ opacity: 0.3; transform: scale(1); }}
            50% {{ opacity: 0.6; transform: scale(1.1); }}
        }}
        
        .hero h1 {{
            font-size: 2.5rem;
            font-weight: 900;
            background: linear-gradient(to right, #3b82f6, #8b5cf6, #ec4899);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            position: relative;
            margin-bottom: 10px;
        }}
        
        .hero .subtitle {{
            color: var(--text-muted);
            font-size: 1.1rem;
            position: relative;
        }}
        
        .container {{ max-width: 1100px; margin: 0 auto; padding: 30px 20px; }}
        
        .meta-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-bottom: 40px;
        }}
        
        .meta-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
            text-align: center;
        }}
        
        .meta-card .label {{ color: var(--text-muted); font-size: 0.85rem; margin-bottom: 4px; }}
        .meta-card .value {{ font-size: 1.5rem; font-weight: 700; color: var(--primary); }}
        .meta-card .value.success {{ color: var(--success); }}
        .meta-card .value.warning {{ color: var(--warning); }}
        
        .section {{ margin-bottom: 40px; }}
        .section-title {{
            font-size: 1.4rem;
            font-weight: 700;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 2px solid var(--primary);
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        
        .prompt-block {{
            background: linear-gradient(135deg, var(--surface) 0%, var(--surface-2) 100%);
            border: 1px solid var(--primary);
            border-radius: 12px;
            padding: 24px;
            font-size: 1.2rem;
            text-align: center;
            margin-bottom: 30px;
            position: relative;
        }}
        .prompt-block::before {{
            content: '📝 Prompt';
            position: absolute;
            top: -12px;
            right: 20px;
            background: var(--primary);
            color: white;
            padding: 2px 12px;
            border-radius: 6px;
            font-size: 0.8rem;
        }}
        
        .stage-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            margin-bottom: 16px;
            overflow: hidden;
            transition: all 0.3s ease;
        }}
        .stage-card:hover {{ border-color: var(--primary); }}
        .stage-card.success {{ border-right: 4px solid var(--success); }}
        .stage-card.skipped {{ border-right: 4px solid var(--warning); }}
        .stage-card.failed {{ border-right: 4px solid var(--danger); }}
        
        .stage-header {{
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 16px 20px;
            background: var(--surface-2);
        }}
        
        .stage-number {{
            background: var(--primary);
            color: white;
            width: 28px;
            height: 28px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.85rem;
            font-weight: 700;
        }}
        
        .stage-name {{ flex: 1; font-weight: 600; font-family: 'JetBrains Mono', monospace; font-size: 0.95rem; }}
        .stage-status {{ font-size: 1.2rem; }}
        .stage-duration {{
            background: var(--surface);
            padding: 2px 10px;
            border-radius: 20px;
            font-size: 0.8rem;
            color: var(--text-muted);
            font-family: 'JetBrains Mono', monospace;
        }}
        
        .stage-description {{ padding: 10px 20px; color: var(--text-muted); font-size: 0.9rem; }}
        
        .stage-output {{ padding: 0 20px 16px; }}
        
        .output-block {{
            background: #0d1117;
            border: 1px solid #21262d;
            border-radius: 8px;
            padding: 16px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.8rem;
            overflow-x: auto;
            max-height: 400px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-break: break-word;
            color: #c9d1d9;
        }}
        
        .error-block {{
            background: rgba(239,68,68,0.1);
            border: 1px solid var(--danger);
            border-radius: 8px;
            padding: 12px;
            margin-top: 8px;
            color: var(--danger);
        }}
        
        .code-section {{
            background: #0d1117;
            border: 1px solid #21262d;
            border-radius: 12px;
            padding: 20px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.82rem;
            overflow-x: auto;
            max-height: 600px;
            overflow-y: auto;
            white-space: pre-wrap;
            color: #c9d1d9;
            line-height: 1.6;
        }}
        
        .design-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin-bottom: 20px;
        }}
        
        .design-chip {{
            background: var(--surface-2);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px;
            text-align: center;
        }}
        .design-chip .chip-label {{ font-size: 0.75rem; color: var(--text-muted); }}
        .design-chip .chip-value {{ font-size: 1.1rem; font-weight: 700; color: var(--accent); }}
        
        .llm-badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: linear-gradient(135deg, #4c1d95, #6d28d9);
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.8rem;
            margin: 2px;
        }}
        
        .footer {{
            text-align: center;
            padding: 30px;
            color: var(--text-muted);
            font-size: 0.85rem;
            border-top: 1px solid var(--border);
        }}
    </style>
</head>
<body>
    <div class="hero">
        <h1>🚁 eljazari.ai — Full Pipeline Experiment Report</h1>
        <p class="subtitle">Build & Test a Drone from a Prompt — Full Pipeline Experiment</p>
    </div>
    
    <div class="container">
        <!-- Prompt -->
        <div class="prompt-block">
            {_html_escape(data["prompt"])}
        </div>
        
        <!-- Meta Cards -->
        <div class="meta-grid">
            <div class="meta-card">
                <div class="label">⏱️ Total Duration</div>
                <div class="value">{data["total_duration_s"]}s</div>
            </div>
            <div class="meta-card">
                <div class="label">📊 Stage Count</div>
                <div class="value">{len(data["stages"])}</div>
            </div>
            <div class="meta-card">
                <div class="label">✅ Successful Stages</div>
                <div class="value success">{sum(1 for s in data["stages"] if s["status"] == "success")}</div>
            </div>
            <div class="meta-card">
                <div class="label">🤖 Ollama Models</div>
                <div class="value" style="font-size:0.9rem">
                    <span class="llm-badge">🧠 {data["ollama_models"]["design"]}</span>
                    <span class="llm-badge">💻 {data["ollama_models"]["code"]}</span>
                    <span class="llm-badge">📊 {data["ollama_models"]["eval"]}</span>
                </div>
            </div>
        </div>
        
        <!-- Pipeline Stages -->
        <div class="section">
            <div class="section-title">🔄 Pipeline Stages</div>
            {stages_html}
        </div>
        
        <!-- Design Tree -->
        <div class="section">
            <div class="section-title">🛩️ Structural Design Tree</div>
            <div class="design-grid">
                <div class="design-chip">
                    <div class="chip-label">Topology</div>
                    <div class="chip-value">{design_tree.get("topology", "N/A")}</div>
                </div>
                <div class="design-chip">
                    <div class="chip-label">Frame</div>
                    <div class="chip-value">{frame.get("type", "N/A")}</div>
                </div>
                <div class="design-chip">
                    <div class="chip-label">Motors</div>
                    <div class="chip-value">{len(propulsion)}</div>
                </div>
                <div class="design-chip">
                    <div class="chip-label">Battery</div>
                    <div class="chip-value">{battery.get("cells", "?")}S {battery.get("capacity_mah", "?")}mAh</div>
                </div>
                <div class="design-chip">
                    <div class="chip-label">Hover Time</div>
                    <div class="chip-value">{perf.get("hover_time_s", "N/A")}s</div>
                </div>
                <div class="design-chip">
                    <div class="chip-label">Weight</div>
                    <div class="chip-value">{perf.get("total_weight_g", "N/A")}g</div>
                </div>
            </div>
            <div class="code-section">{_html_escape(design_json)}</div>
        </div>
        
        <!-- Generated Code -->
        <div class="section">
            <div class="section-title">💻 Generated Mission Code (GSCE + MAVSDK)</div>
            <div class="code-section">{_html_escape(code_content)}</div>
        </div>
        
        <!-- Safety Report -->
        <div class="section">
            <div class="section-title">🛡️ Safety Report</div>
            <div class="code-section">{_html_escape(json.dumps(safety, ensure_ascii=False, indent=2, default=str) if safety else "N/A")}</div>
        </div>
        
        <!-- Monitor Report -->
        <div class="section">
            <div class="section-title">📈 Final Performance Report</div>
            <div class="code-section">{_html_escape(json.dumps(monitor, ensure_ascii=False, indent=2, default=str) if monitor else "N/A")}</div>
        </div>
    </div>
    
    <div class="footer">
        eljazari.ai Experiment Report — Generated {data["timestamp"]}<br>
        Project ID: {data["project_id"]}
    </div>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")


def _html_escape(text: str) -> str:
    """Escape HTML special characters."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


if __name__ == "__main__":
    result = asyncio.run(run_experiment())
    # Print summary
    print(f"\nExperiment data saved to: {OUTPUT_DIR}")
