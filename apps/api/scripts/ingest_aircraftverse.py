"""Convert a real AircraftVerse export into eljazari.ai's ``designs.json``.

The repo ships a repackaged AircraftVerse corpus under its separate license.
Additional native exports are not necessarily in the application schema.
This script bridges that gap: it normalises a downloaded AircraftVerse export (or any UAV dataset you
flatten to JSON/JSONL) into the schema the Design-Agent RAG loader reads.

Input  (``--source``): a ``.json`` (list of design dicts) or ``.jsonl`` file.
       AircraftVerse's native distribution is a set of per-design pickles; use
       its own loaders to dump a list of design dicts to JSON first, then run
       this. Field names are matched leniently (see ``_FIELD_ALIASES``).
Output (``--target``): ``designs.json`` — a JSON list in the eljazari.ai schema:

    {
      "topology":   "quadcopter|hexacopter|octocopter|tricopter|...",
      "use_case":   "free text",
      "frame":      {"type": "X", "arm_length_mm": 250},
      "battery":    {"cells": 4, "capacity_mah": 5000, "weight_g": 480},
      "propulsion": [{"motor": "...", "propeller": "..."}, ...],
      "electronics":{"flight_controller": "PX4", "gps": true},
      "payload":    {"max_weight_g": 500}
    }

Only ``topology`` + ``frame`` are required for a usable record; everything else
is best-effort. Records that cannot be mapped are skipped (counted in the
summary) rather than aborting the whole run.

Usage:
    python -m scripts.ingest_aircraftverse --source raw.json --target \
        ../../data/aircraftverse/designs.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

# Lenient field-name matching: maps many common dataset column names onto the
# canonical keys this project uses. Extend this when adapting a new dataset.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "rotor_count": (
        "rotor_count",
        "num_rotors",
        "n_rotors",
        "n_motors",
        "num_motors",
        "motor_count",
        "rotors",
    ),
    "arm_length_mm": ("arm_length_mm", "arm_length", "arm_mm", "arm_len_mm"),
    "frame_type": ("frame_type", "frame", "geometry", "config"),
    "battery_cells": ("battery_cells", "cells", "n_cells", "lipo_cells", "s_rating"),
    "battery_capacity_mah": ("battery_capacity_mah", "capacity_mah", "capacity", "battery_mah"),
    "battery_weight_g": ("battery_weight_g", "battery_mass_g", "battery_weight"),
    "use_case": ("use_case", "mission", "application", "category", "description"),
    "payload_g": ("payload_g", "payload_mass_g", "max_payload_g", "payload"),
    "flight_controller": ("flight_controller", "fc", "autopilot"),
}

_TOPOLOGY_BY_ROTORS = {3: "tricopter", 4: "quadcopter", 6: "hexacopter", 8: "octocopter"}


def _first(record: dict, canonical: str) -> Any:
    for key in _FIELD_ALIASES.get(canonical, (canonical,)):
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    f = _as_float(value)
    return int(round(f)) if f is not None else None


def normalise_record(record: dict) -> dict | None:
    """Map one raw design dict onto the eljazari.ai schema (or None to skip)."""
    if not isinstance(record, dict):
        return None

    # Topology: explicit field, else inferred from rotor count.
    topology = record.get("topology")
    rotors = _as_int(_first(record, "rotor_count"))
    if not topology and rotors is not None:
        topology = _TOPOLOGY_BY_ROTORS.get(rotors)
    if not topology:
        return None  # cannot place a design without knowing what it is

    if rotors is None:
        rotors = {"tricopter": 3, "quadcopter": 4, "hexacopter": 6, "octocopter": 8}.get(
            topology, 4
        )

    frame_type = _first(record, "frame_type")
    if isinstance(frame_type, dict):  # a nested frame object — pass through
        frame = {
            "type": str(frame_type.get("type", "X")),
            "arm_length_mm": _as_float(frame_type.get("arm_length_mm")) or 250.0,
        }
    else:
        frame = {
            "type": str(frame_type or "X"),
            "arm_length_mm": _as_float(_first(record, "arm_length_mm")) or 250.0,
        }

    out: dict[str, Any] = {"topology": topology, "frame": frame}

    use_case = _first(record, "use_case")
    if use_case:
        out["use_case"] = str(use_case)[:200]

    cells = _as_int(_first(record, "battery_cells"))
    cap = _as_int(_first(record, "battery_capacity_mah"))
    if cells or cap:
        out["battery"] = {
            "cells": cells or 4,
            "capacity_mah": cap or 5000,
            "weight_g": _as_float(_first(record, "battery_weight_g")) or 0.0,
        }

    out["propulsion"] = [{"motor": "unknown", "propeller": "unknown"}] * rotors

    payload = _as_float(_first(record, "payload_g"))
    if payload is not None:
        out["payload"] = {"max_weight_g": payload}

    fc = _first(record, "flight_controller")
    if fc:
        out["electronics"] = {"flight_controller": str(fc), "gps": True}

    return out


def _iter_raw(source: Path) -> Iterable[dict]:
    if source.suffix == ".jsonl":
        for line in source.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                yield json.loads(line)
        return
    raw = json.loads(source.read_text(encoding="utf-8"))
    yield from (raw if isinstance(raw, list) else raw.get("designs", []))


def convert(source: Path, target: Path, limit: int | None = None) -> tuple[int, int]:
    kept: list[dict] = []
    skipped = 0
    for record in _iter_raw(source):
        norm = normalise_record(record)
        if norm is None:
            skipped += 1
            continue
        kept.append(norm)
        if limit is not None and len(kept) >= limit:
            break
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(kept), skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Raw AircraftVerse export (.json list or .jsonl).",
    )
    parser.add_argument(
        "--target",
        required=True,
        type=Path,
        help="Destination designs.json in the eljazari.ai schema.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Optional cap on the number of designs written."
    )
    args = parser.parse_args()

    if not args.source.exists():
        print(f"error: source not found: {args.source}", file=sys.stderr)
        return 2

    kept, skipped = convert(args.source, args.target, args.limit)
    print(f"wrote {kept} designs -> {args.target}  (skipped {skipped} unmappable)")
    if kept == 0:
        print(
            "error: no designs were mappable — check the field names against _FIELD_ALIASES.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
