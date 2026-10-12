"""eljazari.ai - Code Router (generated mission code + HITL)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from middleware.auth import AuthUser
from repositories.generated_code import GeneratedCodeRepository
from services.pipeline_runtime import continue_pipeline_background
from services.project_access import require_project_access

logger = logging.getLogger(__name__)
router = APIRouter()


class ModifyCodeRequest(BaseModel):
    feedback: str = Field(..., min_length=5)


class CodeResponse(BaseModel):
    project_id: str
    code: str | None = None
    language: str = "python"
    iteration: int = 0
    safety_report: dict | None = None
    status: str


def _code_status(record) -> str:
    return "validated" if getattr(record, "safety_validated", False) else "generated"


@router.get("/{project_id}/code", response_model=CodeResponse)
async def get_code(
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> CodeResponse:
    """Fetch the latest generated code artifact for a project."""
    project = await require_project_access(db, project_id, user)
    repo = GeneratedCodeRepository(db)
    record = await repo.get_latest_for_project(project.id)
    if record is None:
        return CodeResponse(project_id=str(project.id), status="pending")
    return CodeResponse(
        project_id=str(project.id),
        code=record.code_content,
        iteration=record.version or 0,
        safety_report=record.safety_report,
        status=_code_status(record),
    )


@router.post("/{project_id}/code/approve")
async def approve_code(
    project_id: str,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """HITL approval: advance pipeline to simulation phase."""
    project = await require_project_access(db, project_id, user)
    try:
        continue_pipeline_background(str(project.id), phase="code", approved=True, feedback="")
        return {"status": "approved", "project_id": str(project.id)}
    except Exception as exc:
        logger.exception("approve_code error project=%s", project.id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{project_id}/code/modify")
async def modify_code(
    project_id: str,
    body: ModifyCodeRequest,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """HITL modification: send feedback and re-run code generation."""
    project = await require_project_access(db, project_id, user)
    try:
        continue_pipeline_background(
            str(project.id),
            phase="code",
            approved=False,
            feedback=body.feedback,
        )
        return {"status": "modification_requested", "project_id": str(project.id)}
    except Exception as exc:
        logger.exception("modify_code error project=%s", project.id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
