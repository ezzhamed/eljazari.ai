"""eljazari.ai — Repository: Project"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from models.project import Project, ProjectStatus
from schemas.project import ProjectCreate, ProjectUpdate
from .base import BaseRepository


class ProjectRepository(BaseRepository[Project]):
    model = Project

    async def create_from_schema(self, data: ProjectCreate) -> Project:
        return await self.create(**data.model_dump())

    async def update_from_schema(self, project: Project, data: ProjectUpdate) -> Project:
        payload = data.model_dump(exclude_unset=True)
        if payload:
            payload["updated_at"] = datetime.now(timezone.utc)
        return await self.update(project, **payload)

    async def set_status(self, id: uuid.UUID, status: ProjectStatus) -> Project:
        project = await self.get_or_raise(id)
        return await self.update(
            project,
            status=status,
            updated_at=datetime.now(timezone.utc),
        )

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: ProjectStatus | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[Project]:
        stmt = (
            select(Project)
            .where(Project.user_id == user_id)
            .order_by(Project.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(Project.status == status)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_owned_or_raise(self, id: uuid.UUID, user_id: uuid.UUID) -> Project:
        project = await self.get_or_raise(id)
        if project.user_id != user_id:
            raise ValueError(f"Project {id} not found")
        return project
