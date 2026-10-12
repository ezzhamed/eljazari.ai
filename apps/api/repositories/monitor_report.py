"""eljazari.ai — Repository: MonitorReport"""

import uuid

from sqlalchemy import select

from models.monitor_report import MonitorReport, AnalysisType, Severity
from schemas.monitor_report import MonitorReportCreate
from .base import BaseRepository


class MonitorReportRepository(BaseRepository[MonitorReport]):
    model = MonitorReport

    async def create_from_schema(self, data: MonitorReportCreate) -> MonitorReport:
        return await self.create(**data.model_dump())

    async def list_by_project(
        self,
        project_id: uuid.UUID,
        *,
        analysis_type: AnalysisType | None = None,
        severity: Severity | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[MonitorReport]:
        stmt = (
            select(MonitorReport)
            .where(MonitorReport.project_id == project_id)
            .order_by(MonitorReport.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if analysis_type is not None:
            stmt = stmt.where(MonitorReport.analysis_type == analysis_type)
        if severity is not None:
            stmt = stmt.where(MonitorReport.severity == severity)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_critical(self, project_id: uuid.UUID) -> list[MonitorReport]:
        return await self.list_by_project(project_id, severity=Severity.critical, limit=100)

    async def get_latest_for_project(
        self,
        project_id: uuid.UUID,
        analysis_type: AnalysisType | None = None,
    ) -> MonitorReport | None:
        stmt = (
            select(MonitorReport)
            .where(MonitorReport.project_id == project_id)
            .order_by(MonitorReport.created_at.desc())
            .limit(1)
        )
        if analysis_type is not None:
            stmt = stmt.where(MonitorReport.analysis_type == analysis_type)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
