"""eljazari.ai - Simulation Router."""

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from db.session import AsyncSessionLocal, get_db
from middleware.auth import AuthUser
from middleware.rate_limit import simulation_start_limit
from repositories.generated_code import GeneratedCodeRepository
from repositories.simulation import SimulationRepository
from services.observations import parse_observations
from services.project_access import require_project_access
from ws.socket_manager import broadcast_to_project

logger = logging.getLogger(__name__)
router = APIRouter()


class SimulationResult(BaseModel):
    simulation_id: str
    project_id: str
    iteration: int
    status: str
    passed: bool | None = None
    score: float | None = None
    duration_s: float | None = None
    # Structured observer output, so a reloaded page can rebuild the feed.
    observations: list[dict[str, Any]] = []


class SimulationResultsResponse(BaseModel):
    project_id: str
    simulations: list[SimulationResult]
    best_score: float | None = None
    final_passed: bool | None = None


def _migrate_telemetry_fields(payload: dict) -> dict:
    """Migrate legacy battery_percent field to battery_pct in telemetry payload."""
    if "battery_percent" in payload and "battery_pct" not in payload:
        payload["battery_pct"] = payload["battery_percent"]
    return payload


async def _run_simulation_bg(
    project_id: str,
    code: str,
    iteration: int,
    model_config: dict[str, Any] | None = None,
    approved_code_sha256: str | None = None,
) -> None:
    """Run closed-loop simulation and stream telemetry via Socket.io."""
    sim_id = f"{project_id}-sim-{iteration}"
    gazebo = None
    launched_sim_id = None
    try:
        from pipelines.semantic_converter import SemanticConverter
        from services.gazebo_service import GazeboService
        from services.px4_service import PX4Service

        gazebo = GazeboService()
        px4 = PX4Service()
        sem = SemanticConverter()

        await broadcast_to_project(
            project_id,
            "sim_status",
            {"status": "running", "iteration": iteration, "project_id": project_id},
        )

        # The design's own aircraft identity; a generated design never falls back to x500.
        launched_sim_id = await gazebo.launch_world(dict(model_config or {}))
        tel = await px4.execute_mission(
            code,
            launched_sim_id,
            mavlink_port=gazebo.get_mavlink_port(launched_sim_id),
            mavsdk_address=gazebo.get_mavsdk_address(launched_sim_id),
            expected_code_sha256=approved_code_sha256,
        )
        await gazebo.stop_world(launched_sim_id)
        launched_sim_id = None

        for point in tel.points:
            payload = point.model_dump() if hasattr(point, "model_dump") else vars(point)
            payload = _migrate_telemetry_fields(payload)
            await broadcast_to_project(
                project_id,
                "simulation_telemetry",
                {"project_id": project_id, "iteration": iteration, "point": payload},
            )
            await asyncio.sleep(0)

        observations = sem.convert(tel)
        observation_payloads = [
            obs.model_dump() if hasattr(obs, "model_dump") else vars(obs) for obs in observations
        ]
        for obs in observations:
            payload = obs.model_dump() if hasattr(obs, "model_dump") else vars(obs)
            await broadcast_to_project(
                project_id, "semantic_observation", {"project_id": project_id, **payload}
            )

        try:
            from models.flight_log import LogType
            from models.simulation import SimulationStatus
            from repositories.flight_log import FlightLogRepository
            from repositories.generated_code import GeneratedCodeRepository
            from repositories.simulation import SimulationRepository
            from schemas.flight_log import FlightLogCreate
            from schemas.simulation import SimulationCreate, SimulationUpdate
            from services.project_access import parse_project_uuid

            telemetry_payloads = []
            for point in tel.points:
                item = point.model_dump() if hasattr(point, "model_dump") else vars(point)
                item = _migrate_telemetry_fields(item)
                telemetry_payloads.append(item)

            async with AsyncSessionLocal() as db:
                code_repo = GeneratedCodeRepository(db)
                sim_repo = SimulationRepository(db)
                log_repo = FlightLogRepository(db)
                project_uuid = parse_project_uuid(project_id)
                code_record = await code_repo.get_latest_for_project(project_uuid)
                if code_record is not None:
                    simulation = await sim_repo.create_from_schema(
                        SimulationCreate(
                            project_id=project_uuid,
                            code_id=code_record.id,
                            iteration=iteration,
                            config={"source": "manual", "sim_id": sim_id},
                        )
                    )
                    passed = tel.mission_completed and not tel.crash_detected
                    normalized = 1.0 if passed else 0.0
                    await sim_repo.update_from_schema(
                        simulation,
                        SimulationUpdate(
                            status=SimulationStatus.completed
                            if passed
                            else SimulationStatus.failed,
                            raw_telemetry={"points": telemetry_payloads},
                            semantic_observations=observation_payloads,
                            success_rate=normalized,
                            completeness=normalized,
                            duration_seconds=tel.duration_s,
                        ),
                    )
                    if telemetry_payloads:
                        base_ts = int(getattr(tel, "start_time_ms", 0) or 0) or None
                        entries = []
                        for idx, item in enumerate(telemetry_payloads):
                            timestamp_ms = int(
                                item.get("timestamp_ms")
                                or ((base_ts or int(time.time() * 1000)) + idx * 1000)
                            )
                            entries.append(
                                FlightLogCreate(
                                    simulation_id=simulation.id,
                                    log_type=LogType.position,
                                    timestamp_ms=timestamp_ms,
                                    data=item,
                                )
                            )
                        await log_repo.bulk_create(entries)
                await db.commit()
        except Exception:
            logger.error(
                "Simulation persistence failed for project=%s sim=%s",
                project_id,
                sim_id,
                exc_info=True,
            )

        passed = tel.mission_completed and not tel.crash_detected
        await broadcast_to_project(
            project_id,
            "simulation_complete",
            {
                "project_id": project_id,
                "sim_id": sim_id,
                "iteration": iteration,
                "passed": passed,
                "duration_s": tel.duration_s,
            },
        )
    except Exception as exc:
        logger.exception("Simulation failed project=%s iter=%d", project_id, iteration)
        await broadcast_to_project(
            project_id,
            "error",
            {"project_id": project_id, "phase": "simulation", "message": str(exc)},
        )
    finally:
        if gazebo is not None and launched_sim_id is not None:
            await gazebo.stop_world(launched_sim_id)


