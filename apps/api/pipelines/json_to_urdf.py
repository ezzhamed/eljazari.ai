"""eljazari.ai — JSON Design Tree → URDF / SDF Pipeline

Converts a DroneDesignTree (from DesignAgent) into URDF and SDF robot
description files suitable for Gazebo Harmonic / ROS2 Jazzy simulation.

Pipeline steps:
  1. Parse physical dimensions from JSON design tree
  2. Generate URDF links (body, arms, motors, propellers)
  3. Generate URDF joints (fixed arms, continuous propellers)
  4. Optionally attach STL mesh files from AircraftVerse / meshes_dir
  5. Validate the resulting URDF (internal + optional check_urdf)
  6. Generate companion SDF file for Gazebo
"""

import logging
import math
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.dom import minidom
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# Project root (two levels above this file: pipelines/ → api/ → ...)
_BASE_DIR = Path(__file__).parent.parent


# ─────────────────────────────────────────────────────────────
# Result dataclasses
# ─────────────────────────────────────────────────────────────


@dataclass
class URDFLink:
    """Represents a single URDF <link> element."""

    name: str
    mass_kg: float
    origin_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
    origin_rpy: tuple[float, float, float] = (0.0, 0.0, 0.0)
    mesh_path: str | None = None
    geometry_type: str = "box"  # "box" | "cylinder" | "sphere"
    dimensions: dict = field(default_factory=dict)


@dataclass
class URDFJoint:
    """Represents a single URDF <joint> element."""

    name: str
    joint_type: str  # "fixed" | "continuous" | "revolute"
    parent: str
    child: str
    origin_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
    origin_rpy: tuple[float, float, float] = (0.0, 0.0, 0.0)
    axis_xyz: tuple[float, float, float] = (0.0, 0.0, 1.0)


