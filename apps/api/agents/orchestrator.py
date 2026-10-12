"""eljazari.ai — Orchestrator (Prompt 2.1)

Manages the full LangGraph pipeline lifecycle:
  • Starts new runs                    (run)
  • Streams state updates via async generator
  • Suspends at HITL interrupt points
  • Resumes after human approval       (approve / reject)
  • Reports current pipeline state     (get_state)
  • Supports LangSmith tracing         (configured via env vars)

LangSmith is enabled automatically when LANGCHAIN_TRACING_V2=true and
LANGCHAIN_API_KEY are set in the environment (handled by config.settings).
"""

import os
import uuid
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import structlog

from graph.state import initial_state
from graph.workflow import workflow, get_workflow_async

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# LangSmith bootstrap
# ─────────────────────────────────────────────────────────────────────────────


def _configure_langsmith() -> None:
    """Push LangSmith env-vars from settings so tracing activates automatically."""
    try:
        from config.settings import settings

        if settings.langchain_tracing_v2 and settings.langchain_api_key:
            os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
            os.environ.setdefault("LANGCHAIN_API_KEY", settings.langchain_api_key)
            os.environ.setdefault("LANGCHAIN_PROJECT", settings.langchain_project)
            log.info("orchestrator.langsmith_enabled", project=settings.langchain_project)
        else:
            log.debug("orchestrator.langsmith_disabled")
    except Exception:
        pass  # settings may not be available in test environments


_configure_langsmith()


