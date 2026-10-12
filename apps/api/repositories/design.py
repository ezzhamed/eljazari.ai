"""eljazari.ai — Repository: Design"""

import uuid

from sqlalchemy import select

from models.design import Design, DesignStatus
from schemas.design import DesignCreate, DesignUpdate
from .base import BaseRepository


class DesignRepository(BaseRepository[Design]):
    model = Design

    async def create_from_schema(self, data: DesignCreate) -> Design:
        return await self.create(**data.model_dump())

    async def update_from_schema(self, design: Design, data: DesignUpdate) -> Design:
        return await self.update(design, **data.model_dump(exclude_unset=True))

    async def get_latest_for_project(self, project_id: uuid.UUID) -> Design | None:
        stmt = (
            select(Design)
            .where(Design.project_id == project_id)
            .order_by(Design.version.desc(), Design.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def next_version(self, project_id: uuid.UUID) -> int:
        latest = await self.get_latest_for_project(project_id)
        return (latest.version + 1) if latest else 1

    async def list_by_project(
        self,
        project_id: uuid.UUID,
        *,
        status: DesignStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Design]:
        stmt = (
            select(Design)
            .where(Design.project_id == project_id)
            .order_by(Design.version.desc())
            .offset(offset)
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(Design.status == status)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
