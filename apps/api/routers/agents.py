"""eljazari.ai - Legacy agents router kept for frontend compatibility."""

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from middleware.auth import AuthUser
from services.pipeline_runtime import continue_pipeline_background, run_pipeline_background
from services.project_access import require_project_access

router = APIRouter()


class RunPipelineRequest(BaseModel):
    project_id: str
    prompt: str


class HITLApprovalRequest(BaseModel):
    project_id: str
    approved: bool
    feedback: str = ""


@router.post("/run")
async def run_pipeline(
    body: RunPipelineRequest,
    background_tasks: BackgroundTasks,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
):
    await require_project_access(db, body.project_id, user)
    background_tasks.add_task(run_pipeline_background, body.project_id, body.prompt)
    return {"status": "started", "project_id": body.project_id}


@router.post("/approve")
async def approve_hitl(
    body: HITLApprovalRequest,
    user: AuthUser,
    db: AsyncSession = Depends(get_db),
):
    await require_project_access(db, body.project_id, user)
    continue_pipeline_background(
        body.project_id,
        phase=None,
        approved=body.approved,
        feedback=body.feedback,
    )
    return {"status": "approved" if body.approved else "rejected"}
