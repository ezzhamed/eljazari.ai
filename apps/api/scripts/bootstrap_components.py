"""Import optional manufacturer data locally without redistributing it.

Run from apps/api: python -m scripts.bootstrap_components
The public catalog contains no manufacturer test extracts. This command copies
its baseline records to .local/components, downloads the source pages there,
and derives a local catalog. Review the publisher's terms for your use.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from config.paths import PROJECT_ROOT
from scripts import extract_propulsion_tests as extractor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / ".local/components")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch even if already imported")
    args = parser.parse_args(argv)
    target = args.output_dir.resolve()
    public = PROJECT_ROOT / "data/components"
    if target == public.resolve() or public.resolve() in target.parents:
        parser.error("Use a private output directory outside data/components")
    tests = target / "propulsion_tests.json"
    required = [
        target / f"{name}.json"
        for name in ("motors", "propellers", "batteries", "escs", "frames", "flight_controllers")
    ]
    required += [
        target / "evidence" / f"{name}.json"
        for name in (*extractor.PAGES, extractor.PROPELLER_EXTRACT)
    ]
    if tests.is_file() and all(path.is_file() for path in required) and not args.refresh:
        records = json.loads(tests.read_text(encoding="utf-8")).get("tests", [])
        motors = json.loads((target / "motors.json").read_text(encoding="utf-8"))
        props = json.loads((target / "propellers.json").read_text(encoding="utf-8"))
        motor_ids = {m["part_number"] for m in motors if m.get("thrust_curve_evidence")}
        prop_ids = {p["part_number"] for p in props}
        if records and all(
            test["motor_part_number"] in motor_ids and test["propeller_part_number"] in prop_ids
            for test in records
        ):
            print(f"Manufacturer data already imported at {target}")
            return 0
    target.mkdir(parents=True, exist_ok=True)
    for name in ("motors", "propellers", "batteries", "escs", "frames", "flight_controllers"):
        if not (target / f"{name}.json").exists():
            shutil.copyfile(public / f"{name}.json", target / f"{name}.json")
    extractor.COMPONENTS = target
    extractor.EVIDENCE_DIR = target / "evidence"
    extractor.TESTS_PATH = tests
    result = extractor.main([])
    if result:
        return result
    extractor.write_catalog()
    print(f"Imported manufacturer data to {target}. Keep this directory private.")
    print("For a custom directory, set COMPONENTS_CATALOG_PATH to this absolute path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
