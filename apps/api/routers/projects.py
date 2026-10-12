"""eljazari.ai - Projects Router (full CRUD)."""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from middleware.auth import AuthUser
from middleware.rate_limit import projects_create_limit
from models.project import ProjectStatus
from repositories.project import ProjectRepository
from schemas.project import ProjectCreate
from services.pipeline_runtime import run_pipeline_background
from services.project_access import require_project_access

logger = logging.getLogger(__name__)
router = APIRouter()


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    prompt: str = Field(..., min_length=10, description="Natural language mission description")


class ProjectResponse(BaseModel):
    id: str
    name: str
    prompt: str
    status: str
    user_id: str
    created_at: str | None = None


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]
    total: int


def _to_project_response(record) -> ProjectResponse:
    return ProjectResponse(
        id=str(record.id),
        name=record.name,
        prompt=record.prompt,
        status=record.status.value if hasattr(record.status, "value") else str(record.status),
        user_id=str(record.user_id),
        created_at=record.created_at.isoformat() if record.created_at else None,
    )


@router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
@projects_create_limit
async def create_project(
    request: Request,
    response: Response,
    body: CreateProjectRequest,
    background_tasks: BackgroundTasks,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Create a new drone project and immediately kick off the AI pipeline."""
    repo = ProjectRepository(db)
    record = await repo.create_from_schema(
        ProjectCreate(
            user_id=user.user_uuid,
            name=body.name,
            prompt=body.prompt,
        )
    )
    record = await repo.set_status(record.id, ProjectStatus.designing)
    background_tasks.add_task(run_pipeline_background, str(record.id), body.prompt)
    logger.info("Project created id=%s user=%s", record.id, user.user_id)
    return _to_project_response(record)


@router.get("/", response_model=ProjectListResponse)
async def list_projects(
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
    offset: int = 0,
    limit: int = 50,
) -> ProjectListResponse:
    """List all projects owned by the current user."""
    repo = ProjectRepository(db)
    records = await repo.list_by_user(user.user_uuid, offset=offset, limit=limit)
    total = await repo.count(user_id=user.user_uuid)
    return ProjectListResponse(
        items=[_to_project_response(record) for record in records],
        total=total,
    )


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Fetch a project by ID."""
    record = await require_project_access(db, project_id, user)
    return _to_project_response(record)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a project owned by the current user."""
    repo = ProjectRepository(db)
    record = await require_project_access(db, project_id, user)
    try:
        await repo.delete(record)
    except Exception as exc:
        logger.warning("delete_project error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to delete project") from exc
