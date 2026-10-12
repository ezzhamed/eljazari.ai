#!/usr/bin/env python3
"""Run the published prompt benchmark through the real pipeline graph.

Every case goes through the compiled LangGraph workflow the API runs
(``graph.workflow``, with in-memory checkpoints instead of PostgreSQL). Both human
reviews are approved by ``Orchestrator.approve``, which binds the code approval
to its SHA-256 exactly as the UI does. Nothing is mocked: design and code come
from the configured Ollama models, flights run in the configured SITL runtime,
and the deterministic acceptance gate decides the outcome.

Failed, timed-out and crashed runs stay in the denominator. Each run is appended
to ``runs.jsonl`` as soon as it finishes, so an interrupted benchmark continues
with ``--resume`` instead of starting over and losing its failures.

    python scripts/run_benchmark.py --out ../../benchmarks/results/<run-id>
    python scripts/run_benchmark.py --only en-hover-camera --repeats 1 --out <dir>
    python scripts/run_benchmark.py --summarize <dir>      # rebuild summary only
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import logging
import math
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[1]
DEFAULT_SUITE = REPO_ROOT / "benchmarks" / "prompt_suite_v2.json"
sys.path.insert(0, str(API_ROOT))

REJECTION_OUTCOMES = frozenset(
    {
        "rejected_prompt",
        "rejected_design",
        "rejected_components",
        "rejected_model",
        "rejected_code",
        "rejected_safety",
        "blocked_simulation",
    }
)
UNDECIDED_OUTCOMES = frozenset({"timeout", "harness_error", "incomplete"})
MAX_APPROVALS = 24


# ── Scoring (pure; unit-tested) ──────────────────────────────────────────────


def classify(values: dict[str, Any], *, timed_out: bool = False, harness_error: str = "") -> str:
    """Name the terminal state of one run from its final graph state."""
    if timed_out:
        return "timeout"
    if harness_error:
        return "harness_error"
    acceptance = values.get("simulation_acceptance") or {}
    if acceptance.get("passed") is True:
        return "accepted"
    simulation_status = values.get("simulation_status")
    if simulation_status == "blocked":
        return "blocked_simulation"
    if acceptance or simulation_status in {"complete", "failed"}:
        return "failed_simulation"
    if values.get("current_phase") == "error":
        return "rejected_prompt"
    if values.get("design_status") == "rejected":
        return "rejected_design"
    if values.get("bom_status") == "failed":
        return "rejected_components"
    if values.get("conversion_status") == "failed":
        return "rejected_model"
    if values.get("code_status") == "rejected":
        return "rejected_code"
    if values.get("generated_code") and not values.get("safety_passed"):
        return "rejected_safety"
    return "incomplete"


def design_source(method: str | None) -> str | None:
    """Where the approved design came from.

    ``<method>_repaired`` means the design agent discarded the LLM candidate and
    substituted its deterministic baseline, so the LLM's design was not used.
    """
    if not method:
        return None
    if method.endswith("_repaired"):
        return "baseline_after_llm_candidate_rejected"
    if method in {"rag_llm", "llm_only"}:
        return "llm"
    return method


def expected_met(category: str, outcome: str) -> bool | None:
    """Whether the outcome is the correct one for the case's category.

    Ambiguous prompts have no single correct outcome and are not scored.
    """
    if category == "supported":
        return outcome == "accepted"
    if category in {"impossible", "unsupported"}:
        return outcome in REJECTION_OUTCOMES
    return None


def wilson_interval(successes: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    """95% Wilson score interval; honest at the small n a local benchmark has."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def nearest_rank(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile: always an observed value, no interpolation."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate run records. Every record counts, whatever its outcome."""
    by_category: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_category.setdefault(record["category"], []).append(record)

    categories: dict[str, Any] = {}
    for category, runs in sorted(by_category.items()):
        outcomes: dict[str, int] = {}
        for run in runs:
            outcomes[run["outcome"]] = outcomes.get(run["outcome"], 0) + 1
        scored = [run for run in runs if run["expected_met"] is not None]
        correct = sum(1 for run in scored if run["expected_met"])
        wall = [float(run["wall_time_s"]) for run in runs]
        retries = [int(run["retries"]) for run in runs]
        interval = wilson_interval(correct, len(scored))
        categories[category] = {
            "runs": len(runs),
            "scored_runs": len(scored),
            "correct": correct if scored else None,
            "correct_rate": (correct / len(scored)) if scored else None,
            "correct_rate_wilson95": list(interval) if interval else None,
            "decided": sum(1 for run in runs if run["outcome"] not in UNDECIDED_OUTCOMES),
            "outcomes": dict(sorted(outcomes.items())),
            "wall_time_s": {
                "p50": nearest_rank(wall, 0.50),
                "p95": nearest_rank(wall, 0.95),
                "max": max(wall) if wall else None,
            },
            "retries": {
                "total": sum(retries),
                "max": max(retries) if retries else None,
                "runs_with_retry": sum(1 for value in retries if value > 0),
            },
            "design_not_from_llm_runs": sum(1 for run in runs if run.get("design_fallback")),
            "code_fallback_runs": sum(1 for run in runs if run.get("code_fallback")),
        }

    cases: dict[str, Any] = {}
    for record in records:
        entry = cases.setdefault(
            record["case_id"],
            {"category": record["category"], "language": record["language"], "outcomes": []},
        )
        entry["outcomes"].append(record["outcome"])
        # How closely each accepted flight followed its approved code's mission.
        conformance = ((record.get("acceptance") or {}).get("flight_conformance") or {}).get(
            "metrics"
        ) or {}
        if record["outcome"] == "accepted" and conformance:
            entry.setdefault("accepted_flights", []).append(
                {
                    key: conformance.get(key)
                    for key in ("horizontal_error_max_m", "altitude_rmse_m", "landing_error_m")
                }
            )
    for entry in cases.values():
        met = [expected_met(entry["category"], outcome) for outcome in entry["outcomes"]]
        entry["correct"] = None if met and met[0] is None else sum(bool(item) for item in met)

    by_language: dict[str, Any] = {}
    for language in sorted({record["language"] for record in records}):
        scored = [
            record
            for record in records
            if record["language"] == language and record["expected_met"] is not None
        ]
        correct = sum(1 for record in scored if record["expected_met"])
        by_language[language] = {
            "scored_runs": len(scored),
            "correct": correct,
            "correct_rate": (correct / len(scored)) if scored else None,
        }

    return {
        "runs": len(records),
        "categories": categories,
        "languages": by_language,
        "cases": dict(sorted(cases.items())),
        "method": {
            "percentiles": "nearest-rank over every run in the category, including failures",
            "interval": "Wilson score, 95%",
            "retries": "extra code generations + extra simulation attempts in one run",
        },
    }


# ── Environment capture ──────────────────────────────────────────────────────


def _run(command: list[str], timeout: float = 60.0) -> str:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"
    return (completed.stdout or completed.stderr).strip()


def _git_state() -> dict[str, Any]:
    head = _run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])
    branch = _run(["git", "-C", str(REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"])
    try:
        diff = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "diff", "HEAD", "--binary"],
            capture_output=True,
            timeout=120,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        diff = b""
    status = _run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"])
    lines = [line for line in status.splitlines() if line.strip()]
    return {
        "head": head,
        "branch": branch,
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest() if diff else None,
        "changed_paths": sum(1 for line in lines if not line.startswith("??")),
        "untracked_paths": sum(1 for line in lines if line.startswith("??")),
    }


def _ollama_state(base_url: str, models: list[str]) -> dict[str, Any]:
    import httpx

    state: dict[str, Any] = {"base_url": base_url}
    try:
        with httpx.Client(timeout=10.0) as client:
            state["version"] = client.get(f"{base_url}/api/version").json().get("version")
            tags = client.get(f"{base_url}/api/tags").json().get("models", [])
    except Exception as exc:  # noqa: BLE001 — recorded, not fatal
        state["error"] = str(exc)
        return state
    by_name = {tag.get("name"): tag for tag in tags}
    state["models"] = {
        name: {
            "digest": (by_name.get(name) or {}).get("digest"),
            "size": (by_name.get(name) or {}).get("size"),
            "quantization": ((by_name.get(name) or {}).get("details") or {}).get(
                "quantization_level"
            ),
        }
        for name in sorted(set(models))
    }
    return state


def _simulator_state(settings: Any) -> dict[str, Any]:
    runtime = (settings.sitl_force_runtime or settings.sitl_runtime or "").lower()
    state: dict[str, Any] = {
        "runtime": runtime,
        "require_real": settings.sitl_require_real,
    }
    if runtime != "wsl":
        return state
    script = (
        f"cd {settings.sitl_wsl_px4_dir} && git describe --always --tags && "
        "git rev-parse HEAD && (gz sim --versions 2>/dev/null | head -1) && "
        f"{settings.sitl_wsl_python_bin} -c "
        "\"import importlib.metadata as m; print(m.version('mavsdk'))\" && "
        "(uname -r)"
    )
    command = ["wsl.exe"]
    if settings.sitl_wsl_distro:
        command += ["-d", settings.sitl_wsl_distro]
    if settings.sitl_wsl_user:
        command += ["-u", settings.sitl_wsl_user]
    output = _run([*command, "--", "bash", "-lc", script]).splitlines()
    keys = ["px4_describe", "px4_commit", "gazebo_sim", "mission_mavsdk", "wsl_kernel"]
    state.update(dict(zip(keys, output, strict=False)))
    return state


def capture_environment(settings: Any) -> dict[str, Any]:
    try:
        import psutil

        memory_gb: float | None = round(psutil.virtual_memory().total / 1e9, 1)
    except ImportError:
        memory_gb = None
    models = [settings.ollama_design_model, settings.ollama_code_model, settings.ollama_eval_model]
    return {
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
            "memory_gb": memory_gb,
            "python": sys.version.split()[0],
        },
        "git": _git_state(),
        "llm": {
            "design_model": settings.ollama_design_model,
            "code_model": settings.ollama_code_model,
            "eval_model": settings.ollama_eval_model,
            "ollama": _ollama_state(settings.ollama_base_url.rstrip("/"), models),
        },
        "simulator": _simulator_state(settings),
        "pipeline": {
            "checkpoints": "memory (benchmark harness; the API uses PostgreSQL)",
            "max_simulation_iterations": settings.max_simulation_iterations,
            "approvals": "automatic: Orchestrator.approve for design and code",
        },
    }


# ── One run ──────────────────────────────────────────────────────────────────


class _WarningCapture(logging.Handler):
    """Collect agent warnings for one run; the code agent's fallback is only logged."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(("agents.", "pipelines.", "services.", "utils.")):
            self.messages.append(f"{record.name}: {record.getMessage()}"[:300])


