"""Canonical structural flat X multirotor specification derived from selected catalog parts.

This is a mass-consistent structural approximation, not a calibrated flight model.
Actuator dynamics, sensors and PX4 allocation are intentionally not inferred here.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.component_models import SelectedComponents
from agents.design_models import DroneDesignTree

Multirotor = Literal["quadcopter", "hexacopter", "octocopter"]


class StructuralLink(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    name: str
    parent: str | None = None
    joint_type: Literal["fixed", "continuous"] = "fixed"
    mass_kg: float = Field(gt=0)
    shape: Literal["box", "cylinder"] = "box"
    # Box: x,y,z; cylinder: radius,length,0. Dimensions are in metres.
    dimensions: tuple[float, float, float]
    # Absolute pose in model frame (FLU); link-local geometry/inertia are centered.
    pose: tuple[float, float, float, float, float, float] = (0, 0, 0, 0, 0, 0)

    @model_validator(mode="after")
    def valid_dimensions(self):
        dimensions = self.dimensions if self.shape == "box" else self.dimensions[:2]
        if any(value <= 0 for value in dimensions):
            raise ValueError("Structural dimensions must be positive")
        return self


class ResolvedDroneSpec(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    schema_version: Literal[1] = 1
    validation_scope: Literal["structural_only"] = "structural_only"
    coordinate_frame: Literal["FLU"] = "FLU"
    topology: Multirotor = "quadcopter"
    components: SelectedComponents
    payload_g: float = Field(ge=0)
    payload_basis: Literal["installed", "requested_maximum"]
    links: list[StructuralLink]
    assumptions: list[str]

    @model_validator(mode="after")
    def valid_structure(self):
        names = [link.name for link in self.links]
        if len(names) != len(set(names)) or names.count("base_link") != 1:
            raise ValueError("Structural links must be unique and include base_link")
        parents = {link.name: link.parent for link in self.links}
        if parents["base_link"] is not None:
            raise ValueError("base_link must be the root")
        for name in names:
            seen = set()
            cursor = name
            while cursor != "base_link":
                if cursor in seen or cursor not in parents or parents[cursor] is None:
                    raise ValueError("Structural links must form a tree rooted at base_link")
                seen.add(cursor)
                cursor = parents[cursor]
        if not math.isclose(
            sum(link.mass_kg for link in self.links) * 1000, self.total_mass_g, abs_tol=1e-6
        ):
            raise ValueError("Structural link mass does not match BOM and payload")
        return self

    @property
    def total_mass_g(self) -> float:
        return self.components.total_bom_weight_g + self.payload_g

    @property
    def sha256(self) -> str:
        data = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(data.encode("utf-8")).hexdigest()


# Flat X multirotors: topology → motor count. Tricopters, bicopters and fixed
# wings need tilting rotors or control surfaces and are refused, not approximated.
SUPPORTED_MULTIROTORS = {"quadcopter": 4, "hexacopter": 6, "octocopter": 8}


def motor_positions(count: int, arm_m: float) -> list[tuple[float, float]]:
    """Motor (x, y) in FLU metres on a flat X frame with ``count`` arms.

    Quad-X keeps its historical order and arithmetic so existing Quad-X
    fingerprints are unchanged; larger frames go round the circle from 180°/count.
    """
    if count == 4:
        return [
            (sx * arm_m / math.sqrt(2), sy * arm_m / math.sqrt(2))
            for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1))
        ]
    angles = [math.pi / count + 2 * math.pi * k / count for k in range(count)]
    return [(arm_m * math.cos(a), arm_m * math.sin(a)) for a in angles]


def resolve_drone(design: DroneDesignTree, selected: SelectedComponents) -> ResolvedDroneSpec:
    """Reject unsupported geometry rather than silently generating another airframe."""
    count = SUPPORTED_MULTIROTORS.get(design.topology)
    if (
        count is None
        or selected.motor_count != count
        or selected.frame.geometry != "X"
        or selected.frame.arm_count != count
    ):
        raise ValueError(
            "Resolved structural model supports flat X multirotors only: a quadcopter, "
            "hexacopter or octocopter on a frame with that many arms"
        )
    if design.propulsion and len(design.propulsion) != count:
        raise ValueError(f"{design.topology} propulsion list must contain {count} motors")
    if selected.propeller.diameter_inch > selected.frame.max_prop_size_inch:
        raise ValueError("Selected propeller exceeds the frame size limit")
    # Snapshot and revalidate mutable catalog records before retaining them.
    selected = SelectedComponents.model_validate(selected.model_dump())
    body = design.frame.center_body
    dims = (body.length / 1000, body.width / 1000, body.height / 1000)
    if any(not math.isfinite(v) or v <= 0 for v in dims):
        raise ValueError("Center body dimensions must be finite and positive")
    arm = selected.frame.arm_length_mm / 1000
    radius = selected.propeller.diameter_inch * 0.0254 / 2
    # Adjacent motors on a regular N-arm frame are 2*arm*sin(pi/N) apart.
    if 2 * radius >= 2 * arm * math.sin(math.pi / count):
        raise ValueError(f"Propeller discs overlap on the selected {design.topology} geometry")
    links = [
        StructuralLink(name="base_link", mass_kg=selected.frame.weight_g / 2000, dimensions=dims)
    ]

    def add(
        name,
        mass_g,
        dimensions,
        pose=(0, 0, 0, 0, 0, 0),
        parent="base_link",
        shape="box",
        joint_type="fixed",
    ):
        links.append(
            StructuralLink(
                name=name,
                parent=parent,
                mass_kg=mass_g / 1000,
                dimensions=dimensions,
                pose=pose,
                shape=shape,
                joint_type=joint_type,
            )
        )

    for index, (x, y) in enumerate(motor_positions(count, arm)):
        yaw = math.atan2(y, x)
        add(
            f"arm_{index}",
            selected.frame.weight_g / (2 * count),
            (arm, 0.012, selected.frame.arm_thickness_mm / 1000),
            (x / 2, y / 2, 0, 0, 0, yaw),
        )
        add(
            f"motor_{index}",
            selected.motor.weight_g,
            (selected.motor.stator_diameter_mm / 2000, selected.motor.stator_height_mm / 1000, 0),
            (x, y, 0.02, 0, 0, 0),
            shape="cylinder",
        )
        add(
            f"prop_{index}",
            selected.propeller.weight_g,
            (radius, 0.003, 0),
            (x, y, 0.035, 0, 0, 0),
            parent=f"motor_{index}",
            shape="cylinder",
            joint_type="continuous",
        )
        if selected.esc_quantity == count:
            add(
                f"esc_{index}",
                selected.esc.weight_g,
                (0.03, 0.015, 0.005),
                (x * 0.65, y * 0.65, 0.01, 0, 0, 0),
            )
    if selected.esc_quantity == 1:
        add("esc", selected.esc.weight_g, (0.036, 0.036, 0.005), (0, 0, 0.012, 0, 0, 0))
    add(
        "flight_controller",
        selected.flight_controller.weight_g,
        (0.036, 0.036, 0.005),
        (0, 0, 0.03, 0, 0, 0),
    )
    add("battery", selected.battery.weight_g, (0.075, 0.035, 0.035), (0, 0, 0.06, 0, 0, 0))
    payload = design.payload.design_load_g
    if not math.isfinite(payload) or payload < 0:
        raise ValueError("Payload mass must be finite and non-negative")
    if payload:
        mounts = {
            "bottom": (0, 0, -0.04),
            "gimbal": (0, 0, -0.04),
            "top": (0, 0, 0.10),
            "front": (dims[0] / 2 + 0.02, 0, 0),
        }
        if design.payload.mount_type not in mounts:
            raise ValueError("Unsupported payload mount")
        add("payload", payload, (0.03, 0.03, 0.03), (*mounts[design.payload.mount_type], 0, 0, 0))
    spec = ResolvedDroneSpec(
        # Checked against SUPPORTED_MULTIROTORS above; pydantic checks it again.
        topology=cast(Multirotor, design.topology),
        components=selected,
        payload_g=payload,
        payload_basis="installed"
        if design.payload.installed_weight_g is not None
        else "requested_maximum",
        links=links,
        assumptions=[
            "Structural approximation only: no sensor, motor thrust/torque plugin or PX4 allocation is supplied.",
            f"Frame mass is split 50% into the body and 50% equally into {'four' if count == 4 else count} arms; arm width is assumed 12 mm.",
            "Motor stator dimensions approximate its envelope; battery, ESC, FC and payload shapes/placements are placeholders.",
            "Inertia uses uniform-density primitives, not measured mass distributions; mounting and collision clearances need validation.",
            "Wiring, fasteners, power distribution and external sensors are not included in mass.",
            "Propeller axes use +Z; CW/CCW allocation and thrust curves require a separate validated flight configuration.",
        ],
    )
    if not math.isclose(
        sum(link.mass_kg for link in links) * 1000, spec.total_mass_g, abs_tol=1e-6
    ):
        raise ValueError("Structural link mass does not match the selected BOM and payload")
    return spec
