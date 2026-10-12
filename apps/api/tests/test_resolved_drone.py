"""Structural artifact identity, mass and transform regressions."""

import hashlib
import json
import math
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from pipelines.json_to_urdf import JSONToURDFConverter
from pipelines.resolved_drone import resolve_drone
from services.component_catalog import get_catalog
from tests.test_component_selector import _build_selected, _design


def inputs(payload=0):
    design = _design()
    design.payload.installed_weight_g = payload
    return design, _build_selected(get_catalog()).model_copy(deep=True)


def build(path, payload=0, single_esc=False):
    design, parts = inputs(payload)
    if single_esc:
        parts.esc = parts.esc.model_copy(update={"channels": 1})
    spec = resolve_drone(design, parts)
    result = JSONToURDFConverter().convert(design.model_dump(), path, resolved_spec=spec)
    return spec, result


@pytest.mark.parametrize("payload", [0, 125])
@pytest.mark.parametrize("single_esc", [False, True])
def test_bom_urdf_sdf_manifest_mass(tmp_path, payload, single_esc):
    spec, result = build(tmp_path, payload, single_esc)
    urdf = ET.parse(result.urdf_path).getroot()
    sdf = ET.parse(result.sdf_path).getroot().find("model")
    expected = spec.components.total_bom_weight_g + payload
    assert sum(
        float(e.attrib["value"]) for e in urdf.findall("link/inertial/mass")
    ) * 1000 == pytest.approx(expected, abs=0.01)
    assert sum(float(e.text) for e in sdf.findall("link/inertial/mass")) * 1000 == pytest.approx(
        expected, abs=1e-5
    )
    assert len([e for e in urdf.findall("link") if e.attrib["name"].startswith("esc")]) == (
        4 if single_esc else 1
    )
    assert (urdf.find("link[@name='payload']") is not None) == bool(payload)
    assert result.validation.valid
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["resolved_spec_sha256"] == spec.sha256
    assert manifest["flight_ready"] is False
    assert manifest["validation_scope"] == "structural_only"
    assert sdf.attrib["name"] == urdf.attrib["name"] == manifest["model_name"]
    for name, digest in manifest["files"].items():
        assert b"\r\n" not in (result.manifest_path.parent / name).read_bytes()
        assert (
            hashlib.sha256((result.manifest_path.parent / name).read_bytes()).hexdigest() == digest
        )


def test_changed_payload_and_battery_change_identity_and_mass(tmp_path):
    design, parts = inputs()
    first = resolve_drone(design, parts)
    design.payload.installed_weight_g = 250
    second = resolve_drone(design, parts)
    assert second.sha256 != first.sha256
    assert second.total_mass_g - first.total_mass_g == pytest.approx(250)
    # Resolve must snapshot parts, not retain a mutable caller-owned object.
    parts = parts.model_copy(deep=True)
    parts.battery.weight_g += 100
    third = resolve_drone(design, parts)
    assert third.sha256 != second.sha256
    assert third.total_mass_g - second.total_mass_g == pytest.approx(100)
    old = JSONToURDFConverter().convert(design.model_dump(), tmp_path, resolved_spec=first)
    new = JSONToURDFConverter().convert(design.model_dump(), tmp_path, resolved_spec=third)
    assert old.urdf_path != new.urdf_path
    assert old.urdf_path.exists() and new.urdf_path.exists()


def test_repeat_generation_is_deterministic(tmp_path):
    spec, result = build(tmp_path)
    content = result.manifest_path.read_bytes()
    _, again = build(tmp_path)
    assert again.spec_sha256 == spec.sha256
    assert again.manifest_path.read_bytes() == content


def test_pose_inertia_and_prop_dimensions(tmp_path):
    spec, result = build(tmp_path)
    urdf = ET.parse(result.urdf_path).getroot()
    sdf = ET.parse(result.sdf_path).getroot().find("model")
    for i in range(4):
        motor = urdf.find(f"joint[@name='motor_{i}_joint']/origin")
        rotor = urdf.find(f"joint[@name='prop_{i}_joint']/origin")
        position = [
            sum(p)
            for p in zip(
                map(float, motor.attrib["xyz"].split()), map(float, rotor.attrib["xyz"].split())
            )
        ]
        target = list(map(float, sdf.find(f"link[@name='prop_{i}']/pose").text.split()))[:3]
        assert position == pytest.approx(target, abs=1e-6)
        assert math.hypot(*position[:2]) == pytest.approx(
            spec.components.frame.arm_length_mm / 1000, abs=1e-6
        )
        radius = float(
            urdf.find(f"link[@name='prop_{i}']/visual/geometry/cylinder").attrib["radius"]
        )
        assert radius == pytest.approx(
            spec.components.propeller.diameter_inch * 0.0254 / 2, abs=0.00005
        )
        origin = urdf.find(f"link[@name='motor_{i}']/inertial/origin").attrib["xyz"]
        assert list(map(float, origin.split())) == [0, 0, 0]
    for e in urdf.findall("link/inertial/inertia"):
        diag = [float(e.attrib[k]) for k in ("ixx", "iyy", "izz")]
        assert all(v > 0 for v in diag)
        assert 2 * max(diag) <= sum(diag) + 1e-8