def _clip(items: list[Any], limit: int = 12, width: int = 400) -> list[str]:
    return [str(item)[:width] for item in list(items)[:limit]]


def build_record(
    case: dict[str, Any],
    repeat: int,
    project_id: str,
    values: dict[str, Any],
    *,
    wall_time_s: float,
    timeline: list[tuple[str, float]],
    approvals: dict[str, int],
    warnings: list[str],
    timed_out: bool,
    harness_error: str,
    process_run_index: int = 0,
) -> dict[str, Any]:
    outcome = classify(values, timed_out=timed_out, harness_error=harness_error)
    nodes = [node for node, _ in timeline]
    code_generations = nodes.count("generate_code")
    simulation_runs = nodes.count("run_simulation")
    design = values.get("design_tree") or {}
    design_generation = values.get("design_generation") or {}
    design_method = design_generation.get("method")
    components = values.get("selected_components") or {}
    performance = values.get("real_performance") or {}
    acceptance = values.get("simulation_acceptance") or {}
    attestation = values.get("simulation_model_attestation") or {}
    return {
        "case_id": case["id"],
        "category": case["category"],
        "language": case["language"],
        "repeat": repeat,
        "project_id": project_id,
        "finished_at": dt.datetime.now(dt.UTC).isoformat(),
        "wall_time_s": round(wall_time_s, 2),
        # The first run in a process also loads the RAG index and embedding model.
        "process_run_index": process_run_index,
        "outcome": outcome,
        "expected_met": expected_met(case["category"], outcome),
        "retries": max(0, code_generations - 1) + max(0, simulation_runs - 1),
        "code_generations": code_generations,
        "simulation_runs": simulation_runs,
        "safety_retry_count": values.get("safety_retry_count", 0),
        "approvals": approvals,
        # What the deterministic parser extracted from the prompt (parse_prompt).
        "requirements": (values.get("design_params") or {}).get("requirements"),
        "design": {
            "method": design_method,
            "source": design_source(design_method),
            "topology": design.get("topology"),
            "payload_max_g": (design.get("payload") or {}).get("max_weight_g"),
            "payload_installed_g": (design.get("payload") or {}).get("installed_weight_g"),
            "status": values.get("design_status"),
        },
        "design_fallback": design_source(design_method) not in {None, "llm"},
        "code_fallback": any("Code generation fallback engaged" in item for item in warnings),
        "components": {
            "bom_status": values.get("bom_status"),
            "motor": (components.get("motor") or {}).get("model_name"),
            "motor_count": components.get("motor_count"),
            "battery": (components.get("battery") or {}).get("model_name"),
            "thrust_to_weight": performance.get("thrust_to_weight_ratio"),
            "hover_endurance_min": performance.get("hover_endurance_min"),
        },
        "identity": {
            "resolved_drone_sha256": values.get("resolved_drone_sha256"),
            "flight_model_bundle_sha256": values.get("flight_model_bundle_sha256"),
            "approved_code_sha256": values.get("approved_code_sha256"),
        },
        "flight_model": {
            "status": values.get("flight_model_status"),
            "block_reason": values.get("flight_model_block_reason"),
        },
        "safety_passed": values.get("safety_passed"),
        "safety_issues": _clip(values.get("safety_issues") or []),
        "simulation": {
            "status": values.get("simulation_status"),
            "block_reason": values.get("simulation_block_reason"),
            "runtime_mode": values.get("runtime_mode"),
            "telemetry_source": values.get("telemetry_source"),
            "mission_isolation": values.get("mission_isolation"),
            "mission_started": values.get("mission_started"),
            "mission_completed": values.get("mission_completed"),
            "crash_detected": values.get("crash_detected"),
            "geofence_violation": values.get("geofence_violation"),
            "telemetry_points": len(values.get("telemetry_data") or []),
            "duration_s": values.get("duration_s"),
            "mission_error": (values.get("mission_error") or "")[:400],
            "artifacts": values.get("simulation_artifacts") or {},
        },
        "acceptance": {
            "passed": acceptance.get("passed"),
            "scope": acceptance.get("scope"),
            "aircraft": acceptance.get("aircraft"),
            "generated_identity_attested": acceptance.get("generated_identity_attested"),
            "generated_aircraft_validated": acceptance.get("generated_aircraft_validated"),
            "reasons": _clip(acceptance.get("reasons") or []),
            # Requested versus flown, as the acceptance gate compared them.
            "requirements": acceptance.get("requirements"),
            # The flight against the approved code's own mission: path, altitude,
            # hold, landing and sampling, with the metrics behind the verdict.
            "flight_conformance": {
                key: (acceptance.get("flight_conformance") or {}).get(key)
                for key in ("evaluated", "passed", "reasons", "metrics")
            },
        },
        "attestation": {
            "status": attestation.get("status"),
            "simulated_total_mass_kg": attestation.get("simulated_total_mass_kg"),
            "px4_params_checked": attestation.get("px4_params_checked"),
        }
        if attestation
        else None,
        "errors": _clip(values.get("errors") or []),
        "warnings": _clip(warnings, limit=20),
        "timeline": [[node, round(t, 2)] for node, t in timeline],
        "harness_error": harness_error or None,
    }


