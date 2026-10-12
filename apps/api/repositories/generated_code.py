"""eljazari.ai — Repository: GeneratedCode"""

import uuid

from sqlalchemy import select

from models.generated_code import GeneratedCode, CodeType
from schemas.generated_code import GeneratedCodeCreate, GeneratedCodeUpdate
from .base import BaseRepository


class GeneratedCodeRepository(BaseRepository[GeneratedCode]):
    model = GeneratedCode

    async def create_from_schema(self, data: GeneratedCodeCreate) -> GeneratedCode:
        return await self.create(**data.model_dump())

    async def update_from_schema(
        self, code: GeneratedCode, data: GeneratedCodeUpdate
    ) -> GeneratedCode:
        return await self.update(code, **data.model_dump(exclude_unset=True))

    async def mark_safety_validated(self, id: uuid.UUID, safety_report: dict) -> GeneratedCode:
        code = await self.get_or_raise(id)
        return await self.update(code, safety_validated=True, safety_report=safety_report)

    async def list_by_project(
        self,
        project_id: uuid.UUID,
        *,
        code_type: CodeType | None = None,
        validated_only: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> list[GeneratedCode]:
        stmt = (
            select(GeneratedCode)
            .where(GeneratedCode.project_id == project_id)
            .order_by(GeneratedCode.version.desc(), GeneratedCode.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if code_type is not None:
            stmt = stmt.where(GeneratedCode.code_type == code_type)
        if validated_only:
            stmt = stmt.where(GeneratedCode.safety_validated.is_(True))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_latest_for_project(
        self,
        project_id: uuid.UUID,
        code_type: CodeType | None = None,
    ) -> GeneratedCode | None:
        stmt = (
            select(GeneratedCode)
            .where(GeneratedCode.project_id == project_id)
            .order_by(GeneratedCode.version.desc(), GeneratedCode.created_at.desc())
            .limit(1)
        )
        if code_type is not None:
            stmt = stmt.where(GeneratedCode.code_type == code_type)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_for_design(
        self, design_id: uuid.UUID, code_type: CodeType | None = None
    ) -> GeneratedCode | None:
        stmt = (
            select(GeneratedCode)
            .where(GeneratedCode.design_id == design_id)
            .order_by(GeneratedCode.version.desc())
            .limit(1)
        )
        if code_type is not None:
            stmt = stmt.where(GeneratedCode.code_type == code_type)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def next_version(self, project_id: uuid.UUID) -> int:
        latest = await self.get_latest_for_project(project_id)
        return (latest.version + 1) if latest else 1
