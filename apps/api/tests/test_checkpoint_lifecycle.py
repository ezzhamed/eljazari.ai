"""Operation-scoped checkpoint resources and explicit durability policy."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.orchestrator import Orchestrator
from config.settings import Settings, settings
from graph import workflow as module


@pytest.mark.asyncio
async def test_connection_lifetime(monkeypatch):
    lifecycle = []
    saver = SimpleNamespace(setup=AsyncMock())

    @asynccontextmanager
    async def connection(dsn):
        lifecycle.append("opened")
        try:
            yield saver
        finally:
            lifecycle.append("closed")

    monkeypatch.setattr(module, "_postgres_saver_context", connection)
    monkeypatch.setattr(module, "_build_graph", lambda s: s)
    async with module.get_workflow_async(setup=True) as graph:
        assert graph is saver
        assert lifecycle == ["opened"]
        saver.setup.assert_awaited_once()
    assert lifecycle == ["opened", "closed"]


@pytest.mark.asyncio
async def test_setup_failure_closes_without_memory_fallback(monkeypatch):
    closed = []
    saver = SimpleNamespace(setup=AsyncMock(side_effect=RuntimeError("DB failed")))

    @asynccontextmanager
    async def connection(dsn):
        try:
            yield saver
        finally:
            closed.append(True)

    monkeypatch.setattr(module, "_postgres_saver_context", connection)
    monkeypatch.setattr(module, "_make_memory_saver", lambda: pytest.fail("No fallback"))
    with pytest.raises(RuntimeError, match="memory fallback is disabled"):
        async with module.get_workflow_async(setup=True):
            pytest.fail("No graph")
    assert closed == [True]


@pytest.mark.asyncio
async def test_missing_dependency_is_explicit(monkeypatch):
    def missing(dsn):
        raise ImportError("Missing dependency")

    monkeypatch.setattr(module, "_postgres_saver_context", missing)
    with pytest.raises(RuntimeError, match="checkpoint storage is unavailable"):
        async with module.get_workflow_async():
            pytest.fail("No graph")


@pytest.mark.asyncio
async def test_operation_connections_not_cached(monkeypatch):
    opened, closed = [], []

    @asynccontextmanager
    async def graph_context():
        graph = SimpleNamespace(
            aget_state=AsyncMock(return_value=SimpleNamespace(values={}, next=[]))
        )
        opened.append(graph)
        try:
            yield graph
        finally:
            closed.append(graph)

    monkeypatch.setattr("agents.orchestrator.get_workflow_async", graph_context)
    orch = Orchestrator(use_postgres=True)
    await orch.get_state("test")
    await orch.get_state("test")
    assert len(opened) == 2 and opened[0] is not opened[1]
    assert closed == opened


@pytest.mark.asyncio
async def test_cancellation_closes_connection(monkeypatch):
    import asyncio

    opened, closed = asyncio.Event(), []

    @asynccontextmanager
    async def graph_context():
        async def read(config):
            opened.set()
            await asyncio.Event().wait()

        try:
            yield SimpleNamespace(aget_state=read)
        finally:
            closed.append(True)

    monkeypatch.setattr("agents.orchestrator.get_workflow_async", graph_context)
    task = asyncio.create_task(Orchestrator(use_postgres=True).get_state("test"))
    await opened.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


def test_policy(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "checkpoint_backend", "auto")
    assert Orchestrator()._use_postgres
    monkeypatch.setattr(settings, "environment", "development")
    assert settings.resolved_checkpoint_backend == "memory"
    monkeypatch.setattr(settings, "checkpoint_backend", "postgres")
    assert Orchestrator()._use_postgres


def test_production_rejects_memory():
    with pytest.raises(ValueError, match="in-memory checkpoints"):
        Settings(environment="production", checkpoint_backend="memory")


@pytest.mark.asyncio
async def test_closing_partial_run_releases_graph(monkeypatch):
    closed = []

    @asynccontextmanager
    async def graph_context():
        try:
            yield SimpleNamespace()
        finally:
            closed.append(True)

    monkeypatch.setattr("agents.orchestrator.get_workflow_async", graph_context)
    stream = Orchestrator(use_postgres=True).run("test", "project")
    assert (await anext(stream))["event"] == "pipeline_started"
    await stream.aclose()
    assert closed == [True]


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.asyncio
async def test_server_probes_durability_and_refuses_failure(monkeypatch, fail):
    import main
    import db.session

    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(execute=AsyncMock())

    engine = SimpleNamespace(connect=connection, dispose=AsyncMock())
    monkeypatch.setattr(db.session, "engine", engine)
    monkeypatch.setattr(main, "ensure_docker_services", lambda: None)
    monkeypatch.setattr(settings, "aircraftverse_require_real", False)
    monkeypatch.setattr(settings, "checkpoint_backend", "postgres")
    lifecycle = []

    @asynccontextmanager
    async def checkpoint(**kwargs):
        assert kwargs == {"setup": True}
        lifecycle.append("probe")
        if fail:
            raise RuntimeError("Checkpoint unavailable")
        try:
            yield object()
        finally:
            lifecycle.append("closed")

    monkeypatch.setattr(module, "get_workflow_async", checkpoint)
    if fail:
        with pytest.raises(RuntimeError, match="Checkpoint unavailable"):
            async with main.lifespan(None):
                pytest.fail("Must not start without requested durability")
        assert lifecycle == ["probe"]
    else:
        async with main.lifespan(None):
            assert lifecycle == ["probe", "closed"]
    engine.dispose.assert_awaited_once()
