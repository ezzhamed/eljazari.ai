"""Check that a pipeline worker thread can save a simulation record (real database).

Reproduces the condition behind run_simulation.persist_skipped: the API's loop
has pooled connections in the shared engine, and the graph runs on a worker
thread with its own event loop. Saves one record for an existing project's
approved code, through the run's session factory as pipeline_runtime sets it,
then deletes the row. Pass --shared-pool to try the shared pool instead (the
old behaviour, which fails on a pooled engine).

Usage (Linux/WSL, API environment, PostgreSQL in .env):
    python scripts/check_run_persistence.py PROJECT_ID [--shared-pool]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, text  # noqa: E402

from db.session import AsyncSessionLocal, use_session_factory  # noqa: E402
from graph.nodes import _persist_simulation_record  # noqa: E402
from models.simulation import Simulation  # noqa: E402
from repositories.generated_code import GeneratedCodeRepository  # noqa: E402
from services.pipeline_runtime import _build_worker_session_factory  # noqa: E402
from services.project_access import parse_project_uuid  # noqa: E402


async def _api_loop_use(project_id: str) -> str:
    # Leaves a connection from this loop in the shared pool, as API requests do.
    async with AsyncSessionLocal() as db:
        await db.execute(text("SELECT 1"))
        code = await GeneratedCodeRepository(db).get_latest_for_project(
            parse_project_uuid(project_id)
        )
    if code is None:
        raise SystemExit(f"Project {project_id} has no generated code")
    return code.code_content


async def _worker(project_id: str, code: str, shared_pool: bool) -> str | None:
    async def save() -> str | None:
        return await _persist_simulation_record(
            project_id=project_id,
            executed_code=code,
            iteration=99,
            telemetry=[],
            observations=[],
            mission_completed=True,
            crash_detected=False,
            telemetry_source="persistence_check",
            duration_s=0.0,
        )

    engine, factory = _build_worker_session_factory()
    try:
        if shared_pool:
            record_id = await save()
        else:
            with use_session_factory(factory):
                record_id = await save()
        if record_id:
            async with factory() as db:
                await db.execute(
                    delete(Simulation).where(Simulation.id == parse_project_uuid(record_id))
                )
                await db.commit()
        return record_id
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project_id")
    parser.add_argument("--shared-pool", action="store_true")
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    code = loop.run_until_complete(_api_loop_use(args.project_id))

    result: dict[str, str | None] = {}
    thread = threading.Thread(
        target=lambda: result.update(
            record_id=asyncio.run(_worker(args.project_id, code, args.shared_pool))
        )
    )
    thread.start()
    thread.join()
    loop.close()

    record_id = result.get("record_id")
    mode = "shared pool" if args.shared_pool else "run session factory"
    print(f"{mode}: {'saved ' + record_id if record_id else 'NOT saved'}")
    return 0 if record_id else 1


if __name__ == "__main__":
    raise SystemExit(main())
