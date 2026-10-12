"""Evidence identity checks; declarations below are synthetic test fixtures."""

import json
import re
from datetime import date

import pytest
from pydantic import ValidationError

from agents.component_models import MotorThrustPoint, PropulsionCurveEvidence
from pipelines.performance_solver import PerformanceSolver
from pipelines.propulsion_evidence import assess_propulsion_evidence, thrust_curve_sha256
from pipelines.resolved_drone import resolve_drone
from tests.test_resolved_drone import inputs


def declared_parts():
    _, parts = inputs()
    parts.motor.thrust_curve_evidence = PropulsionCurveEvidence(
        motor_part_number=parts.motor.part_number,
        propeller_part_number=parts.propeller.part_number,
        test_voltage_v=parts.battery.pack_voltage_nominal_v,
        source_kind="bench_test",
        source_reference="Synthetic unit-test fixture; NOT measured hardware data",
        source_sha256="a" * 64,
        source_date=date(2026, 9, 20),
        curve_sha256=thrust_curve_sha256(parts.motor.thrust_curve),
    )
    return parts


def test_missing_source_is_explicit_and_strict_solver_refuses():
    _, parts = inputs()
    report = PerformanceSolver().solve(parts)
    assert report.estimate_scope == "preliminary_catalog_estimate"
    assert report.propulsion_evidence.status == "missing"
    assert not report.propulsion_evidence.source_independently_verified
    assert any("Preliminary propulsion estimate" in w for w in report.warnings)
    with pytest.raises(ValueError, match="No motor/propeller/source"):
        PerformanceSolver().solve(parts, require_matching_evidence=True)


def test_matching_declaration_is_not_source_or_flight_verification():
    parts = declared_parts()
    # Extra mass keeps hover inside the fixture curve; below it, strict mode refuses.
    report = PerformanceSolver().solve(parts, extra_mass_g=500.0, require_matching_evidence=True)
    assert report.propulsion_evidence.status == "matched_declaration"
    assert not report.propulsion_evidence.source_independently_verified
    assert report.propulsion_evidence.source_sha256 == "a" * 64
    assert report.estimate_scope == "preliminary_catalog_estimate"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("motor", "Declared motor"),
        ("propeller", "Declared propeller"),
        ("battery", "battery nominal voltage"),
        ("curve", "fingerprint"),
        ("mixed_voltage", "declared test condition"),
        ("rpm", "RPM exceeds"),
        ("test_voltage", "Declared test voltage"),
    ],
)
def test_selection_or_measurement_change_invalidates_declaration(change, reason):
    parts = declared_parts()
    if change == "motor":
        parts.motor.part_number = "different_motor"
    elif change == "propeller":
        parts.propeller.part_number = "different_propeller"
    elif change == "battery":
        parts.battery.cells_s = 6
    elif change == "curve":
        parts.motor.thrust_curve[0].thrust_g += 1
    elif change == "mixed_voltage":
        parts.motor.thrust_curve[0].voltage_v = 22.2
    elif change == "rpm":
        parts.propeller.max_rpm_safe = 100
    else:
        parts.motor.thrust_curve_evidence.test_voltage_v = 22.2
    assessment = assess_propulsion_evidence(parts)
    assert assessment.status == "mismatch"
    assert any(reason in s for s in assessment.reasons)
    with pytest.raises(ValueError, match="Propulsion evidence"):
        PerformanceSolver().solve(parts, require_matching_evidence=True)


def test_reordering_points_preserves_hash_but_changing_data_does_not():
    parts = declared_parts()
    original = thrust_curve_sha256(parts.motor.thrust_curve)
    parts.motor.thrust_curve.reverse()
    assert thrust_curve_sha256(parts.motor.thrust_curve) == original
    parts.motor.thrust_curve[0].power_w += 1
    assert thrust_curve_sha256(parts.motor.thrust_curve) != original


