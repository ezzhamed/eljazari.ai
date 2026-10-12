"""Pipeline runtime helpers for background execution and persistence."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Iterable
from typing import Any

from config.settings import settings
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from agents.orchestrator import Orchestrator
from db.session import AsyncSessionLocal, use_session_factory
from models.design import DesignStatus
from models.flight_log import LogType
from models.monitor_report import AnalysisType, Severity
from models.project import ProjectStatus
from models.simulation import SimulationStatus
from repositories.design import DesignRepository
from repositories.flight_log import FlightLogRepository
from repositories.generated_code import GeneratedCodeRepository
from pipelines.simulation_acceptance import summarize_validation
from services.run_lock import project_run_lock
from repositories.monitor_report import MonitorReportRepository
from repositories.project import ProjectRepository
from repositories.simulation import SimulationRepository
from schemas.design import DesignCreate, DesignUpdate
from schemas.flight_log import FlightLogCreate
from schemas.generated_code import GeneratedCodeCreate, GeneratedCodeUpdate
from schemas.monitor_report import MonitorReportCreate
from schemas.simulation import SimulationUpdate
from services.observations import parse_observations
from services.project_access import parse_project_uuid
from ws.socket_manager import broadcast_to_project

logger = logging.getLogger(__name__)

_ORCHESTRATOR = Orchestrator()


def get_orchestrator() -> Orchestrator:
    return _ORCHESTRATOR


def _decode_observation_text(value: Any) -> str:
    text = str(value)
    if "\\u" not in text and "\\x" not in text:
        return text
    try:
        return text.encode("ascii").decode("unicode_escape")
    except Exception:
        return text


def _start_background_thread(name: str, coro_factory) -> None:
    def runner() -> None:
        try:
            asyncio.run(coro_factory())
        except Exception:
            logger.exception("Background pipeline task failed: %s", name)

    threading.Thread(target=runner, name=name, daemon=True).start()


async def _publish_pipeline_events(project_id: str, events) -> None:
    async for event in events:
        await _persist_pipeline_event(project_id, event)
        for ws_event, payload in _ws_messages_from_event(project_id, event):
            await broadcast_to_project(project_id, ws_event, payload)


def _build_worker_session_factory() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        settings.database_url,
        echo=settings.debug,
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    return engine, session_factory


async def _run_pipeline(project_id: str, prompt: str) -> None:
    async with project_run_lock(project_id) as owned:
        if not owned:
            logger.warning("pipeline.run refused: project %s already has an active run", project_id)
            return
        engine, session_factory = _build_worker_session_factory()
        try:
            with use_session_factory(session_factory):
                async for event in _ORCHESTRATOR.run(prompt, project_id=project_id):
                    await _persist_pipeline_event(
                        project_id, event, session_factory=session_factory
                    )
                    for ws_event, payload in _ws_messages_from_event(project_id, event):
                        await broadcast_to_project(project_id, ws_event, payload)
        finally:
            await engine.dispose()


def run_pipeline_background(project_id: str, prompt: str) -> None:
    """Launch a new pipeline run on a dedicated worker thread."""
    _start_background_thread(
        f"pipeline-run-{project_id}",
        lambda: _run_pipeline(project_id, prompt),
    )


async def _continue_pipeline(
    project_id: str,
    *,
    phase: str | None,
    approved: bool,
    feedback: str,
) -> None:
    resolved_phase = phase
    if resolved_phase not in {"design", "code"}:
        resolved_phase = await _ORCHESTRATOR._resolve_phase(project_id, None)

    # One owner per project, across threads and worker processes. Checking the
    # gate first and locking second let two simultaneous approvals both see an
    # open gate and both resume, i.e. two simulations for one approval.
    async with project_run_lock(project_id) as owned:
        if not owned:
            logger.info(
                "pipeline.continue ignored: project %s already has an active run", project_id
            )
            return
        await _continue_owned_pipeline(
            project_id, resolved_phase=resolved_phase, approved=approved, feedback=feedback
        )


async def _continue_owned_pipeline(
    project_id: str, *, resolved_phase: str, approved: bool, feedback: str
) -> None:
    # Idempotency guard: only act when the graph is actually suspended at the
    # matching HITL review gate. A duplicate/late approval (e.g. an over-eager
    # client polling the approve endpoint) would otherwise re-apply
    # aupdate_state + resume and re-run the pipeline from the current
    # checkpoint, producing duplicated simulation/monitor runs.
    try:
        gate_open = await _ORCHESTRATOR.is_awaiting_review(project_id, resolved_phase)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "pipeline.continue gate-check failed project=%s phase=%s: %s",
            project_id,
            resolved_phase,
            exc,
        )
        gate_open = True  # fail open: preserve previous behaviour on error
    if not gate_open:
        logger.info(
            "pipeline.continue ignored (no open %s gate) project=%s approved=%s",
            resolved_phase,
            project_id,
            approved,
        )
        return

    # This runs on a worker thread with its own event loop, so the shared
    # engine's pool cannot be used here — hence the per-run factory.
    engine, session_factory = _build_worker_session_factory()
    try:
        with use_session_factory(session_factory):
            if approved:
                await _ORCHESTRATOR.approve(project_id, phase=resolved_phase, feedback=feedback)
                await _persist_hitl_approval(
                    project_id, resolved_phase, session_factory=session_factory
                )
                next_phase = "urdf_conversion" if resolved_phase == "design" else "simulation"
                await broadcast_to_project(
                    project_id,
                    "phase_changed",
                    {"phase": next_phase, "project_id": project_id},
                )
            else:
                await _ORCHESTRATOR.reject(project_id, phase=resolved_phase, feedback=feedback)
                next_phase = "design" if resolved_phase == "design" else "code_generation"
                payload = {"phase": next_phase, "project_id": project_id}
                if feedback:
                    payload["feedback"] = feedback
                await broadcast_to_project(project_id, "phase_changed", payload)

            async for event in _ORCHESTRATOR.resume(project_id):
                await _persist_pipeline_event(project_id, event, session_factory=session_factory)
                for ws_event, payload in _ws_messages_from_event(project_id, event):
                    await broadcast_to_project(project_id, ws_event, payload)
    finally:
        await engine.dispose()


def continue_pipeline_background(
    project_id: str,
    *,
    phase: str | None,
    approved: bool,
    feedback: str = "",
) -> None:
    """Apply a HITL decision, resume the graph, and stream persisted updates."""
    _start_background_thread(
        f"pipeline-resume-{project_id}-{phase}",
        lambda: _continue_pipeline(
            project_id,
            phase=phase,
            approved=approved,
            feedback=feedback,
        ),
    )


def _agent_log_messages(
    project_id: str,
    data: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Turn a node's agent_logs delta into `agent_log` socket events.

    graph/nodes.py has always recorded per-agent progress via _log_entry(), and
    the frontend has always listened for `agent_log` to drive its activity feed
    — but nothing bridged the two, so the feed only ever showed the coarse
    phase transitions.

    The orchestrator streams with stream_mode="updates", so `data` holds only
    what the node just returned, not the accumulated state. Emitting every entry
    here therefore cannot replay earlier logs.
    """
    messages: list[tuple[str, dict[str, Any]]] = []
    for entry in data.get("agent_logs") or []:
        if not isinstance(entry, dict) or not entry.get("agent"):
            continue
        messages.append(
            (
                "agent_log",
                {
                    "project_id": project_id,
                    "agent": entry.get("agent"),
                    "status": entry.get("status", "success"),
                    "message": entry.get("message", ""),
                    "ts": entry.get("ts"),
                },
            )
        )
    return messages


