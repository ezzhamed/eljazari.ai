"""Convert raw AircraftVerse design directories into eljazari.ai design records.

AircraftVerse (NeurIPS 2023, CC-BY-4.0, doi:10.5281/zenodo.6525446) stores each
design as a directory containing:

- ``design_tree.json``      — topology + continuous parameters (arm length, fuselage)
- ``design_low_level.json`` — the symmetry-expanded component list (authoritative
  source for *how many* motors/propellers/batteries a design actually has)
- ``output.json``           — physics-evaluated performance metrics
- ``corpus_dic``            — the component catalogue shared by every design

The tree uses symmetry to avoid repetition, so ``design_tree.json`` lists one
representative arm rather than all of them. Counting motors from the tree — or
from the ``ConnectedHubN_*`` hub name — undercounts or overcounts roughly a
quarter of the corpus, so component counts are always taken from the low-level
representation and component *specifications* are resolved against the corpus.
"""

from __future__ import annotations

import io
import pickle
from typing import Any

# AircraftVerse motor counts map onto the multirotor topologies eljazari.ai
# can actually build a URDF for. Counts outside this table are kept but flagged
# via ``airframe_class`` so retrieval can still surface them.
TOPOLOGY_BY_MOTOR_COUNT = {
    2: "bicopter",
    3: "tricopter",
    4: "quadcopter",
    6: "hexacopter",
    8: "octocopter",
}

MOTOR_POSITIONS = {
    "bicopter": ["left", "right"],
    "tricopter": ["front_left", "front_right", "rear_center"],
    "quadcopter": ["front_left", "front_right", "rear_left", "rear_right"],
    "hexacopter": [
        "front_left",
        "front_right",
        "mid_left",
        "mid_right",
        "rear_left",
        "rear_right",
    ],
    "octocopter": [
        "front_left",
        "front_right",
        "mid_left_front",
        "mid_right_front",
        "mid_left_rear",
        "mid_right_rear",
        "rear_left",
        "rear_right",
    ],
}

# A LiPo cell is nominally 3.7 V; the corpus stores pack voltage.
_LIPO_CELL_VOLTAGE = 3.7
_MAX_CELLS = 12  # BatterySpec.cells is constrained to 1..12


class _SafeUnpickler(pickle.Unpickler):
    """Refuse to import any global — ``corpus_dic`` is third-party data."""

    _ALLOWED = {("builtins", "dict"), ("builtins", "list"), ("builtins", "set")}

    def find_class(self, module: str, name: str):
        if (module, name) in self._ALLOWED:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"blocked global during corpus load: {module}.{name}")


def load_corpus(raw: bytes) -> dict[str, dict[str, dict[str, Any]]]:
    """Load ``corpus_dic`` without allowing arbitrary code execution."""
    return _SafeUnpickler(io.BytesIO(raw)).load()