@pytest.mark.parametrize("change", ["empty", "one", "duplicate", "falling", "nan"])
def test_invalid_curves_never_enter_preliminary_solver(change):
    _, parts = inputs()
    points = parts.motor.thrust_curve
    if change == "empty":
        points.clear()
    elif change == "one":
        del points[1:]
    elif change == "duplicate":
        points[1].throttle_pct = points[0].throttle_pct
    elif change == "falling":
        points[1].thrust_g = points[0].thrust_g - 1
    else:
        points[0].power_w = float("nan")
    with pytest.raises(ValueError):
        PerformanceSolver().solve(parts)


def test_nonfinite_points_rejected_on_parse():
    with pytest.raises(ValidationError):
        MotorThrustPoint(
            throttle_pct=20,
            rpm=100,
            thrust_g=100,
            current_a=float("inf"),
            power_w=50,
            voltage_v=14.8,
        )


def test_mutated_declaration_revalidated():
    parts = declared_parts()
    parts.motor.thrust_curve_evidence.source_reference = " "
    assert assess_propulsion_evidence(parts).status == "mismatch"


def test_propulsion_source_changes_resolved_aircraft_identity():
    design, parts = inputs()
    before = resolve_drone(design, parts).sha256
    parts.motor.thrust_curve_evidence = declared_parts().motor.thrust_curve_evidence
    after = resolve_drone(design, parts).sha256
    assert before != after
    parts.motor.thrust_curve_evidence.source_sha256 = "b" * 64
    assert resolve_drone(design, parts).sha256 != after


def test_evidence_reaches_engineering_export(tmp_path, monkeypatch):
    from config.settings import settings
    from graph.nodes import _write_engineering_deliverables

    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    design, _ = inputs()
    parts = declared_parts()
    state = {
        "project_id": "evidence",
        "design_tree": design.model_dump(),
        "selected_components": parts.model_dump(),
        "bom_status": "selected",
    }
    assert _write_engineering_deliverables(state, {}, [], None)
    exported = json.loads((tmp_path / "evidence/deliverables/selected_components.json").read_text())
    assert exported["performance"]["propulsion_evidence"]["status"] == "matched_declaration"
    assert exported["components"]["motor"]["thrust_curve_evidence"]["source_sha256"] == "a" * 64
    assert any("Propulsion evidence" in s for s in exported["limitations"])


def test_hover_below_the_curve_is_named_not_presented_as_data():
    parts = declared_parts()
    lowest = min(p.thrust_g for p in parts.motor.thrust_curve)
    report = PerformanceSolver().solve(parts)
    assert report.hover_thrust_per_motor_g < lowest
    assert any("Outside the propulsion curve" in w for w in report.warnings)
    with pytest.raises(ValueError, match="outside the propulsion curve"):
        PerformanceSolver().solve(parts, require_matching_evidence=True)
    inside = PerformanceSolver().solve(parts, extra_mass_g=500.0)
    assert not any("Outside the propulsion curve" in w for w in inside.warnings)


def _rows_for(extract, motor, propeller, voltage):
    """The extract rows of this motor, propeller and voltage, as published."""
    family, kv = motor.model_name.split()
    item = f"{family}KV{kv.removesuffix('KV')}"
    rows = []
    for row in extract["test_data_rows"]:
        size = re.search(r"(\d+(?:\.\d+)?)\*(\d+(?:\.\d+)?)", row[2])
        if (
            row[0].replace(" ", "") == item
            and float(row[1]) == voltage
            and size
            and (float(size.group(1)), float(size.group(2)))
            == (propeller.diameter_inch, propeller.pitch_inch)
        ):
            rows.append(row)
    return rows


