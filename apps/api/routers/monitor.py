"""eljazari.ai - Monitor Router (performance reports + logs)."""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from middleware.auth import AuthUser
from repositories.flight_log import FlightLogRepository
from repositories.monitor_report import MonitorReportRepository
from pipelines.simulation_acceptance import summarize_validation
from services.project_access import require_project_access

logger = logging.getLogger(__name__)
router = APIRouter()


class PerformanceReportResponse(BaseModel):
    project_id: str
    overall_score: float | None = None
    metrics: dict
    recommendations: list[str]
    # Which aircraft flew and whether the run passed the deterministic gate.
    validation: dict | None = None


class FlightLogsResponse(BaseModel):
    project_id: str
    logs: list[dict]
    total: int


def _normalize_recommendations(items) -> list[str]:
    recommendations: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            text = item.get("text") or item.get("recommendation") or item.get("description")
            if text:
                recommendations.append(str(text))
        elif item:
            recommendations.append(str(item))
    return recommendations


def _normalize_monitor_record(record) -> tuple[float | None, dict, list[str]]:
    findings = record.findings or {}
    metrics = findings.get("metrics") if isinstance(findings, dict) else {}
    overall_score = findings.get("overall_score") if isinstance(findings, dict) else None
    recommendations = _normalize_recommendations(record.recommendations)
    return overall_score, metrics or {}, recommendations


@router.get("/{project_id}/monitor/report", response_model=PerformanceReportResponse)
async def get_performance_report(
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> PerformanceReportResponse:
    """Fetch the performance analysis report for a completed project."""
    project = await require_project_access(db, project_id, user)
    repo = MonitorReportRepository(db)
    record = await repo.get_latest_for_project(project.id)
    if record is None:
        return PerformanceReportResponse(
            project_id=str(project.id),
            metrics={},
            recommendations=["Complete a simulation to generate a report"],
        )
    overall_score, metrics, recommendations = _normalize_monitor_record(record)
    return PerformanceReportResponse(
        project_id=str(project.id),
        overall_score=overall_score,
        metrics=metrics,
        recommendations=recommendations,
        validation=summarize_validation(record.findings),
    )


@router.get("/{project_id}/monitor/logs", response_model=FlightLogsResponse)
async def get_flight_logs(
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
    offset: int = 0,
) -> FlightLogsResponse:
    """Return paginated flight log entries for a project."""
    project = await require_project_access(db, project_id, user)
    repo = FlightLogRepository(db)
    records = await repo.list_for_project(project.id, limit=limit, offset=offset)
    total = await repo.count_for_project(project.id)
    logs = [
        {
            "id": str(record.id),
            "timestamp_ms": record.timestamp_ms,
            "log_type": record.log_type.value
            if hasattr(record.log_type, "value")
            else str(record.log_type),
            "data": record.data or {},
        }
        for record in records
    ]
    return FlightLogsResponse(project_id=str(project.id), logs=logs, total=total)
