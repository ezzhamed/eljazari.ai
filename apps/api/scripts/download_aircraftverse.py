"""
AircraftVerse Dataset Downloader & Packager (download_aircraftverse.py)

Downloads the AircraftVerse dataset (NeurIPS 2023, CC-BY-4.0,
doi:10.5281/zenodo.6525446) and packages it into the `designs.json` format used
by the eljazari.ai design RAG.

Key features:
- Downloads Zenodo zip parts plus the shared `corpus_dic` component catalogue.
- Reads design files directly from the zip in memory — no full extraction.
- Resolves every motor / propeller / battery against the corpus so records carry
  real KV, capacity, and geometry rather than placeholders.
- Refuses to write a degenerate corpus: if the packaged designs collapse to a
  handful of distinct records the run fails loudly instead of shipping a file
  that looks populated but has no retrieval value.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("aircraftverse_downloader")

ZENODO_BASE = "https://zenodo.org/records/6525446/files"
ZENODO_URLS = {
    1: f"{ZENODO_BASE}/AircraftVerse_1.zip?download=1",
    2: f"{ZENODO_BASE}/AircraftVerse_2.zip?download=1",
    3: f"{ZENODO_BASE}/AircraftVerse_3.zip?download=1",
}
CORPUS_URL = f"{ZENODO_BASE}/corpus_dic?download=1"

# A healthy packaged corpus has thousands of distinct designs. The previous
# parser silently produced 27,714 copies of a single record; this floor makes
# that failure mode impossible to ship again.
MIN_DISTINCT_RATIO = 0.5

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.append(str(PROJECT_ROOT / "apps" / "api"))

from scripts.aircraftverse_parser import load_corpus, parse_design  # noqa: E402

try:
    from config.settings import settings

    TARGET_PATH = settings.resolved_aircraftverse_path
except ImportError:
    TARGET_PATH = PROJECT_ROOT / "data" / "aircraftverse" / "designs.json"


def download_file(url: str, dest: Path) -> None:
    """Download a file with a command-line progress bar."""
    logger.info(f"Downloading {url} -> {dest}")

    def progress_hook(count, block_size, total_size):
        if total_size <= 0:
            return
        progress = count * block_size
        percent = min(100, int(progress * 100 / total_size))
        sys.stdout.write(
            f"\rDownload progress: {percent}% "
            f"({progress / 1048576:.1f}/{total_size / 1048576:.1f} MB)"
        )
        sys.stdout.flush()

    urllib.request.urlretrieve(url, dest, progress_hook)
    print()
    logger.info("Download completed.")


def _read_json(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as handle:
        return json.loads(handle.read().decode("utf-8"))


def process_zip_file(zip_path: Path, corpus: dict, max_designs: int) -> list[dict]:
    """Package designs directly out of a zip archive."""
    logger.info(f"Opening {zip_path}...")
    designs: list[dict] = []
    skipped = 0

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        low_level_paths = sorted(n for n in names if n.endswith("design_low_level.json"))
        logger.info(f"Found {len(low_level_paths)} designs in the archive.")

        for path in low_level_paths:
            if len(designs) >= max_designs:
                break
            base = path.rsplit("/", 1)[0]
            design_id = base.rsplit("/", 1)[-1]
            tree_path = f"{base}/design_tree.json"
            output_path = f"{base}/output.json"
            try:
                low_level = _read_json(zf, path)
                tree = _read_json(zf, tree_path) if tree_path in names else {}
                output = _read_json(zf, output_path) if output_path in names else None
                record = parse_design(
                    design_id=design_id,
                    tree=tree,
                    low_level=low_level,
                    output=output,
                    corpus=corpus,
                )
                if record is None:
                    skipped += 1
                    continue
                designs.append(record)
                if len(designs) % 2000 == 0:
                    logger.info(f"Packaged {len(designs)} designs so far...")
            except Exception as exc:
                skipped += 1
                logger.debug(f"Skip {path}: {exc}")

    logger.info(f"Archive done — packaged={len(designs)} skipped={skipped}")
    return designs


def summarise(designs: list[dict]) -> dict:
    """Build a diversity/provenance report for the packaged corpus."""
    identity = {
        json.dumps(
            {k: v for k, v in d.items() if k not in {"performance_simulated", "design_id"}},
            sort_keys=True,
        )
        for d in designs
    }
    return {
        "total": len(designs),
        "distinct_designs": len(identity),
        "topologies": Counter(d["topology"] for d in designs),
        "airframe_classes": Counter(d["airframe_class"] for d in designs),
        "evaluated": sum(1 for d in designs if d.get("evaluated")),
        "distinct_motors": len({m["motor"] for d in designs for m in d["propulsion"]}),
        "distinct_props": len({m["propeller"] for d in designs for m in d["propulsion"]}),
        "distinct_batteries": len({d["battery"].get("part", "") for d in designs}),
    }


def print_report(report: dict) -> None:
    print()
    print("=" * 62)
    print("PACKAGED CORPUS REPORT")
    print("=" * 62)
    print(f"  total designs      : {report['total']}")
    print(f"  distinct designs   : {report['distinct_designs']}")
    print(f"  evaluated (physics): {report['evaluated']}")
    print(f"  distinct motors    : {report['distinct_motors']}")
    print(f"  distinct propellers: {report['distinct_props']}")
    print(f"  distinct batteries : {report['distinct_batteries']}")
    print("  airframe classes   :")
    for key, count in report["airframe_classes"].most_common():
        print(f"      {key:<24} {count}")
    print("  topologies (top 10):")
    for key, count in report["topologies"].most_common(10):
        print(f"      {key:<24} {count}")
    print("=" * 62)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and package the AircraftVerse dataset")
    parser.add_argument("--zip-file", help="Path to a pre-downloaded AircraftVerse_X.zip")
    parser.add_argument(
        "--zip-dir",
        help="Directory holding pre-downloaded AircraftVerse_*.zip files",
    )
    parser.add_argument(
        "--corpus-file",
        help="Path to a pre-downloaded corpus_dic (downloaded automatically otherwise)",
    )
    parser.add_argument(
        "--max-designs",
        type=int,
        default=30000,
        help="Maximum designs to package (default: 30000 — the full corpus)",
    )
    parser.add_argument(
        "--cleanup", action="store_true", help="Delete downloaded zips after processing"
    )
    parser.add_argument(
        "--parts",
        default="1,2,3",
        help="Comma-separated Zenodo parts to use (default: 1,2,3 — the full corpus)",
    )
    args = parser.parse_args()

    TARGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(args.zip_dir) if args.zip_dir else TARGET_PATH.parent / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # The component catalogue is shared by every part and is required to resolve
    # motor / battery / propeller specifications.
    corpus_path = Path(args.corpus_file) if args.corpus_file else temp_dir / "corpus_dic"
    if not corpus_path.exists():
        download_file(CORPUS_URL, corpus_path)
    corpus = load_corpus(corpus_path.read_bytes())
    logger.info(
        "Component corpus loaded — %s",
        ", ".join(f"{k}={len(v)}" for k, v in corpus.items()),
    )

    all_designs: list[dict] = []

    if args.zip_file:
        zips = [Path(args.zip_file).expanduser().resolve()]
    else:
        zips = []
        for part in [int(p.strip()) for p in args.parts.split(",")]:
            if part not in ZENODO_URLS:
                logger.error(f"Invalid part {part} — must be 1, 2, or 3")
                continue
            zip_path = temp_dir / f"AircraftVerse_{part}.zip"
            if not zip_path.exists():
                download_file(ZENODO_URLS[part], zip_path)
            else:
                logger.info(f"Using existing {zip_path}")
            zips.append(zip_path)

    for zip_path in zips:
        if not zip_path.exists():
            logger.error(f"Zip not found: {zip_path}")
            continue
        remaining = args.max_designs - len(all_designs)
        if remaining <= 0:
            break
        all_designs.extend(process_zip_file(zip_path, corpus, remaining))
        if args.cleanup:
            logger.info(f"Removing {zip_path}")
            zip_path.unlink(missing_ok=True)

    if not all_designs:
        logger.error("No designs were packaged. Aborting.")
        return 1

    report = summarise(all_designs)
    print_report(report)

    # Guard against the degenerate-corpus failure mode.
    ratio = report["distinct_designs"] / report["total"]
    if ratio < MIN_DISTINCT_RATIO:
        logger.error(
            "Refusing to write: only %d distinct designs out of %d (%.1f%%). "
            "This means the parser is not matching the dataset schema.",
            report["distinct_designs"],
            report["total"],
            ratio * 100,
        )
        return 1

    if TARGET_PATH.exists():
        backup_path = TARGET_PATH.with_suffix(TARGET_PATH.suffix + ".bak")
        try:
            TARGET_PATH.replace(backup_path)
            logger.info(f"Previous dataset backed up to {backup_path}")
        except Exception as exc:
            logger.warning(f"Could not back up existing dataset: {exc}")

    with open(TARGET_PATH, "w", encoding="utf-8") as handle:
        json.dump(all_designs, handle, ensure_ascii=False)
    logger.info(f"Wrote {len(all_designs)} designs to {TARGET_PATH}")

    report_path = TARGET_PATH.parent / "corpus_report.json"
    report_path.write_text(
        json.dumps(
            {
                "source": "AircraftVerse (doi:10.5281/zenodo.6525446), CC-BY-4.0",
                "total": report["total"],
                "distinct_designs": report["distinct_designs"],
                "evaluated": report["evaluated"],
                "distinct_motors": report["distinct_motors"],
                "distinct_propellers": report["distinct_props"],
                "distinct_batteries": report["distinct_batteries"],
                "airframe_classes": dict(report["airframe_classes"]),
                "topologies": dict(report["topologies"]),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(f"Corpus report written to {report_path}")

    # Force a ChromaDB rebuild so the new corpus is actually indexed.
    try:
        chroma_dir = TARGET_PATH.parent / "chroma_db"
        if chroma_dir.exists():
            import shutil

            shutil.rmtree(chroma_dir)
            logger.info("Cleared stale ChromaDB index.")
    except Exception as exc:
        logger.warning(f"Could not clear ChromaDB index: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
