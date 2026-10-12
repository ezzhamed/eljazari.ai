from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from config.settings import settings

# Small curated samples legitimately reuse components; the diversity check only
# applies once a file is large enough that uniformity means a packaging failure.
MIN_DESIGNS_FOR_DIVERSITY_CHECK = 50


def _validate_dataset(path: Path) -> tuple[bool, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, "file not found"
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"

    designs = raw if isinstance(raw, list) else raw.get("designs", [])
    if not isinstance(designs, list) or not designs:
        return False, "dataset must contain a non-empty list of designs"

    sample = designs[0]
    required_keys = {"topology", "frame", "battery", "payload"}
    missing = sorted(required_keys.difference(sample))
    if missing:
        return False, f"sample design is missing required keys: {', '.join(missing)}"

    # Existence and shape are not enough. A packaging bug once produced a file
    # with 27,714 entries that were byte-identical apart from their performance
    # block — it passed every structural check while making retrieval useless.
    # Distinct component choices are the cheapest honest signal of a real corpus.
    distinct_motors = {
        motor.get("motor")
        for design in designs
        for motor in (design.get("propulsion") or [])
        if isinstance(motor, dict)
    }
    distinct_batteries = {
        design.get("battery", {}).get("part")
        for design in designs
        if isinstance(design.get("battery"), dict)
    }
    distinct_batteries.discard(None)

    if len(designs) >= MIN_DESIGNS_FOR_DIVERSITY_CHECK and len(distinct_motors) < 2:
        return False, (
            f"{len(designs)} designs but only {len(distinct_motors)} distinct motor(s) — "
            "the corpus is degenerate. Re-run scripts.download_aircraftverse to repackage it."
        )

    detail = (
        f"{len(designs)} designs, {len(distinct_motors)} distinct motors, "
        f"{len(distinct_batteries)} distinct batteries"
    )
    return True, detail


def bootstrap_dataset(target: Path, source: Path | None, allow_backup: bool) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        ok, detail = _validate_dataset(target)
        if ok:
            print(f"[aircraftverse] dataset ready at {target} ({detail})")
            return 0
        print(f"[aircraftverse] existing dataset is invalid: {detail}", file=sys.stderr)
        return 1

    candidates: list[Path] = []
    if source is not None:
        candidates.append(source)
    if allow_backup:
        candidates.append(target.with_suffix(target.suffix + ".bak"))

    for candidate in candidates:
        if not candidate.exists():
            continue
        shutil.copy2(candidate, target)
        ok, detail = _validate_dataset(target)
        if ok:
            print(f"[aircraftverse] restored dataset from {candidate} -> {target} ({detail})")
            return 0
        target.unlink(missing_ok=True)
        print(
            f"[aircraftverse] candidate {candidate} is invalid: {detail}",
            file=sys.stderr,
        )
        return 1

    print(
        f"[aircraftverse] dataset missing. Provide --source or place designs.json at {target}",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or bootstrap AircraftVerse dataset")
    parser.add_argument("--target", default=str(settings.resolved_aircraftverse_path))
    parser.add_argument("--source", help="Optional source JSON file to copy from")
    parser.add_argument(
        "--from-backup",
        action="store_true",
        help="Allow restoring from designs.json.bak next to the target",
    )
    args = parser.parse_args()

    target = Path(args.target).expanduser().resolve()
    source = Path(args.source).expanduser().resolve() if args.source else None
    return bootstrap_dataset(target=target, source=source, allow_backup=args.from_backup)


if __name__ == "__main__":
    raise SystemExit(main())
