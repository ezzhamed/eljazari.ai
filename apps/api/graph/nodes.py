"""eljazari.ai — LangGraph Node Implementations (Prompt 2.1)

Each node is an async function that:
  - receives the full ElJazariState
  - returns a *partial* dict that LangGraph merges back into state

Node order in the pipeline:
  parse_prompt → generate_design → human_review_design → convert_to_urdf
  → generate_code → safety_check → human_review_code
  → run_simulation → evaluate_simulation → generate_monitor_report
"""

import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Any

import structlog

from graph.state import ElJazariState

log = structlog.get_logger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────


def _log_entry(agent: str, status: str, message: str) -> dict[str, Any]:
    return {
        "agent": agent,
        "status": status,
        "message": message,
        "ts": time.time(),
    }


def _maybe_model_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def _extract_design_tree(result: Any) -> tuple[dict[str, Any], dict[str, Any], str]:
    if isinstance(result, dict):
        if isinstance(result.get("design_tree"), dict):
            return (
                result["design_tree"],
                result.get("performance_estimate") or {},
                result.get("generation_method", "legacy"),
            )
        raw_response = result.get("raw_response")
        if isinstance(raw_response, str):
            parsed = json.loads(raw_response)
            if isinstance(parsed, dict):
                return (
                    parsed,
                    result.get("performance_estimate") or {},
                    result.get("generation_method", "legacy"),
                )
        return (
            result,
            result.get("performance_estimate") or {},
            result.get("generation_method", "legacy"),
        )

    design_tree = _maybe_model_dump(getattr(result, "design_tree", {}) or {})
    performance_estimate = _maybe_model_dump(getattr(result, "performance_estimate", {}) or {})
    generation_method = getattr(result, "generation_method", "rag_llm")
    return design_tree, performance_estimate, generation_method