async def run_case(
    case: dict[str, Any], repeat: int, timeout_s: float, process_run_index: int = 0
) -> dict[str, Any]:
    from agents.orchestrator import Orchestrator
    from config.settings import settings
    from graph.state import initial_state
    from graph.workflow import workflow

    # A UUID like the API's own projects; the case id is recorded alongside.
    project_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": project_id}}
    orchestrator = Orchestrator(use_postgres=False)
    timeline: list[tuple[str, float]] = []
    approvals = {"human_review_design": 0, "human_review_code": 0}
    capture = _WarningCapture()
    logging.getLogger().addHandler(capture)
    started = time.monotonic()

    async def drive(stream_input: Any) -> None:
        async for event in workflow.astream(stream_input, config=config, stream_mode="updates"):
            for node in event:
                timeline.append((node, time.monotonic() - started))

    async def pipeline() -> None:
        await drive(initial_state(case["prompt"], project_id, settings.max_simulation_iterations))
        for _ in range(MAX_APPROVALS):
            snapshot = await workflow.aget_state(config)
            if not snapshot.next:
                return
            gate = list(snapshot.next)[0]
            approvals[gate] = approvals.get(gate, 0) + 1
            await orchestrator.approve(project_id, approved=True)
            await drive(None)
        raise RuntimeError(f"more than {MAX_APPROVALS} review gates in one run")

    timed_out = False
    harness_error = ""
    try:
        await asyncio.wait_for(pipeline(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
    except Exception as exc:  # noqa: BLE001 — a crash is a result, not a reason to stop
        harness_error = f"{type(exc).__name__}: {exc}"[:600]
    finally:
        logging.getLogger().removeHandler(capture)
    wall = time.monotonic() - started
    try:
        values = dict((await workflow.aget_state(config)).values)
    except Exception:  # noqa: BLE001
        values = {}
    return build_record(
        case,
        repeat,
        project_id,
        values,
        wall_time_s=wall,
        timeline=timeline,
        approvals=approvals,
        warnings=capture.messages,
        timed_out=timed_out,
        harness_error=harness_error,
        process_run_index=process_run_index,
    )


# ── Driver ───────────────────────────────────────────────────────────────────


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _secs(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} s"


def render_markdown(summary: dict[str, Any], environment: dict[str, Any]) -> str:
    """Tables for the published report, generated from summary.json only."""
    lines = [
        f"# Benchmark results — {summary.get('suite')}",
        "",
        "Generated by `apps/api/scripts/run_benchmark.py --summarize`; do not edit by hand.",
        "",
        "| Category | Runs | Correct | Rate (Wilson 95%) | Decided | p50 | p95 | Retries | "
        "Design not from LLM | Code fallback |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, cat in summary["categories"].items():
        interval = cat["correct_rate_wilson95"]
        rate = (
            f"{_pct(cat['correct_rate'])} ({_pct(interval[0])}–{_pct(interval[1])})"
            if interval
            else "not scored"
        )
        correct = "—" if cat["correct"] is None else f"{cat['correct']}/{cat['scored_runs']}"
        lines.append(
            f"| {name} | {cat['runs']} | {correct} | {rate} | {cat['decided']}/{cat['runs']} | "
            f"{_secs(cat['wall_time_s']['p50'])} | {_secs(cat['wall_time_s']['p95'])} | "
            f"{cat['retries']['total']} in {cat['retries']['runs_with_retry']} runs | "
            f"{cat['design_not_from_llm_runs']} | {cat['code_fallback_runs']} |"
        )
    lines += ["", "| Category | Outcomes |", "|---|---|"]
    for name, cat in summary["categories"].items():
        outcomes = ", ".join(f"{key} {value}" for key, value in cat["outcomes"].items())
        lines.append(f"| {name} | {outcomes} |")
    lines += ["", "| Case | Language | Category | Outcomes |", "|---|---|---|---|"]
    for case_id, case in summary["cases"].items():
        lines.append(
            f"| `{case_id}` | {case['language']} | {case['category']} | "
            f"{', '.join(case['outcomes'])} |"
        )
    lines += ["", "| Language | Scored runs | Correct |", "|---|---|---|"]
    for language, stats in summary["languages"].items():
        lines.append(
            f"| {language} | {stats['scored_runs']} | {stats['correct']} "
            f"({_pct(stats['correct_rate'])}) |"
        )
    llm = environment.get("llm", {})
    sim = environment.get("simulator", {})
    host = environment.get("host", {})
    git = environment.get("git", {})
    lines += [
        "",
        "## Environment",
        "",
        f"- Host: {host.get('platform')}, {host.get('logical_cpus')} logical CPUs, "
        f"{host.get('memory_gb')} GB RAM, Python {host.get('python')}",
        f"- Models (Ollama {llm.get('ollama', {}).get('version')}): design "
        f"`{llm.get('design_model')}`, code `{llm.get('code_model')}`, "
        f"eval `{llm.get('eval_model')}`; digests in environment.json",
        f"- Simulator: {sim.get('runtime')} runtime, PX4 `{sim.get('px4_describe')}`, "
        f"Gazebo {sim.get('gazebo_sim')}, mission MAVSDK {sim.get('mission_mavsdk')}",
        f"- Code: `{git.get('head')}` on `{git.get('branch')}` with "
        f"{git.get('changed_paths')} changed and {git.get('untracked_paths')} untracked paths "
        f"(tracked diff SHA-256 `{git.get('tracked_diff_sha256')}`)",
        f"- Method: {summary['method']['percentiles']}; interval {summary['method']['interval']}; "
        f"retries = {summary['method']['retries']}",
        "",
    ]
    return "\n".join(lines)


def write_summary(out_dir: Path, suite: dict[str, Any]) -> dict[str, Any]:
    records = load_records(out_dir / "runs.jsonl")
    summary = {"suite": suite.get("suite"), **summarize(records)}
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    environment_path = out_dir / "environment.json"
    environment = (
        json.loads(environment_path.read_text(encoding="utf-8"))
        if environment_path.exists()
        else {}
    )
    (out_dir / "report.md").write_text(render_markdown(summary, environment), encoding="utf-8")
    return summary


# The run timeout is enforced with asyncio, which cannot interrupt a call that
# never returns inside a worker thread: on 2026-09-29 one run sat in a Chroma
# query for 17 hours and the whole benchmark with it. Past the timeout plus this
# grace, a thread outside the event loop records the run as a timeout and ends
# the process; ``--supervise`` then resumes with the next run.
WATCHDOG_GRACE_S = 300.0
WATCHDOG_EXIT_CODE = 75


def abandon_run(
    runs_path: Path, case: dict[str, Any], repeat: int, started: float, process_run_index: int
) -> None:
    """Record a run the event loop could not end, then exit the process."""
    record = build_record(
        case,
        repeat,
        "",
        {},
        wall_time_s=round(time.monotonic() - started, 2),
        timeline=[],
        approvals={},
        warnings=["harness watchdog: the run outlived its timeout; the process was ended"],
        timed_out=True,
        harness_error="",
        process_run_index=process_run_index,
    )
    with runs_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    print(f"    timeout (watchdog) wall={record['wall_time_s']}s", flush=True)
    os._exit(WATCHDOG_EXIT_CODE)


def supervise(argv: list[str]) -> int:
    """Run the benchmark, resuming after each watchdog exit until it finishes."""
    command = [sys.executable, str(Path(__file__).resolve()), *argv, "--resume"]
    while True:
        code = subprocess.run(command, check=False).returncode
        if code != WATCHDOG_EXIT_CODE:
            return code
        print("supervisor: a run was abandoned by the watchdog; resuming", flush=True)


def plan_runs(
    suite: dict[str, Any], only: list[str], repeats: int, repeats_other: int
) -> list[tuple[dict[str, Any], int]]:
    runs: list[tuple[dict[str, Any], int]] = []
    for case in suite["cases"]:
        if only and case["id"] not in only:
            continue
        count = repeats if case["category"] == "supported" else repeats_other
        runs.extend((case, index) for index in range(1, count + 1))
    return runs


async def main(args: argparse.Namespace) -> int:
    suite = json.loads(Path(args.suite).read_text(encoding="utf-8"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.summarize:
        print(json.dumps(write_summary(out_dir, suite), indent=2, ensure_ascii=False))
        return 0

    from config.settings import settings

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(out_dir / "benchmark.log", encoding="utf-8")],
    )
    runs_path = out_dir / "runs.jsonl"
    done = {(r["case_id"], r["repeat"]) for r in load_records(runs_path)} if args.resume else set()
    if runs_path.exists() and not args.resume:
        raise SystemExit(f"{runs_path} exists; pass --resume or choose a new --out")

    environment_path = out_dir / "environment.json"
    if not environment_path.exists():
        environment = {
            "suite": suite.get("suite"),
            "suite_sha256": hashlib.sha256(Path(args.suite).read_bytes()).hexdigest(),
            **capture_environment(settings),
        }
        environment_path.write_text(
            json.dumps(environment, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    planned = plan_runs(suite, args.only, args.repeats, args.repeats_other)
    pending = [(case, repeat) for case, repeat in planned if (case["id"], repeat) not in done]
    print(f"{len(planned)} planned, {len(planned) - len(pending)} already recorded", flush=True)
    for index, (case, repeat) in enumerate(pending, start=1):
        print(f"[{index}/{len(pending)}] {case['id']} #{repeat} …", flush=True)
        watchdog = threading.Timer(
            args.run_timeout_s + WATCHDOG_GRACE_S,
            abandon_run,
            args=(runs_path, case, repeat, time.monotonic(), index),
        )
        watchdog.daemon = True
        watchdog.start()
        try:
            record = await run_case(case, repeat, args.run_timeout_s, process_run_index=index)
        finally:
            watchdog.cancel()
        with runs_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        print(
            f"    {record['outcome']} expected_met={record['expected_met']} "
            f"wall={record['wall_time_s']}s retries={record['retries']}",
            flush=True,
        )
    summary = write_summary(out_dir, suite)
    print(json.dumps(summary["categories"], indent=2, ensure_ascii=False))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--suite", default=str(DEFAULT_SUITE))
    parser.add_argument("--out", required=True, help="Result directory (runs.jsonl, summary.json)")
    parser.add_argument("--only", nargs="*", default=[], help="Case ids to run")
    parser.add_argument("--repeats", type=int, default=3, help="Runs per supported case")
    parser.add_argument("--repeats-other", type=int, default=3, help="Runs per other case")
    parser.add_argument("--run-timeout-s", type=float, default=2400.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--summarize", action="store_true", help="Rebuild summary.json only")
    parser.add_argument(
        "--supervise",
        action="store_true",
        help="Resume after a run is abandoned by the watchdog, until every run is recorded",
    )
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.repeats_other < 0:
        parser.error("--repeats must be >= 1 and --repeats-other >= 0")
    return args


if __name__ == "__main__":
    parsed = parse_args()
    if parsed.supervise:
        raise SystemExit(supervise([a for a in sys.argv[1:] if a != "--supervise"]))
    try:
        # mavsdk installs uvloop's policy on import where uvloop exists; settle
        # the policy before the loop that spawns simulator subprocesses.
        import mavsdk  # noqa: F401
    except ImportError:
        pass
    raise SystemExit(asyncio.run(main(parsed)))
