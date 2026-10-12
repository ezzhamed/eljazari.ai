#!/usr/bin/env python3
"""Extract manufacturer propulsion test tables into sourced catalog test cases.

T-MOTOR publishes, on each Navigator (MN) motor's store page, a «Test Data» table
of throttle, current, power, thrust and RPM for several propellers and voltages.
This script fetches those pages and writes, without editing any value:

* ``local catalog evidence/<page>.json`` — every test row and specification row
  of one page, cell text verbatim (merged cells repeated on each row they span);
* ``data/components/propulsion_tests.json`` — one test case per (catalog motor,
  catalog propeller, voltage), each declaring the SHA-256 of its extract.

Only combinations whose motor and propeller exist in the catalog become test
cases; the rest stay in the extracts. A table that is not strictly increasing
in throttle and thrust is listed under ``excluded`` with the reason, not fixed.

    python scripts/extract_propulsion_tests.py                  # fetch and write
    python scripts/extract_propulsion_tests.py --check          # re-fetch, compare only
    python scripts/extract_propulsion_tests.py --write-catalog  # from committed extracts

``--write-catalog`` upserts the sourced motors and propellers into motors.json and
propellers.json from the committed extracts, so no catalog value is typed by hand.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
from config.paths import PROJECT_ROOT as REPO_ROOT

COMPONENTS = REPO_ROOT / ".local" / "components"
EVIDENCE_DIR = COMPONENTS / "evidence"
TESTS_PATH = COMPONENTS / "propulsion_tests.json"

PAGES = {
    "tmotor_mn3508": "https://store.tmotor.com/product/mn3508-motor-navigator-type.html",
    "tmotor_mn3110": "https://store.tmotor.com/product/mn3110-motor-navigator-type.html",
    "tmotor_mn4014": "https://store.tmotor.com/product/mn4014-kv400-motor-navigator-type.html",
}
# Specification pages of the T-MOTOR P-series carbon-fibre propellers in the tests.
PROPELLER_PAGES = {
    "tmotor_p12_4": "https://store.tmotor.com/product/polish-carbon-fiber-12x4-prop.html",
    "tmotor_p13_44": "https://store.tmotor.com/product/polish-carbon-fiber-13x4_4-prop.html",
    "tmotor_p14_48": "https://store.tmotor.com/product/polish-carbon-fiber-14x4_8-prop.html",
    "tmotor_p15_5": "https://store.tmotor.com/product/polish-carbon-fiber-15x5-prop.html",
    "tmotor_p16_54": "https://store.tmotor.com/product/polish-carbon-fiber-16x5_4-prop.html",
    "tmotor_p17_58": "https://store.tmotor.com/goods.php?id=383",
}
PROPELLER_EXTRACT = "tmotor_p_series_propellers"
# Test-table «Item No.» → catalog motor part number.
MOTORS = {
    "MN3508 KV380": "tmotor_mn3508_380kv",
    "MN3508 KV580": "tmotor_mn3508_580kv",
    "MN3508 KV700": "tmotor_mn3508_700kv",
    "MN3110 KV470": "tmotor_mn3110_470kv",
    "MN3110 KV700": "tmotor_mn3110_700kv",
    "MN3110 KV780": "tmotor_mn3110_780kv",
    "MN4014 KV330": "tmotor_mn4014_330kv",
    "MN4014 KV400": "tmotor_mn4014_400kv",
}
# Diameter*pitch in the Prop cell (all T-MOTOR carbon-fibre, two-blade) → catalog part.
# Only propellers with a published specification page: the 9*3, 10*3.3 and 11*3.7
# rows stay in the extracts but cannot become catalog test cases without a
# sourced propeller mass and RPM range.
PROPELLERS = {
    "12*4": "tmotor_p12_4",
    "13*4.4": "tmotor_p13_44",
    "14*4.8": "tmotor_p14_48",
    "15*5": "tmotor_p15_5",
    "16*5.4": "tmotor_p16_54",
    "17*5.8": "tmotor_p17_58",
}
NOT_STATED = [
    "ESC model and firmware, and what the throttle percentage commands",
    "whether the voltage was held constant by a supply or sagged under load",
    "air density, temperature and test rig geometry",
    "measurement uncertainty and sample count",
    "reaction torque (not published for these motors)",
    "a publication or revision date",
]
REDISTRIBUTION = (
    "Not granted. These are the publisher's numeric test values, reproduced with "
    "attribution for engineering reference; the page itself is not archived here. Ask "
    "T-MOTOR before redistributing this extract outside the project."
)


class _Tables(HTMLParser):
    """Collect every <table> as rows of (text, rowspan, colspan) cells.

    The store pages leave some tables unterminated, so a new <table> always
    starts a new table rather than nesting inside the previous one.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[tuple[str, int, int]]]] = []
        self._open: list[int] = []
        self._cell: list[str] | None = None
        self._span = (1, 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.tables.append([])
            self._open.append(len(self.tables) - 1)
        elif tag == "tr" and self._open:
            self.tables[self._open[-1]].append([])
        elif tag in ("td", "th") and self._open:
            values = dict(attrs)
            self._cell = []
            self._span = (int(values.get("rowspan") or 1), int(values.get("colspan") or 1))
        elif tag in ("br", "p") and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._open:
            table = self.tables[self._open[-1]]
            if not table:
                table.append([])
            text = " ".join("".join(self._cell).split())
            table[-1].append((text, *self._span))
            self._cell = None
        elif tag == "table" and self._open:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _grid(rows: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """Expand rowspan/colspan so every row carries every column's text."""
    grid: dict[int, dict[int, str]] = {}
    for r, row in enumerate(rows):
        grid.setdefault(r, {})
        c = 0
        for text, rowspan, colspan in row:
            while c in grid[r]:
                c += 1
            for dr in range(rowspan):
                for dc in range(colspan):
                    grid.setdefault(r + dr, {})[c + dc] = text
            c += colspan
    return [[grid[r][c] for c in sorted(grid[r])] for r in sorted(grid)]


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 eljazari-extract"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_page(html: str) -> dict[str, Any]:
    parser = _Tables()
    parser.feed(html)
    test = next(t for t in parser.tables if any("Throttle" in c[0] for c in t[0]))
    grid = _grid(test)
    header, body = grid[0], grid[1:]
    rows = [row for row in body if len(row) >= 9 and row[3].endswith("%")]
    notes = sorted({row[0] for row in body if row and row[0].startswith("Notes")})
    specs = []
    for table in parser.tables:
        cells = [[c[0] for c in row] for row in table]
        if not cells or not cells[0] or cells[0][0] != "Test Item":
            continue
        pairs: dict[str, str] = {}
        for row in cells:
            for key, value in zip(row[0::2], row[1::2], strict=False):
                if key:
                    pairs[key] = value
        specs.append(pairs)
    return {
        "test_data_columns": header,
        "test_data_rows": rows,
        "specifications": specs,
        "publisher_notes": notes,
    }


def parse_propeller_page(html: str) -> dict[str, str]:
    """Key/value pairs of a propeller page's specification table, verbatim."""
    parser = _Tables()
    parser.feed(html)
    table = next(t for t in parser.tables if t and t[0] and t[0][0][0] == "Model No.")
    pairs: dict[str, str] = {}
    for row in table:
        cells = [c[0] for c in row]
        for key, value in zip(cells[0::2], cells[1::2], strict=False):
            if key:
                pairs[key] = value
    return pairs


def propeller_catalog_values(spec: dict[str, str]) -> dict[str, float]:
    """Catalog fields read from a propeller specification table.

    ``max_rpm_safe`` is the upper end of the publisher's «Optimum RPM» range: T-MOTOR
    publishes no maximum, and a curve above the optimum should not pass as in range.
    """
    size = re.match(r"(\d+(?:\.\d+)?)\*(\d+(?:\.\d+)?)", spec["Model No."])
    weight = re.match(r"(\d+(?:\.\d+)?)", spec["Weight(Single Blade)"])
    rpm = re.findall(r"(\d+)", spec["Optimum RPM"])
    thrust = re.match(r"(\d+(?:\.\d+)?)\s*kg", spec["Thrust Limitation"])
    if not (size and weight and rpm and thrust):
        raise ValueError(f"Unrecognised propeller specification: {spec}")
    return {
        "diameter_inch": float(size.group(1)),
        "pitch_inch": float(size.group(2)),
        "weight_g": float(weight.group(1)),
        "max_rpm_safe": int(rpm[-1]),
        "thrust_limit_g": float(thrust.group(1)) * 1000.0,
    }


def _prop_key(cell: str) -> str | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*\*\s*(\d+(?:\.\d+)?)", cell)
    return f"{match.group(1)}*{match.group(2)}" if match else None


def _item_key(cell: str) -> str:
    match = re.match(r"(MN\d+)\s*(KV\d+)", cell.replace(" ", ""))
    return f"{match.group(1)} {match.group(2)}" if match else cell


def build_tests(extracts: dict[str, dict[str, Any]], shas: dict[str, str]) -> dict[str, Any]:
    groups: dict[tuple[str, str, str, str], list[list[str]]] = {}
    for page, extract in extracts.items():
        for row in extract["test_data_rows"]:
            item, volts, prop = _item_key(row[0]), row[1], _prop_key(row[2])
            groups.setdefault((page, item, volts, prop or row[2]), []).append(row)
    tests, excluded = [], []
    for (page, item, volts, prop), rows in sorted(groups.items()):
        motor, propeller = MOTORS.get(item), PROPELLERS.get(prop)
        if motor is None or propeller is None:
            continue
        points = [
            {
                "throttle_pct": float(r[3].rstrip("%")),
                "rpm": int(float(r[7])),
                "thrust_g": float(r[6]),
                "current_a": float(r[4]),
                "power_w": float(r[5]),
                "voltage_v": float(volts),
            }
            for r in rows
        ]
        points.sort(key=lambda p: p["throttle_pct"])
        label = f"{item} / {volts} V / {rows[0][2]}"
        # A published row out of order (e.g. MN4014 KV400 17*5.8 lists 2.17 A and
        # 64.56 g/W at 85%, between 16.7 A and 25.7 A — a likely typo for 21.7 A)
        # excludes the whole combination; the value is reported, never corrected.
        broken = [
            f"{key} falls from {a[key]:g} at {a['throttle_pct']:g}% to {b[key]:g} at "
            f"{b['throttle_pct']:g}%"
            for key in ("thrust_g", "current_a", "power_w", "rpm")
            for a, b in zip(points, points[1:])
            if b[key] <= a[key]
        ]
        if broken or len({p["throttle_pct"] for p in points}) != len(points):
            excluded.append(
                {"source_rows": label, "reason": "; ".join(broken) or "duplicate throttle rows"}
            )
            continue
        tests.append(
            {
                "motor_part_number": motor,
                "propeller_part_number": propeller,
                "test_voltage_v": float(volts),
                "source_kind": "manufacturer_test",
                "source_reference": (
                    f"T-MOTOR store page {PAGES[page]}, Specifications > Test Data, rows "
                    f"{label}; extract at local catalog evidence/{page}.json"
                ),
                "source_sha256": shas[page],
                "source_date": extracts[page]["retrieved_on"],
                "points": points,
            }
        )
    return {
        "schema_version": 1,
        "generated_by": "apps/api/scripts/extract_propulsion_tests.py",
        "tests": tests,
        "excluded": excluded,
    }


def _dump(data: Any) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _number(text: str) -> float:
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match is None:
        raise ValueError(f"No number in {text!r}")
    return float(match.group(0))


def catalog_motor(item: str, spec: dict[str, str], default: dict[str, Any]) -> dict[str, Any]:
    """A catalog motor built from its specification rows and one sourced test.

    T-MOTOR publishes no burst rating, so ``max_burst_current_a`` repeats the 180 s
    continuous rating instead of claiming unpublished headroom.
    """
    sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
    from agents.component_models import MotorThrustPoint
    from pipelines.propulsion_evidence import thrust_curve_sha256

    family, kv = item.split(" KV")
    cells = [int(n) for n in re.findall(r"\d+", spec["No.of Cells(Lipo)"])]
    continuous = _number(spec["Max Continuous Current 180S"])
    evidence = {
        key: default[key]
        for key in (
            "motor_part_number",
            "propeller_part_number",
            "test_voltage_v",
            "source_kind",
            "source_reference",
            "source_sha256",
            "source_date",
        )
    }
    evidence["curve_sha256"] = thrust_curve_sha256(
        [MotorThrustPoint(**point) for point in default["points"]]
    )
    return {
        "part_number": MOTORS[item],
        "manufacturer": "T-Motor",
        "model_name": f"{family} {kv}KV",
        "kv": int(kv),
        "weight_g": _number(spec["Weight Including Cables"]),
        "max_continuous_current_a": continuous,
        "max_burst_current_a": continuous,
        "stator_diameter_mm": _number(spec["Stator Diameter"]),
        "stator_height_mm": _number(spec.get("Stator Height") or spec["StatorHeight"]),
        "shaft_diameter_mm": _number(spec["Shaft Diameter"]),
        "recommended_voltage_cells_s": list(range(cells[0], cells[-1] + 1)),
        "thrust_curve": default["points"],
        "thrust_curve_evidence": evidence,
        "url": None,
    }


def catalog_propeller(part: str, page: dict[str, Any]) -> dict[str, Any]:
    values = propeller_catalog_values(page["specification"])
    model = page["specification"]["Model No."].split("(")[0].replace("*", "x")
    return {
        "part_number": part,
        "manufacturer": "T-Motor",
        "model_name": f"P{model} CF",
        "diameter_inch": values["diameter_inch"],
        "pitch_inch": values["pitch_inch"],
        "blades": 2,
        "material": "carbon_fiber",
        "weight_g": values["weight_g"],
        "max_rpm_safe": values["max_rpm_safe"],
        # Catalog shaft-fit heuristic (4 mm MN motors); not from the page.
        "hub_id_mm": 4.0,
        "url": page["source_url"],
    }


def _format_entries(entries: list[dict[str, Any]]) -> str:
    """The catalog files' layout: one key per line, one curve point per line."""
    out = ["["]
    for index, entry in enumerate(entries):
        out.append("  {")
        keys = list(entry)
        for k, key in enumerate(keys):
            value = entry[key]
            comma = "," if k < len(keys) - 1 else ""
            if key == "thrust_curve":
                out.append(f'    "{key}": [')
                for j, point in enumerate(value):
                    tail = "," if j < len(value) - 1 else ""
                    out.append("      " + json.dumps(point, ensure_ascii=False) + tail)
                out.append("    ]" + comma)
            elif isinstance(value, dict):
                inner = json.dumps(value, indent=2, ensure_ascii=False).replace("\n", "\n    ")
                out.append(f'    "{key}": {inner}{comma}')
            else:
                out.append(f'    "{key}": {json.dumps(value, ensure_ascii=False)}{comma}')
        out.append("  }" + ("," if index < len(entries) - 1 else ""))
    out.append("]")
    return "\n".join(out) + "\n"


def write_catalog() -> None:
    tests = json.loads(TESTS_PATH.read_text(encoding="utf-8"))["tests"]
    specs: dict[str, dict[str, str]] = {}
    urls: dict[str, str] = {}
    for page in PAGES:
        extract = json.loads((EVIDENCE_DIR / f"{page}.json").read_text(encoding="utf-8"))
        for spec in extract["specifications"]:
            specs[_item_key(spec["Test Item"])] = spec
            urls[_item_key(spec["Test Item"])] = extract["source_url"]
    motors_path, props_path = COMPONENTS / "motors.json", COMPONENTS / "propellers.json"
    motors = json.loads(motors_path.read_text(encoding="utf-8"))
    by_part = {m["part_number"]: m for m in motors}
    for item, part in MOTORS.items():
        own = [t for t in tests if t["motor_part_number"] == part]
        if not own:
            continue
        # The default curve: the existing sourced one if it is still a test, else
        # the highest-voltage test. A matching combination test replaces it anyway.
        current = (by_part.get(part) or {}).get("thrust_curve_evidence") or {}
        default = next(
            (
                t
                for t in own
                if t["propeller_part_number"] == current.get("propeller_part_number")
                and t["test_voltage_v"] == current.get("test_voltage_v")
            ),
            max(own, key=lambda t: (t["test_voltage_v"], t["propeller_part_number"])),
        )
        entry = catalog_motor(item, specs[item], default)
        entry["url"] = urls[item]
        if part in by_part:
            if by_part[part].get("price_usd") is not None:
                entry["price_usd"] = by_part[part]["price_usd"]
            motors[motors.index(by_part[part])] = entry
        else:
            motors.append(entry)
    motors_path.write_text(_format_entries(motors), encoding="utf-8")

    pages = json.loads((EVIDENCE_DIR / f"{PROPELLER_EXTRACT}.json").read_text(encoding="utf-8"))
    props = json.loads(props_path.read_text(encoding="utf-8"))
    by_prop = {p["part_number"]: p for p in props}
    for part, page in pages["propellers"].items():
        entry = catalog_propeller(part, page)
        if part in by_prop:
            old = by_prop[part]
            entry["hub_id_mm"] = old.get("hub_id_mm", entry["hub_id_mm"])
            if old.get("price_usd") is not None:
                entry["price_usd"] = old["price_usd"]
            props[props.index(old)] = entry
        else:
            props.append(entry)
    props_path.write_text(_format_entries(props), encoding="utf-8")
    print(f"catalog: {len(motors)} motors, {len(props)} propellers")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="Compare with locally imported extracts"
    )
    parser.add_argument("--write-catalog", action="store_true", help="Upsert catalog entries")
    args = parser.parse_args(argv)
    if args.write_catalog:
        write_catalog()
        return 0
    today = dt.date.today().isoformat()
    extracts: dict[str, dict[str, Any]] = {}
    shas: dict[str, str] = {}
    changed = False
    for page, url in PAGES.items():
        parsed = parse_page(fetch(url))
        path = EVIDENCE_DIR / f"{page}.json"
        if args.check:
            committed = json.loads(path.read_text(encoding="utf-8"))
            same = committed["test_data_rows"] == parsed["test_data_rows"]
            print(
                f"{page}: {'unchanged' if same else 'CHANGED'} ({len(parsed['test_data_rows'])} rows)"
            )
            changed |= not same
            continue
        extract = {
            "publisher": "T-MOTOR",
            "source_url": url,
            "source_section": "Specifications > Test Data; Specifications > Specification",
            "retrieved_on": today,
            "page_publication_date": None,
            "retrieval_method": (
                "HTTP GET of the product page; tables parsed by "
                "apps/api/scripts/extract_propulsion_tests.py. Cell text verbatim with "
                "whitespace collapsed; a cell spanning several rows is repeated on each."
            ),
            **parsed,
            "not_stated_by_source": NOT_STATED,
            "redistribution": REDISTRIBUTION,
        }
        raw = _dump(extract)
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        extracts[page] = extract
        shas[page] = hashlib.sha256(raw).hexdigest()
        print(f"{page}: {len(parsed['test_data_rows'])} rows -> {path.name} {shas[page][:12]}")
    propellers = {
        part: {"source_url": url, "specification": parse_propeller_page(fetch(url))}
        for part, url in PROPELLER_PAGES.items()
    }
    prop_path = EVIDENCE_DIR / f"{PROPELLER_EXTRACT}.json"
    if args.check:
        committed = json.loads(prop_path.read_text(encoding="utf-8"))
        same = committed["propellers"] == propellers
        print(f"{PROPELLER_EXTRACT}: {'unchanged' if same else 'CHANGED'}")
        return 1 if changed or not same else 0
    prop_path.write_bytes(
        _dump(
            {
                "publisher": "T-MOTOR",
                "source_section": "product page specification table",
                "retrieved_on": today,
                "retrieval_method": (
                    "HTTP GET of each product page; the specification table parsed by "
                    "apps/api/scripts/extract_propulsion_tests.py, cell text verbatim."
                ),
                "propellers": propellers,
                "redistribution": REDISTRIBUTION,
            }
        )
    )
    print(f"{PROPELLER_EXTRACT}: {len(propellers)} propellers -> {prop_path.name}")
    tests = build_tests(extracts, shas)
    TESTS_PATH.write_bytes(_dump(tests))
    print(
        f"{len(tests['tests'])} test cases, {len(tests['excluded'])} excluded -> {TESTS_PATH.name}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
