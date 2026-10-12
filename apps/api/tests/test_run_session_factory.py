"""Pipeline runs use their own DB sessions, never the API loop's pooled ones.

A run executes on a worker thread with its own event loop. When the graph's
simulation node opened a session from the shared engine, the pooled asyncpg
connection belonged to the API's loop and the save failed with "Future attached
to a different loop" (run_simulation.persist_skipped), so the simulation row of
an accepted flight was never stored.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any, TypedDict
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph

from db.session import AsyncSessionLocal, current_session_factory, use_session_factory
from graph.nodes import _persist_simulation_record
from services import pipeline_runtime as runtime


def test_shared_factory_outside_a_run():
    assert current_session_factory() is AsyncSessionLocal


def test_run_factory_is_scoped_and_restored():
    run_factory = MagicMock()
    with use_session_factory(run_factory):
        assert current_session_factory() is run_factory
    assert current_session_factory() is AsyncSessionLocal


@pytest.mark.asyncio
async def test_run_factory_reaches_langgraph_nodes():
    class State(TypedDict, total=False):
        seen: Any

    async def node(state: State) -> State:
        # LangGraph runs nodes in tasks of its own; the value must follow.
        return {"seen": current_session_factory()}

    builder = StateGraph(State)
    builder.add_node("node", node)
    builder.add_edge(START, "node")
    builder.add_edge("node", END)
    graph = builder.compile()

    run_factory = MagicMock()
    with use_session_factory(run_factory):
        seen = [update async for update in graph.astream({}, stream_mode="updates")]
    assert seen == [{"node": {"seen": run_factory}}]


@pytest.mark.asyncio
async def test_simulation_record_is_saved_with_the_run_factory():
    project_id, code_id = uuid4(), uuid4()
    session = MagicMock()
    session.commit = AsyncMock()

    @asynccontextmanager
    async def run_session():
        yield session

    run_factory = MagicMock(side_effect=run_session)
    row = MagicMock(id=uuid4())
    with (
        patch("repositories.generated_code.GeneratedCodeRepository") as codes,
        patch("repositories.simulation.SimulationRepository") as sims,
        patch("db.session.AsyncSessionLocal", side_effect=AssertionError("shared pool used")),
    ):
        codes.return_value.get_latest_for_project = AsyncMock(
            return_value=MagicMock(id=code_id, code_content="code")
        )
        sims.return_value.create_from_schema = AsyncMock(return_value=row)
        sims.return_value.update_from_schema = AsyncMock()
        with use_session_factory(run_factory):
            record_id = await _persist_simulation_record(
                project_id=str(project_id),
                executed_code="code",
                iteration=1,
                telemetry=[],
                observations=[],
                mission_completed=True,
                crash_detected=False,
                telemetry_source="real_native_sitl",
                duration_s=60.0,
            )
    assert record_id == str(row.id)
    run_factory.assert_called_once()
    session.commit.assert_awaited_once()


def _worker_factory():
    engine = MagicMock()
    engine.dispose = AsyncMock()
    return engine, MagicMock(name="run_factory")


@asynccontextmanager
async def _owned(_project_id):
    yield True


@pytest.mark.asyncio
async def test_new_run_installs_its_factory_for_the_graph():
    engine, run_factory = _worker_factory()
    seen = []

    async def fake_run(prompt, project_id):
        seen.append(current_session_factory())
        # Graph nodes run in tasks created during the stream.
        seen.append(await asyncio.create_task(asyncio.sleep(0, current_session_factory())))
        yield {"node": "parse_prompt", "data": {}}

    with (
        patch.object(runtime, "project_run_lock", _owned),
        patch.object(runtime, "_build_worker_session_factory", return_value=(engine, run_factory)),
        patch.object(runtime._ORCHESTRATOR, "run", fake_run),
        patch.object(runtime, "_persist_pipeline_event", AsyncMock()),
        patch.object(runtime, "broadcast_to_project", AsyncMock()),
    ):
        await runtime._run_pipeline(str(uuid4()), "prompt")
    assert seen == [run_factory, run_factory]
    assert current_session_factory() is AsyncSessionLocal
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_resumed_run_installs_its_factory_for_the_graph():
    engine, run_factory = _worker_factory()
    seen = []

    async def fake_resume(project_id):
        seen.append(current_session_factory())
        yield {"node": "run_simulation", "data": {}}

    orchestrator = runtime._ORCHESTRATOR
    with (
        patch.object(runtime, "_build_worker_session_factory", return_value=(engine, run_factory)),
        patch.object(orchestrator, "is_awaiting_review", AsyncMock(return_value=True)),
        patch.object(orchestrator, "approve", AsyncMock()),
        patch.object(orchestrator, "resume", fake_resume),
        patch.object(runtime, "_persist_hitl_approval", AsyncMock()),
        patch.object(runtime, "_persist_pipeline_event", AsyncMock()),
        patch.object(runtime, "broadcast_to_project", AsyncMock()),
    ):
        await runtime._continue_owned_pipeline(
            str(uuid4()), resolved_phase="code", approved=True, feedback=""
        )
    assert seen == [run_factory]
    assert current_session_factory() is AsyncSessionLocal