def _ws_messages_from_event(
    project_id: str,
    event: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    data = event.get("data") or {}
    # Agent logs lead, so the activity feed shows the work before the phase
    # transition it produced.
    return _agent_log_messages(project_id, data) + _node_messages_from_event(project_id, event)


def _node_messages_from_event(
    project_id: str,
    event: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    event_type = event.get("event")
    node = event.get("node")
    data = event.get("data") or {}

    if event_type == "pipeline_started":
        return [("phase_changed", {"project_id": project_id, "phase": "design"})]

    if event_type == "pipeline_complete":
        return [("phase_changed", {"project_id": project_id, "phase": "complete"})]

    if event_type == "hitl_interrupt":
        phase = "review_design" if node == "human_review_design" else "review_code"
        payload = {"project_id": project_id, "phase": phase}
        if data.get("design_tree") is not None:
            payload["design_tree"] = data["design_tree"]
        if data.get("generated_code") is not None:
            payload["generated_code"] = data["generated_code"]
        if data.get("safety_report") is not None:
            payload["safety_report"] = data["safety_report"]
        return [("phase_changed", payload)]

    if event_type != "node_complete":
        return []

    if node == "generate_design":
        return [
            ("design_ready", {"project_id": project_id, "data": data}),
            ("phase_changed", {"project_id": project_id, "phase": "review_design"}),
        ]
    if node == "convert_to_urdf":
        return [("phase_changed", {"project_id": project_id, "phase": "code_generation"})]
    if node == "generate_code":
        return [
            (
                "code_ready",
                {
                    "project_id": project_id,
                    "data": {
                        "code": data.get("generated_code", ""),
                        "iteration": data.get("simulation_iteration", 0),
                    },
                },
            )
        ]
    if node == "safety_check":
        return [
            ("safety_report", {"project_id": project_id, "data": data.get("safety_report") or {}}),
            ("phase_changed", {"project_id": project_id, "phase": "review_code"}),
        ]
    if node == "run_simulation" and data.get("simulation_status") == "blocked":
        return [
            (
                "sim_status",
                {
                    "project_id": project_id,
                    "status": "failed",
                    "reason": data.get("simulation_block_reason"),
                    "blocked": True,
                },
            )
        ]
    if node == "run_simulation":
        iteration = data.get("simulation_iteration", 0)
        payload = {
            "project_id": project_id,
            "sim_id": data.get("simulation_record_id") or f"{project_id}-sim-{iteration}",
            "iteration": iteration,
            "passed": False,  # Collection completion is not an evaluation verdict.
            "duration_s": data.get("duration_s") or 0.0,
        }
        messages = [
            (
                "sim_status",
                {"project_id": project_id, "status": "completed", "iteration": iteration},
            ),
            ("simulation_complete", payload),
        ]
        for point in _iter_telemetry_points(data.get("telemetry_data")):
            messages.append(
                (
                    "simulation_telemetry",
                    {"project_id": project_id, "iteration": iteration, "point": point},
                )
            )
        for obs in parse_observations(data.get("semantic_observations")):
            messages.append(
                (
                    "semantic_observation",
                    {"project_id": project_id, "iteration": iteration, **obs},
                )
            )
        return messages
    if node == "evaluate_simulation":
        # The score used to reach the UI only through a REST hydrate; a page that
        # watched the run live drew an empty "Iteration Scores" chart.
        score = data.get("simulation_score")
        return [
            (
                "sim_evaluated",
                {
                    "project_id": project_id,
                    "iteration": data.get("simulation_iteration", 0),
                    "passed": bool(data.get("simulation_success")),
                    "score": float(score) if score is not None else None,
                    "status": data.get("evaluation_status", "completed"),
                    "feedback": data.get("code_feedback", ""),
                    "acceptance": data.get("simulation_acceptance"),
                    "sim_id": data.get("simulation_record_id"),
                },
            )
        ]
    if node == "generate_monitor_report":
        return [
            (
                "monitor_update",
                {
                    "project_id": project_id,
                    "data": {
                        **(data.get("monitor_report") or {}),
                        "validation": summarize_validation(data.get("monitor_report") or {}),
                    },
                },
            ),
            ("phase_changed", {"project_id": project_id, "phase": "complete"}),
        ]
    return []


async def _persist_hitl_approval(
    project_id: str,
    phase: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    """Record an approval before the graph resumes.

    Project status otherwise only advances when the next node *completes*. A
    simulation runs for minutes, and for all of that time the project still
    read `coding` with a validated code artefact — exactly the state the UI
    treats as "code review gate open", so reloading the page during a flight
    re-offered the approve button for code that was already flying.
    """
    try:
        project_uuid = parse_project_uuid(project_id)
    except Exception:
        return
    async with session_factory() as db:
        try:
            if phase == "code":
                await ProjectRepository(db).set_status(project_uuid, ProjectStatus.simulating)
            elif phase == "design":
                design_repo = DesignRepository(db)
                design = await design_repo.get_latest_for_project(project_uuid)
                if design is not None and design.status != DesignStatus.converted:
                    await design_repo.update_from_schema(
                        design, DesignUpdate(status=DesignStatus.approved)
                    )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning("HITL approval persistence skipped for %s: %s", project_id, exc)


async def _persist_pipeline_event(
    project_id: str,
    event: dict[str, Any],
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    try:
        project_uuid = parse_project_uuid(project_id)
    except Exception:
        logger.warning("Skipping persistence for invalid project id %s", project_id)
        return

    async with session_factory() as db:
        try:
            await _persist_pipeline_event_inner(db, project_uuid, event)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning("Pipeline persistence skipped for %s: %s", project_id, exc)


async def _persist_pipeline_event_inner(
    db: AsyncSession,
    project_id: uuid.UUID,
    event: dict[str, Any],
) -> None:
    project_repo = ProjectRepository(db)
    try:
        await project_repo.get_or_raise(project_id)
    except ValueError:
        return

    event_type = event.get("event")
    if event_type == "pipeline_started":
        await project_repo.set_status(project_id, ProjectStatus.designing)
        return
    if event_type == "pipeline_complete":
        await project_repo.set_status(project_id, ProjectStatus.completed)
        return
    if event_type != "node_complete":
        return

    node = event.get("node")
    data = event.get("data") or {}
    await _update_project_status_for_node(project_repo, project_id, node)

    if node == "generate_design":
        await _persist_design(db, project_id, data)
    elif node == "convert_to_urdf":
        await _persist_urdf_paths(db, project_id, data)
    elif node == "generate_code":
        await _persist_generated_code(db, project_id, data)
    elif node == "safety_check":
        await _persist_safety_report(db, project_id, data)
    elif node == "run_simulation":
        await _persist_simulation(db, project_id, data)
    elif node == "evaluate_simulation":
        await _persist_simulation_evaluation(db, project_id, data)
    elif node == "generate_monitor_report":
        await _persist_monitor_report(db, project_id, data)


async def _update_project_status_for_node(
    project_repo: ProjectRepository,
    project_id: uuid.UUID,
    node: str | None,
) -> None:
    mapping = {
        "generate_design": ProjectStatus.designing,
        "convert_to_urdf": ProjectStatus.designing,
        "generate_code": ProjectStatus.coding,
        "safety_check": ProjectStatus.coding,
        "run_simulation": ProjectStatus.simulating,
        "evaluate_simulation": ProjectStatus.testing,
        "generate_monitor_report": ProjectStatus.monitoring,
    }
    status = mapping.get(node or "")
    if status is not None:
        await project_repo.set_status(project_id, status)


async def _persist_design(
    db: AsyncSession,
    project_id: uuid.UUID,
    data: dict[str, Any],
) -> None:
    repo = DesignRepository(db)
    version = await repo.next_version(project_id)
    await repo.create_from_schema(
        DesignCreate(
            project_id=project_id,
            design_tree=data.get("design_tree"),
            design_params=data.get("design_params"),
            performance_estimate=data.get("performance_estimate"),
            version=version,
        )
    )


async def _persist_urdf_paths(
    db: AsyncSession,
    project_id: uuid.UUID,
    data: dict[str, Any],
) -> None:
    repo = DesignRepository(db)
    design = await repo.get_latest_for_project(project_id)
    if design is None:
        return
    await repo.update_from_schema(
        design,
        DesignUpdate(
            urdf_file_path=data.get("urdf_path"),
            sdf_file_path=data.get("sdf_path"),
            status=DesignStatus.converted,
        ),
    )


async def _persist_generated_code(
    db: AsyncSession,
    project_id: uuid.UUID,
    data: dict[str, Any],
) -> None:
    design_repo = DesignRepository(db)
    code_repo = GeneratedCodeRepository(db)
    design = await design_repo.get_latest_for_project(project_id)
    code_content = data.get("generated_code")
    if design is None or not code_content:
        return
    version = await code_repo.next_version(project_id)
    await code_repo.create_from_schema(
        GeneratedCodeCreate(
            project_id=project_id,
            design_id=design.id,
            code_content=code_content,
            gsce_config=data.get("gsce_config"),
            version=version,
        )
    )


async def _persist_safety_report(
    db: AsyncSession,
    project_id: uuid.UUID,
    data: dict[str, Any],
) -> None:
    code_repo = GeneratedCodeRepository(db)
    code = await code_repo.get_latest_for_project(project_id)
    if code is None:
        return
    await code_repo.update_from_schema(
        code,
        GeneratedCodeUpdate(
            safety_validated=bool(data.get("safety_passed")),
            safety_report=data.get("safety_report"),
            code_content=data.get("generated_code") or code.code_content,
        ),
    )


async def _simulation_for_event(db, project_id, data):
    """Never infer a write target from project recency or iteration alone."""
    value = data.get("simulation_record_id")
    if not value:
        logger.warning("Skipping simulation persistence: event has no record ID")
        return None
    try:
        record_id = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        logger.warning("Skipping simulation persistence: invalid record ID")
        return None
    simulation = await SimulationRepository(db).get(record_id)
    if simulation is None or str(simulation.project_id) != str(project_id):
        logger.warning("Skipping simulation persistence: record/project mismatch")
        return None
    if (
        type(data.get("simulation_iteration")) is not int
        or simulation.iteration != data["simulation_iteration"]
    ):
        logger.warning("Skipping simulation persistence: attempt mismatch")
        return None
    return simulation


async def _persist_simulation(db, project_id, data) -> None:
    # The graph owns the simulation row; event handling only supplements logs.
    if data.get("simulation_status") == "blocked":
        return
    simulation = await _simulation_for_event(db, project_id, data)
    if simulation is None:
        return
    existing_raw = simulation.raw_telemetry if isinstance(simulation.raw_telemetry, dict) else {}
    if existing_raw.get("pipeline_logs_written"):
        return
    telemetry_points = list(_iter_telemetry_points(data.get("telemetry_data")))
    if telemetry_points:
        base_ms = int(time.time() * 1000)
        entries = [
            FlightLogCreate(
                simulation_id=simulation.id,
                log_type=LogType.position,
                timestamp_ms=int(point.get("timestamp_ms") or (base_ms + idx * 1000)),
                data=point,
            )
            for idx, point in enumerate(telemetry_points)
        ]
        await FlightLogRepository(db).bulk_create(entries)
    await SimulationRepository(db).update_from_schema(
        simulation, SimulationUpdate(raw_telemetry={**existing_raw, "pipeline_logs_written": True})
    )


async def _persist_simulation_evaluation(
    db: AsyncSession,
    project_id: uuid.UUID,
    data: dict[str, Any],
) -> None:
    if data.get("simulation_status") == "blocked":
        return
    sim_repo = SimulationRepository(db)
    simulation = await _simulation_for_event(db, project_id, data)
    if simulation is None:
        return
    score = float(data.get("simulation_score") or data.get("score") or 0.0)
    normalized = max(0.0, min(1.0, score / 100.0))
    status = (
        SimulationStatus.completed if data.get("simulation_success") else SimulationStatus.failed
    )
    await sim_repo.update_from_schema(
        simulation,
        SimulationUpdate(
            status=status,
            success_rate=normalized,
            completeness=normalized,
            raw_telemetry={
                **(simulation.raw_telemetry if isinstance(simulation.raw_telemetry, dict) else {}),
                "acceptance": data.get("simulation_acceptance"),
            },
        ),
    )


async def _persist_monitor_report(
    db: AsyncSession,
    project_id: uuid.UUID,
    data: dict[str, Any],
) -> None:
    report_repo = MonitorReportRepository(db)
    simulation = await _simulation_for_event(db, project_id, data)
    if simulation is None:
        return

    report = dict(data.get("monitor_report") or {})
    recommendations = [
        {"text": str(item)}
        for item in (data.get("recommendations") or report.get("recommendations") or [])
    ]
    score = float(report.get("overall_score") or 0.0)
    severity = Severity.info
    if score < 40:
        severity = Severity.critical
    elif score < 70:
        severity = Severity.warning

    report.setdefault("overall_score", score)

    await report_repo.create_from_schema(
        MonitorReportCreate(
            project_id=project_id,
            simulation_id=simulation.id,
            analysis_type=AnalysisType.performance,
            findings=report,
            recommendations=recommendations,
            severity=severity,
        )
    )


def _iter_telemetry_points(telemetry: Any) -> Iterable[dict[str, Any]]:
    if not telemetry:
        return []
    if isinstance(telemetry, dict) and isinstance(telemetry.get("points"), list):
        return [point for point in telemetry["points"] if isinstance(point, dict)]
    if isinstance(telemetry, list):
        return [point for point in telemetry if isinstance(point, dict)]
    return []
