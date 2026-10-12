"""Opt-in PostgreSQL checkpoint restart smoke test, isolated in a temporary schema.

Run from apps/api with required dependencies installed. Uses configured local
PostgreSQL credentials without printing them. It runs three separate processes,
never invokes an LLM or simulator, and removes only its own generated schema.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))


def worker(stage: str) -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def check():
        from config.settings import settings
        from graph import workflow as module
        from graph.state import ElJazariState
        from langgraph.graph import StateGraph, END
        from agents.orchestrator import Orchestrator

        settings.database_url = os.environ["CHECKPOINT_TEST_DSN"]
        record_id = os.environ["CHECKPOINT_TEST_RECORD"]

        def tiny_graph(saver):
            builder = StateGraph(ElJazariState)
            builder.add_node(
                "prepare",
                lambda state: {
                    "current_phase": "human_review_design",
                    "simulation_record_id": record_id,
                },
            )
            builder.add_node("human_review_design", lambda state: {"current_phase": "complete"})
            builder.set_entry_point("prepare")
            builder.add_edge("prepare", "human_review_design")
            builder.add_edge("human_review_design", END)
            return builder.compile(checkpointer=saver, interrupt_before=["human_review_design"])

        module._build_graph = tiny_graph
        orch = Orchestrator(use_postgres=True)
        if stage == "write":
            async with module.get_workflow_async(setup=True):
                pass
            events = [event async for event in orch.run("checkpoint smoke", "restart-smoke")]
            assert events[-1]["event"] == "hitl_interrupt"
        elif stage == "resume":
            snapshot = await orch.get_state("restart-smoke")
            assert snapshot["next_nodes"] == ["human_review_design"]
            await orch.approve("restart-smoke", phase="design")
            events = [event async for event in orch.resume("restart-smoke")]
            assert events[-1]["event"] == "pipeline_complete"
        elif stage == "verify":
            async with module.get_workflow_async() as graph:
                snapshot = await graph.aget_state({"configurable": {"thread_id": "restart-smoke"}})
                assert snapshot.values["current_phase"] == "complete"
                assert snapshot.values["design_status"] == "approved"
                assert snapshot.values["simulation_record_id"] == record_id
                assert not snapshot.next
        else:
            raise ValueError("Unknown worker stage")

    asyncio.run(check())
    print(f"Checkpoint {stage}: PASS")


def main() -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from config.settings import settings

    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection_info = conninfo_to_dict(dsn)
    if connection_info.get("host") not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("This smoke test requires explicitly configured local PostgreSQL")
    schema = "checkpoint_smoke_" + uuid4().hex
    # CREATE/DROP is restricted to this unique schema. Existing application data
    # and checkpoint tables are never used as test storage.
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            env = os.environ.copy()
            env["CHECKPOINT_TEST_DSN"] = make_conninfo(
                dsn, options=f"-csearch_path={schema}", connect_timeout=5
            )
            env["CHECKPOINT_TEST_RECORD"] = str(uuid4())
            env["PYTHONPATH"] = os.pathsep.join(str(p) for p in sys.path if p)
            for stage in ("write", "resume", "verify"):
                process = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--worker", stage],
                    env=env,
                    cwd=API_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                if process.returncode:
                    # Keep connection details and environment values out of output.
                    raise RuntimeError(
                        f"Checkpoint {stage} process failed (exit {process.returncode})"
                    )
                print(f"Checkpoint {stage}: PASS")
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    print("Restart smoke: PASS; temporary schema removed")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(sys.argv[2])
    else:
        main()