# ─────────────────────────────────────────────────────────────────────────────
# Event helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_event(
    project_id: str,
    event_type: str,
    node: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "event": event_type,
        "node": node,
        "data": data or {},
        "ts": time.time(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────


class Orchestrator:
    """Central coordinator for the eljazari.ai multi-agent pipeline.

    Usage (FastAPI background task)::

        orch = Orchestrator()
        async for event in orch.run(prompt, project_id):
            await sio.emit("pipeline_event", event, room=project_id)

        # When HITL interrupt fires:
        await orch.approve(project_id, approved=True)

        # Resume execution:
        async for event in orch.resume(project_id):
            ...
    """

    def __init__(self, use_postgres: bool | None = None) -> None:
        if use_postgres is None:
            from config.settings import settings

            use_postgres = settings.resolved_checkpoint_backend == "postgres"
        self._use_postgres = use_postgres
        self._graph = workflow  # Used only for explicit/development memory mode
        self._graph_ready = not use_postgres

    # ── Graph initialisation ──────────────────────────────────────────────────

    async def _get_graph(self):
        """Development graph accessor; retained for local callers and tests."""
        if self._use_postgres:
            raise RuntimeError("PostgreSQL graphs require an operation-scoped context")
        return self._graph

    @asynccontextmanager
    async def _graph_context(self):
        if getattr(self, "_use_postgres", False):
            async with get_workflow_async() as graph:
                yield graph
        else:
            yield await self._get_graph()

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(
        self,
        user_prompt: str,
        project_id: str | None = None,
        max_iterations: int = 5,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Start a new pipeline run.

        Yields one event dict per node execution.  Suspends automatically at
        HITL interrupt nodes and emits a ``hitl_interrupt`` event so the caller
        knows to wait for human input before calling :meth:`resume`.
        """
        project_id = project_id or str(uuid.uuid4())
        async with self._graph_context() as graph:
            config = {"configurable": {"thread_id": project_id}}
            state = initial_state(user_prompt, project_id, max_iterations)

            log.info("orchestrator.run.start", project_id=project_id, prompt_len=len(user_prompt))
            yield _make_event(project_id, "pipeline_started", data={"project_id": project_id})

            async for raw_event in graph.astream(state, config=config, stream_mode="updates"):
                node_name, node_updates = next(iter(raw_event.items()))
                log.debug("orchestrator.node_complete", node=node_name, project_id=project_id)
                yield _make_event(
                    project_id,
                    "node_complete",
                    node=node_name,
                    data=node_updates,
                )

            # After stream ends check if we're paused at a HITL node
            current = await graph.aget_state(config)
            if current.next:
                interrupted_at = list(current.next)
                log.info("orchestrator.hitl_interrupt", nodes=interrupted_at, project_id=project_id)
                yield _make_event(
                    project_id,
                    "hitl_interrupt",
                    node=interrupted_at[0] if interrupted_at else None,
                    data={
                        "interrupted_before": interrupted_at,
                        "phase": current.values.get("current_phase"),
                        "design_tree": current.values.get("design_tree"),
                        "generated_code": current.values.get("generated_code"),
                        "safety_report": current.values.get("safety_report"),
                    },
                )
            else:
                log.info("orchestrator.run.complete", project_id=project_id)
                yield _make_event(project_id, "pipeline_complete")

    async def resume(
        self,
        project_id: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Resume a pipeline that was suspended at a HITL interrupt.

        Call :meth:`approve` or :meth:`reject` *first* to patch the state,
        then call this method to continue execution.
        """
        async with self._graph_context() as graph:
            config = {"configurable": {"thread_id": project_id}}

            log.info("orchestrator.resume", project_id=project_id)
            yield _make_event(project_id, "pipeline_resumed")

            async for raw_event in graph.astream(None, config=config, stream_mode="updates"):
                node_name, node_updates = next(iter(raw_event.items()))
                log.debug("orchestrator.node_complete", node=node_name, project_id=project_id)
                yield _make_event(
                    project_id,
                    "node_complete",
                    node=node_name,
                    data=node_updates,
                )

            current = await graph.aget_state(config)
            if current.next:
                interrupted_at = list(current.next)
                yield _make_event(
                    project_id,
                    "hitl_interrupt",
                    node=interrupted_at[0] if interrupted_at else None,
                    data={"interrupted_before": interrupted_at},
                )
            else:
                log.info("orchestrator.resume.complete", project_id=project_id)
                yield _make_event(project_id, "pipeline_complete")

    async def approve(
        self,
        project_id: str,
        phase: str | bool | None = None,  # "design" | "code" or legacy approved bool
        feedback: str = "",
        approved: bool | None = None,
    ) -> None:
        """Mark the current HITL checkpoint as *approved* and patch state.

        After this call, invoke :meth:`resume` to continue execution.
        """
        if isinstance(phase, bool) and approved is None:
            approved = phase
            phase = None
        if approved is None:
            approved = True
        if not approved:
            await self.reject(
                project_id,
                phase=await self._resolve_phase(project_id, phase),
                feedback=feedback,
            )
            return

        async with self._graph_context() as graph:
            config = {"configurable": {"thread_id": project_id}}
            resolved_phase = await self._resolve_phase(project_id, phase)

            patch: dict[str, Any] = {
                "human_approval_needed": False,
                "human_feedback": feedback or None,
            }
            if resolved_phase == "design":
                patch["design_status"] = "approved"
            elif resolved_phase == "code":
                patch["code_status"] = "approved"

            await graph.aupdate_state(config, patch)
            log.info("orchestrator.approved", project_id=project_id, phase=resolved_phase)

    async def reject(
        self,
        project_id: str,
        phase: str,
        feedback: str,
    ) -> None:
        """Mark the current HITL checkpoint as *rejected* with feedback.

        The graph will loop back to the relevant generation node.
        """
        async with self._graph_context() as graph:
            config = {"configurable": {"thread_id": project_id}}

            patch: dict[str, Any] = {
                "human_approval_needed": False,
                "human_feedback": feedback,
            }
            if phase == "design":
                patch["design_status"] = "rejected"
                patch["design_feedback"] = feedback
            elif phase == "code":
                patch["code_status"] = "rejected"
                patch["code_feedback"] = feedback

            await graph.aupdate_state(config, patch)
            log.info("orchestrator.rejected", project_id=project_id, phase=phase)

    async def _resolve_phase(self, project_id: str, phase: str | None) -> str:
        if phase in {"design", "code"}:
            return phase
        async with self._graph_context() as graph:
            config = {"configurable": {"thread_id": project_id}}
            snapshot = await graph.aget_state(config)
            next_nodes = set(snapshot.next or [])
            if "human_review_design" in next_nodes:
                return "design"
            if "human_review_code" in next_nodes:
                return "code"
            current_phase = snapshot.values.get("current_phase")
            if current_phase in {"generate_design", "human_review_design"}:
                return "design"
            return "code"

    async def is_awaiting_review(self, project_id: str, phase: str | None = None) -> bool:
        """True iff the graph is currently suspended at a HITL review node.

        Used to make ``/design/approve`` and ``/code/approve`` idempotent:
        an approval that arrives when the graph is NOT paused at the matching
        review gate (already past it, mid-execution elsewhere, or completed)
        must be a no-op. Without this guard, a duplicate or late approval
        re-applies ``aupdate_state`` + ``resume`` and re-runs the pipeline
        from the current checkpoint — the cause of the duplicated
        simulation/monitor runs seen under rapid repeated approvals.

        When ``phase`` is given ("design" | "code"), checks that the
        specific gate is open; otherwise true if either gate is open.
        """
        async with self._graph_context() as graph:
            config = {"configurable": {"thread_id": project_id}}
            snapshot = await graph.aget_state(config)
            next_nodes = set(snapshot.next or [])
            if phase == "design":
                return "human_review_design" in next_nodes
            if phase == "code":
                return "human_review_code" in next_nodes
            return bool(next_nodes & {"human_review_design", "human_review_code"})

    async def get_state(self, project_id: str) -> dict[str, Any]:
        """Return the current pipeline state snapshot for a project."""
        async with self._graph_context() as graph:
            config = {"configurable": {"thread_id": project_id}}
            snapshot = await graph.aget_state(config)
            return {
                "project_id": project_id,
                "current_phase": snapshot.values.get("current_phase", "unknown"),
                "next_nodes": list(snapshot.next) if snapshot.next else [],
                "design_status": snapshot.values.get("design_status"),
                "code_status": snapshot.values.get("code_status"),
                "simulation_success": snapshot.values.get("simulation_success"),
                "simulation_iteration": snapshot.values.get("simulation_iteration", 0),
                "safety_passed": snapshot.values.get("safety_passed", False),
                "errors": snapshot.values.get("errors", []),
                "human_approval_needed": snapshot.values.get("human_approval_needed", False),
                "approved_code_sha256": snapshot.values.get("approved_code_sha256"),
                # Identity of the aircraft a manual run must fly (see run_simulation).
                "simulation_model_config": {
                    key: snapshot.values[key]
                    for key in (
                        "urdf_path",
                        "sdf_path",
                        "model_manifest_path",
                        "resolved_drone_sha256",
                        "flight_model_manifest_path",
                        "bom_status",
                    )
                    if snapshot.values.get(key)
                },
            }

    async def get_history(self, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return checkpoint history for a project (most recent first)."""
        async with self._graph_context() as graph:
            config = {"configurable": {"thread_id": project_id}}
            history = []
            async for state in graph.aget_state_history(config):
                history.append(
                    {
                        "ts": getattr(state, "created_at", None),
                        "next": list(state.next) if state.next else [],
                        "phase": state.values.get("current_phase"),
                        "checkpoint_id": getattr(
                            state.config.get("configurable", {}), "get", lambda k, d=None: d
                        )("checkpoint_id"),
                    }
                )
                if len(history) >= limit:
                    break
            return history
