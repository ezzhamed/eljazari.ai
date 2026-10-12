#!/usr/bin/env python3
"""Compare the simulated aircraft with real measurements (docs/CALIBRATION.md).

Bench — a thrust-stand CSV of one motor, propeller and voltage:

    python scripts/calibrate.py bench --csv run.csv --motor tmotor_mn3110_700kv \\
        --propeller tmotor_p12_4 --nominal-voltage 14.8 --date 2026-10-01 \\
        --reference "RCbenchmark 1585, lab A, run 3" --out bench_test.json

prints the measured curve against the catalog's for that combination and writes
a ``bench_test`` propulsion test (keyed by the log's SHA-256) for review before it
is added to data/components/propulsion_tests.json.

Flight — a real PX4 log (ULog) of the generated aircraft hovering:

    python scripts/calibrate.py flight --ulog flight.ulg \\
        --manifest <storage>/<spec sha>/flight/flight_manifest.json

prints PX4's measured hover thrust against the flight model's prediction.
Without a real log there is no result: nothing is assumed or defaulted.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))


def bench(args: argparse.Namespace) -> int:
    from pipelines.calibration import (
        bench_test_case,
        compare_bench_to_curve,
        read_thrust_stand_csv,
        steady_points,
    )
    from services.component_catalog import get_catalog

    log = read_thrust_stand_csv(args.csv)
    points = steady_points(log, esc_min_us=args.esc_min, esc_max_us=args.esc_max)
    case = bench_test_case(
        log,
        points,
        motor_part_number=args.motor,
        propeller_part_number=args.propeller,
        nominal_voltage_v=args.nominal_voltage,
        measured_on=date.fromisoformat(args.date),
        reference=args.reference,
    )
    catalog = get_catalog()
    catalog.motor(args.motor)
    catalog.propeller(args.propeller)
    published = catalog.propulsion_test(args.motor, args.propeller, args.nominal_voltage)
    report = {
        "bench_log": {"path": log.path, "sha256": log.sha256, "columns": log.columns},
        "steady_steps": len(points),
        "propulsion_test": case,
        "compared_with": published.source_reference if published else None,
        "comparison": compare_bench_to_curve(points, [p.model_dump() for p in published.points])
        if published
        else None,
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    if published is None:
        print(
            "No published test of this combination to compare with; the bench test "
            "itself can become the sourced curve after review.",
            file=sys.stderr,
        )
    return 0


def flight(args: argparse.Namespace) -> int:
    from pipelines.calibration import compare_hover, hover_from_ulog

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model = json.loads((manifest_path.parent / "flight_model.json").read_text(encoding="utf-8"))
    evidence = hover_from_ulog(args.ulog, motor_count=int(manifest["motor_count"]))
    report = {
        "flight_bundle": {
            "model_name": manifest["model_name"],
            "bundle_sha256": manifest["bundle_sha256"],
            "resolved_spec_sha256": manifest["resolved_spec_sha256"],
            "propulsion_source": manifest.get("propulsion_source"),
        },
        "hover": compare_hover(evidence, float(model["hover_command"]), tolerance=args.tolerance),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["hover"]["within_tolerance"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("bench", help="Thrust-stand CSV against the catalog curve")
    b.add_argument("--csv", required=True)
    b.add_argument("--motor", required=True)
    b.add_argument("--propeller", required=True)
    b.add_argument("--nominal-voltage", type=float, required=True)
    b.add_argument("--date", required=True, help="Measurement date, YYYY-MM-DD")
    b.add_argument("--reference", required=True, help="Stand, site and run identifier")
    b.add_argument("--esc-min", type=float, default=1000.0)
    b.add_argument("--esc-max", type=float, default=2000.0)
    b.add_argument("--out")
    b.set_defaults(handler=bench)
    f = sub.add_parser("flight", help="Real PX4 hover log against the flight model")
    f.add_argument("--ulog", required=True)
    f.add_argument("--manifest", required=True)
    f.add_argument("--tolerance", type=float, default=0.05)
    f.set_defaults(handler=flight)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
