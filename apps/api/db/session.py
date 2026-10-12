"""eljazari.ai — Async SQLAlchemy engine & session factory"""

from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import platform

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from config.settings import settings


_is_sqlite = settings.database_url.startswith("sqlite")


def _build_engine():
    if _is_sqlite:
        # SQLite for local development — no external DB required
        return create_async_engine(
            settings.database_url,
            echo=settings.debug,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

    engine_kwargs = {
        "echo": settings.debug,
        "pool_pre_ping": True,
    }

    # Windows test and dev loops are prone to reusing pooled asyncpg connections
    # across closed event loops. NullPool avoids cross-loop connection reuse.
    if platform.system() == "Windows":
        engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs.update(
            pool_size=10,
            max_overflow=20,
            pool_recycle=3600,
        )

    return create_async_engine(settings.database_url, **engine_kwargs)


engine = _build_engine()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


# Pipeline runs execute on a worker thread with their own event loop. Pooled
# asyncpg connections from the shared engine belong to the API's loop, and using
# one there fails with "Future attached to a different loop". The worker
# installs its own per-run factory here for the code the graph calls.
_run_session_factory: ContextVar[async_sessionmaker[AsyncSession] | None] = ContextVar(
    "run_session_factory", default=None
)


def current_session_factory() -> async_sessionmaker[AsyncSession]:
    """The session factory for the running pipeline, or the shared one."""
    return _run_session_factory.get() or AsyncSessionLocal


@contextmanager
def use_session_factory(factory: async_sessionmaker[AsyncSession]) -> Iterator[None]:
    """Make `current_session_factory()` return `factory` within this context."""
    token = _run_session_factory.set(factory)
    try:
        yield
    finally:
        _run_session_factory.reset(token)


async def create_tables():
    """Create all tables (used for SQLite dev mode)."""
    from db.base import Base

    # Import all models so they register with Base.metadata
    import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