def test_every_sourced_curve_is_its_extract_rows_verbatim():
    """A declaration must be reproducible from the committed extract it names.

    Checks the bytes and their transcription into curves and motor ratings, not
    the publisher's measurements. Motors without a declaration claim nothing.
    """
    import hashlib
    from pathlib import Path

    from services.component_catalog import get_catalog

    catalog = get_catalog()
    evidence_dir = Path(catalog.root) / "evidence"
    curves = [(t.declaration(), t.points) for t in catalog.propulsion_tests]
    curves += [(m.thrust_curve_evidence, m.thrust_curve) for m in catalog.motors]
    curves = [(e, pts) for e, pts in curves if e is not None]
    assert len(catalog.propulsion_tests) == 25
    for evidence, points in curves:
        name = evidence.source_reference.rsplit("/", 1)[1]
        raw = (evidence_dir / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == evidence.source_sha256
        extract = json.loads(raw)
        assert evidence.source_kind == "manufacturer_test"
        assert extract["redistribution"].startswith("Not granted")
        assert evidence.curve_sha256 == thrust_curve_sha256(points)
        motor = catalog.motor(evidence.motor_part_number)
        propeller = catalog.propeller(evidence.propeller_part_number)
        rows = _rows_for(extract, motor, propeller, evidence.test_voltage_v)
        curve = sorted(points, key=lambda p: p.throttle_pct)
        assert len(rows) == len(curve) == 5
        for row, point in zip(rows, curve, strict=True):
            _, volts, _, throttle, amps, watts, grams, rpm, *_ = row
            assert float(volts) == point.voltage_v == evidence.test_voltage_v
            assert float(throttle.rstrip("%")) == point.throttle_pct
            assert (float(amps), float(watts)) == (point.current_a, point.power_w)
            assert (float(grams), int(rpm)) == (point.thrust_g, point.rpm)
        spec = next(
            s
            for s in extract["specifications"]
            if s["Test Item"].replace(" ", "") == rows[0][0].replace(" ", "")
        )
        assert motor.weight_g == float(spec["Weight Including Cables"].rstrip("g"))
        assert motor.max_continuous_current_a == float(
            spec["Max Continuous Current 180S"].rstrip("A")
        )


def test_a_published_row_out_of_order_excludes_its_combination():
    from pathlib import Path

    from services.component_catalog import get_catalog

    catalog = get_catalog()
    tests = json.loads((Path(catalog.root) / "propulsion_tests.json").read_text("utf-8"))
    assert tests["excluded"] == [
        {
            "source_rows": "MN4014 KV400 / 22.2 V / T-MOTOR 17*5.8CF",
            "reason": "current_a falls from 16.7 at 75% to 2.17 at 85%; "
            "power_w falls from 370.74 at 75% to 48.17 at 85%",
        }
    ]
    assert catalog.propulsion_test("tmotor_mn4014_400kv", "tmotor_p17_58", 22.2) is None


def test_propeller_ratings_come_from_their_pages():
    from pathlib import Path

    from scripts.extract_propulsion_tests import propeller_catalog_values
    from services.component_catalog import get_catalog

    catalog = get_catalog()
    pages = json.loads(
        (Path(catalog.root) / "evidence" / "tmotor_p_series_propellers.json").read_text("utf-8")
    )["propellers"]
    assert len(pages) == 6
    for part, page in pages.items():
        values = propeller_catalog_values(page["specification"])
        prop = catalog.propeller(part)
        assert (prop.weight_g, prop.max_rpm_safe) == (values["weight_g"], values["max_rpm_safe"])
        assert (prop.diameter_inch, prop.pitch_inch) == (
            values["diameter_inch"],
            values["pitch_inch"],
        )


def test_bundled_declaration_matches_only_its_own_test_condition():
    from agents.component_models import SelectedComponents
    from services.component_catalog import get_catalog

    catalog = get_catalog()
    parts = SelectedComponents(
        motor=catalog.motor("tmotor_mn3508_380kv"),
        propeller=catalog.propeller("tmotor_p13_44"),
        battery=catalog.battery("tattu_plus_10000_6s_25c"),
        esc=catalog.escs[0],
        flight_controller=catalog.flight_controllers[0],
        frame=catalog.frame("tarot_680_pro_hex"),
        motor_count=6,
    )
    assert assess_propulsion_evidence(parts).status == "matched_declaration"
    four_cell = parts.model_copy(update={"battery": catalog.battery("tattu_plus_6000_4s_25c")})
    assessment = assess_propulsion_evidence(four_cell)
    assert assessment.status == "mismatch"
    assert any("battery nominal voltage" in reason for reason in assessment.reasons)