def _simulation_score(record) -> float | None:
    if record.success_rate is not None:
        return round(float(record.success_rate) * 100.0, 2)
    if record.completeness is not None:
        return round(float(record.completeness) * 100.0, 2)
    return None


def _simulation_passed(record) -> bool | None:
    score = _simulation_score(record)
    if score is not None:
        return score >= settings.simulation_pass_threshold
    return True if getattr(record.status, "value", str(record.status)) == "completed" else None


@router.post("/{project_id}/simulate/start")
@simulation_start_limit
async def start_simulation(
    request: Request,
    response: Response,
    project_id: str,
    background_tasks: BackgroundTasks,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manually trigger a simulation run for the latest approved code."""
    project = await require_project_access(db, project_id, user)
    repo = GeneratedCodeRepository(db)
    record = await repo.get_latest_for_project(project.id)
    if record is None or not record.code_content:
        raise HTTPException(status_code=422, detail="No approved code found for this project")

    from graph.nodes import code_approval_block_reason
    from services.pipeline_runtime import get_orchestrator

    # The code row is only the latest generation; approval lives in the graph
    # state and is bound to the exact bytes that were reviewed.
    state = await get_orchestrator().get_state(str(project.id))
    approved = state.get("approved_code_sha256")
    reason = code_approval_block_reason(approved, record.code_content)
    if reason:
        raise HTTPException(status_code=409, detail=reason)

    iteration = (record.version or 0) + 1
    background_tasks.add_task(
        _run_simulation_bg,
        str(project.id),
        record.code_content,
        iteration,
        state.get("simulation_model_config") or {},
        approved,
    )
    return {"status": "started", "project_id": str(project.id), "iteration": iteration}


@router.post("/{project_id}/simulate/stop")
async def stop_simulation(
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Stop the running Gazebo simulation for this project."""
    project = await require_project_access(db, project_id, user)
    try:
        from services.gazebo_service import GazeboService

        svc = GazeboService()
        await svc.stop_world(str(project.id))
        await broadcast_to_project(
            str(project.id),
            "sim_status",
            {"status": "stopped", "project_id": str(project.id)},
        )
        return {"status": "stopped", "project_id": str(project.id)}
    except Exception as exc:
        logger.exception("stop_simulation error project=%s", project.id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{project_id}/simulate/results", response_model=SimulationResultsResponse)
async def get_simulation_results(
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> SimulationResultsResponse:
    """Return all simulation run summaries for a project."""
    project = await require_project_access(db, project_id, user)
    repo = SimulationRepository(db)
    records = await repo.list_for_project(project.id)
    sims = [
        SimulationResult(
            simulation_id=str(record.id),
            project_id=str(project.id),
            iteration=record.iteration or 0,
            status=record.status.value if hasattr(record.status, "value") else str(record.status),
            passed=_simulation_passed(record),
            score=_simulation_score(record),
            duration_s=record.duration_seconds,
            observations=parse_observations(record.semantic_observations),
        )
        for record in records
    ]
    best_score = max((sim.score for sim in sims if sim.score is not None), default=None)
    final_passed = sims[0].passed if sims else None
    return SimulationResultsResponse(
        project_id=str(project.id),
        simulations=sims,
        best_score=best_score,
        final_passed=final_passed,
    )