def _extract_code_payload(result: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(result, dict):
        return str(result.get("code", "")), result.get("gsce_config") or {}
    code = getattr(result, "code", "")
    constraints_applied = getattr(result, "constraints_applied", None) or []
    return str(code), {"constraints_applied": constraints_applied}


def _extract_monitor_report_payload(report: Any) -> dict[str, Any]:
    if isinstance(report, dict):
        return dict(report)
    payload = _maybe_model_dump(report)
    return payload if isinstance(payload, dict) else {}


def build_raw_telemetry(
    *,
    points: list[dict[str, Any]],
    telemetry_source: str,
    mission_error: str = "",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble the stored telemetry blob, including why a run failed.

    mission_error and warnings are produced by PX4Service and used to be dropped
    at persistence time, so a failed simulation reached the UI with no cause and
    the explanation survived only in a temp-directory artifact. A run that fails
    without saying why is nearly as unhelpful as no run at all.
    """
    return {
        "points": points,
        "telemetry_source": telemetry_source,
        "mission_error": mission_error or None,
        "warnings": list(warnings or []),
    }


async def _persist_simulation_record(
    *,
    project_id: str,
    executed_code: str,
    iteration: int,
    telemetry: list[dict[str, Any]],
    observations: list[Any],
    mission_completed: bool,
    crash_detected: bool,
    telemetry_source: str,
    duration_s: float | None,
    mission_error: str = "",
    warnings: list[str] | None = None,
    model_attestation: dict[str, Any] | None = None,
) -> str | None:
    """Persist a Simulation row so /simulate/results reflects orchestrator runs.

    Return the committed row ID; never associate a different code revision.
    Best-effort: never raises. The pipeline must succeed even when persistence
    fails (e.g. unmigrated DB in a smoke test).
    """
    try:
        from db.session import current_session_factory
        from models.simulation import SimulationStatus
        from repositories.generated_code import GeneratedCodeRepository
        from repositories.simulation import SimulationRepository
        from schemas.simulation import SimulationCreate, SimulationUpdate
        from services.project_access import parse_project_uuid

        project_uuid = parse_project_uuid(project_id)
        observation_payloads: list[dict[str, Any]] = []
        for obs in observations:
            if isinstance(obs, dict):
                observation_payloads.append(obs)
            elif hasattr(obs, "model_dump"):
                observation_payloads.append(obs.model_dump())
            else:
                observation_payloads.append({"description": str(obs)})

        async with current_session_factory()() as db:
            code_repo = GeneratedCodeRepository(db)
            sim_repo = SimulationRepository(db)
            code_record = await code_repo.get_latest_for_project(project_uuid)
            if code_record is None:
                return None
            if code_record.code_content != executed_code:
                log.warning("run_simulation.persist_code_mismatch", project_id=project_id)
                return None
            simulation = await sim_repo.create_from_schema(
                SimulationCreate(
                    project_id=project_uuid,
                    code_id=code_record.id,
                    iteration=max(iteration, 1),
                    config={
                        "source": "orchestrator",
                        "telemetry_source": telemetry_source,
                        # Which aircraft flew: None means the reference x500.
                        "model_attestation": model_attestation,
                    },
                )
            )
            await sim_repo.update_from_schema(
                simulation,
                SimulationUpdate(
                    status=(
                        SimulationStatus.crashed
                        if crash_detected
                        else SimulationStatus.pending
                        if mission_completed
                        else SimulationStatus.failed
                    ),
                    raw_telemetry=build_raw_telemetry(
                        points=telemetry,
                        telemetry_source=telemetry_source,
                        mission_error=mission_error,
                        warnings=warnings,
                    ),
                    semantic_observations=observation_payloads,
                    success_rate=None,
                    completeness=None,
                    duration_seconds=float(duration_s) if duration_s is not None else None,
                ),
            )
            record_id = str(simulation.id)
            await db.commit()
            return record_id
    except Exception as exc:  # pragma: no cover — best-effort persistence
        log.warning(
            "run_simulation.persist_skipped",
            project_id=project_id,
            error=str(exc),
        )


def _write_engineering_deliverables(
    state: ElJazariState,
    report_payload: dict[str, Any],
    commanded_waypoints: list[dict[str, float]],
    target_alt_m: float | None,
) -> list[str]:
    """Phase 14 wiring: write BOM / assembly / preflight / maintenance /
    engineering report Markdown + CSV files into the project's storage
    directory after the orchestrator finishes. Best-effort; any failure
    leaves the rest of the pipeline state intact.
    """
    paths: list[str] = []
    try:
        from pathlib import Path

        from config.settings import settings
        from pipelines.engineering_deliverables import (
            ComponentLine,
            DeliverableBundle,
            render_all_deliverables,
            component_lines_from_selection,
        )
        from agents.component_models import SelectedComponents
        from agents.design_models import DroneDesignTree
        from pipelines.performance_solver import PerformanceSolver

        project_id = str(state.get("project_id") or "unknown")
        out_dir = Path(settings.storage_path) / project_id / "deliverables"
        out_dir.mkdir(parents=True, exist_ok=True)

        design_tree = state.get("design_tree") or {}
        frame = design_tree.get("frame") or {}
        battery = design_tree.get("battery") or {}
        topology = design_tree.get("topology", "")

        # Phase 11 metrics from the just-finished monitor report.
        metrics = (report_payload or {}).get("metrics") or {}
        fq_score = metrics.get("flight_quality_score") or report_payload.get("overall_score")
        fq_grade = metrics.get("flight_quality_grade") or report_payload.get("grade")
        if state.get("simulation_block_reason"):
            fq_score = None
            fq_grade = None

        # Phase 6 performance estimate (hover endurance + max speed) from the
        # design phase, so the engineering report doesn't show "—" for them.
        perf = state.get("performance_estimate") or {}
        hover_time_s = perf.get("hover_time_s")
        hover_endurance_min = float(hover_time_s) / 60.0 if hover_time_s not in (None, 0) else None
        max_speed_ms = perf.get("max_speed_ms")
        max_speed_ms = float(max_speed_ms) if max_speed_ms not in (None, 0) else None

        # Persist the generated mission code next to the URDF and reference it
        # in the bundle so the report lists a real path instead of "—".
        mission_code_path = ""
        generated_code = state.get("generated_code") or ""
        if generated_code:
            code_file = Path(settings.storage_path) / project_id / "mission_code.py"
            try:
                code_file.write_text(generated_code, encoding="utf-8")
                mission_code_path = str(code_file)
            except Exception:
                mission_code_path = ""

        # Legacy checkpoints may lack a selected BOM; keep their exports explicitly preliminary.
        components: list[ComponentLine] = []
        total_weight_g = float(perf.get("total_weight_g") or 0)
        frame_type = str(frame.get("type") or topology or "")
        arm_length_mm = float(frame.get("arm_length_mm") or 0)
        engineering_notes = [
            "Preliminary design only: selected components are unavailable; BOM is incomplete."
        ]
        battery_chemistry = str(battery.get("type") or "LiPo")
        cells = int(battery.get("cells") or 4)
        capacity = float(battery.get("capacity_mah") or 0)
        components.append(
            ComponentLine(
                category="Frame",
                part_name=f"{topology or 'multicopter'} "
                f"{frame.get('type', 'X')}-frame "
                f"({frame.get('arm_length_mm', 0):.0f} mm arms)",
                qty=1,
            )
        )
        components.append(
            ComponentLine(
                category="Battery",
                part_name=f"LiPo {cells}S {capacity:.0f}mAh ({battery.get('weight_g', 0):.0f} g)",
                qty=1,
                weight_g=float(battery.get("weight_g") or 0),
            )
        )

        selected_data = state.get("selected_components")
        resolved_spec = None
        if selected_data:
            import hashlib
            import json

            selected = SelectedComponents.model_validate(selected_data)
            design = DroneDesignTree.model_validate(design_tree)
            payload_g = design.payload.design_load_g
            if state.get("resolved_drone_sha256"):
                from pipelines.resolved_drone import resolve_drone, ResolvedDroneSpec

                expected = resolve_drone(design, selected)
                stored = ResolvedDroneSpec.model_validate(state.get("resolved_drone_spec"))
                if (
                    expected.sha256 != state["resolved_drone_sha256"]
                    or stored.sha256 != expected.sha256
                ):
                    raise ValueError(
                        "Selected components or geometry changed after model generation"
                    )
            resolved_performance = PerformanceSolver().solve(selected, extra_mass_g=payload_g)
            components = component_lines_from_selection(selected)
            total_weight_g = resolved_performance.total_mass_g
            hover_endurance_min = resolved_performance.hover_endurance_min
            # The catalog solver does not estimate maximum speed; do not mix in
            # the earlier design's unrelated KV-based speed prediction.
            max_speed_ms = None
            frame_type = selected.frame.geometry
            arm_length_mm = selected.frame.arm_length_mm
            battery_chemistry = selected.battery.chemistry
            cells = selected.battery.cells_s
            capacity = selected.battery.capacity_mah
            engineering_notes = [
                "Mass includes selected catalog parts and declared payload; wiring, fasteners, "
                "power distribution and external sensors still need a measured allowance.",
                "Performance is an estimate; matching motor/propeller/voltage test data "
                "and validation of the generated airframe in SITL are still required.",
                f"Payload assessed: {payload_g:g} g ("
                + (
                    "installed payload)."
                    if design.payload.installed_weight_g is not None
                    else "full requested payload; installed mass was not specified)."
                ),
            ]
            evidence = resolved_performance.propulsion_evidence
            engineering_notes.append(
                f"Propulsion evidence: {evidence.status}. "
                + "; ".join(evidence.reasons)
                + " Source declarations are not independently verified or flight validation."
            )
            acceptance = state.get("simulation_acceptance")
            if acceptance is not None and not acceptance.get("passed"):
                engineering_notes.append(
                    "Simulation rejected by basic checks: "
                    + "; ".join(acceptance.get("reasons", []))
                )
            if state.get("resolved_drone_sha256"):
                engineering_notes.append(
                    f"Structural model fingerprint: {state['resolved_drone_sha256']}. "
                    "URDF/SDF mass and geometry are linked to this BOM; this is not flight validation."
                )
            if state.get("flight_model_bundle_sha256"):
                engineering_notes.append(
                    f"SITL flight model bundle: {state['flight_model_bundle_sha256']}. Scope "
                    "sitl_catalog_estimate: unverified catalog thrust curves plus PX4 x500 "
                    "reference coefficients; not a calibrated or airworthy model."
                )
            elif state.get("flight_model_block_reason"):
                engineering_notes.append(
                    f"No SITL flight model: {state['flight_model_block_reason']}"
                )
            attestation = state.get("simulation_model_attestation")
            if attestation and acceptance and acceptance.get("generated_identity_attested"):
                engineering_notes.append(
                    f"PX4 SITL flew the generated model {attestation.get('model_instance')} "
                    f"(simulator mass {attestation.get('simulated_total_mass_kg')} kg). "
                    "This is simulation evidence only."
                )
            if payload_g:
                components.append(
                    ComponentLine(
                        category="Payload",
                        part_name="Declared payload (specification only)",
                        weight_g=payload_g,
                        notes="Not a catalog purchase item; price unavailable",
                    )
                )
            if state.get("simulation_block_reason"):
                engineering_notes.append(state["simulation_block_reason"])
            resolved_spec = {
                "schema_version": 1,
                "scope": "selected_components_only_not_a_validated_airframe",
                "components": selected.model_dump(mode="json"),
                "structural_model_sha256": state.get("resolved_drone_sha256"),
                "model_validation_scope": state.get("model_validation_scope", "unavailable"),
                "flight_model_bundle_sha256": state.get("flight_model_bundle_sha256"),
                "simulation_model_attestation": state.get("simulation_model_attestation"),
                "payload_g": payload_g,
                "payload_basis": "installed"
                if design.payload.installed_weight_g is not None
                else "requested_maximum",
                "quantities": {line.category: line.qty for line in components},
                "performance": resolved_performance.model_dump(mode="json"),
                "limitations": engineering_notes,
            }
            canonical = json.dumps(
                resolved_spec, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            resolved_spec["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        bundle = DeliverableBundle(
            project_id=project_id,
            project_name=f"Drone {project_id[:8]}",
            mission_prompt=state.get("user_prompt") or "",
            frame_type=frame_type,
            arm_length_mm=arm_length_mm,
            total_weight_g=total_weight_g,
            engineering_notes=engineering_notes,
            battery_summary=f"{cells}S {capacity:.0f}mAh {battery_chemistry}",
            components=components,
            flight_quality_score=float(fq_score) if fq_score is not None else None,
            flight_quality_grade=str(fq_grade) if fq_grade else None,
            position_rmse_m=float(metrics.get("position_rmse_m") or 0),
            altitude_rmse_m=float(metrics.get("altitude_rmse_m") or 0),
            tracking_evaluated=bool(metrics.get("tracking_evaluated")),
            altitude_tracking_evaluated=bool(metrics.get("altitude_tracking_evaluated")),
            return_to_home_error_m=float(metrics.get("return_to_home_error_m") or 0),
            hover_endurance_min=hover_endurance_min,
            max_speed_ms=max_speed_ms,
            tested_scenarios=[],
            urdf_path=str(state.get("urdf_path") or ""),
            sdf_path=str(state.get("sdf_path") or ""),
            mission_code_path=mission_code_path,
        )

        files = render_all_deliverables(bundle)
        if resolved_spec is not None:
            files["selected_components.json"] = (
                json.dumps(resolved_spec, indent=2, allow_nan=False) + "\n"
            )
        elif (out_dir / "selected_components.json").exists():
            # Do not leave a previous run's selection beside a preliminary report.
            (out_dir / "selected_components.json").unlink()
        for name, content in files.items():
            target = out_dir / name
            target.write_text(content, encoding="utf-8")
            paths.append(str(target))
        log.info(
            "deliverables.written",
            project_id=project_id,
            count=len(paths),
            dir=str(out_dir),
        )
    except Exception as exc:  # pragma: no cover — best-effort writer
        log.warning("deliverables.write_failed", error=str(exc))
    return paths


def _extract_mission_plan_from_code(code: str) -> tuple[list[dict[str, float]], float | None]:
    """Best-effort extraction of (commanded_waypoints, target_altitude_m) from
    generated mission code. Phase 11's flight quality solver needs both so it
    can grade cross-track and altitude tracking — without them the tracking
    pillar reports "not evaluated". Falls back to an empty plan on any error.

    The function consumes either real MAVSDK code (``MissionItem(...)`` list
    with NED-derived lat/lon) or legacy skill-API code
    (``fly_to_relative(north_m=, east_m=)``); the post-processor in
    ``pipelines/code_post_processor`` already converts the latter to the
    former, so by the time this runs the input is usually real MAVSDK — but
    we cover both paths to stay robust.
    """
    try:
        from pipelines.code_post_processor import extract_plan_from_legacy_code
    except Exception:
        return [], None
    if not code:
        return [], None
    try:
        plan = extract_plan_from_legacy_code(code)
        if plan.waypoints:
            commanded = [
                {"north_m": float(wp.north_m), "east_m": float(wp.east_m)} for wp in plan.waypoints
            ]
            return commanded, float(plan.takeoff_altitude_m)
    except Exception:
        pass
    # NOTE: real-MAVSDK code stores waypoints as MissionItem(latitude_deg=...)
    # which the legacy extractor above does not parse. Reconstructing the
    # commanded NED frame post-hoc proved unreliable (home/convention mismatch
    # produced spurious cross-track), so we deliberately return "not evaluated"
    # rather than a misleading tracking grade. The flight-quality score drops
    # the tracking pillar accordingly.
    return [], None


def _decode_observation_text(value: Any) -> str:
    text = str(value)
    if "\\u" not in text and "\\x" not in text:
        return text
    try:
        return text.encode("ascii").decode("unicode_escape")
    except Exception:
        return text


# ─────────────────────────────────────────────────────────────────────────────
# 1. parse_prompt
# ─────────────────────────────────────────────────────────────────────────────


async def parse_prompt(state: ElJazariState) -> dict[str, Any]:
    """Validate and normalise the user prompt before any agent work."""
    prompt: str = state.get("user_prompt", "").strip()

    if not prompt:
        return {
            "errors": ["Empty user prompt — pipeline cannot start"],
            "current_phase": "error",
            "agent_logs": [_log_entry("parse_prompt", "error", "Empty prompt")],
        }

    from agents.safety_models import FlightConstraints
    from config.settings import settings
    from pipelines.request_requirements import (
        request_limit_violations,
        requested_requirements,
    )
    from utils.mission_parser import parse_mission_spec

    # Extract the request's explicit values deterministically (no LLM here).
    spec = parse_mission_spec(prompt)
    params: dict[str, Any] = {
        "original": prompt,
        "length": len(prompt),
        "language": spec.language,
        "requirements": requested_requirements(spec),
    }
    if spec.duration_min is not None:
        # Component selection enforces it; it defaulted to 5 min before.
        params["min_endurance_min"] = spec.duration_min

    # A request that itself breaks a limit is infeasible. Letting it reach code
    # generation meant the safety layer rejected it and the retry loop flew a
    # weaker mission instead (docs/BENCHMARK.md, finding 1).
    violations = request_limit_violations(
        spec,
        max_altitude_m=settings.max_altitude_m,
        min_altitude_m=FlightConstraints.model_fields["min_altitude_m"].default,
        max_geofence_radius_m=settings.max_geofence_radius_m,
    )
    if violations:
        log.info("parse_prompt.infeasible", project_id=state.get("project_id"), reasons=violations)
        return {
            "design_params": params,
            "errors": [f"Request cannot be flown as stated: {reason}" for reason in violations],
            "current_phase": "error",
            "agent_logs": [
                _log_entry("parse_prompt", "error", "Infeasible request: " + "; ".join(violations))
            ],
        }

    log.info("parse_prompt.complete", project_id=state.get("project_id"), lang=params["language"])
    return {
        "design_params": params,
        "design_status": "pending",
        "current_phase": "parse_prompt",
        "agent_logs": [
            _log_entry(
                "parse_prompt",
                "complete",
                f"Prompt parsed ({params['language']}, {params['length']} chars)",
            )
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. generate_design
# ─────────────────────────────────────────────────────────────────────────────


async def generate_design(state: ElJazariState) -> dict[str, Any]:
    """Call DesignAgent to convert the prompt into a JSON design tree."""
    from agents.design_agent import DesignAgent  # lazy import avoids circular

    project_id = state.get("project_id", "unknown")
    prompt = state.get("user_prompt", "")
    feedback = state.get("design_feedback") or ""

    log.info("generate_design.start", project_id=project_id)

    try:
        agent = DesignAgent()
        result = await agent.run(prompt=prompt, feedback=feedback)

        # result is a DesignResult Pydantic model — extract the design tree as dict
        design_tree, performance_estimate, generation_method = _extract_design_tree(result)
        repair_attempts = int(getattr(result, "repair_attempts", 0) or 0)
        design_diagnostics = getattr(result, "diagnostics", {}) or {}

        log.info("generate_design.complete", project_id=project_id)
        return {
            "design_tree": design_tree,
            "performance_estimate": performance_estimate,
            "design_generation": {
                "method": generation_method,
                "repair_attempts": repair_attempts,
                "diagnostics": design_diagnostics,
            },
            "design_diagnostics": design_diagnostics,
            "design_status": "generated",
            "current_phase": "generate_design",
            "agent_logs": [
                _log_entry(
                    "design_agent",
                    "complete",
                    f"Design tree generated via {generation_method} (repairs={repair_attempts})",
                )
            ],
        }

    except Exception as exc:
        log.error("generate_design.error", error=str(exc), project_id=project_id)
        return {
            "design_status": "rejected",
            "errors": [f"Design generation failed: {exc}"],
            "current_phase": "generate_design",
            "agent_logs": [_log_entry("design_agent", "error", str(exc))],
        }


# ─────────────────────────────────────────────────────────────────────────────
# 3. human_review_design  (HITL interrupt point)
# ─────────────────────────────────────────────────────────────────────────────


async def human_review_design(state: ElJazariState) -> dict[str, Any]:
    """Set the HITL flag and pause — LangGraph `interrupt_before` handles the rest.

    When the graph is compiled with ``interrupt_before=["human_review_design"]``
    this node is never actually executed; the graph suspends *before* it.
    The node body runs only when the graph is resumed with an updated state
    (human_feedback set, human_approval_needed=False).
    """
    feedback = state.get("human_feedback") or ""
    approved = not state.get("human_approval_needed", True)

    if approved:
        status = "approved"
        log.info("human_review_design.approved", project_id=state.get("project_id"))
    else:
        status = "rejected"
        log.info("human_review_design.rejected", project_id=state.get("project_id"))

    return {
        "design_status": status,
        "design_feedback": feedback if not approved else state.get("design_feedback"),
        "current_phase": "human_review_design",
        "human_approval_needed": False,
        "agent_logs": [_log_entry("hitl", status, f"Design review: {status}")],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3.5  select_components  (Phase 6 — Real BOM)
# ─────────────────────────────────────────────────────────────────────────────


async def select_components(state: ElJazariState) -> dict[str, Any]:
    """Pick real, purchaseable components matching the approved design.

    Runs *after* human_review_design and *before* convert_to_urdf so the URDF
    uses real component masses, not templated boxes.
    """
    from agents.component_selector_agent import ComponentSelectorAgent
    from agents.design_models import DroneDesignTree

    project_id = state.get("project_id", "unknown")
    design_tree = state.get("design_tree") or {}
    log.info("select_components.start", project_id=project_id)

    try:
        design = DroneDesignTree(**design_tree)
        agent = ComponentSelectorAgent()
        result = agent.select(
            design=design,
            payload_g=design.payload.design_load_g,
            min_endurance_min=float(
                (state.get("design_params") or {}).get("min_endurance_min", 5.0)
            ),
            # The request's limit on the whole aircraft, when it states one.
            max_total_mass_g=(
                ((state.get("design_params") or {}).get("requirements") or {}).get(
                    "max_total_mass_g"
                )
            ),
        )
        chosen_payload = {
            "name": result.chosen.name,
            "score": result.chosen.score,
            "components": result.chosen.components.model_dump(),
            "performance": result.chosen.performance.model_dump(),
        }
        alts = [
            {
                "name": t.name,
                "score": t.score,
                "components": t.components.model_dump(),
                "performance": t.performance.model_dump(),
            }
            for t in result.alternatives
        ]
        log.info(
            "select_components.complete",
            project_id=project_id,
            chosen_tier=result.chosen.name,
            score=result.chosen.score,
            warnings=len(result.chosen.performance.warnings),
            t_w=result.chosen.performance.thrust_to_weight_ratio,
        )
        return {
            "selected_components": chosen_payload["components"],
            "real_performance": chosen_payload["performance"],
            "component_alternatives": alts,
            "component_search": {
                "evaluated_candidates": result.evaluated_candidates,
                "feasible_candidates": result.feasible_candidates,
                "sourced_candidates": result.sourced_candidates,
                "notes": list(result.notes),
                "scope": "catalog_candidates_under_existing_filters",
            },
            "bom_status": "selected",
            "current_phase": "select_components",
            "agent_logs": [
                _log_entry(
                    "component_selector",
                    "complete",
                    (
                        f"{result.chosen.name} tier: "
                        f"{result.chosen.components.motor.model_name} × {result.chosen.components.motor_count} | "
                        f"T:W={result.chosen.performance.thrust_to_weight_ratio:.2f} | "
                        f"hover={result.chosen.performance.hover_endurance_min:.1f}min"
                    ),
                ),
                *(_log_entry("component_selector", "warning", note) for note in result.notes),
            ],
        }
    except Exception as exc:
        log.error("select_components.error", error=str(exc), project_id=project_id)
        return {
            "bom_status": "failed",
            "selected_components": None,
            "real_performance": None,
            "component_alternatives": [],
            "component_search": None,
            "errors": [f"Component selection failed: {exc}"],
            "current_phase": "select_components",
            "agent_logs": [_log_entry("component_selector", "error", str(exc))],
        }


# ─────────────────────────────────────────────────────────────────────────────
# 4. convert_to_urdf
# ─────────────────────────────────────────────────────────────────────────────


_NO_FLIGHT_BUNDLE = {
    "flight_model_manifest_path": None,
    "flight_model_bundle_sha256": None,
    "flight_model_status": "unavailable",
    "flight_model_block_reason": None,
}


def _write_flight_bundle(resolved: Any, output_dir: Path) -> dict[str, Any]:
    """Derive the PX4/Gazebo bundle; a refusal blocks flight, not the conversion."""
    from pipelines.flight_model import verify_flight_bundle, write_flight_model

    try:
        manifest = write_flight_model(resolved, output_dir)
        bundle = verify_flight_bundle(manifest, resolved.sha256)
    except Exception as exc:  # noqa: BLE001 - reported as the flight block reason
        log.warning("convert_to_urdf.flight_model_unavailable", error=str(exc))
        return {
            **_NO_FLIGHT_BUNDLE,
            "flight_model_status": "failed",
            "flight_model_block_reason": str(exc),
        }
    return {
        "flight_model_manifest_path": str(manifest),
        "flight_model_bundle_sha256": bundle.bundle_sha256,
        "flight_model_status": "generated",
        "flight_model_block_reason": None,
    }


def _code_sha256(code: str) -> str:
    import hashlib

    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def code_approval_block_reason(approved_sha256: str | None, code: str) -> str | None:
    """Refuse to execute code that is not byte-identical to what was approved."""
    if not approved_sha256:
        return "Mission code has no recorded approval; it will not be executed."
    if _code_sha256(code) != approved_sha256:
        return "Mission code differs from the approved version; it will not be executed."
    return None


def _simulation_model_config(state: ElJazariState) -> dict[str, Any]:
    """Identity fields the SITL backend needs to load and attest the aircraft."""
    keys = (
        "urdf_path",
        "sdf_path",
        "model_manifest_path",
        "resolved_drone_sha256",
        "flight_model_manifest_path",
        "bom_status",
    )
    return {key: state[key] for key in keys if state.get(key)}


async def convert_to_urdf(state: ElJazariState) -> dict[str, Any]:
    """Convert the approved JSON design tree to URDF/SDF files."""
    from pipelines.json_to_urdf import JSONToURDFConverter
    from config.settings import settings

    project_id = state.get("project_id", "unknown")
    design_tree = state.get("design_tree") or {}

    log.info("convert_to_urdf.start", project_id=project_id)

    try:
        output_dir = Path(settings.storage_path) / str(project_id) / "urdf"
        converter = JSONToURDFConverter()
        resolved = None
        if state.get("selected_components"):
            from agents.component_models import SelectedComponents
            from agents.design_models import DroneDesignTree
            from pipelines.resolved_drone import resolve_drone

            resolved = resolve_drone(
                DroneDesignTree.model_validate(design_tree),
                SelectedComponents.model_validate(state["selected_components"]),
            )
            paths = converter.convert(design_tree, output_dir, resolved_spec=resolved)
        else:
            if state.get("bom_status") == "selected":
                raise ValueError("Selected BOM is missing; refusing a legacy model fallback")
            paths = converter.convert(design_tree, output_dir)
        if isinstance(paths, dict):
            urdf_path = str(paths.get("urdf", ""))
            sdf_path = str(paths.get("sdf", ""))
        else:
            urdf_path = str(getattr(paths, "urdf_path", "") or "")
            sdf_path = str(getattr(paths, "sdf_path", "") or "")

        flight = _write_flight_bundle(resolved, output_dir) if resolved else _NO_FLIGHT_BUNDLE
        log.info("convert_to_urdf.complete project_id=%s urdf=%s", project_id, urdf_path)
        logs = [_log_entry("urdf_converter", "complete", f"URDF saved: {Path(urdf_path).name}")]
        if resolved:
            logs.append(
                _log_entry(
                    "urdf_converter",
                    "complete" if flight["flight_model_status"] == "generated" else "warning",
                    "SITL flight model generated (catalog estimate)"
                    if flight["flight_model_status"] == "generated"
                    else f"No SITL flight model: {flight['flight_model_block_reason']}",
                )
            )
        return {
            "urdf_path": urdf_path,
            "sdf_path": sdf_path,
            "conversion_status": "complete",
            "resolved_drone_spec": resolved.model_dump(mode="json") if resolved else None,
            "resolved_drone_sha256": resolved.sha256 if resolved else None,
            "model_manifest_path": str(paths.manifest_path) if resolved else None,
            "model_validation_scope": "structural_only" if resolved else "legacy_visualization",
            **flight,
            "current_phase": "convert_to_urdf",
            # The feed shows this to the user; a full local path is noise there
            # and leaks the machine's layout into screenshots.
            "agent_logs": logs,
        }

    except Exception as exc:
        log.error("convert_to_urdf.error", error=str(exc), project_id=project_id)
        return {
            "conversion_status": "failed",
            "urdf_path": None,
            "sdf_path": None,
            "resolved_drone_spec": None,
            "resolved_drone_sha256": None,
            "model_manifest_path": None,
            "model_validation_scope": "unavailable",
            **_NO_FLIGHT_BUNDLE,
            "errors": [f"URDF conversion failed: {exc}"],
            "current_phase": "convert_to_urdf",
            "agent_logs": [_log_entry("urdf_converter", "error", str(exc))],
        }


# ─────────────────────────────────────────────────────────────────────────────
# 5. generate_code
# ─────────────────────────────────────────────────────────────────────────────


async def generate_code(state: ElJazariState) -> dict[str, Any]:
    """Call CodeAgent (GSCE framework) to produce PX4/MAVSDK mission code."""
    from agents.code_agent import CodeGenerationAgent  # type: ignore[attr-defined]

    project_id = state.get("project_id", "unknown")
    iteration = state.get("simulation_iteration", 0)
    feedback = state.get("code_feedback") or state.get("human_feedback") or ""

    log.info("generate_code.start", project_id=project_id, iteration=iteration)

    try:
        from pipelines.codegen_design import codegen_design_context

        code_design = codegen_design_context(state)
        agent = CodeGenerationAgent()
        result = await agent.generate_code(
            mission_description=state.get("user_prompt", ""),
            design_tree=code_design,
            constraints_params=state.get("gsce_config") or {},
            feedback=feedback,
        )
        code, gsce_config = _extract_code_payload(result)

        log.info("generate_code.complete", project_id=project_id, code_len=len(code))
        return {
            "approved_code_sha256": None,
            "generated_code": code,
            "gsce_config": {**(state.get("gsce_config") or {}), **gsce_config},
            "code_status": "generated",
            "code_feedback": None,
            "current_phase": "generate_code",
            "agent_logs": [
                _log_entry(
                    "code_agent",
                    "complete",
                    f"Code generated ({len(code)} chars, iter {iteration})",
                )
            ],
        }

    except Exception as exc:
        log.error("generate_code.error", error=str(exc), project_id=project_id)
        return {
            "approved_code_sha256": None,
            "code_status": "rejected",
            "generated_code": "",
            "gsce_config": {},
            "errors": [f"Code generation failed: {exc}"],
            "current_phase": "generate_code",
            "agent_logs": [_log_entry("code_agent", "error", str(exc))],
        }


# ─────────────────────────────────────────────────────────────────────────────
# 6. safety_check
# ─────────────────────────────────────────────────────────────────────────────


async def safety_check(state: ElJazariState) -> dict[str, Any]:
    """Run SafetyLayer: AST analysis + reachability + protocol injection."""
    from agents.safety_layer import SafetyLayer
    from config.settings import settings

    project_id = state.get("project_id", "unknown")
    code = state.get("generated_code") or ""

    log.info("safety_check.start", project_id=project_id)

    if not code:
        return {
            "safety_passed": False,
            "safety_issues": ["No code provided to safety checker"],
            "errors": ["safety_check: generated_code is empty"],
            "current_phase": "safety_check",
            "agent_logs": [_log_entry("safety_layer", "error", "Empty code — cannot analyse")],
        }

    mission_limits = state.get("gsce_config") or {}
    safety = SafetyLayer(
        max_altitude=min(
            settings.max_altitude_m,
            float(mission_limits.get("max_altitude_m", settings.max_altitude_m)),
        ),
        max_speed=min(
            settings.max_speed_ms, float(mission_limits.get("max_speed_ms", settings.max_speed_ms))
        ),
        max_geofence_radius=min(
            settings.max_geofence_radius_m,
            float(mission_limits.get("geofence_radius_m", settings.max_geofence_radius_m)),
        ),
    )
    report = safety.analyze(code)
    report_dict = report.model_dump()

    # Use modified code if safety injected protocols
    final_code = report.modified_code if report.modified_code else code
    issue_strs = [f"[{i.severity.upper()}] {i.description}" for i in report.issues]

    log.info(
        "safety_check.complete",
        passed=report.passed,
        risk=report.risk_level,
        issues=len(report.issues),
        project_id=project_id,
    )

    # Count only the failures: this is what bounds the regeneration loop.
    retries = state.get("safety_retry_count", 0)
    max_retries = state.get("max_iterations") or settings.max_simulation_iterations
    if not report.passed:
        retries += 1

    result: dict[str, Any] = {
        "safety_passed": report.passed,
        "safety_report": report_dict,
        "safety_issues": issue_strs,
        "safety_retry_count": retries,
        "generated_code": final_code,  # may include injected protocols
        "code_status": "validated" if report.passed else "rejected",
        "code_feedback": "; ".join(issue_strs) if not report.passed else None,
        "current_phase": "safety_check",
        "agent_logs": [
            _log_entry(
                "safety_layer",
                "passed" if report.passed else "failed",
                f"Risk: {report.risk_level} | Issues: {len(report.issues)}",
            )
        ],
    }

    # Out of attempts. Say so, loudly: the run used to sit in this loop
    # regenerating the same file with no counter moving and nothing on screen
    # to explain why the simulation never started.
    if not report.passed and retries >= max_retries:
        summary = "; ".join(issue_strs[:5]) or "unspecified safety failure"
        message = (
            f"Safety check still failing after {retries} regeneration "
            f"attempts — giving up. Outstanding issues: {summary}"
        )
        log.error("safety_check.retries_exhausted", project_id=project_id, retries=retries)
        result["errors"] = [message]
        result["agent_logs"].append(_log_entry("safety_layer", "error", message))

        from ws.socket_manager import broadcast_to_project

        await broadcast_to_project(
            project_id,
            "error",
            {"project_id": project_id, "phase": "safety_check", "message": message},
        )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 7. human_review_code  (HITL interrupt point)
# ─────────────────────────────────────────────────────────────────────────────


async def human_review_code(state: ElJazariState) -> dict[str, Any]:
    """HITL code review — mirrors human_review_design in behaviour."""
    feedback = state.get("human_feedback") or ""
    approved = not state.get("human_approval_needed", True)

    status = "approved" if approved else "rejected"
    log.info("human_review_code", status=status, project_id=state.get("project_id"))
    code = state.get("generated_code") or ""

    return {
        "code_status": status,
        # Bind the approval to these exact bytes; run_simulation re-checks them.
        "approved_code_sha256": _code_sha256(code) if approved and code else None,
        "code_feedback": feedback if not approved else state.get("code_feedback"),
        "current_phase": "human_review_code",
        "human_approval_needed": False,
        "agent_logs": [_log_entry("hitl", status, f"Code review: {status}")],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 8. run_simulation
# ─────────────────────────────────────────────────────────────────────────────

# Hard ceiling on a single simulation attempt.  Internal timeouts (mission
# timeout, WSL headroom, MAVSDK connect) should all fire before this, but if
# any of them hang or are misconfigured this prevents the pipeline from
# blocking forever.
RUN_SIMULATION_TIMEOUT_S = 300.0


async def run_simulation(state: ElJazariState) -> dict[str, Any]:
    """Execute the generated code in Gazebo SITL and collect telemetry."""
    from agents.test_agent import SimulationTestAgent  # type: ignore[attr-defined]
    from ws.socket_manager import broadcast_to_project

    reset = {
        "simulation_record_id": None,
        "simulation_iteration": state.get("simulation_iteration", 0) + 1,
        "simulation_success": False,
        "simulation_score": None,
        "simulation_acceptance": None,
        "simulation_model_attestation": None,
        "simulation_block_reason": None,
        "evaluation_status": None,
        "evaluation_skip_reason": None,
        "telemetry_data": [],
        "semantic_observations": [],
        "duration_s": None,
        "mission_started": False,
        "mission_completed": False,
        "crash_detected": False,
        "geofence_violation": False,
        "runtime_mode": "not_started",
        "telemetry_source": "none",
        "mission_error": "",
        "simulation_warnings": [],
        "simulation_artifacts": {},
        "mission_isolation": "unknown",
    }
    project_id = state.get("project_id", "unknown")
    iteration = state.get("simulation_iteration", 0)
    code = state.get("generated_code") or ""

    log.info("run_simulation.start", project_id=project_id, iteration=iteration)

    from pipelines.simulation_readiness import generated_model_block_reason

    reason = generated_model_block_reason(state)
    if reason:
        return {
            **reset,
            "simulation_status": "blocked",
            "simulation_block_reason": reason,
            "simulation_success": False,
            "simulation_score": None,
            "evaluation_status": "skipped",
            "evaluation_skip_reason": reason,
            "mission_started": False,
            "mission_completed": False,
            "mission_error": reason,
            "telemetry_data": [],
            "telemetry_source": "none",
            "runtime_mode": "not_started",
            "duration_s": None,
            "simulation_artifacts": {},
            "simulation_warnings": [reason],
            "current_phase": "run_simulation",
            "agent_logs": [_log_entry("test_agent", "blocked", reason)],
        }

    if code:
        approval_reason = code_approval_block_reason(state.get("approved_code_sha256"), code)
        if approval_reason:
            return {
                **reset,
                "simulation_status": "blocked",
                "simulation_block_reason": approval_reason,
                "evaluation_status": "skipped",
                "evaluation_skip_reason": approval_reason,
                "mission_error": approval_reason,
                "simulation_warnings": [approval_reason],
                "current_phase": "run_simulation",
                "agent_logs": [_log_entry("test_agent", "blocked", approval_reason)],
            }

    if not code:
        return {
            **reset,
            "simulation_status": "failed",
            "errors": ["run_simulation: no code to execute"],
            "current_phase": "run_simulation",
            "agent_logs": [_log_entry("test_agent", "error", "No code available for simulation")],
        }

    try:
        agent = SimulationTestAgent()

        # Push each telemetry sample to the browser as it is measured.
        # Previously every point was delivered in one burst after the flight
        # ended, so the viewer could only ever replay a recording; the aircraft
        # now moves while it is actually flying.
        loop = asyncio.get_running_loop()

        def _publish_point(point: dict) -> None:
            # Called from the pipe reader, which is on this loop; scheduling
            # keeps the reader draining instead of awaiting a socket write.
            loop.create_task(
                broadcast_to_project(
                    project_id,
                    "simulation_telemetry",
                    {
                        "project_id": project_id,
                        "iteration": iteration + 1,
                        "point": point,
                        "live": True,
                    },
                )
            )

        try:
            result = await asyncio.wait_for(
                agent.run_single(
                    code=code,
                    urdf_path=state.get("urdf_path") or "",
                    mission_description=state.get("user_prompt", ""),
                    iteration=iteration + 1,
                    on_point=_publish_point if project_id else None,
                    drone_config=_simulation_model_config(state),
                    expected_code_sha256=state.get("approved_code_sha256"),
                ),
                timeout=RUN_SIMULATION_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.error(
                "run_simulation.timeout",
                project_id=project_id,
                iteration=iteration + 1,
                timeout_s=RUN_SIMULATION_TIMEOUT_S,
            )
            return {
                **reset,
                "simulation_status": "failed",
                "simulation_iteration": iteration + 1,
                "errors": [
                    f"Simulation timed out after {RUN_SIMULATION_TIMEOUT_S:.0f}s. "
                    "SITL environment may not be available or responsive."
                ],
                "current_phase": "run_simulation",
                "agent_logs": [
                    _log_entry(
                        "test_agent",
                        "timeout",
                        f"Simulation hard-timeout after {RUN_SIMULATION_TIMEOUT_S:.0f}s",
                    )
                ],
            }

        telemetry: list[dict] = result.get("telemetry", [])
        observations: list[str] = result.get("semantic_observations", [])
        mission_completed = result.get("mission_completed") is True
        telemetry_source = str(result.get("telemetry_source") or "unknown")
        mission_error = str(result.get("mission_error") or "")
        warnings = [str(item) for item in (result.get("warnings") or []) if str(item).strip()]
        crash_detected = bool(result.get("crash_detected", False))
        duration_s = result.get("duration_s")

        record_id = None
        try:
            record_id = await _persist_simulation_record(
                project_id=str(project_id),
                executed_code=code,
                iteration=iteration + 1,
                telemetry=telemetry,
                observations=observations,
                mission_completed=mission_completed,
                crash_detected=crash_detected,
                telemetry_source=telemetry_source,
                duration_s=duration_s,
                mission_error=mission_error,
                warnings=warnings,
                model_attestation=result.get("model_attestation"),
            )
        except Exception as exc:
            # Persistence is best-effort. A DB outage must not turn a
            # successful simulation into a "failed" pipeline result.
            log.warning(
                "run_simulation.persist_unhandled",
                project_id=project_id,
                error=str(exc),
            )

        if record_id is None:
            warnings.append(
                "Simulation record was not saved. This attempt cannot update stored "
                "evaluations or reports; inspect persistence logs."
            )

        log.info(
            "run_simulation.complete",
            project_id=project_id,
            iteration=iteration + 1,
            telemetry_points=len(telemetry),
            mission_completed=mission_completed,
            telemetry_source=telemetry_source,
        )
        return {
            **reset,
            "simulation_status": "complete",
            "simulation_record_id": record_id,
            "simulation_iteration": iteration + 1,
            "telemetry_data": telemetry,
            "semantic_observations": observations,
            "duration_s": duration_s,
            "mission_started": bool(result.get("mission_started", False)),
            "mission_completed": mission_completed,
            "crash_detected": crash_detected,
            "geofence_violation": bool(result.get("geofence_violation", False)),
            "runtime_mode": str(result.get("runtime_mode") or "unknown"),
            "telemetry_source": telemetry_source,
            "mission_error": mission_error,
            "simulation_warnings": warnings,
            "simulation_artifacts": dict(result.get("artifacts") or {}),
            "simulation_model_attestation": result.get("model_attestation"),
            "mission_isolation": str(result.get("mission_isolation") or "unknown"),
            "current_phase": "run_simulation",
            "agent_logs": [
                _log_entry(
                    "test_agent",
                    "complete",
                    (
                        f"Simulation iter {iteration} done — {len(telemetry)} telemetry points | "
                        f"mission_completed={mission_completed} | source={telemetry_source}"
                    ),
                )
            ],
        }

    except Exception as exc:
        log.error("run_simulation.error", error=str(exc), project_id=project_id)
        logging.getLogger(__name__).exception("run_simulation traceback project_id=%s", project_id)
        return {
            **reset,
            "simulation_status": "failed",
            "simulation_iteration": iteration + 1,
            "errors": [f"Simulation failed: {exc}"],
            "current_phase": "run_simulation",
            "agent_logs": [_log_entry("test_agent", "error", str(exc))],
        }


# ─────────────────────────────────────────────────────────────────────────────
# 9. evaluate_simulation
# ─────────────────────────────────────────────────────────────────────────────


async def evaluate_simulation(state: ElJazariState) -> dict[str, Any]:
    """LLM evaluation of semantic observations against the original mission."""
    from agents.test_agent import SimulationTestAgent  # type: ignore[attr-defined]

    project_id = state.get("project_id", "unknown")
    observations = [
        _decode_observation_text(item) for item in (state.get("semantic_observations") or [])
    ]
    mission = state.get("user_prompt", "")
    log.info("evaluate_simulation.start", project_id=project_id, obs=len(observations))

    from pipelines.simulation_readiness import generated_model_block_reason

    reason = generated_model_block_reason(state)
    if reason:
        return {
            "simulation_record_id": None,
            "simulation_iteration": state.get("simulation_iteration", 0),
            "simulation_status": "blocked",
            "simulation_acceptance": None,
            "simulation_block_reason": reason,
            "simulation_success": False,
            "simulation_score": None,
            "evaluation_status": "skipped",
            "evaluation_skip_reason": reason,
            "code_feedback": reason,
            "current_phase": "evaluate_simulation",
            "agent_logs": [_log_entry("evaluator", "blocked", reason)],
        }

    from pipelines.simulation_acceptance import assess_simulation

    acceptance = assess_simulation(state)
    if not acceptance["passed"]:
        feedback = "; ".join(acceptance["reasons"])
        return {
            "simulation_record_id": state.get("simulation_record_id"),
            "simulation_iteration": state.get("simulation_iteration", 0),
            "simulation_acceptance": acceptance,
            "simulation_success": False,
            "simulation_score": None,
            "evaluation_status": "rejected_by_checks",
            "evaluation_skip_reason": feedback,
            "code_feedback": feedback,
            "current_phase": "evaluate_simulation",
            "agent_logs": [_log_entry("evaluator", "fail", feedback)],
        }

    if not observations:
        return {
            "simulation_record_id": state.get("simulation_record_id"),
            "simulation_iteration": state.get("simulation_iteration", 0),
            "simulation_acceptance": acceptance,
            "simulation_success": False,
            "simulation_score": None,
            "evaluation_status": "skipped",
            "evaluation_skip_reason": "no_semantic_observations",
            "code_feedback": "No semantic observations available for mission evaluation",
            "current_phase": "evaluate_simulation",
            "agent_logs": [_log_entry("evaluator", "warning", "Empty observations")],
        }

    try:
        agent = SimulationTestAgent()
        eval_result = await agent.evaluate(
            observations=observations,
            mission_description=mission,
        )

        passed = eval_result.get("passed")
        score = eval_result.get("score")
        if type(passed) is not bool or type(score) not in (int, float):
            raise ValueError("Evaluator must return a boolean verdict and numeric score")
        if not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError("Evaluator score must be finite and within 0..100")
        feedback: str = eval_result.get("feedback", "")

        log.info("evaluate_simulation.result", passed=passed, score=score, project_id=project_id)
        return {
            "simulation_record_id": state.get("simulation_record_id"),
            "simulation_iteration": state.get("simulation_iteration", 0),
            "simulation_acceptance": acceptance,
            "simulation_success": passed,
            "simulation_score": score,
            "evaluation_status": "completed",
            "evaluation_skip_reason": None,
            "code_feedback": feedback if not passed else None,
            "current_phase": "evaluate_simulation",
            "agent_logs": [
                _log_entry(
                    "evaluator",
                    "pass" if passed else "fail",
                    f"Score: {score:.0f}/100 | {feedback[:120]}",
                )
            ],
        }

    except Exception as exc:
        log.error("evaluate_simulation.error", error=str(exc), project_id=project_id)
        return {
            "simulation_record_id": state.get("simulation_record_id"),
            "simulation_iteration": state.get("simulation_iteration", 0),
            "simulation_success": False,
            "simulation_score": None,
            "evaluation_status": "failed",
            "simulation_acceptance": acceptance,
            "evaluation_skip_reason": None,
            "code_feedback": f"Evaluation error: {exc}",
            "current_phase": "evaluate_simulation",
            "agent_logs": [_log_entry("evaluator", "error", str(exc))],
        }


# ─────────────────────────────────────────────────────────────────────────────
# 10. generate_monitor_report
# ─────────────────────────────────────────────────────────────────────────────


async def generate_monitor_report(state: ElJazariState) -> dict[str, Any]:
    """MonitorAgent: analyse all telemetry + logs → performance report."""
    from agents.monitor_agent import MonitorAgent
    from pipelines.simulation_acceptance import assess_simulation

    # Recheck the final data, including resumed runs and failures routed directly
    # here. A cached success or a monitor-generated score cannot replace the gate.
    acceptance = None if state.get("simulation_block_reason") else assess_simulation(state)
    state = {
        **state,
        "simulation_record_id": None
        if state.get("simulation_block_reason")
        else state.get("simulation_record_id"),
        "simulation_acceptance": acceptance,
        "simulation_success": bool(state.get("simulation_success"))
        and bool(acceptance and acceptance["passed"]),
    }
    project_id = state.get("project_id", "unknown")
    log.info("generate_monitor_report.start", project_id=project_id)

    # Build simulation results list from accumulated state
    sim_results: list[dict[str, Any]] = []
    commanded_waypoints: list[dict[str, float]] = []
    target_alt_m: float | None = None
    if state.get("telemetry_data") or state.get("simulation_status") or state.get("mission_error"):
        # Phase 11 wiring: extract the commanded waypoints + target altitude
        # from the generated mission code so flight_quality_solver can grade
        # cross-track error and altitude RMSE rather than reporting "tracking
        # not evaluated".
        commanded_waypoints, target_alt_m = _extract_mission_plan_from_code(
            state.get("generated_code") or ""
        )
        sim_results.append(
            {
                "passed": state.get("simulation_success", False),
                "telemetry": state.get("telemetry_data", []),
                "observations": [
                    _decode_observation_text(item)
                    for item in state.get("semantic_observations", [])
                ],
                "iterations": state.get("simulation_iteration", 0),
                "duration_s": state.get("duration_s"),
                "evaluation": {"score": 80 if state.get("simulation_success") else 40},
                "mission_started": state.get("mission_started", False),
                "mission_completed": state.get("mission_completed", False),
                "telemetry_source": state.get("telemetry_source", "unknown"),
                "mission_error": state.get("mission_error", ""),
                "warnings": state.get("simulation_warnings", []),
                # Phase 11 inputs — let the production evaluator grade tracking.
                "waypoints": commanded_waypoints,
                "target_altitude_m": target_alt_m,
            }
        )

    try:
        agent = MonitorAgent()
        report = await agent.run(
            simulation_results=sim_results,
            mission_description=state.get("user_prompt", ""),
        )

        report_payload = _extract_monitor_report_payload(report)
        acceptance = state.get("simulation_acceptance")
        if acceptance is not None:
            report_payload["simulation_acceptance"] = acceptance
            if not acceptance.get("passed"):
                report_payload["overall_score"] = None
                report_payload["grade"] = None
                report_payload["flight_validation_status"] = "rejected_by_checks"
                report_payload["flight_validation_reason"] = "; ".join(
                    acceptance.get("reasons", [])
                )
                report_payload["metrics"] = {
                    **(report_payload.get("metrics") or {}),
                    "flight_quality_score": None,
                    "flight_quality_grade": None,
                }
        attestation = state.get("simulation_model_attestation")
        report_payload["aircraft_under_test"] = (
            {
                "kind": "generated",
                "validation_scope": attestation.get("validation_scope"),
                "model_instance": attestation.get("model_instance"),
                "resolved_drone_sha256": attestation.get("resolved_spec_sha256"),
                "flight_model_bundle_sha256": attestation.get("bundle_sha256"),
                "simulated_total_mass_kg": attestation.get("simulated_total_mass_kg"),
                "identity_attested": bool(
                    acceptance and acceptance.get("generated_identity_attested")
                ),
            }
            if attestation
            else {"kind": "reference_x500" if state.get("mission_started") else "not_flown"}
        )
        if state.get("simulation_block_reason"):
            report_payload["metrics"] = {
                **(report_payload.get("metrics") or {}),
                "flight_quality_score": None,
                "flight_quality_grade": None,
            }
            report_payload.update(
                {
                    "overall_score": None,
                    "grade": None,
                    "mission_summary": state["simulation_block_reason"],
                    "flight_validation_status": "blocked",
                    "flight_validation_reason": state["simulation_block_reason"],
                    "model_validation_scope": state.get("model_validation_scope", "unavailable"),
                    "resolved_drone_sha256": state.get("resolved_drone_sha256"),
                }
            )
        recommendations: list[str] = [
            str(item) for item in (report_payload.get("recommendations") or [])
        ]

        score_value = (
            report_payload.get("overall_score") if isinstance(report_payload, dict) else None
        )
        score_display = "—" if score_value is None else f"{float(score_value):.0f}"

        log.info(
            "generate_monitor_report.complete",
            score=score_value,
            project_id=project_id,
        )

        # Phase 14 wiring: drop a folder of field-ready engineering
        # deliverables next to the URDF so an engineer can hand the BOM
        # to procurement, the assembly guide to the workshop, and the
        # pre-flight checklist to the pilot.
        deliverables_paths = _write_engineering_deliverables(
            state,
            report_payload,
            commanded_waypoints,
            target_alt_m,
        )

        return {
            "simulation_record_id": state.get("simulation_record_id"),
            "simulation_iteration": state.get("simulation_iteration", 0),
            "monitor_report": report_payload,
            "simulation_acceptance": acceptance,
            "simulation_success": state["simulation_success"],
            "recommendations": recommendations,
            "current_phase": "complete",
            "deliverables_paths": deliverables_paths,
            "deliverables_status": "complete" if deliverables_paths else "failed",
            "errors": []
            if deliverables_paths
            else ["Engineering deliverables failed; check model/BOM consistency and storage logs"],
            "agent_logs": [
                _log_entry(
                    "monitor_agent",
                    "complete",
                    f"Score: {score_display} | {len(recommendations)} recommendations | "
                    f"deliverables: {len(deliverables_paths)} files",
                )
            ],
        }

    except Exception as exc:
        log.error("generate_monitor_report.error", error=str(exc), project_id=project_id)
        return {
            "simulation_record_id": state.get("simulation_record_id"),
            "simulation_iteration": state.get("simulation_iteration", 0),
            "monitor_report": {"error": str(exc)},
            "deliverables_status": "failed",
            "deliverables_paths": [],
            "current_phase": "complete",
            "errors": [f"Monitor report failed: {exc}"],
            "agent_logs": [_log_entry("monitor_agent", "error", str(exc))],
        }
