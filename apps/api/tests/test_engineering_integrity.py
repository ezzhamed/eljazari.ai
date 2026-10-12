"""Regression checks for the engineering review's BOM and feasibility failures."""

import csv
import hashlib
import json

import pytest
from pydantic import ValidationError

from agents.component_selector_agent import ComponentSelectorAgent
from agents.design_models import PayloadSpec
from pipelines.performance_solver import PerformanceSolver
from services.component_catalog import ComponentCatalog, get_catalog
from tests.test_component_selector import _build_selected, _design


def test_impossible_payload_is_rejected_instead_of_returning_zero_score():
    with pytest.raises(RuntimeError, match="not flyable"):
        ComponentSelectorAgent().select(_design(), payload_g=100000)


def test_required_endurance_is_a_hard_constraint():
    with pytest.raises(RuntimeError, match="below required 1000 min"):
        ComponentSelectorAgent().select(_design(), min_endurance_min=1000)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
@pytest.mark.parametrize("parameter", ["payload_g", "min_endurance_min"])
def test_invalid_requirements_cannot_bypass_selection(value, parameter):
    with pytest.raises(ValueError, match="finite and non-negative"):
        ComponentSelectorAgent().select(_design(), **{parameter: value})


def test_single_channel_escs_are_counted_for_every_motor():
    selected = _build_selected(get_catalog())
    selected.esc = selected.esc.model_copy(
        update={"channels": 1, "weight_g": 10.0, "price_usd": 12.0}
    )
    other_mass = (
        selected.total_propulsion_weight_g
        + selected.battery.weight_g
        + selected.flight_controller.weight_g
        + selected.frame.weight_g
    )
    assert selected.esc_quantity == 4
    assert selected.total_bom_weight_g == pytest.approx(other_mass + 40)
    multi_esc_cost = selected.total_bom_price_usd
    selected.esc = selected.esc.model_copy(update={"channels": 4})
    assert selected.esc_quantity == 1
    assert multi_esc_cost - selected.total_bom_price_usd == pytest.approx(36)


def test_underrated_single_channel_esc_is_not_flyable():
    selected = _build_selected(get_catalog())
    selected.esc = selected.esc.model_copy(
        update={"channels": 1, "max_continuous_current_per_channel_a": 1.0}
    )
    report = PerformanceSolver().solve(selected)
    assert not report.is_flyable
    assert any("ESC underrated" in w for w in report.warnings)


def test_esc_voltage_mismatch_is_rejected_by_selection():
    catalog = ComponentCatalog()
    catalog.escs = [e.model_copy(update={"min_cells_s": 6}) for e in catalog.escs]
    with pytest.raises(RuntimeError, match="No viable BOM"):
        ComponentSelectorAgent(catalog=catalog).select(_design(cells=4))


def test_px4_selection_does_not_fall_back_to_unsupported_firmware():
    catalog = ComponentCatalog()
    catalog.flight_controllers = [
        f.model_copy(update={"supports_px4": False}) for f in catalog.flight_controllers
    ]
    with pytest.raises(RuntimeError, match="No viable BOM"):
        ComponentSelectorAgent(catalog=catalog).select(_design())


def test_negative_installed_payload_is_invalid():
    with pytest.raises(ValidationError):
        PayloadSpec(installed_weight_g=-1)


@pytest.mark.parametrize("installed, expected", [(None, 100000), (0, 0), (125, 125)])
def test_payload_basis_is_explicit(installed, expected):
    assert PayloadSpec(max_weight_g=100000, installed_weight_g=installed).design_load_g == expected


@pytest.mark.asyncio
async def test_selection_uses_installed_payload_and_clears_stale_bom_on_failure():
    from graph.nodes import select_components

    design = _design()
    design.payload.max_weight_g = 100000
    design.payload.installed_weight_g = 0
    state = {"project_id": "integrity", "design_tree": design.model_dump()}
    good = await select_components(state)
    assert good["bom_status"] == "selected"
    state.update(good)
    design.payload.installed_weight_g = 100000
    state["design_tree"] = design.model_dump()
    bad = await select_components(state)
    assert bad["bom_status"] == "failed"
    assert bad["selected_components"] is None
    assert bad["real_performance"] is None
    assert bad["component_alternatives"] == []