def _walk(node: Any):
    """Yield every (key, value) pair in a nested JSON structure."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _components_by_type(low_level: dict) -> dict[str, list[str]]:
    """Group ``component_choice`` values by ``component_type``."""
    grouped: dict[str, list[str]] = {}
    for component in low_level.get("components") or []:
        if not isinstance(component, dict):
            continue
        ctype = component.get("component_type")
        choice = component.get("component_choice")
        if ctype:
            grouped.setdefault(str(ctype), []).append(str(choice or ""))
    return grouped


def _arm_length_mm(tree: dict) -> float:
    """Mean arm length across the arms described in the design tree."""
    lengths = [
        float(value)
        for key, value in _walk(tree)
        if key == "armLength" and isinstance(value, (int, float))
    ]
    if not lengths:
        return 0.0
    return round(sum(lengths) / len(lengths), 2)


def _fuselage(tree: dict) -> dict[str, float]:
    for key, value in _walk(tree):
        if key == "fuselage" and isinstance(value, dict):
            return value
    return {}


def _battery_spec(choice: str, corpus: dict, pack_count: int) -> dict[str, Any]:
    spec = corpus.get("Battery", {}).get(choice, {})
    voltage = float(spec.get("VOLTAGE") or 0.0)
    cells = int(round(voltage / _LIPO_CELL_VOLTAGE)) if voltage else 0
    capacity = float(spec.get("CAPACITY") or 0.0)
    weight_kg = float(spec.get("WEIGHT") or 0.0)
    return {
        "cells": max(1, min(cells, _MAX_CELLS)) if cells else 4,
        # Packs in AircraftVerse are wired in parallel, so capacity adds up.
        "capacity_mah": int(round(capacity * pack_count)) if capacity else 5000,
        "weight_g": int(round(weight_kg * 1000 * pack_count)) if weight_kg else 480,
        "part": choice,
        "pack_count": pack_count,
        "voltage_v": round(voltage, 2) if voltage else None,
    }


def _motor_spec(choice: str, corpus: dict) -> dict[str, Any]:
    spec = corpus.get("Motor", {}).get(choice, {})
    return {
        "kv": float(spec.get("KV")) if spec.get("KV") else None,
        "weight_g": round(float(spec["WEIGHT"]) * 1000, 1) if spec.get("WEIGHT") else None,
        "max_power_w": float(spec.get("MAX_POWER")) if spec.get("MAX_POWER") else None,
        "max_current_a": float(spec.get("MAX_CURRENT")) if spec.get("MAX_CURRENT") else None,
    }


def _propeller_spec(choice: str, corpus: dict) -> dict[str, Any]:
    spec = corpus.get("Propeller", {}).get(choice, {})
    return {
        "diameter_mm": round(float(spec["DIAMETER"]), 2) if spec.get("DIAMETER") else None,
        "pitch_mm": round(float(spec["PITCH"]), 2) if spec.get("PITCH") else None,
        "weight_g": round(float(spec["WEIGHT"]) * 1000, 2) if spec.get("WEIGHT") else None,
    }


def _derive_use_case(
    *,
    topology: str,
    has_wing: bool,
    mass_kg: float,
    max_speed_ms: float,
    hover_time_s: float,
    max_distance_m: float,
    prop_diameter_mm: float | None,
) -> str:
    """Derive a searchable use-case label from *measured* design attributes.

    AircraftVerse ships no use-case annotation, so this label is derived rather
    than observed. It is grounded entirely in the physics-evaluated metrics from
    ``output.json`` plus the resolved component geometry.
    """
    if has_wing:
        return "fixed-wing VTOL long range"
    if mass_kg >= 20:
        return "heavy payload transport"
    if mass_kg >= 8:
        return "cargo delivery"
    if max_speed_ms >= 40 and (prop_diameter_mm or 0) < 300:
        return "racing FPV high speed"
    if hover_time_s >= 1800:
        return "endurance surveillance"
    if max_distance_m >= 20000:
        return "long range mapping"
    if topology in {"hexacopter", "octocopter"}:
        return "agriculture spraying"
    return "industrial inspection"


def parse_design(
    *,
    design_id: str,
    tree: dict,
    low_level: dict,
    output: dict | None,
    corpus: dict,
) -> dict[str, Any] | None:
    """Build one eljazari.ai design record. Returns ``None`` if unusable."""
    components = _components_by_type(low_level)
    motors = [c for c in components.get("Motor", []) if c]
    propellers = [c for c in components.get("Propeller", []) if c]
    batteries = [c for c in components.get("Battery", []) if c]

    if not motors:
        return None

    motor_count = len(motors)
    has_wing = bool(components.get("Wing"))
    mapped = TOPOLOGY_BY_MOTOR_COUNT.get(motor_count)

    # Designs outside the buildable multirotor topologies are still indexed —
    # they are real designs and useful for retrieval — but they are labelled
    # truthfully rather than coerced into a supported topology. Silently
    # defaulting here is what collapsed the previous corpus into one design.
    if has_wing:
        topology = f"fixed_wing_{motor_count}_motor"
        airframe_class = "fixed_wing_hybrid"
    elif mapped:
        topology = mapped
        airframe_class = "multirotor"
    else:
        topology = f"multirotor_{motor_count}_motor"
        airframe_class = "multirotor_unsupported"

    positions = MOTOR_POSITIONS.get(topology, [])
    propulsion = []
    for index in range(motor_count):
        motor_choice = motors[index]
        prop_choice = (
            propellers[index] if index < len(propellers) else (propellers[0] if propellers else "")
        )
        entry: dict[str, Any] = {
            "motor": motor_choice,
            "propeller": prop_choice,
            "position": positions[index] if index < len(positions) else f"arm_{index + 1}",
        }
        entry.update({k: v for k, v in _motor_spec(motor_choice, corpus).items() if v is not None})
        prop_spec = _propeller_spec(prop_choice, corpus)
        if prop_spec.get("diameter_mm"):
            entry["prop_diameter_mm"] = prop_spec["diameter_mm"]
        if prop_spec.get("pitch_mm"):
            entry["prop_pitch_mm"] = prop_spec["pitch_mm"]
        propulsion.append(entry)

    battery = (
        _battery_spec(batteries[0], corpus, len(batteries))
        if batteries
        else {
            "cells": 4,
            "capacity_mah": 5000,
            "weight_g": 480,
            "part": "",
            "pack_count": 0,
            "voltage_v": None,
        }
    )

    fuselage = _fuselage(tree)
    arm_length = _arm_length_mm(tree)

    performance = {k: v for k, v in (output or {}).items() if isinstance(v, (int, float))}
    mass_kg = float(performance.get("Mass") or 0.0)
    # A design that failed physics evaluation reports zeroed metrics.
    evaluated = float(performance.get("Max_Distance") or 0.0) > 0

    # Payload is the cargo the airframe carries; AircraftVerse encodes it as a
    # Cargo component rather than a mass, so fall back to a mass-fraction
    # estimate when the catalogue has no entry.
    cargo_count = len(components.get("Cargo", []))
    payload_g = int(round(mass_kg * 1000 * 0.25)) if cargo_count and mass_kg else 0

    sensors = " ".join(components.get("Sensor", [])).lower()

    record = {
        "design_id": design_id,
        "topology": topology,
        "airframe_class": airframe_class,
        "use_case": _derive_use_case(
            topology=topology,
            has_wing=has_wing,
            mass_kg=mass_kg,
            max_speed_ms=float(performance.get("Max_Speed") or 0.0),
            hover_time_s=float(performance.get("Hover_Time") or 0.0),
            max_distance_m=float(performance.get("Max_Distance") or 0.0),
            prop_diameter_mm=propulsion[0].get("prop_diameter_mm") if propulsion else None,
        ),
        "frame": {
            "type": "X",
            "arm_length_mm": int(round(arm_length)) if arm_length else 250,
            "motor_count": motor_count,
            "fuselage_length_mm": fuselage.get("length"),
            "fuselage_vert_diameter_mm": fuselage.get("vertDiameter"),
            "fuselage_horz_diameter_mm": fuselage.get("horzDiameter"),
            "has_wing": has_wing,
        },
        "battery": battery,
        "propulsion": propulsion,
        "electronics": {
            "flight_controller": "PX4",
            "gps": "gps" in sensors,
            "compass": "gps" in sensors,
            "sensor_count": len(components.get("Sensor", [])),
        },
        "payload": {
            "max_weight_g": payload_g,
            "mount_type": "bottom",
            "cargo_count": cargo_count,
        },
        "mass_kg": round(mass_kg, 4) if mass_kg else None,
        "evaluated": evaluated,
        "performance_simulated": performance,
    }
    return record
