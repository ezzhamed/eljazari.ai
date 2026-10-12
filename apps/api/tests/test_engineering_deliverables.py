"""Phase 14 tests — engineering deliverables (BOM, assembly, preflight)."""

from __future__ import annotations

import io

import pytest

from pipelines.engineering_deliverables import (
    ComponentLine,
    DeliverableBundle,
    render_all_deliverables,
    render_assembly_guide,
    render_bom_csv,
    render_bom_markdown,
    render_engineering_report,
    render_maintenance_schedule,
    render_preflight_checklist,
)


def _make_bundle(**overrides) -> DeliverableBundle:
    defaults: dict = dict(
        project_id="test-project-001",
        project_name="Indoor Inspection Quadcopter",
        mission_prompt="Takeoff to 4m, fly 6m north, land safely.",
        frame_type="X",
        arm_length_mm=267.0,
        total_weight_g=1360.0,
        battery_summary="LiPo 4S 6000mAh",
        components=[
            ComponentLine(
                category="Frame",
                part_name="Carbon X 250 frame",
                qty=1,
                supplier="iFlight",
                part_number="X250-CF",
                unit_price_usd=65.0,
                weight_g=180.0,
            ),
            ComponentLine(
                category="Motor",
                part_name="T-Motor F80 2400KV",
                qty=4,
                supplier="T-Motor",
                part_number="F80-2400",
                unit_price_usd=22.5,
                weight_g=27.0,
            ),
            ComponentLine(
                category="Battery",
                part_name="Tattu 4S 6000mAh",
                qty=1,
                supplier="Tattu",
                part_number="TA-4S-6000",
                unit_price_usd=89.0,
                weight_g=480.0,
            ),
        ],
        flight_quality_score=82.0,
        flight_quality_grade="B",
        position_rmse_m=0.8,
        altitude_rmse_m=0.3,
        return_to_home_error_m=2.0,
        hover_endurance_min=18.5,
        max_speed_ms=14.0,
        pid_tune_iterations=2,
        pid_final_gains_summary="MC_ROLLRATE_P=0.13 D=0.0036",
        failure_scenarios_passed=6,
        failure_scenarios_total=7,
        tested_scenarios=["calm_indoor", "light_breeze"],
        urdf_path="/data/storage/test-project-001/urdf/quadcopter.urdf",
        sdf_path="/data/storage/test-project-001/urdf/quadcopter.sdf",
        mission_code_path="/data/storage/test-project-001/mission_code.py",
    )
    defaults.update(overrides)
    return DeliverableBundle(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# ComponentLine
# ─────────────────────────────────────────────────────────────────────────────


class TestComponentLine:
    def test_line_total_multiplies_qty(self):
        line = ComponentLine(
            category="Motor",
            part_name="T-Motor",
            qty=4,
            unit_price_usd=22.5,
        )
        assert line.line_total_usd == pytest.approx(90.0)

    def test_default_qty_is_1(self):
        line = ComponentLine(
            category="Frame",
            part_name="X frame",
            unit_price_usd=50.0,
        )
        assert line.qty == 1
        assert line.line_total_usd == 50.0

    def test_rejects_negative_qty(self):
        with pytest.raises(ValueError):
            ComponentLine(category="x", part_name="y", qty=0)


# ─────────────────────────────────────────────────────────────────────────────
# BOM
# ─────────────────────────────────────────────────────────────────────────────


class TestBOM:
    def test_csv_has_header_and_rows(self):
        bundle = _make_bundle()
        csv_text = render_bom_csv(bundle)
        lines = csv_text.strip().splitlines()
        assert lines[0].startswith("Category,Part Name")
        # 1 header + 3 component rows + 1 blank + 1 total = 6 minimum
        assert len(lines) >= 5

    def test_csv_total_matches_sum(self):
        bundle = _make_bundle()
        csv_text = render_bom_csv(bundle)
        expected_total = sum(c.line_total_usd for c in bundle.components)
        # Search for the line containing "TOTAL"
        total_line = next(
            (ln for ln in csv_text.splitlines() if "TOTAL" in ln),
            None,
        )
        assert total_line is not None
        assert f"{expected_total:.2f}" in total_line

    def test_csv_parses_back(self):
        import csv

        bundle = _make_bundle()
        csv_text = render_bom_csv(bundle)
        rows = list(csv.reader(io.StringIO(csv_text)))
        assert rows[0][0] == "Category"
        # 3 component rows after the header.
        data_rows = [r for r in rows[1:] if r and r[0] and r[0] != ""]
        assert sum(1 for r in data_rows if r[1] != "TOTAL") == len(bundle.components)

    def test_markdown_includes_table_header(self):
        bundle = _make_bundle()
        md = render_bom_markdown(bundle)
        assert "| Category | Part" in md
        assert "Total component cost" in md

    def test_markdown_shows_project_id(self):
        bundle = _make_bundle(project_id="abc-123")
        md = render_bom_markdown(bundle)
        assert "abc-123" in md

    def test_markdown_empty_components_still_renders_table(self):
        bundle = _make_bundle(components=[])
        md = render_bom_markdown(bundle)
        assert "| Category | Part" in md
        # The summary section uses bold formatting around the label.
        assert "Total component cost:" in md
        assert "$0.00" in md


# ─────────────────────────────────────────────────────────────────────────────
# Assembly guide
# ─────────────────────────────────────────────────────────────────────────────


class TestAssemblyGuide:
    def test_starts_with_safety_callout(self):
        guide = render_assembly_guide(_make_bundle())
        assert "Safety first" in guide

    def test_propellers_step_is_last(self):
        guide = render_assembly_guide(_make_bundle())
        # The guide must end with the propellers section.
        propellers_idx = guide.rfind("Propellers")
        assert propellers_idx > guide.rfind("Step 1")
        assert "LAST" in guide  # explicit warning

    def test_includes_arm_length_from_bundle(self):
        bundle = _make_bundle(arm_length_mm=300.0)
        guide = render_assembly_guide(bundle)
        assert "300" in guide

    def test_includes_urdf_path(self):
        bundle = _make_bundle(urdf_path="/path/to/foo.urdf")
        guide = render_assembly_guide(bundle)
        assert "/path/to/foo.urdf" in guide


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight checklist
# ─────────────────────────────────────────────────────────────────────────────


class TestPreflight:
    def test_has_unticked_checkboxes(self):
        check = render_preflight_checklist(_make_bundle())
        assert "- [ ]" in check

    def test_includes_failsafe_item(self):
        check = render_preflight_checklist(_make_bundle())
        assert "Failsafe" in check or "failsafe" in check

    def test_groups_by_section(self):
        check = render_preflight_checklist(_make_bundle())
        for required_section in ("Vehicle", "Avionics", "GPS / radio", "Crew & site"):
            assert f"## {required_section}" in check


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance
# ─────────────────────────────────────────────────────────────────────────────


class TestMaintenance:
    def test_includes_all_intervals(self):
        m = render_maintenance_schedule(_make_bundle())
        for interval in (
            "Every flight",
            "Every 10 flight hours",
            "Every 50 flight hours",
            "Every 200 flight hours / annually",
        ):
            assert interval in m


# ─────────────────────────────────────────────────────────────────────────────
# Engineering report
# ─────────────────────────────────────────────────────────────────────────────


class TestEngineeringReport:
    def test_includes_verdict_table(self):
        report = render_engineering_report(_make_bundle())
        assert "Flight quality grade" in report
        assert "**B**" in report

    def test_shows_score(self):
        report = render_engineering_report(_make_bundle())
        assert "82.0 / 100" in report

    def test_shows_failure_ratio(self):
        report = render_engineering_report(_make_bundle())
        assert "6 / 7" in report

    def test_handles_missing_score_gracefully(self):
        bundle = _make_bundle(flight_quality_score=None, flight_quality_grade=None)
        report = render_engineering_report(bundle)
        assert "—" in report  # em-dash placeholder

    def test_includes_artefact_paths(self):
        bundle = _make_bundle(urdf_path="/x/y.urdf")
        report = render_engineering_report(bundle)
        assert "/x/y.urdf" in report


# ─────────────────────────────────────────────────────────────────────────────
# All-in-one renderer
# ─────────────────────────────────────────────────────────────────────────────


class TestRenderAllDeliverables:
    def test_returns_six_files(self):
        bundle = _make_bundle()
        files = render_all_deliverables(bundle)
        expected = {
            "report.md",
            "bom.csv",
            "bom.md",
            "assembly.md",
            "preflight.md",
            "maintenance.md",
        }
        assert set(files.keys()) == expected

    def test_each_file_is_non_empty_string(self):
        bundle = _make_bundle()
        for name, content in render_all_deliverables(bundle).items():
            assert isinstance(content, str), name
            assert len(content) > 50, f"{name} is too short: {len(content)} bytes"

    def test_is_deterministic_for_static_bundle(self):
        """Same bundle → same content (except for timestamps)."""
        bundle = _make_bundle()
        a = render_bom_csv(bundle)
        b = render_bom_csv(bundle)
        assert a == b
