"""Project ownership helpers shared across routers and WebSocket handlers."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from middleware.auth import CurrentUser
from repositories.project import ProjectRepository


def parse_project_uuid(project_id: str | uuid.UUID) -> uuid.UUID:
    """Parse a project identifier and return a UUID or raise 400."""
    if isinstance(project_id, uuid.UUID):
        return project_id
    try:
        return uuid.UUID(project_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid project ID",
        ) from exc


async def get_owned_project(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    user_id: uuid.UUID,
):
    """Load a project only if it belongs to the requesting user."""
    repo = ProjectRepository(db)
    project_uuid = parse_project_uuid(project_id)
    try:
        return await repo.get_owned_or_raise(project_uuid, user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        ) from exc


async def require_project_access(
    db: AsyncSession,
    project_id: str | uuid.UUID,
    user: CurrentUser,
):
    """Convenience wrapper for authenticated FastAPI handlers."""
    return await get_owned_project(db, project_id, user.user_uuid)
