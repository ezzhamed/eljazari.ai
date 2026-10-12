"""Archive each benchmark flight's raw SITL telemetry next to its run records.

Every run that reached the simulator keeps its runner payload in a temporary
artifacts directory (``simulation.artifacts.telemetry_payload`` in runs.jsonl).
This copies each one, gzip-compressed with a fixed timestamp so the archive is
reproducible, into ``<results>/telemetry/<case>_<repeat>.json.gz`` and writes
``manifest.json`` with the SHA-256 and size of every original.

    python scripts/export_benchmark_telemetry.py ../../benchmarks/results/2026-09-29-v3
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path


def export(results: Path) -> dict[str, dict]:
    out = results / "telemetry"
    out.mkdir(exist_ok=True)
    manifest: dict[str, dict] = {}
    missing: list[str] = []
    for line in (results / "runs.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        source = ((record.get("simulation") or {}).get("artifacts") or {}).get("telemetry_payload")
        if not source:
            continue
        name = f"{record['case_id']}_{record['repeat']}.json.gz"
        path = Path(source)
        if not path.is_file():
            missing.append(f"{name}: {source}")
            continue
        raw = path.read_bytes()
        with gzip.GzipFile(out / name, "wb", mtime=0) as handle:
            handle.write(raw)
        manifest[name] = {
            "case_id": record["case_id"],
            "repeat": record["repeat"],
            "project_id": record["project_id"],
            "outcome": record["outcome"],
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_bytes": len(raw),
        }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for item in missing:
        print(f"missing telemetry payload: {item}", file=sys.stderr)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", help="A benchmarks/results/<run> directory")
    manifest = export(Path(parser.parse_args().results))
    print(f"{len(manifest)} flights archived")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
