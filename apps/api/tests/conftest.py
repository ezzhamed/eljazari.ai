"""Pytest fixtures for environments with restricted system temp directories."""

from __future__ import annotations

import contextlib
import re
import shutil
import uuid
from pathlib import Path

import pytest


_TMP_ROOT = Path(__file__).resolve().parents[1] / ".pytest-local-tmp"


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Path:
    """Use a workspace-local temp directory when `%TEMP%` is locked down."""
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    # Capped: Windows refuses a path component over 255 characters, and escaped
    # non-ASCII parameter IDs (the Arabic prompts) exceed that.
    test_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name).strip("_")[:80]
    path = _TMP_ROOT / f"{test_name}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _in_memory_design_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests off the developer's persisted AircraftVerse index.

    A local .env selecting the persistent mode made every design-agent test add
    5,000 documents to data/aircraftverse/chroma_db.
    """
    from config.settings import settings

    monkeypatch.setattr(settings, "aircraftverse_rag_mode", "memory")


@pytest.fixture(autouse=True)
def _private_storage(monkeypatch: pytest.MonkeyPatch):
    """Keep tests out of the configured storage directory.

    Found in a clean clone: an orchestrator test wrote the deliverables of
    test-proj-001 into STORAGE_PATH, then `data/storage` at the root of drive C:.
    """
    from config.settings import settings

    path = _TMP_ROOT / f"storage-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "storage_path", str(path))
    yield
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _private_simulator_lock(monkeypatch: pytest.MonkeyPatch):
    """Keep tests off the real one-simulator-per-host lock.

    A simulation running on the developer's machine held the shared lock and a
    launch test waited its full timeout, then failed with an unrelated message.
    """
    from services import gazebo_service

    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    lock = _TMP_ROOT / f"sitl-{uuid.uuid4().hex}.lock"
    monkeypatch.setattr(gazebo_service, "_SITL_HOST_LOCK", lock)
    yield
    # A test that leaves its run open still holds the lock; Windows refuses to
    # delete a held file, and the name is never reused.
    with contextlib.suppress(OSError):
        lock.unlink(missing_ok=True)