@pytest.mark.asyncio
async def test_node_honors_structured_endurance_requirement():
    from graph.nodes import select_components

    result = await select_components(
        {
            "project_id": "endurance",
            "design_tree": _design().model_dump(),
            "design_params": {"min_endurance_min": 1000},
        }
    )
    assert result["bom_status"] == "failed"
    assert "1000 min" in result["errors"][0]


def test_exports_use_selected_parts_quantities_mass_and_payload(tmp_path, monkeypatch):
    from config.settings import settings
    from graph.nodes import _write_engineering_deliverables

    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    selected = _build_selected(get_catalog())
    selected.esc = selected.esc.model_copy(update={"channels": 1, "weight_g": 10.0})
    selected.battery = selected.battery.model_copy(update={"chemistry": "Li-ion"})
    design = _design()
    design.payload.max_weight_g = 1000
    design.payload.installed_weight_g = 125
    state = {
        "project_id": "export",
        "design_tree": design.model_dump(),
        "selected_components": selected.model_dump(),
        "performance_estimate": {"total_weight_g": 99999, "max_speed_ms": 999},
        "real_performance": {"total_mass_g": 88888},
    }
    paths = _write_engineering_deliverables(state, {}, [], None)
    folder = tmp_path / "export" / "deliverables"
    assert len(paths) == 7
    with (folder / "bom.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    parts = {r["Category"]: r for r in rows if r["Category"]}
    assert set(parts) == {
        "Frame",
        "Motor",
        "Propeller",
        "ESC",
        "Flight controller",
        "Battery",
        "Payload",
    }
    assert parts["Motor"]["Part Number"] == selected.motor.part_number
    assert parts["Battery"]["Part Number"] == selected.battery.part_number
    assert int(parts["Motor"]["Qty"]) == 4
    assert int(parts["Propeller"]["Qty"]) == 4
    assert int(parts["ESC"]["Qty"]) == 4
    total_mass = selected.total_bom_weight_g + 125
    assert sum(float(r["Weight (g)"]) * int(r["Qty"]) for r in parts.values()) == pytest.approx(
        total_mass, abs=0.1
    )
    report = (folder / "report.md").read_text(encoding="utf-8")
    assert f"Estimated takeoff mass: `{total_mass:.0f} g`" in report
    assert "99999" not in report and "88888" not in report and "999.0 m/s" not in report
    assert "wiring, fasteners" in report
    assert f"{selected.battery.cells_s}S {selected.battery.capacity_mah}mAh Li-ion" in report
    manifest = json.loads((folder / "selected_components.json").read_text())
    digest = manifest.pop("sha256")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert digest == hashlib.sha256(canonical.encode()).hexdigest()
    assert manifest["performance"]["total_mass_g"] == pytest.approx(total_mass, abs=0.1)
    assert manifest["payload_basis"] == "installed"
    assert manifest["quantities"]["ESC"] == 4
    _write_engineering_deliverables(state, {}, [], None)
    assert json.loads((folder / "selected_components.json").read_text())["sha256"] == digest


def test_preliminary_export_does_not_retain_previous_selected_manifest(tmp_path, monkeypatch):
    from config.settings import settings
    from graph.nodes import _write_engineering_deliverables

    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    selected = _build_selected(get_catalog())
    state = {
        "project_id": "repeat",
        "design_tree": _design().model_dump(),
        "selected_components": selected.model_dump(),
    }
    assert _write_engineering_deliverables(state, {}, [], None)
    folder = tmp_path / "repeat" / "deliverables"
    assert (folder / "selected_components.json").exists()
    state["selected_components"] = None
    assert _write_engineering_deliverables(state, {}, [], None)
    assert not (folder / "selected_components.json").exists()
    assert "Preliminary design only" in (folder / "report.md").read_text(encoding="utf-8")
