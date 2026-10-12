"""One active pipeline per project, across threads, workers and hosts.

With durable checkpoints (the only configuration that allows several worker
processes) the lock is a PostgreSQL session advisory lock. It is held on a
dedicated connection and released when that connection closes, including when
its process dies, so a crash never leaves a project locked. With in-memory
checkpoints everything lives in one process and a process-local lock suffices.

The lock never waits: a second approval or run for a busy project is refused,
not queued, so a duplicated or redelivered request cannot start a second run.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_NAMESPACE = b"eljazari.pipeline-run:"
_local_locks: dict[str, threading.Lock] = {}
_local_guard = threading.Lock()


def advisory_key(project_id: str) -> int:
    """Stable signed 64-bit key; a collision only serializes two projects."""
    digest = hashlib.sha256(_NAMESPACE + project_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@asynccontextmanager
async def project_run_lock(project_id: str) -> AsyncIterator[bool]:
    """Yield True when this caller now owns the project's pipeline."""
    from config.settings import settings

    if settings.resolved_checkpoint_backend == "postgres":
        async with _advisory_lock(settings.database_url, advisory_key(project_id)) as acquired:
            yield acquired
        return
    with _local_guard:
        lock = _local_locks.setdefault(project_id, threading.Lock())
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


@asynccontextmanager
async def _advisory_lock(database_url: str, key: int) -> AsyncIterator[bool]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    # Own engine: callers run on worker threads with their own event loops.
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            acquired = bool(result.scalar())
            try:
                yield acquired
            finally:
                if acquired:
                    await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    finally:
        await engine.dispose()