@pytest.mark.parametrize("change", ["hex", "frame", "propulsion", "body", "overlap"])
def test_unsupported_models_fail(change):
    design, parts = inputs()
    if change == "hex":
        design.topology = "hexacopter"
        parts.motor_count = 6
    elif change == "frame":
        parts.frame.geometry = "H"
    elif change == "propulsion":
        from agents.design_models import MotorSpec

        design.propulsion = [MotorSpec()]
    elif change == "body":
        design.frame.center_body.length = -1
    else:
        parts.frame.arm_length_mm = 20
    with pytest.raises(ValueError):
        resolve_drone(design, parts)


@pytest.mark.asyncio
async def test_graph_and_bom_share_identity(tmp_path, monkeypatch):
    from config.settings import settings
    from graph.nodes import convert_to_urdf, _write_engineering_deliverables

    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    design, parts = inputs(125)
    state = {
        "project_id": "resolved",
        "bom_status": "selected",
        "design_tree": design.model_dump(),
        "selected_components": parts.model_dump(),
    }
    result = await convert_to_urdf(state)
    assert result["conversion_status"] == "complete"
    state.update(result)
    assert state["model_validation_scope"] == "structural_only"
    assert Path(state["model_manifest_path"]).exists()
    assert _write_engineering_deliverables(state, {}, [], None)
    exported = json.loads(
        (tmp_path / "resolved" / "deliverables" / "selected_components.json").read_text()
    )
    assert exported["structural_model_sha256"] == result["resolved_drone_sha256"]
    state["selected_components"]["battery"]["weight_g"] += 100
    assert _write_engineering_deliverables(state, {}, [], None) == []


@pytest.mark.asyncio
async def test_failed_conversion_clears_stale_identity(tmp_path, monkeypatch):
    from config.settings import settings
    from graph.nodes import convert_to_urdf

    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    design, parts = inputs()
    design.topology = "hexacopter"
    result = await convert_to_urdf(
        {
            "project_id": "bad",
            "design_tree": design.model_dump(),
            "selected_components": parts.model_dump(),
            "resolved_drone_sha256": "old",
            "urdf_path": "old.urdf",
        }
    )
    assert result["conversion_status"] == "failed"
    assert result["urdf_path"] is None
    assert result["resolved_drone_sha256"] is None


def test_resolved_fields_are_graph_channels():
    from graph.state import initial_state
    from graph.workflow import _build_graph, _make_memory_saver

    state = initial_state("test", "test")
    graph = _build_graph(_make_memory_saver())
    for name in (
        "resolved_drone_spec",
        "resolved_drone_sha256",
        "model_manifest_path",
        "model_validation_scope",
    ):
        assert name in state
        assert name in graph.channels


def test_spec_snapshots_caller_owned_parts():
    design, parts = inputs()
    spec = resolve_drone(design, parts)
    digest = spec.sha256
    parts.battery.weight_g += 100
    assert spec.sha256 == digest
    assert resolve_drone(design, parts).sha256 != digest


def test_export_rejects_mutated_mass_contract(tmp_path):
    design, parts = inputs()
    spec = resolve_drone(design, parts)
    spec.links[0].mass_kg += 1
    with pytest.raises(ValueError, match="mass does not match"):
        JSONToURDFConverter().convert(design.model_dump(), tmp_path, resolved_spec=spec)


@pytest.mark.asyncio
async def test_selected_bom_cannot_silently_fall_back_to_legacy(tmp_path, monkeypatch):
    from config.settings import settings
    from graph.nodes import convert_to_urdf

    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    result = await convert_to_urdf(
        {"project_id": "missing", "bom_status": "selected", "design_tree": _design().model_dump()}
    )
    assert result["conversion_status"] == "failed"
    assert "refusing a legacy model fallback" in result["errors"][0]


@pytest.mark.asyncio
async def test_failed_exports_are_reported_as_failure(monkeypatch):
    from unittest.mock import AsyncMock
    from graph.nodes import generate_monitor_report

    monkeypatch.setattr(
        "agents.monitor_agent.MonitorAgent.run",
        AsyncMock(return_value={"metrics": {}, "overall_score": 80}),
    )
    monkeypatch.setattr("graph.nodes._write_engineering_deliverables", lambda *a: [])
    result = await generate_monitor_report({"project_id": "failed-export"})
    assert result["deliverables_status"] == "failed"
    assert result["deliverables_paths"] == []
    assert any("deliverables failed" in error for error in result["errors"])
