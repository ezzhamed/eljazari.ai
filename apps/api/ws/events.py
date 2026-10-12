"""eljazari.ai - Socket.io event handlers."""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException

from config.settings import settings
from db.session import AsyncSessionLocal
from middleware.auth import ANONYMOUS_USER_ID, decode_token
from services.pipeline_runtime import continue_pipeline_background
from services.project_access import get_owned_project, parse_project_uuid
from ws.socket_manager import broadcast_to_project, project_room, sio

logger = logging.getLogger(__name__)


async def _get_socket_user_id(sid: str) -> uuid.UUID:
    session = await sio.get_session(sid)
    user_id = session.get("user_id")
    if not user_id:
        raise PermissionError("Authentication required")
    return uuid.UUID(str(user_id))


async def _authorize_project(sid: str, project_id: str) -> str:
    project_uuid = parse_project_uuid(project_id)
    user_id = await _get_socket_user_id(sid)
    async with AsyncSessionLocal() as db:
        project = await get_owned_project(db, project_uuid, user_id)
        return str(project.id)


async def _remember_project(sid: str, project_id: str) -> None:
    """Record the project this socket joined.

    GCS events carry no project_id — the client has already joined a room — so
    they resolve the target from the session instead of trusting the payload.
    """
    session = await sio.get_session(sid)
    session["project_id"] = project_id
    await sio.save_session(sid, session)


async def current_project(sid: str) -> str:
    """Project this socket joined, re-checked against ownership.

    Membership is re-verified on every call rather than trusted from the
    session, so a project that changed hands after `join_project` cannot keep
    serving a stale socket.
    """
    session = await sio.get_session(sid)
    project_id = session.get("project_id")
    if not project_id:
        raise PermissionError("join_project required before GCS commands")
    return await _authorize_project(sid, str(project_id))


@sio.on("connect")
async def on_connect(sid: str, environ: dict, auth: dict | None = None) -> bool | None:
    """Client connects. Auth may carry a JWT token for validation."""
    token = (auth or {}).get("token")
    try:
        if token:
            payload = decode_token(token)
            user_id = payload["sub"]
        elif settings.anonymous_auth_enabled:
            # Must match the HTTP path in middleware.auth.get_current_user.
            # Gating this on settings.debug instead left the socket open to
            # anonymous clients whenever DEBUG=true, even with
            # ALLOW_ANONYMOUS_AUTH=false locking down every REST route.
            user_id = ANONYMOUS_USER_ID
        else:
            return False
        await sio.save_session(sid, {"user_id": user_id})
        logger.info("WS connect sid=%s user=%s", sid, user_id)
        return True
    except HTTPException:
        logger.warning("WS rejected sid=%s due to invalid token", sid)
        return False


@sio.on("disconnect")
async def on_disconnect(sid: str) -> None:
    logger.info("WS disconnect sid=%s", sid)


@sio.on("join_project")
async def on_join_project(sid: str, data: dict) -> dict:
    project_id = data.get("project_id")
    if not project_id:
        return {"error": "project_id required"}
    try:
        authorized_project_id = await _authorize_project(sid, project_id)
        room = project_room(authorized_project_id)
        await sio.enter_room(sid, room)
        await _remember_project(sid, authorized_project_id)
        logger.info("WS join sid=%s project=%s", sid, authorized_project_id)
        return {"joined": room}
    except Exception as exc:
        return {"error": str(exc)}


@sio.on("leave_project")
async def on_leave_project(sid: str, data: dict) -> dict:
    project_id = data.get("project_id")
    if not project_id:
        return {"error": "project_id required"}
    await sio.leave_room(sid, project_room(project_id))
    return {"left": project_room(project_id)}


@sio.on("approve_design")
async def on_approve_design(sid: str, data: dict) -> dict:
    project_id = data.get("project_id")
    feedback = data.get("feedback", "")
    if not project_id:
        return {"error": "project_id required"}
    try:
        authorized_project_id = await _authorize_project(sid, project_id)
        continue_pipeline_background(
            authorized_project_id,
            phase="design",
            approved=True,
            feedback=feedback,
        )
        return {"status": "approved"}
    except Exception as exc:
        logger.exception("approve_design failed project=%s", project_id)
        return {"error": str(exc)}


@sio.on("approve_code")
async def on_approve_code(sid: str, data: dict) -> dict:
    project_id = data.get("project_id")
    if not project_id:
        return {"error": "project_id required"}
    try:
        authorized_project_id = await _authorize_project(sid, project_id)
        continue_pipeline_background(
            authorized_project_id,
            phase="code",
            approved=True,
            feedback="",
        )
        return {"status": "approved"}
    except Exception as exc:
        logger.exception("approve_code failed project=%s", project_id)
        return {"error": str(exc)}


@sio.on("request_modification")
async def on_request_modification(sid: str, data: dict) -> dict:
    project_id = data.get("project_id")
    phase = data.get("phase")
    feedback = data.get("feedback", "")
    if not project_id or not feedback:
        return {"error": "project_id and feedback required"}
    try:
        authorized_project_id = await _authorize_project(sid, project_id)
        resolved_phase = "design" if phase == "design" else "code"
        continue_pipeline_background(
            authorized_project_id,
            phase=resolved_phase,
            approved=False,
            feedback=feedback,
        )
        return {"status": "modification_requested"}
    except Exception as exc:
        logger.exception("request_modification failed project=%s", project_id)
        return {"error": str(exc)}


@sio.on("stop_simulation")
async def on_stop_simulation(sid: str, data: dict) -> dict:
    project_id = data.get("project_id")
    if not project_id:
        return {"error": "project_id required"}
    try:
        from services.gazebo_service import GazeboService

        authorized_project_id = await _authorize_project(sid, project_id)
        svc = GazeboService()
        await svc.stop_world(authorized_project_id)
        await broadcast_to_project(
            authorized_project_id,
            "sim_status",
            {"status": "stopped", "project_id": authorized_project_id},
        )
        return {"status": "stopped"}
    except Exception as exc:
        logger.exception("stop_simulation failed project=%s", project_id)
        return {"error": str(exc)}


@sio.on("restart_phase")
async def on_restart_phase(sid: str, data: dict) -> dict:
    project_id = data.get("project_id")
    phase = data.get("phase")
    if not project_id or not phase:
        return {"error": "project_id and phase required"}

    try:
        from graph.workflow import workflow

        authorized_project_id = await _authorize_project(sid, project_id)
        thread_config = {"configurable": {"thread_id": authorized_project_id}}
        await workflow.aupdate_state(
            thread_config,
            {"current_phase": phase, "human_approval_needed": False},
        )
        await broadcast_to_project(
            authorized_project_id,
            "phase_changed",
            {"phase": phase, "project_id": authorized_project_id, "restarted": True},
        )
        return {"status": "restarted", "phase": phase}
    except Exception as exc:
        logger.exception("restart_phase failed project=%s phase=%s", project_id, phase)
        return {"error": str(exc)}
