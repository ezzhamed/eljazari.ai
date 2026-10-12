"""scripts/prepare_assets.py: what it reports, where it builds, and that it asks first."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import prepare_assets as pa


def _settings(**overrides):
    base = dict(
        ollama_base_url="http://127.0.0.1:1",
        ollama_design_model="gemma3:4b",
        ollama_default_model="llama3.2:latest",
        ollama_code_model="qwen2.5-coder:7b",
        ollama_eval_model="llama3.2",
        ollama_embed_model="nomic-embed-text",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_each_model_is_listed_once_with_its_tag():
    assert pa.wanted_models(_settings()) == [
        "gemma3:4b",
        "llama3.2:latest",
        "qwen2.5-coder:7b",
        "nomic-embed-text:latest",
    ]


def test_models_are_reported_against_what_ollama_has(monkeypatch):
    monkeypatch.setattr(pa, "_installed_models", lambda base: {"gemma3:4b", "llama3.2:latest"})
    status = {a.name: a.present for a in pa.model_assets(_settings())}
    assert status == {
        "model gemma3:4b": True,
        "model llama3.2:latest": True,
        "model qwen2.5-coder:7b": False,
        "model nomic-embed-text:latest": False,
    }
    monkeypatch.setattr(pa, "_installed_models", lambda base: None)
    unreachable = pa.model_assets(_settings())
    assert not any(a.present for a in unreachable)
    assert "ollama serve" in unreachable[0].detail


def test_the_index_is_built_where_the_api_looks_for_it(tmp_path, monkeypatch):
    """The script derives the directory from the design agent's own key; check it
    against the directory the agent actually opens."""
    from agents import design_agent
    from config.settings import settings

    corpus = tmp_path / "designs.json"
    corpus.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(settings, "aircraftverse_path", str(corpus))
    monkeypatch.setattr(settings, "aircraftverse_chroma_path", str(tmp_path / "chroma"))
    monkeypatch.setattr(pa, "embedder_model_name", lambda s: "all-MiniLM-L6-v2")

    opened = {}

    class FakeChroma:
        def __init__(self, **kwargs):
            opened["dir"] = kwargs["persist_directory"]
            self._collection = SimpleNamespace(count=lambda: 1)

    rag = design_agent.AircraftVerseRAG(
        SimpleNamespace(model_name="all-MiniLM-L6-v2"), mode="persistent"
    )
    monkeypatch.setattr(rag, "_load_designs", lambda: ["one design"])
    rag._open_or_build(rag._corpus_key(), FakeChroma, lambda **kw: kw)

    assert Path(opened["dir"]) == pa.index_dir(settings)


def _asset(group, present, name=None):
    return pa.Asset(group, name or group, present, "", 1.0, MagicMock())


def test_the_plan_keeps_order_and_skips_what_is_present():
    assets = [
        _asset("sim-image", False),
        _asset("index", False),
        _asset("models", True, "have"),
        _asset("models", False, "need"),
    ]
    todo = pa.plan(assets, {"models", "index", "sim-image"})
    assert [a.name for a in todo] == ["need", "index", "sim-image"]
    assert pa.plan(assets, {"models"}) == [assets[3]]


@pytest.fixture
def gathered(monkeypatch):
    assets = [_asset("models", False, "need"), _asset("embedder", False), _asset("index", False)]
    monkeypatch.setattr(pa, "gather", lambda settings, with_simulator=True: assets)
    return assets


def test_status_alone_fetches_nothing(gathered):
    assert pa.main([]) == 0
    assert not any(a.fetch.called for a in gathered)


def test_nothing_is_downloaded_without_consent(gathered, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert pa.main(["--models"]) == 2
    assert not any(a.fetch.called for a in gathered)


def test_the_index_brings_its_embedder(gathered):
    assert pa.main(["--index", "--yes"]) == 0
    models, embedder, index = gathered
    assert not models.fetch.called
    assert embedder.fetch.called and index.fetch.called


def test_requested_image_without_docker_fails(monkeypatch, capsys):
    asset = pa.Asset("sim-image", "image eljazari-sim", False, "docker not found")
    monkeypatch.setattr(pa, "gather", lambda settings, with_simulator=True: [asset])
    assert pa.main(["--sim-image", "--yes"]) == 1
    assert "docker not found" in capsys.readouterr().err
    assert pa.main([]) == 0


def test_existing_requested_image_needs_no_fetch(monkeypatch):
    asset = pa.Asset("sim-image", "image eljazari-sim", True, "present")
    monkeypatch.setattr(pa, "gather", lambda settings, with_simulator=True: [asset])
    assert pa.main(["--sim-image", "--yes"]) == 0


def test_failed_download_returns_failure(gathered):
    gathered[0].fetch.side_effect = RuntimeError("download failed")
    assert pa.main(["--models", "--yes"]) == 1
