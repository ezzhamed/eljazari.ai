"""Build code-generation context from the same BOM as the structural model."""

from copy import deepcopy
from typing import Any

from agents.component_models import SelectedComponents
from agents.design_models import DroneDesignTree
from pipelines.resolved_drone import ResolvedDroneSpec, resolve_drone


def codegen_design_context(state: dict[str, Any]) -> dict[str, Any]:
    original = deepcopy(state.get("design_tree") or {})
    selected = state.get("selected_components")
    if not selected:
        if state.get("bom_status") == "selected" or state.get("resolved_drone_sha256"):
            raise ValueError("Selected components missing from structural model context")
        return original

    design = DroneDesignTree.model_validate(original)
    original = design.model_dump()
    parts = SelectedComponents.model_validate(selected)
    expected = resolve_drone(design, parts)
    stored = ResolvedDroneSpec.model_validate(state.get("resolved_drone_spec"))
    if expected.sha256 != state.get("resolved_drone_sha256") or stored.sha256 != expected.sha256:
        raise ValueError("Selected components or payload changed after structural model generation")

    original["battery"] = {
        "type": parts.battery.chemistry,
        "cells": parts.battery.cells_s,
        "capacity_mah": parts.battery.capacity_mah,
        "weight_g": parts.battery.weight_g,
    }
    original["frame"].update(type=parts.frame.geometry, arm_length_mm=parts.frame.arm_length_mm)
    original["payload"]["installed_weight_g"] = expected.payload_g
    for motor in original.get("propulsion", []):
        motor.update(motor=parts.motor.model_name, propeller=parts.propeller.model_name)
    original["electronics"]["flight_controller"] = parts.flight_controller.model_name
    original["catalog_context"] = {
        "resolved_drone_sha256": expected.sha256,
        "total_mass_g": expected.total_mass_g,
        "payload_basis": expected.payload_basis,
        "validation_scope": expected.validation_scope,
        "flight_ready": False,
        "parts": {
            key: getattr(parts, key).part_number
            for key in ("motor", "propeller", "battery", "esc", "flight_controller", "frame")
        },
    }
    return original