@dataclass
class ValidationResult:
    """Result of URDF structural and kinematic validation."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        status = "VALID" if self.valid else "INVALID"
        return f"[{status}] {len(self.errors)} errors, {len(self.warnings)} warnings"


@dataclass
class URDFResult:
    """Complete output of a single JsonToUrdfPipeline.convert() call."""

    urdf_path: Path
    sdf_path: Path | None
    links: list[URDFLink]
    joints: list[URDFJoint]
    validation: ValidationResult
    design_name: str = "drone"
    resolved_spec_path: Path | None = None
    spec_sha256: str | None = None
    manifest_path: Path | None = None
    validation_scope: str = "legacy_visualization"


# ─────────────────────────────────────────────────────────────
# Inertia helpers
# ─────────────────────────────────────────────────────────────


def _box_inertia(mass: float, lx: float, ly: float, lz: float) -> dict[str, float]:
    """Inertia tensor for a uniform solid box (all three axes)."""
    return {
        "ixx": mass / 12.0 * (ly**2 + lz**2),
        "iyy": mass / 12.0 * (lx**2 + lz**2),
        "izz": mass / 12.0 * (lx**2 + ly**2),
        "ixy": 0.0,
        "ixz": 0.0,
        "iyz": 0.0,
    }


def _cylinder_inertia(mass: float, r: float, h: float) -> dict[str, float]:
    """Inertia tensor for a solid cylinder (spin axis = z)."""
    return {
        "ixx": mass / 12.0 * (3.0 * r**2 + h**2),
        "iyy": mass / 12.0 * (3.0 * r**2 + h**2),
        "izz": mass / 2.0 * r**2,
        "ixy": 0.0,
        "ixz": 0.0,
        "iyz": 0.0,
    }


# ─────────────────────────────────────────────────────────────
# Motor position tables (NED body frame)
# ─────────────────────────────────────────────────────────────


def _motor_xy_table(topology: str, arm_m: float) -> dict[str, tuple[float, float]]:
    """
    Returns (x, y) body-frame offsets per named motor position.
    Convention: x = forward (North), y = right (East).
    """
    d45 = arm_m * math.cos(math.radians(45))  # diagonal component
    d60 = arm_m * math.cos(math.radians(30))  # 60° component

    tables: dict[str, dict[str, tuple[float, float]]] = {
        "quadcopter": {
            "front_left": (d45, -d45),
            "front_right": (d45, d45),
            "rear_left": (-d45, -d45),
            "rear_right": (-d45, d45),
        },
        "hexacopter": {
            "front_left": (d60, -arm_m * 0.5),
            "front_right": (d60, arm_m * 0.5),
            "mid_left": (0.0, -arm_m),
            "mid_right": (0.0, arm_m),
            "rear_left": (-d60, -arm_m * 0.5),
            "rear_right": (-d60, arm_m * 0.5),
        },
        "octocopter": {
            "front_left": (d45, -d45 * 0.4),
            "front_right": (d45, d45 * 0.4),
            "mid_left_front": (d45 * 0.4, -arm_m),
            "mid_right_front": (d45 * 0.4, arm_m),
            "mid_left_rear": (-d45 * 0.4, -arm_m),
            "mid_right_rear": (-d45 * 0.4, arm_m),
            "rear_left": (-d45, -d45 * 0.4),
            "rear_right": (-d45, d45 * 0.4),
        },
        "tricopter": {
            "front_left": (d45, -d45),
            "front_right": (d45, d45),
            "rear_center": (-arm_m, 0.0),
        },
        "bicopter": {
            "left": (0.0, -arm_m),
            "right": (0.0, arm_m),
        },
    }
    return tables.get(topology, tables["quadcopter"])


# ─────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────


class JsonToUrdfPipeline:
    """
    Converts a DroneDesignTree JSON dict into URDF and SDF files.

    Usage::

        pipeline = JsonToUrdfPipeline()
        result = pipeline.convert(design_tree_dict, Path("/tmp/drone_out"))
        print(result.validation.summary)
    """

    def __init__(
        self,
        meshes_dir: Path | None = None,
        include_components: bool = False,
    ) -> None:
        """
        Args:
            meshes_dir: Directory containing STL mesh files.
                        Falls back to ``<api_root>/data/meshes/``.
            include_components: If True, battery, camera, and flight controller links and joints are created.
        """
        self.meshes_dir = meshes_dir or (_BASE_DIR / "data" / "meshes")
        self.include_components = include_components

    # ── Public API ────────────────────────────────────────────

    def convert(
        self, design_tree: dict[str, Any], output_dir: Path, *, resolved_spec=None
    ) -> URDFResult:
        """
        Main entry: JSON design_tree → URDF + SDF files.

        A resolved_spec selects the BOM-linked structural Quad-X exporter.
        Its artifacts are structural_only: no sensors, thrust dynamics or PX4
        flight configuration are implied by successful XML validation.

        Steps:
        1. Generate base/arm links
        2. Generate motor/propeller links
        3. Generate all joints
        4. Attach available STL meshes
        5. Write URDF XML
        6. Validate URDF
        7. Generate companion SDF

        Returns:
            URDFResult with file paths, links, joints, and validation info.
        """
        if resolved_spec is not None:
            from pipelines.structural_model import write_structural_model

            return write_structural_model(resolved_spec, output_dir, self)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        topology = design_tree.get("topology", "quadcopter")
        name = topology.replace(" ", "_")
        frame = design_tree.get("frame", {})
        propulsion = design_tree.get("propulsion", [])

        # 1 + 2. Links
        links = self.generate_links(frame, topology, propulsion)
        links.extend(self._motor_prop_links(propulsion))

        # 3. Joints
        joints = self.generate_joints(propulsion, frame, topology)

        # 3.5 Optional extra components (battery, camera, flight controller)
        if self.include_components:
            cb = frame.get("center_body", {})
            l_m = cb.get("length", 180.0) / 1000.0
            h_m = cb.get("height", 45.0) / 1000.0

            # Battery link and joint
            bat = design_tree.get("battery", {})
            bat_w_g = bat.get("weight_g", 220.0)
            links.append(
                URDFLink(
                    name="battery",
                    mass_kg=bat_w_g / 1000.0,
                    origin_xyz=(0.0, 0.0, 0.0),
                    geometry_type="box",
                    dimensions={"x": 0.07, "y": 0.035, "z": 0.03},
                )
            )
            joints.append(
                URDFJoint(
                    name="battery_joint",
                    joint_type="fixed",
                    parent="base_link",
                    child="battery",
                    origin_xyz=(0.0, 0.0, h_m / 2.0 + 0.015),  # mounted on top plate
                )
            )

            # FPV Camera link and joint
            pay = design_tree.get("payload", {})
            pay_w_g = pay.get("installed_weight_g")
            if pay_w_g is None:
                pay_w_g = pay.get("max_weight_g", 30.0)
            links.append(
                URDFLink(
                    name="camera",
                    mass_kg=pay_w_g / 1000.0,
                    origin_xyz=(0.0, 0.0, 0.0),
                    geometry_type="box",
                    dimensions={"x": 0.02, "y": 0.02, "z": 0.02},
                )
            )
            joints.append(
                URDFJoint(
                    name="camera_joint",
                    joint_type="fixed",
                    parent="base_link",
                    child="camera",
                    origin_xyz=(l_m / 2.0, 0.0, 0.0),  # mounted at the nose
                )
            )

            # Flight Controller (FC) link and joint
            links.append(
                URDFLink(
                    name="flight_controller",
                    mass_kg=0.015,  # 15g
                    origin_xyz=(0.0, 0.0, 0.0),
                    geometry_type="box",
                    dimensions={"x": 0.036, "y": 0.036, "z": 0.005},
                )
            )
            joints.append(
                URDFJoint(
                    name="flight_controller_joint",
                    joint_type="fixed",
                    parent="base_link",
                    child="flight_controller",
                    origin_xyz=(0.0, 0.0, 0.0),  # centered inside the frame
                )
            )

        # 4. Build XML tree
        robot_elem = self._build_robot_xml(name, links, joints)

        # 5. Optionally attach meshes
        stl_paths = self._find_mesh_paths(design_tree)
        if stl_paths:
            self.attach_meshes(robot_elem, stl_paths)

        # 6. Write URDF
        urdf_path = output_dir / f"{name}.urdf"
        self._write_xml(robot_elem, urdf_path)

        # 7. Validate
        validation = self.validate_urdf(str(urdf_path))

        # 8. SDF companion
        sdf_path = self._generate_sdf(design_tree, output_dir, name)

        logger.info(
            "URDF pipeline complete urdf=%s valid=%s links=%s joints=%s",
            str(urdf_path),
            validation.valid,
            len(links),
            len(joints),
        )
        return URDFResult(
            urdf_path=urdf_path,
            sdf_path=sdf_path,
            links=links,
            joints=joints,
            validation=validation,
            design_name=name,
        )

    def generate_links(
        self,
        frame: dict[str, Any],
        topology: str = "quadcopter",
        propulsion: list[dict[str, Any]] = [],
    ) -> list[URDFLink]:
        """
        Generate structural URDF links from frame spec:
        - ``base_link``: central body (mass calculated realistically from carbon plates)
        - ``arm_0`` … ``arm_N``: dynamic arms matching the motor topology

        Args:
            frame:      Frame sub-dict from DroneDesignTree
            topology:   UAV topology (e.g. quadcopter, hexacopter)
            propulsion: List of motor specs containing target positions

        Returns:
            List of URDFLink objects
        """
        cb = frame.get("center_body", {})
        l_m = cb.get("length", 180.0) / 1000.0
        w_m = cb.get("width", 120.0) / 1000.0
        h_m = cb.get("height", 45.0) / 1000.0

        # Real central body is hollow, built from top/bottom plates + spacer hardware
        # Top/bottom carbon fiber plates (2mm thickness, ~1600 kg/m³) + 80g assembly hardware
        body_mass_kg = max(l_m * w_m * 0.002 * 1600.0 * 2.0 + 0.080, 0.15)

        links: list[URDFLink] = [
            URDFLink(
                name="base_link",
                mass_kg=body_mass_kg,
                geometry_type="box",
                dimensions={"x": l_m, "y": w_m, "z": h_m},
            )
        ]

        arm_mm = frame.get("arm_length_mm", 250.0)
        arm_m = arm_mm / 1000.0
        arm_r = 0.008  # 8 mm tube radius
        pos_table = _motor_xy_table(topology, arm_m)

        # Generate an arm leading to each motor position
        n_arms = len(propulsion) if propulsion else 4
        for idx in range(n_arms):
            spec = propulsion[idx] if idx < len(propulsion) else {}
            pos_key = spec.get("position", f"motor_{idx}")
            xy = pos_table.get(
                pos_key, (arm_m * 0.707, arm_m * 0.707 * (1 if idx % 2 == 0 else -1))
            )
            x_motor, y_motor = float(xy[0]), float(xy[1])

            # Length of this specific arm extending from center to motor mount
            arm_len = math.hypot(x_motor, y_motor)
            # Arm yaw angle pointing towards the motor
            angle = math.atan2(y_motor, x_motor)
            # Center of the arm link is exactly halfway between body center and motor mount
            x_center = x_motor / 2.0
            y_center = y_motor / 2.0

            # Weight calculated from carbon fiber tube dimensions (density ~1600 kg/m³)
            arm_mass_kg = max(arm_len * math.pi * arm_r**2 * 1600.0, 0.01)

            links.append(
                URDFLink(
                    name=f"arm_{idx}",
                    mass_kg=arm_mass_kg,
                    origin_xyz=(x_center, y_center, 0.0),
                    origin_rpy=(0.0, 0.0, angle),
                    geometry_type="cylinder",
                    dimensions={"radius": arm_r, "length": arm_len},
                )
            )

        return links

    def generate_joints(
        self,
        propulsion: list[dict[str, Any]],
        frame: dict[str, Any],
        topology: str = "quadcopter",
    ) -> list[URDFJoint]:
        """
        Generate URDF joints:
        - Fixed joints: each arm → base_link
        - Fixed joints: each motor mount → base_link (at arm tip)
        - Continuous joints: propeller disc → motor (spinning axis)

        Args:
            propulsion: List of motor dicts (motor, propeller, position)
            frame:      Frame sub-dict from DroneDesignTree
            topology:   UAV topology string

        Returns:
            List of URDFJoint objects
        """
        joints: list[URDFJoint] = []
        arm_m = frame.get("arm_length_mm", 250.0) / 1000.0
        pos_table = _motor_xy_table(topology, arm_m)

        # Arm → body (fixed)
        n_arms = len(propulsion) if propulsion else 4
        for i in range(n_arms):
            joints.append(
                URDFJoint(
                    name=f"arm_{i}_joint",
                    joint_type="fixed",
                    parent="base_link",
                    child=f"arm_{i}",
                )
            )

        # Motor mount + propeller per propulsion entry
        for idx, spec in enumerate(propulsion):
            pos_key = spec.get("position", f"motor_{idx}")
            # Fall back to diagonal offset if position key not in table
            xy = pos_table.get(
                pos_key, (arm_m * 0.707, arm_m * 0.707 * (1 if idx % 2 == 0 else -1))
            )
            x, y = float(xy[0]), float(xy[1])

            # Even indices spin CCW (+z), odd CW (-z) — standard X-frame convention
            spin = 1.0 if idx % 2 == 0 else -1.0

            # Motor mount (fixed to body at arm tip)
            joints.append(
                URDFJoint(
                    name=f"motor_{idx}_mount",
                    joint_type="fixed",
                    parent="base_link",
                    child=f"motor_{idx}",
                    origin_xyz=(x, y, 0.020),  # 20 mm above base plate
                )
            )

            # Propeller (continuous spin)
            joints.append(
                URDFJoint(
                    name=f"prop_{idx}_joint",
                    joint_type="continuous",
                    parent=f"motor_{idx}",
                    child=f"prop_{idx}",
                    origin_xyz=(0.0, 0.0, 0.015),  # 15 mm above motor stator
                    axis_xyz=(0.0, 0.0, spin),
                )
            )

        return joints

    def attach_meshes(self, robot_elem: ET.Element, stl_paths: list[str]) -> None:
        """
        Replace geometry elements in visual sub-trees with <mesh> references
        for links that have a matching STL file.

        Args:
            robot_elem: Root <robot> Element (mutated in-place)
            stl_paths:  List of absolute STL file paths
        """
        for link_elem in robot_elem.findall("link"):
            link_name = link_elem.get("name", "")
            mesh_path = self._match_mesh(link_name, stl_paths)
            if not mesh_path:
                continue

            vis = link_elem.find("visual")
            if vis is None:
                vis = ET.SubElement(link_elem, "visual")

            geom = vis.find("geometry")
            if geom is not None:
                vis.remove(geom)

            geom = ET.SubElement(vis, "geometry")
            mesh = ET.SubElement(geom, "mesh")
            pkg_path = f"package://drone_model/meshes/{Path(mesh_path).name}"
            mesh.set("filename", pkg_path)
            logger.debug("Attached mesh", link=link_name, mesh=pkg_path)

    def validate_urdf(self, urdf_path: str) -> ValidationResult:
        """
        Validate a URDF file.

        Runs:
        1. Internal XML parse check
        2. Structural rules (required links/joints, parent/child resolution)
        3. Kinematic connectivity check (orphaned links → warnings)
        4. External ``check_urdf`` from ROS2 urdf_parser (if installed)

        Args:
            urdf_path: Absolute path to the URDF file

        Returns:
            ValidationResult
        """
        errors: list[str] = []
        warnings: list[str] = []

        try:
            tree = ET.parse(urdf_path)
            root = tree.getroot()
        except ET.ParseError as exc:
            return ValidationResult(valid=False, errors=[f"XML parse error: {exc}"])

        errors.extend(self._validate_structure(root))
        warnings.extend(self._validate_kinematics(root))

        if shutil.which("check_urdf"):
            errors.extend(self._run_check_urdf(urdf_path))

        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    # ── Private helpers ───────────────────────────────────────

    def _motor_prop_links(self, propulsion: list[dict]) -> list[URDFLink]:
        """Create motor and propeller disc links for every propulsion entry."""
        links: list[URDFLink] = []
        for idx, spec in enumerate(propulsion):
            links.append(
                URDFLink(
                    name=f"motor_{idx}",
                    mass_kg=0.055,
                    geometry_type="cylinder",
                    dimensions={"radius": 0.014, "length": 0.025},
                )
            )
            prop_r = self._parse_prop_radius(spec.get("propeller", "5045"))
            links.append(
                URDFLink(
                    name=f"prop_{idx}",
                    mass_kg=0.012,
                    geometry_type="cylinder",
                    dimensions={"radius": prop_r, "length": 0.003},
                )
            )
        return links

    def _parse_prop_radius(self, spec: str) -> float:
        """Return propeller radius in metres from a spec string like '5045' → 0.0635."""
        # "5x4.5" explicit notation
        m = re.match(r"(\d+(?:\.\d+)?)[xX]", spec)
        if m:
            inch = float(m.group(1))
        elif re.match(r"^\d{4,}", spec):
            # 4-digit prop code: first 2 digits ≥ 20 → diameter×10 (50→5.0 in);
            # first 2 digits < 20 → direct inches (15→15.0 in)
            first_two = int(spec[:2])
            inch = first_two / 10.0 if first_two >= 20 else float(first_two)
        else:
            m = re.match(r"(\d+(?:\.\d+)?)", spec)
            inch = float(m.group(1)) if m else 5.0
        return (inch * 25.4) / 2000.0  # inch → mm → half → m

    def _build_robot_xml(
        self, name: str, links: list[URDFLink], joints: list[URDFJoint]
    ) -> ET.Element:
        robot = ET.Element("robot", name=name)

        for lk in links:
            link_elem = ET.SubElement(robot, "link", name=lk.name)

            # Inertial
            inertial = ET.SubElement(link_elem, "inertial")
            ET.SubElement(inertial, "mass", value=f"{lk.mass_kg:.6f}")
            ET.SubElement(
                inertial,
                "origin",
                xyz=self._xyz_str(lk.origin_xyz),
                rpy=self._xyz_str(lk.origin_rpy),
            )
            iv = self._inertia_values(lk)
            ET.SubElement(
                inertial,
                "inertia",
                ixx=f"{iv['ixx']:.8f}",
                ixy=f"{iv['ixy']:.8f}",
                ixz=f"{iv['ixz']:.8f}",
                iyy=f"{iv['iyy']:.8f}",
                iyz=f"{iv['iyz']:.8f}",
                izz=f"{iv['izz']:.8f}",
            )

            # Visual
            vis = ET.SubElement(link_elem, "visual")
            ET.SubElement(
                vis, "origin", xyz=self._xyz_str(lk.origin_xyz), rpy=self._xyz_str(lk.origin_rpy)
            )
            self._add_geometry(ET.SubElement(vis, "geometry"), lk)
            mat = ET.SubElement(vis, "material", name="carbon_fiber")
            ET.SubElement(mat, "color", rgba="0.1 0.1 0.1 1.0")

            # Collision (simplified — same shape)
            col = ET.SubElement(link_elem, "collision")
            ET.SubElement(
                col, "origin", xyz=self._xyz_str(lk.origin_xyz), rpy=self._xyz_str(lk.origin_rpy)
            )
            self._add_geometry(ET.SubElement(col, "geometry"), lk)

        for jt in joints:
            jt_elem = ET.SubElement(robot, "joint", name=jt.name, type=jt.joint_type)
            ET.SubElement(jt_elem, "parent", link=jt.parent)
            ET.SubElement(jt_elem, "child", link=jt.child)
            ET.SubElement(
                jt_elem,
                "origin",
                xyz=self._xyz_str(jt.origin_xyz),
                rpy=self._xyz_str(jt.origin_rpy),
            )
            if jt.joint_type in ("continuous", "revolute"):
                ET.SubElement(
                    jt_elem,
                    "axis",
                    xyz=f"{jt.axis_xyz[0]:.1f} {jt.axis_xyz[1]:.1f} {jt.axis_xyz[2]:.1f}",
                )

        return robot

    def _inertia_values(self, lk: URDFLink) -> dict[str, float]:
        d = lk.dimensions
        if lk.geometry_type == "box":
            return _box_inertia(lk.mass_kg, d.get("x", 0.1), d.get("y", 0.1), d.get("z", 0.05))
        return _cylinder_inertia(lk.mass_kg, d.get("radius", 0.01), d.get("length", 0.05))

    def _add_geometry(self, geom_elem: ET.Element, lk: URDFLink) -> None:
        if lk.mesh_path:
            mesh = ET.SubElement(geom_elem, "mesh")
            mesh.set("filename", lk.mesh_path)
        elif lk.geometry_type == "box":
            d = lk.dimensions
            ET.SubElement(
                geom_elem,
                "box",
                size=f"{d.get('x', 0.1):.4f} {d.get('y', 0.1):.4f} {d.get('z', 0.05):.4f}",
            )
        elif lk.geometry_type == "cylinder":
            d = lk.dimensions
            ET.SubElement(
                geom_elem,
                "cylinder",
                radius=f"{d.get('radius', 0.01):.4f}",
                length=f"{d.get('length', 0.05):.4f}",
            )
        else:
            ET.SubElement(geom_elem, "sphere", radius="0.01")

    def _find_mesh_paths(self, design: dict) -> list[str]:
        if not self.meshes_dir.exists():
            return []
        topo = design.get("topology", "quadcopter")
        candidates = [
            self.meshes_dir / f"{topo}_body.stl",
            self.meshes_dir / "arm.stl",
            self.meshes_dir / "motor.stl",
            self.meshes_dir / "prop.stl",
        ]
        return [str(p) for p in candidates if p.exists()]

    def _match_mesh(self, link_name: str, stl_paths: list[str]) -> str | None:
        for path in stl_paths:
            stem = Path(path).stem.lower()
            if "body" in stem and "base" in link_name:
                return path
            if "arm" in stem and "arm_" in link_name:
                return path
            if "motor" in stem and "motor_" in link_name:
                return path
            if "prop" in stem and "prop_" in link_name:
                return path
        return None

    def _generate_sdf(self, design: dict, output_dir: Path, name: str) -> Path | None:
        """Write a Gazebo-compatible SDF 1.9 companion file."""
        try:
            sdf = ET.Element("sdf", version="1.9")
            model = ET.SubElement(sdf, "model", name=name)
            ET.SubElement(model, "static").text = "false"

            link = ET.SubElement(model, "link", name="base_link")
            ET.SubElement(link, "pose").text = "0 0 0.1 0 0 0"

            # Inertial
            frame = design.get("frame", {})
            bat = design.get("battery", {})
            mass_v = (bat.get("weight_g", 480) + 200.0) / 1000.0
            inertial = ET.SubElement(link, "inertial")
            ET.SubElement(inertial, "mass").text = f"{mass_v:.3f}"
            inertia_elem = ET.SubElement(inertial, "inertia")
            for tag, val in [
                ("ixx", "0.025"),
                ("iyy", "0.025"),
                ("izz", "0.045"),
                ("ixy", "0"),
                ("ixz", "0"),
                ("iyz", "0"),
            ]:
                ET.SubElement(inertia_elem, tag).text = val

            # Visual
            visual = ET.SubElement(link, "visual", name="body_visual")
            geom = ET.SubElement(visual, "geometry")
            box = ET.SubElement(geom, "box")
            cb = frame.get("center_body", {})
            lx = cb.get("length", 180) / 1000.0
            ly = cb.get("width", 120) / 1000.0
            lz = cb.get("height", 45) / 1000.0
            ET.SubElement(box, "size").text = f"{lx:.4f} {ly:.4f} {lz:.4f}"

            # Collision
            coll = ET.SubElement(link, "collision", name="body_col")
            cgeom = ET.SubElement(coll, "geometry")
            cbox = ET.SubElement(cgeom, "box")
            ET.SubElement(cbox, "size").text = f"{lx:.4f} {ly:.4f} {lz:.4f}"

            sdf_path = output_dir / f"{name}.sdf"
            self._write_xml(sdf, sdf_path)
            return sdf_path

        except Exception as exc:
            logger.warning("SDF generation failed: %s", str(exc))
            return None

    def _validate_structure(self, root: ET.Element) -> list[str]:
        errors: list[str] = []

        if root.tag != "robot":
            return [f"Root element must be <robot>, found <{root.tag}>"]

        links = {lk.get("name") for lk in root.findall("link")}
        if not links:
            errors.append("URDF contains no <link> elements")
        if "base_link" not in links:
            errors.append("Required 'base_link' is missing")

        for jt in root.findall("joint"):
            jname = jt.get("name", "<unnamed>")
            p_elem = jt.find("parent")
            c_elem = jt.find("child")

            if p_elem is None:
                errors.append(f"Joint '{jname}' missing <parent>")
            elif p_elem.get("link") not in links:
                errors.append(
                    f"Joint '{jname}' parent '{p_elem.get('link')}' not declared as a link"
                )

            if c_elem is None:
                errors.append(f"Joint '{jname}' missing <child>")
            elif c_elem.get("link") not in links:
                errors.append(
                    f"Joint '{jname}' child '{c_elem.get('link')}' not declared as a link"
                )

        return errors

    def _validate_kinematics(self, root: ET.Element) -> list[str]:
        """Warn about links not reachable from base_link via the joint tree."""
        warnings: list[str] = []
        links = {lk.get("name") for lk in root.findall("link")}
        connected: set[str] = {"base_link"}

        for jt in root.findall("joint"):
            c = jt.find("child")
            if c is not None and c.get("link"):
                connected.add(c.get("link"))

        orphans = links - connected
        if orphans:
            warnings.append(f"Links not connected via any joint: {sorted(orphans)}")
        return warnings

    def _run_check_urdf(self, urdf_path: str) -> list[str]:
        """Run the ROS2 check_urdf binary and return any reported errors."""
        try:
            result = subprocess.run(
                ["check_urdf", urdf_path],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return [f"check_urdf: {result.stderr.strip()}"]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # tool not installed or timed out — silently skip
        return []

    @staticmethod
    def _xyz_str(t: tuple[float, float, float]) -> str:
        return f"{t[0]:.4f} {t[1]:.4f} {t[2]:.4f}"

    @staticmethod
    def _write_xml(root: ET.Element, path: Path) -> None:
        raw = ET.tostring(root, encoding="unicode")
        pretty = minidom.parseString(raw).toprettyxml(indent="  ")
        # Drop the extra blank lines minidom inserts
        lines = [ln for ln in pretty.splitlines() if ln.strip()]
        path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


# ─────────────────────────────────────────────────────────────
# Backwards-compatibility alias (used in graph/nodes.py)
# ─────────────────────────────────────────────────────────────

JSONToURDFConverter = JsonToUrdfPipeline
