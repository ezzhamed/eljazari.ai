"""eljazari.ai - Design Router (approve / modify drone design)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from middleware.auth import AuthUser
from repositories.design import DesignRepository
from services.pipeline_runtime import continue_pipeline_background
from services.project_access import require_project_access

logger = logging.getLogger(__name__)
router = APIRouter()


class ModifyDesignRequest(BaseModel):
    feedback: str = Field(..., min_length=5, description="Modification notes in natural language")


class DesignResponse(BaseModel):
    project_id: str
    design_tree: dict | None = None
    status: str
    urdf_path: str | None = None


@router.get("/{project_id}/design", response_model=DesignResponse)
async def get_design(
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> DesignResponse:
    """Fetch the latest design for a project."""
    project = await require_project_access(db, project_id, user)
    repo = DesignRepository(db)
    record = await repo.get_latest_for_project(project.id)
    if record is None:
        return DesignResponse(project_id=str(project.id), status="pending")
    return DesignResponse(
        project_id=str(project.id),
        design_tree=record.design_tree,
        status=record.status.value if hasattr(record.status, "value") else str(record.status),
        urdf_path=record.urdf_file_path,
    )


@router.post("/{project_id}/design/approve")
async def approve_design(
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """HITL approval: advance pipeline to URDF conversion."""
    project = await require_project_access(db, project_id, user)
    try:
        continue_pipeline_background(str(project.id), phase="design", approved=True, feedback="")
        return {"status": "approved", "project_id": str(project.id)}
    except Exception as exc:
        logger.exception("approve_design error project=%s", project.id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{project_id}/design/modify")
async def modify_design(
    project_id: str,
    body: ModifyDesignRequest,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """HITL modification: send feedback and re-run design phase."""
    project = await require_project_access(db, project_id, user)
    try:
        continue_pipeline_background(
            str(project.id),
            phase="design",
            approved=False,
            feedback=body.feedback,
        )
        return {"status": "modification_requested", "project_id": str(project.id)}
    except Exception as exc:
        logger.exception("modify_design error project=%s", project.id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
