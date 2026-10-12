"""PX4/Gazebo flight model derived from one ResolvedDroneSpec.

Scope ``sitl_catalog_estimate``: every flight parameter is computed from the
selected BOM, its (unverified) catalog thrust curve and the declared
assumptions below. The bundle lets PX4 SITL fly *this* aircraft instead of the
reference x500; it is not a calibrated or airworthy model.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Literal
from xml.dom import minidom
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from pipelines.propulsion_evidence import (
    VOLTAGE_ABS_TOLERANCE_V,
    assess_propulsion_evidence,
    validate_curve,
)
from pipelines.resolved_drone import (
    SUPPORTED_MULTIROTORS,
    ResolvedDroneSpec,
    StructuralLink,
    motor_positions,
)

FLIGHT_SCOPE = "sitl_catalog_estimate"
FLIGHT_SCHEMA_VERSION = 1
# PX4 v1.17 generic Quad-X airframe (gz_x500); rotor count and geometry, motor
# scaling and rate-loop gains are overridden per design through PX4_PARAM_*
# variables, so hexa- and octocopters use the same base (CA_AIRFRAME 0).
PX4_BASE_AUTOSTART = 4001
_MODEL_PREFIX = {4: "quadfm", 6: "hexafm", 8: "octofm"}
_LAYOUT_LABEL = {4: "Quad-X", 6: "Hexa-X", 8: "Octo-X"}
# SIM_GZ_EC_MAX is capped at 1000 by PX4's gz_bridge module.yaml, so motor
# commands use a normalised unit: command 1000 == catalog maximum RPM.
COMMAND_MAX = 1000.0
COMMAND_IDLE = 150.0  # PX4 x500 SIM_GZ_EC_MIN, i.e. 15 % of maximum speed
GRAVITY = 9.80665
MOTOR_EFFICIENCY = 0.8  # shaft/electrical power, used only for the yaw moment ratio
SPAWN_CLEARANCE_M = 0.01
MASS_ATTESTATION_TOLERANCE_KG = 1e-4

# PX4 Quad-X motor order. Signs are Gazebo FLU (y left); PX4 params are FRD.
Direction = Literal["cw", "ccw"]
_PX4_QUAD_X: tuple[tuple[int, int, Direction], ...] = (
    (1, -1, "ccw"),
    (-1, 1, "ccw"),
    (1, 1, "cw"),
    (-1, -1, "cw"),
)

# PX4 v1.17 Tools/simulation/gz/models/x500{,_base}/model.sdf and airframe
# 4001_gz_x500. Only used to scale rate-loop gains to the same loop gain.
_X500: dict[str, Any] = {
    "base_mass": 2.0,
    "base_inertia": (0.02166666666666667, 0.02166666666666667, 0.04000000000000001),
    "rotor_mass": 0.016076923076923075,
    "rotor_inertia": (3.8464910483993325e-07, 2.6115851691700804e-05, 2.649858234714004e-05),
    "rotor_xy": 0.174,
    "rotor_z": 0.06,
    "motor_constant": 8.54858e-06,
    "max_rot_velocity": 1000.0,
    "moment_constant": 0.016,
    # CA_ROTORn_PX/PY/KM as set by the airframe (FRD), in PX4 motor order.
    "ca_rotors": (
        (0.13, 0.22, 0.05),
        (-0.13, -0.20, 0.05),
        (0.13, -0.22, -0.05),
        (-0.13, 0.20, -0.05),
    ),
}

# x500 plugin values reused unchanged; see the manifest assumptions.
_TIME_CONSTANT_UP = 0.0125
_TIME_CONSTANT_DOWN = 0.025
_ROTOR_DRAG = 8.06428e-05
_ROLLING_MOMENT = 1e-06
_SLOWDOWN = 10
_RATE_K_RANGE = (0.01, 5.0)  # PX4 MC_*RATE_K parameter bounds
_THR_HOVER_RANGE = (0.1, 0.8)  # PX4 MPC_THR_HOVER parameter bounds


class RotorModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    px4_index: int = Field(ge=0, le=7)
    structural_link: str
    position_flu_m: tuple[float, float, float]
    turning_direction: Literal["cw", "ccw"]


class MotorFit(BaseModel):
    """Quadratic T = k * omega^2 through the catalog points at one voltage."""

    model_config = ConfigDict(allow_inf_nan=False)
    curve_voltage_v: float
    thrust_coefficient_n_per_rad2_s2: float = Field(gt=0)
    max_rpm: float = Field(gt=0)
    max_thrust_n: float = Field(gt=0)
    max_relative_fit_error: float = Field(ge=0)
    moment_constant_m: float = Field(gt=0)
    moment_constant_source: str


class FlightModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    schema_version: Literal[1] = 1
    validation_scope: Literal["sitl_catalog_estimate"] = "sitl_catalog_estimate"
    resolved_spec_sha256: str
    model_name: str
    total_mass_kg: float = Field(gt=0)
    body_mass_kg: float = Field(gt=0)
    body_com_m: tuple[float, float, float]
    # ixx, iyy, izz, ixy, ixz, iyz about the body centre of mass
    body_inertia: tuple[float, float, float, float, float, float]
    rotor_mass_kg: float = Field(gt=0)
    rotor_inertia: tuple[float, float, float]
    rotors: list[RotorModel]
    motor: MotorFit
    motor_constant_command: float = Field(gt=0)
    thrust_to_weight: float = Field(gt=0)
    hover_thrust_fraction: float = Field(gt=0, lt=1)
    hover_command: float
    spawn_height_m: float = Field(gt=0)
    rate_gain_scale: dict[str, float]
    px4_params: dict[str, float | int]
    warnings: list[str]
    assumptions: list[str]


def _local_inertia(link: StructuralLink) -> tuple[float, float, float]:
    m, (a, b, c) = link.mass_kg, link.dimensions
    if link.shape == "box":
        return (m / 12 * (b * b + c * c), m / 12 * (a * a + c * c), m / 12 * (a * a + b * b))
    radial = m / 12 * (3 * a * a + b * b)
    return (radial, radial, m / 2 * a * a)


def _rotated_inertia(diag: tuple[float, float, float], yaw: float) -> list[list[float]]:
    c, s = math.cos(yaw), math.sin(yaw)
    rot = [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]
    return [
        [sum(rot[i][k] * diag[k] * rot[j][k] for k in range(3)) for j in range(3)] for i in range(3)
    ]


def _composite(parts: list[tuple[float, tuple[float, float, float], list[list[float]]]]):
    """Mass, centre of mass and inertia about it for rigidly joined bodies."""
    mass = sum(m for m, _, _ in parts)
    com = tuple(sum(m * p[i] for m, p, _ in parts) / mass for i in range(3))
    total = [[0.0] * 3 for _ in range(3)]
    for m, p, inertia in parts:
        d = [p[i] - com[i] for i in range(3)]
        d2 = sum(v * v for v in d)
        for i in range(3):
            for j in range(3):
                total[i][j] += inertia[i][j] + m * ((d2 if i == j else 0.0) - d[i] * d[j])
    return mass, com, total


def _invert(matrix: list[list[float]]) -> list[list[float]]:
    n = len(matrix)
    aug = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("Control effectiveness matrix is singular")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        scale = aug[col][col]
        aug[col] = [v / scale for v in aug[col]]
        for row in range(n):
            if row != col:
                factor = aug[row][col]
                aug[row] = [v - factor * w for v, w in zip(aug[row], aug[col])]
    return [row[n:] for row in aug]


def _normalized_mix(ca_rotors) -> list[list[float]]:
    """PX4 pseudo-inverse allocation with its multicopter RPY normalization.

    Mirrors ActuatorEffectivenessRotors (roll -py, pitch px, yaw km per unit
    thrust coefficient) and ControlAllocationPseudoInverse's scaling in PX4
    v1.17. Returns mix[rotor][axis] for roll, pitch, yaw.
    """
    effectiveness = [[-py for _, py, _ in ca_rotors], [px for px, _, _ in ca_rotors]]
    effectiveness += [[km for _, _, km in ca_rotors], [-1.0 for _ in ca_rotors]]
    count = len(ca_rotors)
    if count == 4:
        # B is 4x4 (roll, pitch, yaw, thrust); pinv == inverse when full rank.
        mix = _invert(effectiveness)
    else:
        # Moore-Penrose pseudo-inverse of the 4xN matrix: B^T (B B^T)^-1.
        gram = [
            [sum(a * b for a, b in zip(row_i, row_j)) for row_j in effectiveness]
            for row_i in effectiveness
        ]
        inverse = _invert(gram)
        mix = [
            [sum(effectiveness[k][r] * inverse[k][c] for k in range(4)) for c in range(4)]
            for r in range(count)
        ]

    def column(axis):
        return [mix[i][axis] for i in range(count)]

    def rp_scale(axis):
        values = column(axis)
        nonzero = sum(1 for v in values if abs(v) > 1e-3)
        return math.sqrt(sum(v * v for v in values) / (nonzero / 2)) if nonzero else 1.0

    rp = max(rp_scale(0), rp_scale(1))
    yaw = max(column(2))
    return [[mix[i][0] / rp, mix[i][1] / rp, mix[i][2] / yaw] for i in range(count)]


def _torque_per_command(ca_rotors, physical, thrust_slope_n):
    """Physical torque for a unit normalised command on each axis (N m).

    ``physical`` holds (px, py, yaw moment ratio) per rotor in FRD metres.
    """
    mix = _normalized_mix(ca_rotors)
    torques = []
    for axis in range(3):
        total = 0.0
        for i, (px, py, km) in enumerate(physical):
            lever = (-py, px, km)[axis]
            total += lever * mix[i][axis] * thrust_slope_n
        torques.append(abs(total))
    return torques


def _thrust_slope(max_thrust_n: float, hover_fraction: float) -> float:
    """dT/du at hover for u in [0, 1] mapped linearly onto [idle, max] speed."""
    speed = math.sqrt(hover_fraction)
    return 2 * speed * (1 - COMMAND_IDLE / COMMAND_MAX) * max_thrust_n


def _x500_effectiveness() -> tuple[float, float, float]:
    ref = _X500
    rotor_parts = []
    physical = []
    for sx, sy, direction in _PX4_QUAD_X:
        pos = (sx * ref["rotor_xy"], sy * ref["rotor_xy"], ref["rotor_z"])
        inertia = [[ref["rotor_inertia"][i] if i == j else 0.0 for j in range(3)] for i in range(3)]
        rotor_parts.append((ref["rotor_mass"], pos, inertia))
        km = ref["moment_constant"] * (1 if direction == "ccw" else -1)
        physical.append((pos[0], -pos[1], km))
    base = [[ref["base_inertia"][i] if i == j else 0.0 for j in range(3)] for i in range(3)]
    mass, _, inertia = _composite([(ref["base_mass"], (0.0, 0.0, 0.0), base), *rotor_parts])
    max_thrust = ref["motor_constant"] * ref["max_rot_velocity"] ** 2
    hover = mass * GRAVITY / 4 / max_thrust
    torques = _torque_per_command(ref["ca_rotors"], physical, _thrust_slope(max_thrust, hover))
    return tuple(torques[i] / inertia[i][i] for i in range(3))


def fit_motor(spec: ResolvedDroneSpec) -> MotorFit:
    parts = spec.components
    points = parts.motor.thrust_curve
    validate_curve(points)
    voltages = {round(p.voltage_v, 3) for p in points}
    if len(voltages) != 1:
        raise ValueError("Thrust curve mixes test voltages; no single operating condition")
    voltage = points[0].voltage_v
    nominal = parts.battery.pack_voltage_nominal_v
    if not math.isclose(voltage, nominal, abs_tol=VOLTAGE_ABS_TOLERANCE_V, rel_tol=0):
        raise ValueError(
            f"Thrust curve was recorded at {voltage:g} V but the selected battery is "
            f"{nominal:g} V nominal; refusing to reuse the curve at another voltage"
        )
    rated = [p for p in points if p.rpm > 0 and p.thrust_g > 0]
    if len(rated) < 2:
        raise ValueError("At least two operating points with RPM and thrust are required")
    if any(p.rpm > parts.propeller.max_rpm_safe for p in rated):
        raise ValueError("Thrust curve RPM exceeds the selected propeller rating")
    omegas = [p.rpm * 2 * math.pi / 60 for p in rated]
    thrusts = [p.thrust_g * GRAVITY / 1000 for p in rated]
    k = sum(t * w * w for t, w in zip(thrusts, omegas)) / sum(w**4 for w in omegas)
    errors = [abs(k * w * w - t) / t for t, w in zip(thrusts, omegas)]
    ratios = [
        MOTOR_EFFICIENCY * p.power_w / (w * t)
        for p, w, t in zip(rated, omegas, thrusts)
        if p.power_w > 0
    ]
    if not ratios:
        raise ValueError("Thrust curve has no power data for the yaw moment ratio")
    moment = sum(ratios) / len(ratios)
    if not 1e-3 <= moment <= 0.1:
        raise ValueError(f"Derived yaw moment ratio {moment:.4g} m is implausible")
    max_rpm = max(p.rpm for p in rated)
    omega_max = max_rpm * 2 * math.pi / 60
    return MotorFit(
        curve_voltage_v=voltage,
        thrust_coefficient_n_per_rad2_s2=k,
        max_rpm=max_rpm,
        max_thrust_n=k * omega_max**2,
        max_relative_fit_error=max(errors),
        moment_constant_m=moment,
        moment_constant_source=(
            f"mean of {MOTOR_EFFICIENCY:g} * P_electrical / (omega * T) over catalog points"
        ),
    )


def _assign_rotors(props: list[StructuralLink], count: int) -> list[RotorModel]:
    """PX4 index and spin direction for each propeller link.

    Quad-X keeps PX4's x500 motor order. Larger frames number the arms round the
    circle from 180 deg/count with alternating spin, so every neighbour pair
    counter-rotates; PX4 learns the order from the CA_ROTORn parameters.
    """
    rotors = []
    if count == 4:
        for index, (sx, sy, direction) in enumerate(_PX4_QUAD_X):
            match = [
                p
                for p in props
                if math.copysign(1, p.pose[0]) == sx and math.copysign(1, p.pose[1]) == sy
            ]
            if len(match) != 1 or abs(match[0].pose[0]) < 1e-6 or abs(match[0].pose[1]) < 1e-6:
                raise ValueError("Propeller positions do not form a Quad-X layout")
            rotors.append(
                RotorModel(
                    px4_index=index,
                    structural_link=match[0].name,
                    position_flu_m=match[0].pose[:3],
                    turning_direction=direction,
                )
            )
        return rotors
    radius = math.hypot(props[0].pose[0], props[0].pose[1])
    for index, (x, y) in enumerate(motor_positions(count, radius)):
        match = [p for p in props if math.hypot(p.pose[0] - x, p.pose[1] - y) < 1e-6]
        if len(match) != 1:
            raise ValueError(f"Propeller positions do not form a {_LAYOUT_LABEL[count]} layout")
        rotors.append(
            RotorModel(
                px4_index=index,
                structural_link=match[0].name,
                position_flu_m=match[0].pose[:3],
                turning_direction="ccw" if index % 2 == 0 else "cw",
            )
        )
    return rotors


def derive_flight_model(spec: ResolvedDroneSpec) -> FlightModel:
    """Fail closed for anything the flat X multirotor template cannot represent."""
    spec = ResolvedDroneSpec.model_validate(spec.model_dump())
    count = SUPPORTED_MULTIROTORS.get(spec.topology)
    if count is None or spec.components.motor_count != count:
        raise ValueError(
            "Flight model generation supports flat X quadcopters, hexacopters and octocopters"
        )
    # SITL flies documented propulsion data only: a curve published for exactly
    # this motor, propeller and battery voltage. Unsourced curves stay usable for
    # preliminary sizing but never become a flight model.
    evidence = assess_propulsion_evidence(spec.components)
    if evidence.status != "matched_declaration":
        raise ValueError(
            "No published propulsion test for this motor, propeller and battery voltage; "
            "generated-aircraft SITL flies documented combinations only ("
            + "; ".join(evidence.reasons)
            + ")"
        )
    digest = spec.sha256
    props = [link for link in spec.links if link.name.startswith("prop_")]
    if len(props) != count or any(link.joint_type != "continuous" for link in props):
        raise ValueError(f"{_LAYOUT_LABEL[count]} flight model requires {count} propeller links")
    rotors = _assign_rotors(props, count)
    fixed = [link for link in spec.links if link not in props]
    for link in fixed:
        if link.joint_type != "fixed" or any(link.pose[3:5]):
            raise ValueError(f"Link {link.name} cannot be merged into the rigid body")
    body_parts = [
        (link.mass_kg, link.pose[:3], _rotated_inertia(_local_inertia(link), link.pose[5]))
        for link in fixed
    ]
    body_mass, body_com, body_inertia = _composite(body_parts)
    prop = props[0]
    if any(
        not math.isclose(p.mass_kg, prop.mass_kg) or p.dimensions != prop.dimensions for p in props
    ):
        raise ValueError("The flight model requires identical propellers")
    rotor_inertia = _local_inertia(prop)
    total_mass = body_mass + sum(p.mass_kg for p in props)
    if not math.isclose(total_mass * 1000, spec.total_mass_g, abs_tol=1e-6):
        raise ValueError("Flight model mass does not match the resolved BOM and payload")

    motor = fit_motor(spec)
    hover = total_mass * GRAVITY / count / motor.max_thrust_n
    if hover >= 1:
        raise ValueError("Selected propulsion cannot hover this mass at the catalog maximum RPM")
    idle_fraction = (COMMAND_IDLE / COMMAND_MAX) ** 2
    if hover <= idle_fraction:
        raise ValueError("Idle motor speed would already exceed hover thrust")
    warnings = []
    hover_rpm = math.sqrt(hover) * motor.max_rpm
    min_rpm = min(p.rpm for p in spec.components.motor.thrust_curve if p.rpm > 0)
    if hover_rpm < min_rpm:
        warnings.append(
            f"Hover (~{hover_rpm:.0f} RPM) lies below the lowest catalog point ({min_rpm} RPM); "
            "hover thrust is extrapolated by the quadratic fit."
        )
    if motor.max_relative_fit_error > 0.1:
        warnings.append(
            f"Quadratic thrust fit deviates up to {motor.max_relative_fit_error:.0%} "
            "from catalog points."
        )
    hover_command = (math.sqrt(hover) * COMMAND_MAX - COMMAND_IDLE) / (COMMAND_MAX - COMMAND_IDLE)
    thr_hover = min(max(hover_command, _THR_HOVER_RANGE[0]), _THR_HOVER_RANGE[1])
    if thr_hover != hover_command:
        warnings.append(
            f"Estimated hover command {hover_command:.3f} is outside PX4 MPC_THR_HOVER bounds; "
            f"clamped to {thr_hover:.2f} and left to the hover thrust estimator."
        )

    # PX4 FRD positions and yaw moment ratio (positive = CCW) per rotor.
    physical = []
    for rotor in rotors:
        x, y, _ = rotor.position_flu_m
        km = motor.moment_constant_m * (1 if rotor.turning_direction == "ccw" else -1)
        physical.append((x, -y, km))
    rotor_parts = [
        (
            prop.mass_kg,
            rotor.position_flu_m,
            [[rotor_inertia[i] if i == j else 0.0 for j in range(3)] for i in range(3)],
        )
        for rotor in rotors
    ]
    _, _, vehicle_inertia = _composite([(body_mass, body_com, body_inertia), *rotor_parts])
    torques = _torque_per_command(physical, physical, _thrust_slope(motor.max_thrust_n, hover))
    ours = [torques[i] / vehicle_inertia[i][i] for i in range(3)]
    reference = _x500_effectiveness()
    scale = {}
    for axis, name in enumerate(("roll", "pitch", "yaw")):
        raw = reference[axis] / ours[axis]
        clamped = min(max(raw, _RATE_K_RANGE[0]), _RATE_K_RANGE[1])
        if clamped != raw:
            warnings.append(f"{name} rate gain scale {raw:.3g} clamped to PX4 bounds")
        scale[name] = round(clamped, 4)

    params: dict[str, float | int] = {"CA_ROTOR_COUNT": count}
    for rotor, (px, py, km) in zip(rotors, physical):
        i = rotor.px4_index
        params.update(
            {
                f"CA_ROTOR{i}_PX": round(px, 6),
                f"CA_ROTOR{i}_PY": round(py, 6),
                f"CA_ROTOR{i}_KM": round(km, 6),
                f"SIM_GZ_EC_MIN{i + 1}": int(COMMAND_IDLE),
                f"SIM_GZ_EC_MAX{i + 1}": int(COMMAND_MAX),
            }
        )
        if i >= 4:
            # The x500 airframe maps outputs 1-4 to motors; map the rest too.
            params[f"SIM_GZ_EC_FUNC{i + 1}"] = 101 + i
    battery = spec.components.battery
    params.update(
        {
            "MC_ROLLRATE_K": scale["roll"],
            "MC_PITCHRATE_K": scale["pitch"],
            "MC_YAWRATE_K": scale["yaw"],
            "MPC_THR_HOVER": round(thr_hover, 4),
            "BAT1_N_CELLS": battery.cells_s,
            "BAT1_CAPACITY": float(battery.capacity_mah),
        }
    )

    fixed_bottom = min(link.pose[2] - _half_height(link) for link in fixed)
    return FlightModel(
        resolved_spec_sha256=digest,
        model_name=f"{_MODEL_PREFIX[count]}_{digest[:16]}",
        total_mass_kg=total_mass,
        body_mass_kg=body_mass,
        body_com_m=body_com,
        body_inertia=(
            body_inertia[0][0],
            body_inertia[1][1],
            body_inertia[2][2],
            body_inertia[0][1],
            body_inertia[0][2],
            body_inertia[1][2],
        ),
        rotor_mass_kg=prop.mass_kg,
        rotor_inertia=rotor_inertia,
        rotors=rotors,
        motor=motor,
        motor_constant_command=motor.max_thrust_n / COMMAND_MAX**2,
        thrust_to_weight=count * motor.max_thrust_n / (total_mass * GRAVITY),
        hover_thrust_fraction=hover,
        hover_command=hover_command,
        spawn_height_m=-fixed_bottom + SPAWN_CLEARANCE_M,
        rate_gain_scale=scale,
        px4_params=params,
        warnings=warnings,
        assumptions=[
            "Validation scope is sitl_catalog_estimate: catalog curves are unverified and "
            "no bench or flight calibration has been applied.",
            "Thrust uses T = k*omega^2 least-squares fitted to the catalog RPM/thrust points "
            "at the curve voltage; command 1000 equals the catalog maximum RPM.",
            f"Yaw moment ratio assumes {MOTOR_EFFICIENCY:g} motor efficiency on catalog power.",
            "Motor time constants, rotor drag and rolling moment coefficients and sensor noise "
            "reuse PX4 x500 reference values; they are not measured for these parts.",
            "Rate-loop gains are PX4 defaults scaled so loop gain at hover matches the x500 "
            "reference; they are not tuned for this airframe.",
            "All non-rotating structural links are merged into one rigid body; motor bells "
            "are part of that body and propellers are uniform discs.",
            "IMU, barometer, magnetometer and GNSS use PX4 simulation sensor models, not the "
            "selected flight controller's sensors.",
        ],
    )


def _half_height(link: StructuralLink) -> float:
    return link.dimensions[2] / 2 if link.shape == "box" else link.dimensions[1] / 2


def _fmt(values) -> str:
    return " ".join(f"{v:.10g}" for v in values)


def _geometry(parent: ET.Element, link: StructuralLink) -> None:
    geometry = ET.SubElement(parent, "geometry")
    if link.shape == "box":
        ET.SubElement(ET.SubElement(geometry, "box"), "size").text = _fmt(link.dimensions)
    else:
        shape = ET.SubElement(geometry, "cylinder")
        ET.SubElement(shape, "radius").text = f"{link.dimensions[0]:.10g}"
        ET.SubElement(shape, "length").text = f"{link.dimensions[1]:.10g}"


def _noise(parent: ET.Element, stddev: float, mean: bool = True) -> None:
    noise = ET.SubElement(parent, "noise", type="gaussian")
    if mean:
        ET.SubElement(noise, "mean").text = "0"
    ET.SubElement(noise, "stddev").text = f"{stddev:g}"


def _add_sensors(link: ET.Element) -> None:
    """PX4 x500_base sensor set; gz_bridge subscribes to these exact names."""

    def sensor(name, kind, rate):
        element = ET.SubElement(link, "sensor", name=name, type=kind)
        ET.SubElement(element, "gz_frame_id").text = "base_link"
        ET.SubElement(element, "always_on").text = "1"
        ET.SubElement(element, "update_rate").text = str(rate)
        return element

    baro = ET.SubElement(
        ET.SubElement(sensor("air_pressure_sensor", "air_pressure", 50), "air_pressure"), "pressure"
    )
    _noise(baro, 3)
    mag = ET.SubElement(sensor("magnetometer_sensor", "magnetometer", 100), "magnetometer")
    for axis in "xyz":
        _noise(ET.SubElement(mag, axis), 0.0001, mean=False)
    imu = ET.SubElement(sensor("imu_sensor", "imu", 250), "imu")
    gyro = ET.SubElement(imu, "angular_velocity")
    accel = ET.SubElement(imu, "linear_acceleration")
    for axis in "xyz":
        _noise(ET.SubElement(gyro, axis), 0.0008726646)
        _noise(ET.SubElement(accel, axis), 0.00686 if axis == "z" else 0.00637)
    sensor("navsat_sensor", "navsat", 30)


def _contact(collision: ET.Element) -> None:
    ode = ET.SubElement(ET.SubElement(ET.SubElement(collision, "surface"), "contact"), "ode")
    ET.SubElement(ode, "min_depth").text = "0.001"
    ET.SubElement(ode, "max_vel").text = "0"


def build_sdf(model: FlightModel, spec: ResolvedDroneSpec) -> ET.Element:
    sdf = ET.Element("sdf", version="1.9")
    sdf.append(
        ET.Comment(
            f" {FLIGHT_SCOPE}; resolved_spec_sha256={model.resolved_spec_sha256}; "
            "catalog-derived SITL model, not airworthiness evidence "
        )
    )
    root = ET.SubElement(sdf, "model", name=model.model_name)
    ET.SubElement(root, "pose").text = _fmt((0, 0, model.spawn_height_m, 0, 0, 0))
    ET.SubElement(root, "self_collide").text = "false"
    ET.SubElement(root, "static").text = "false"
    base = ET.SubElement(root, "link", name="base_link")
    inertial = ET.SubElement(base, "inertial")
    ET.SubElement(inertial, "pose").text = _fmt((*model.body_com_m, 0, 0, 0))
    ET.SubElement(inertial, "mass").text = f"{model.body_mass_kg:.10g}"
    inertia = ET.SubElement(inertial, "inertia")
    for key, value in zip(("ixx", "iyy", "izz", "ixy", "ixz", "iyz"), model.body_inertia):
        ET.SubElement(inertia, key).text = f"{value:.12g}"
    rotor_links = {rotor.structural_link for rotor in model.rotors}
    for link in spec.links:
        if link.name in rotor_links:
            continue
        for kind in ("visual", "collision"):
            element = ET.SubElement(base, kind, name=f"{link.name}_{kind}")
            ET.SubElement(element, "pose").text = _fmt(link.pose)
            _geometry(element, link)
            if kind == "collision":
                _contact(element)
    _add_sensors(base)
    by_name = {link.name: link for link in spec.links}
    for rotor in model.rotors:
        name = f"rotor_{rotor.px4_index}"
        rotor_link = ET.SubElement(root, "link", name=name)
        ET.SubElement(rotor_link, "pose").text = _fmt((*rotor.position_flu_m, 0, 0, 0))
        inertial = ET.SubElement(rotor_link, "inertial")
        ET.SubElement(inertial, "mass").text = f"{model.rotor_mass_kg:.10g}"
        inertia = ET.SubElement(inertial, "inertia")
        for key, value in zip(("ixx", "iyy", "izz"), model.rotor_inertia):
            ET.SubElement(inertia, key).text = f"{value:.12g}"
        visual = ET.SubElement(rotor_link, "visual", name=f"{name}_visual")
        _geometry(visual, by_name[rotor.structural_link])
        joint = ET.SubElement(root, "joint", name=f"{name}_joint", type="revolute")
        ET.SubElement(joint, "parent").text = "base_link"
        ET.SubElement(joint, "child").text = name
        axis = ET.SubElement(joint, "axis")
        ET.SubElement(axis, "xyz").text = "0 0 1"
        limit = ET.SubElement(axis, "limit")
        ET.SubElement(limit, "lower").text = "-1e+16"
        ET.SubElement(limit, "upper").text = "1e+16"
    for rotor in model.rotors:
        plugin = ET.SubElement(
            root,
            "plugin",
            filename="gz-sim-multicopter-motor-model-system",
            name="gz::sim::systems::MulticopterMotorModel",
        )
        values = {
            "jointName": f"rotor_{rotor.px4_index}_joint",
            "linkName": f"rotor_{rotor.px4_index}",
            "turningDirection": rotor.turning_direction,
            "timeConstantUp": f"{_TIME_CONSTANT_UP:g}",
            "timeConstantDown": f"{_TIME_CONSTANT_DOWN:g}",
            "maxRotVelocity": f"{COMMAND_MAX:.1f}",
            "motorConstant": f"{model.motor_constant_command:.10g}",
            "momentConstant": f"{model.motor.moment_constant_m:.10g}",
            "commandSubTopic": "command/motor_speed",
            "motorNumber": str(rotor.px4_index),
            "rotorDragCoefficient": f"{_ROTOR_DRAG:g}",
            "rollingMomentCoefficient": f"{_ROLLING_MOMENT:g}",
            "rotorVelocitySlowdownSim": str(_SLOWDOWN),
            "motorType": "velocity",
        }
        for key, text in values.items():
            ET.SubElement(plugin, key).text = text
    return sdf


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8", newline="\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pretty(root: ET.Element) -> str:
    pretty = minidom.parseString(ET.tostring(root, encoding="unicode")).toprettyxml(indent="  ")
    return "\n".join(line for line in pretty.splitlines() if line.strip()) + "\n"


def write_flight_model(spec: ResolvedDroneSpec, output_dir: Path) -> Path:
    """Write ``<output_dir>/<spec sha>/flight/`` and return its manifest path."""
    model = derive_flight_model(spec)
    spec = ResolvedDroneSpec.model_validate(spec.model_dump())
    bundle = Path(output_dir) / model.resolved_spec_sha256 / "flight"
    bundle.mkdir(parents=True, exist_ok=True)
    config = ET.Element("model")
    ET.SubElement(config, "name").text = model.model_name
    ET.SubElement(config, "version").text = "1.0"
    ET.SubElement(config, "sdf", version="1.9").text = "model.sdf"
    ET.SubElement(config, "description").text = (
        f"{FLIGHT_SCOPE} {_LAYOUT_LABEL[len(model.rotors)]} for resolved spec "
        f"{model.resolved_spec_sha256}"
    )
    files = {
        "model.sdf": _write(bundle / "model.sdf", _pretty(build_sdf(model, spec))),
        "model.config": _write(bundle / "model.config", _pretty(config)),
        "flight_model.json": _write(
            bundle / "flight_model.json",
            json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=True, allow_nan=False)
            + "\n",
        ),
    }
    manifest = {
        "schema_version": FLIGHT_SCHEMA_VERSION,
        "validation_scope": FLIGHT_SCOPE,
        "flight_ready": False,
        "resolved_spec_sha256": model.resolved_spec_sha256,
        "model_name": model.model_name,
        "total_mass_kg": model.total_mass_kg,
        "motor_count": len(model.rotors),
        "px4_base_autostart": PX4_BASE_AUTOSTART,
        "px4_params": model.px4_params,
        "files": files,
        "warnings": model.warnings,
        "assumptions": model.assumptions,
        # Which published test the rotor model was fitted to (checked in derive).
        "propulsion_source": {
            "reference": spec.components.motor.thrust_curve_evidence.source_reference,
            "source_sha256": spec.components.motor.thrust_curve_evidence.source_sha256,
            "curve_sha256": spec.components.motor.thrust_curve_evidence.curve_sha256,
        }
        if spec.components.motor.thrust_curve_evidence
        else None,
    }
    manifest["bundle_sha256"] = _bundle_digest(manifest)
    path = bundle / "flight_manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _bundle_digest(manifest: dict) -> str:
    body = {key: value for key, value in manifest.items() if key != "bundle_sha256"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class VerifiedFlightBundle(BaseModel):
    manifest_path: str
    bundle_dir: str
    sdf_path: str
    model_name: str
    resolved_spec_sha256: str
    bundle_sha256: str
    total_mass_kg: float
    motor_count: int
    px4_base_autostart: int
    px4_params: dict[str, float | int]


REQUIRED_SENSORS = ("air_pressure_sensor", "magnetometer_sensor", "imu_sensor", "navsat_sensor")


def _required_links(motor_count: int) -> set[str]:
    return {"base_link", *(f"rotor_{i}" for i in range(motor_count))}


class GazeboModelReport(BaseModel):
    links: dict[str, float] = Field(default_factory=dict)
    link_parents: dict[str, str] = Field(default_factory=dict)
    sensors: list[str] = Field(default_factory=list)


def parse_gz_model_links(text: str) -> GazeboModelReport:
    """Parse ``gz model -m <name> -l``: link masses, their parent model and sensors."""
    report = GazeboModelReport()
    link_indent = None
    pending: list = []  # [indent, name, parent]
    for line in text.splitlines():
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("- Name: "):
            name = stripped[len("- Name: ") :].strip()
            if link_indent is None or indent == link_indent:
                pending = [indent, name, ""]
            elif indent > link_indent:
                report.sensors.append(name)
        elif stripped.startswith("- Parent: ") and pending and pending[0] == indent:
            pending[2] = stripped[len("- Parent: ") :].split(" [")[0].strip()
        elif stripped.startswith("- Mass (kg): ") and pending and pending[0] == indent:
            link_indent = indent
            report.links[pending[1]] = float(stripped[len("- Mass (kg): ") :])
            report.link_parents[pending[1]] = pending[2]
            pending = []
    return report


def attest_spawned_model(
    bundle: VerifiedFlightBundle, instance: str, report: GazeboModelReport
) -> dict:
    """Compare what Gazebo actually loaded with the verified bundle; raise on drift."""
    links, sensors = report.links, report.sensors
    if set(links) != _required_links(bundle.motor_count):
        raise ValueError(
            f"Spawned model links {sorted(links)} are not the generated "
            f"{_LAYOUT_LABEL.get(bundle.motor_count, 'airframe')}"
        )
    if set(report.link_parents.values()) != {instance}:
        raise ValueError(f"Spawned links do not all belong to model instance {instance}")
    missing = [name for name in REQUIRED_SENSORS if name not in sensors]
    if missing:
        raise ValueError(f"Spawned model is missing sensors: {', '.join(missing)}")
    total = sum(links.values())
    if not math.isclose(total, bundle.total_mass_kg, abs_tol=MASS_ATTESTATION_TOLERANCE_KG):
        raise ValueError(
            f"Simulator mass {total:.6f} kg differs from the bundle's {bundle.total_mass_kg:.6f} kg"
        )
    return {
        "status": "matched",
        "validation_scope": FLIGHT_SCOPE,
        "model_instance": instance,
        "model_name": bundle.model_name,
        "resolved_spec_sha256": bundle.resolved_spec_sha256,
        "bundle_sha256": bundle.bundle_sha256,
        "expected_total_mass_kg": bundle.total_mass_kg,
        "simulated_total_mass_kg": round(total, 6),
        "link_masses_kg": links,
        "rotor_link_count": sum(1 for name in links if name.startswith("rotor_")),
        "sensors": sorted(set(sensors)),
    }


_PX4_PARAM_LINE = re.compile(r"\b([A-Z][A-Z0-9_]*) \[\d+,\d+\] : (-?[0-9.]+(?:[eE][-+]?\d+)?)")


def parse_px4_param_show(text: str) -> dict[str, float]:
    """Parse ``px4-param show NAME`` lines such as ``x   MC_ROLLRATE_K [546,1031] : 0.1658``."""
    return {m.group(1): float(m.group(2)) for m in _PX4_PARAM_LINE.finditer(text)}


def compare_px4_params(expected: dict[str, float | int], observed: dict[str, float]) -> list[str]:
    """PX4 ``param show`` prints floats to four decimals; compare at that precision."""
    mismatches = []
    for name, value in expected.items():
        if name not in observed:
            mismatches.append(f"{name}: not reported")
        elif not math.isclose(observed[name], float(value), abs_tol=1e-4, rel_tol=0):
            mismatches.append(f"{name}: expected {value}, PX4 reports {observed[name]}")
    return mismatches


def verify_flight_bundle(
    manifest_path: str | Path, expected_spec_sha256: str
) -> VerifiedFlightBundle:
    """Re-hash every file; any drift from the manifest or the spec is fatal."""
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != FLIGHT_SCHEMA_VERSION:
        raise ValueError("Unsupported flight bundle schema")
    if manifest.get("validation_scope") != FLIGHT_SCOPE:
        raise ValueError("Flight bundle scope is not sitl_catalog_estimate")
    if not expected_spec_sha256 or manifest.get("resolved_spec_sha256") != expected_spec_sha256:
        raise ValueError("Flight bundle belongs to a different resolved specification")
    if manifest.get("bundle_sha256") != _bundle_digest(manifest):
        raise ValueError("Flight manifest content does not match its fingerprint")
    files = manifest.get("files") or {}
    if set(files) != {"model.sdf", "model.config", "flight_model.json"}:
        raise ValueError("Flight bundle file list is incomplete")
    for name, digest in files.items():
        if hashlib.sha256((path.parent / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Flight bundle file {name} changed after generation")
    sdf = ET.parse(path.parent / "model.sdf").getroot().find("model")
    if sdf is None or sdf.get("name") != manifest.get("model_name"):
        raise ValueError("SDF model name does not match the flight manifest")
    if int(manifest.get("motor_count", 0)) not in _MODEL_PREFIX:
        raise ValueError("Flight bundle must declare four, six or eight motors")
    return VerifiedFlightBundle(
        manifest_path=str(path),
        bundle_dir=str(path.parent),
        sdf_path=str(path.parent / "model.sdf"),
        model_name=manifest["model_name"],
        resolved_spec_sha256=manifest["resolved_spec_sha256"],
        bundle_sha256=manifest["bundle_sha256"],
        total_mass_kg=float(manifest["total_mass_kg"]),
        motor_count=int(manifest["motor_count"]),
        px4_base_autostart=int(manifest["px4_base_autostart"]),
        px4_params=dict(manifest["px4_params"]),
    )
