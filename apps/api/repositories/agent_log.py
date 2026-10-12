"""eljazari.ai — Repository: AgentLog"""

import uuid

from sqlalchemy import select, func

from models.agent_log import AgentLog, AgentLogStatus
from schemas.agent_log import AgentLogCreate
from .base import BaseRepository


class AgentLogRepository(BaseRepository[AgentLog]):
    model = AgentLog

    async def create_from_schema(self, data: AgentLogCreate) -> AgentLog:
        return await self.create(**data.model_dump())

    async def list_by_project(
        self,
        project_id: uuid.UUID,
        *,
        agent_name: str | None = None,
        status: AgentLogStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[AgentLog]:
        stmt = (
            select(AgentLog)
            .where(AgentLog.project_id == project_id)
            .order_by(AgentLog.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if agent_name is not None:
            stmt = stmt.where(AgentLog.agent_name == agent_name)
        if status is not None:
            stmt = stmt.where(AgentLog.status == status)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def token_usage_summary(self, project_id: uuid.UUID) -> dict:
        """Return total tokens + duration per agent for a project."""
        stmt = (
            select(
                AgentLog.agent_name,
                func.sum(AgentLog.tokens_used).label("total_tokens"),
                func.sum(AgentLog.duration_ms).label("total_duration_ms"),
                func.count().label("calls"),
            )
            .where(AgentLog.project_id == project_id)
            .group_by(AgentLog.agent_name)
        )
        result = await self.session.execute(stmt)
        return {
            row.agent_name: {
                "total_tokens": row.total_tokens or 0,
                "total_duration_ms": row.total_duration_ms or 0,
                "calls": row.calls,
            }
            for row in result.all()
        }
