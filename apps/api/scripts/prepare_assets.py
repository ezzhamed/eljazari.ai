"""Check, and fetch on request, what a run needs beyond the repository.

    python -m scripts.prepare_assets                      # what is here, what is missing, sizes
    python -m scripts.prepare_assets --all                # fetch models, embedder and index
    python -m scripts.prepare_assets --all --sim-image    # and build the simulation image

Nothing is downloaded without an action flag, and the download is confirmed
first (or pass --yes). PX4 and Gazebo in WSL or Linux need sudo and are
installed by hand: docs/SETUP.md#px4-and-gazebo. Run from apps/api.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from config.paths import PROJECT_ROOT as REPO_ROOT

# Measured on the development machine, 2026-09-30.
MODEL_GB = {
    "gemma3:4b": 3.3,
    "llama3.2:latest": 2.0,
    "llama3.2:3b": 2.0,
    "qwen2.5-coder:7b": 4.7,
    "qwen2.5:7b": 4.7,
    "nomic-embed-text:latest": 0.3,
}
EMBEDDER = "all-MiniLM-L6-v2"
EMBEDDER_REPO = f"sentence-transformers/{EMBEDDER}"
EMBEDDER_GB = 0.09
INDEX_GB = 0.02
SIM_IMAGE_GB = 7.4


@dataclass
class Asset:
    group: str  # models | embedder | index | sim-image | corpus | simulator
    name: str
    present: bool
    detail: str
    size_gb: float | None = None
    fetch: Callable[[], None] | None = field(default=None, repr=False)


def _tagged(model: str) -> str:
    return model if ":" in model else f"{model}:latest"


def wanted_models(settings) -> list[str]:
    """The models the pipeline calls, including the fallback embedder, once each."""
    names = [
        settings.ollama_design_model,
        settings.ollama_default_model,
        settings.ollama_code_model,
        settings.ollama_eval_model,
        settings.ollama_embed_model,
    ]
    return list(dict.fromkeys(_tagged(n) for n in names if n))


def _installed_models(base_url: str) -> set[str] | None:
    from scripts.bootstrap_ollama import _request_json

    try:
        tags = _request_json(base_url, "/api/tags", timeout=5)
    except Exception:  # noqa: BLE001 - unreachable is a status, not an error here
        return None
    return {_tagged(m.get("name", "")) for m in tags.get("models", [])}


def model_assets(settings) -> list[Asset]:
    base = settings.ollama_base_url
    installed = _installed_models(base)

    def pull(model: str) -> Callable[[], None]:
        def run() -> None:
            from scripts.bootstrap_ollama import ensure_models, wait_for_ollama

            wait_for_ollama(base, 120)
            if ensure_models(base, [model], pull_missing=True) != 0:
                raise RuntimeError(f"could not pull {model}")

        return run

    assets = []
    for model in wanted_models(settings):
        if installed is None:
            detail = f"Ollama not reachable at {base}; start `ollama serve`"
            present = False
        else:
            present = model in installed
            detail = "present" if present else "missing"
        assets.append(
            Asset("models", f"model {model}", present, detail, MODEL_GB.get(model), pull(model))
        )
    return assets


def embedder_model_name(settings) -> str:
    """The embedder the design agent will use: sentence-transformers, else Ollama."""
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return settings.ollama_embed_model
    return EMBEDDER


def embedder_asset(settings) -> Asset:
    if embedder_model_name(settings) != EMBEDDER:
        return Asset(
            "embedder",
            f"embedder {EMBEDDER}",
            True,
            f"sentence-transformers not installed; retrieval uses {settings.ollama_embed_model}",
        )
    try:
        from huggingface_hub import try_to_load_from_cache

        cached = isinstance(try_to_load_from_cache(EMBEDDER_REPO, "config.json"), str)
    except ImportError:
        cached = False

    def fetch() -> None:
        from sentence_transformers import SentenceTransformer

        SentenceTransformer(EMBEDDER_REPO)

    detail = "in the Hugging Face cache" if cached else "downloads on first use"
    return Asset("embedder", f"embedder {EMBEDDER}", cached, detail, EMBEDDER_GB, fetch)


def index_dir(settings) -> Path:
    """Where the API's persistent index for this corpus and embedder lives.

    Derived by the design agent's own key, so a directory built here is the one
    the API reuses; only the embedder's name is needed, not the model itself.
    """
    from agents.design_agent import AircraftVerseRAG, get_aircraftverse_path

    stub = SimpleNamespace(model_name=embedder_model_name(settings))
    key = AircraftVerseRAG(stub, mode="persistent")._corpus_key()  # type: ignore[arg-type]
    corpus = hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:16]
    base = (
        settings.resolved_aircraftverse_chroma_path
        or get_aircraftverse_path().parent / "vector_index"
    )
    return base / corpus


def index_asset(settings) -> Asset:
    directory = index_dir(settings)
    present = (directory / "vectors.sqlite3").exists()
    note = (
        ""
        if settings.aircraftverse_rag_mode == "persistent"
        else "; the API reuses it only with AIRCRAFTVERSE_RAG_MODE=persistent"
    )

    def build() -> None:
        from agents.design_agent import AircraftVerseRAG, DesignAgent

        rag = AircraftVerseRAG(DesignAgent._build_embeddings(), mode="persistent")
        asyncio.run(rag.initialize())
        if rag._vectorstore is None:
            raise RuntimeError("the index was not built; see the log above")

    detail = (f"built at {directory}" if present else "built on the first design (~80 s)") + note
    return Asset("index", "retrieval index", present, detail, INDEX_GB, build)


def corpus_asset(settings) -> Asset:
    from scripts.bootstrap_aircraftverse import _validate_dataset

    path = settings.resolved_aircraftverse_path
    if not path.exists():
        return Asset(
            "corpus",
            "aircraftverse",
            False,
            f"missing: {path}. Rebuild: python -m scripts.download_aircraftverse "
            "--parts 1,2,3 --cleanup (12.3 GB download)",
        )
    ok, detail = _validate_dataset(path)
    return Asset("corpus", "aircraftverse", ok, f"in the repository: {detail}")


def _docker(*args: str, timeout: float = 30) -> subprocess.CompletedProcess | None:
    if not shutil.which("docker"):
        return None
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def sim_image_asset(settings) -> Asset:
    image = settings.sitl_docker_image
    probe = _docker("image", "inspect", image)
    if probe is None:
        return Asset("sim-image", f"image {image}", False, "docker not found", SIM_IMAGE_GB)
    if probe.returncode != 0 and "daemon" in (probe.stderr or "").lower():
        return Asset("sim-image", f"image {image}", False, "Docker is not running", SIM_IMAGE_GB)

    def build() -> None:
        command = ["docker", "build", "-f", "infrastructure/Dockerfile.sim", "-t", image, "."]
        print("[prepare] " + " ".join(command))
        if subprocess.run(command, cwd=REPO_ROOT, check=False).returncode != 0:
            raise RuntimeError("docker build failed")

    present = probe.returncode == 0
    return Asset(
        "sim-image",
        f"image {image}",
        present,
        "present" if present else "not built (only needed for the Docker runtime)",
        SIM_IMAGE_GB,
        build,
    )


def simulator_asset() -> Asset:
    from scripts.doctor import check_simulator

    results = check_simulator()
    failed = [f"{check}: {detail}" for status, check, detail in results if status == "FAIL"]
    if failed:
        detail = "; ".join(failed)[:160] + " (install by hand: docs/SETUP.md#px4-and-gazebo)"
    else:
        detail = "; ".join(f"{check} {detail}" for _, check, detail in results)[:160]
    return Asset("simulator", "px4 + gazebo", not failed, detail)


def gather(settings, with_simulator: bool = True) -> list[Asset]:
    assets = [corpus_asset(settings), *model_assets(settings), embedder_asset(settings)]
    assets += [index_asset(settings), sim_image_asset(settings)]
    if with_simulator:
        assets.append(simulator_asset())
    return assets


def plan(assets: list[Asset], groups: set[str]) -> list[Asset]:
    """Missing, fetchable assets in the requested groups, in fetch order."""
    order = ["models", "embedder", "index", "sim-image"]
    todo = [a for a in assets if a.group in groups and not a.present and a.fetch]
    return sorted(todo, key=lambda a: order.index(a.group))


def _print(assets: list[Asset]) -> None:
    for a in assets:
        size = f"{a.size_gb:.1f} GB" if a.size_gb is not None else ""
        print(f"[{'OK' if a.present else 'MISS':4}] {a.name:32} {size:>7}  {a.detail}")


def _confirm(todo: list[Asset], assume_yes: bool) -> bool:
    total = sum(a.size_gb or 0 for a in todo)
    print(f"\nTo fetch: {', '.join(a.name for a in todo)} (about {total:.1f} GB)")
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Not a terminal: pass --yes to download.", file=sys.stderr)
        return False
    return input("Continue? [y/N] ").strip().lower() in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", action="store_true", help="pull the Ollama models")
    parser.add_argument("--embedder", action="store_true", help=f"download {EMBEDDER}")
    parser.add_argument("--index", action="store_true", help="build the persistent index")
    parser.add_argument(
        "--sim-image", action="store_true", help=f"build the simulation image ({SIM_IMAGE_GB} GB)"
    )
    parser.add_argument("--all", action="store_true", help="models, embedder and index")
    parser.add_argument("--yes", action="store_true", help="do not ask before downloading")
    parser.add_argument("--no-simulator", action="store_true", help="skip the PX4/Gazebo check")
    args = parser.parse_args(argv)

    from config.settings import settings

    groups = {
        g
        for g, on in {
            "models": args.models or args.all,
            "embedder": args.embedder or args.all,
            "index": args.index or args.all,
            "sim-image": args.sim_image,
        }.items()
        if on
    }
    if "index" in groups:
        groups.add("embedder")  # the index is built with the embedder

    assets = gather(settings, with_simulator=not args.no_simulator)
    _print(assets)
    todo = plan(assets, groups)
    if not groups:
        if any(a.fetch and not a.present for a in assets):
            print("\nFetch what is missing: python -m scripts.prepare_assets --all [--sim-image]")
        return 0
    unavailable = [a for a in assets if a.group in groups and not a.present and not a.fetch]
    if unavailable:
        for asset in unavailable:
            print(f"Cannot prepare {asset.name}: {asset.detail}", file=sys.stderr)
        return 1
    if not todo:
        print("\nNothing to fetch.")
        return 0
    if not _confirm(todo, args.yes):
        return 2

    failed = []
    for asset in todo:
        print(f"\n[prepare] {asset.name} ...")
        try:
            asset.fetch()  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - report every failure, keep going
            print(f"[prepare] {asset.name} failed: {exc}", file=sys.stderr)
            failed.append(asset.name)

    print()
    _print(gather(settings, with_simulator=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
