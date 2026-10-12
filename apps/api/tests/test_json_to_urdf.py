"""Tests for JSON → URDF / SDF Pipeline (Phase 2.3)

Coverage:
- _box_inertia / _cylinder_inertia helpers
- _motor_xy_table position calculations
- JsonToUrdfPipeline.generate_links
- JsonToUrdfPipeline.generate_joints
- JsonToUrdfPipeline.attach_meshes
- JsonToUrdfPipeline.validate_urdf (structural + kinematic)
- JsonToUrdfPipeline.convert (end-to-end)
- SDF companion generation
- Backwards-compatibility alias JSONToURDFConverter
- Edge cases: empty propulsion, custom mesh dir, missing frame keys
"""

import math
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from pipelines.json_to_urdf import (
    JSONToURDFConverter,
    JsonToUrdfPipeline,
    URDFResult,
    _box_inertia,
    _cylinder_inertia,
    _motor_xy_table,
)


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def quad_design() -> dict:
    return {
        "topology": "quadcopter",
        "frame": {
            "type": "X",
            "arm_length_mm": 250,
            "center_body": {"length": 180, "width": 120, "height": 45},
        },
        "propulsion": [
            {"motor": "2206-2300KV", "propeller": "5045", "position": p}
            for p in ["front_left", "front_right", "rear_left", "rear_right"]
        ],
        "battery": {"type": "LiPo", "cells": 4, "capacity_mah": 5000, "weight_g": 480},
        "payload": {"max_weight_g": 500, "mount_type": "bottom"},
        "electronics": {"flight_controller": "PX4", "gps": True},
    }


@pytest.fixture
def hex_design() -> dict:
    return {
        "topology": "hexacopter",
        "frame": {"type": "X", "arm_length_mm": 380, "center_body": {}},
        "propulsion": [
            {"motor": "4114-320KV", "propeller": "1555", "position": p}
            for p in [
                "front_left",
                "front_right",
                "mid_left",
                "mid_right",
                "rear_left",
                "rear_right",
            ]
        ],
        "battery": {"cells": 6, "capacity_mah": 10000, "weight_g": 950},
        "payload": {"max_weight_g": 2000},
        "electronics": {"flight_controller": "PX4"},
    }


@pytest.fixture
def pipeline() -> JsonToUrdfPipeline:
    return JsonToUrdfPipeline(meshes_dir=Path("/nonexistent_meshes"))


@pytest.fixture
def tmp_dir(tmp_path) -> Path:
    return tmp_path


# ─────────────────────────────────────────────────────────────
# 1. Inertia helper tests
# ─────────────────────────────────────────────────────────────


class TestInertiaHelpers:
    def test_box_inertia_keys(self):
        result = _box_inertia(1.0, 0.1, 0.1, 0.1)
        assert set(result.keys()) == {"ixx", "iyy", "izz", "ixy", "ixz", "iyz"}

    def test_box_inertia_cube_symmetry(self):
        r = _box_inertia(1.0, 0.1, 0.1, 0.1)
        assert math.isclose(r["ixx"], r["iyy"], rel_tol=1e-6)
        assert math.isclose(r["iyy"], r["izz"], rel_tol=1e-6)

    def test_box_inertia_cross_terms_zero(self):
        r = _box_inertia(1.0, 0.2, 0.1, 0.05)
        assert r["ixy"] == 0.0
        assert r["ixz"] == 0.0
        assert r["iyz"] == 0.0

    def test_box_inertia_scales_with_mass(self):
        r1 = _box_inertia(1.0, 0.1, 0.1, 0.1)
        r2 = _box_inertia(2.0, 0.1, 0.1, 0.1)
        assert math.isclose(r2["ixx"], 2.0 * r1["ixx"], rel_tol=1e-6)

    def test_cylinder_inertia_keys(self):
        r = _cylinder_inertia(0.1, 0.05, 0.1)
        assert set(r.keys()) == {"ixx", "iyy", "izz", "ixy", "ixz", "iyz"}

    def test_cylinder_ixx_equals_iyy(self):
        r = _cylinder_inertia(0.1, 0.05, 0.1)
        assert math.isclose(r["ixx"], r["iyy"], rel_tol=1e-6)

    def test_cylinder_izz_greater_than_ixx(self):
        # For a flat disc (r >> h), izz > ixx
        r = _cylinder_inertia(0.1, 0.1, 0.002)
        assert r["izz"] > r["ixx"]


# ─────────────────────────────────────────────────────────────
# 2. Motor position table tests
# ─────────────────────────────────────────────────────────────


class TestMotorXyTable:
    def test_quadcopter_has_4_positions(self):
        t = _motor_xy_table("quadcopter", 0.25)
        assert len(t) == 4

    def test_hexacopter_has_6_positions(self):
        t = _motor_xy_table("hexacopter", 0.38)
        assert len(t) == 6

    def test_octocopter_has_8_positions(self):
        t = _motor_xy_table("octocopter", 0.35)
        assert len(t) == 8

    def test_quad_front_left_positive_x_negative_y(self):
        t = _motor_xy_table("quadcopter", 0.25)
        x, y = t["front_left"]
        assert x > 0  # forward
        assert y < 0  # left

    def test_quad_rear_right_negative_x_positive_y(self):
        t = _motor_xy_table("quadcopter", 0.25)
        x, y = t["rear_right"]
        assert x < 0  # backward
        assert y > 0  # right

    def test_arm_length_scales_positions(self):
        t1 = _motor_xy_table("quadcopter", 0.25)
        t2 = _motor_xy_table("quadcopter", 0.50)
        x1, y1 = t1["front_left"]
        x2, y2 = t2["front_left"]
        assert math.isclose(abs(x2), abs(x1) * 2.0, rel_tol=0.05)

    def test_unknown_topology_falls_back_to_quad(self):
        t = _motor_xy_table("unknown_type", 0.25)
        assert "front_left" in t


# ─────────────────────────────────────────────────────────────
# 3. generate_links tests
# ─────────────────────────────────────────────────────────────


class TestGenerateLinks:
    def test_returns_5_links_for_quad_frame(self, pipeline):
        frame = {"arm_length_mm": 250, "center_body": {"length": 180, "width": 120, "height": 45}}
        links = pipeline.generate_links(frame)
        # base_link + 4 arms
        assert len(links) == 5

    def test_first_link_is_base_link(self, pipeline):
        links = pipeline.generate_links({"arm_length_mm": 250})
        assert links[0].name == "base_link"

    def test_arm_links_named_correctly(self, pipeline):
        links = pipeline.generate_links({"arm_length_mm": 250})
        arm_names = {lk.name for lk in links if lk.name.startswith("arm_")}
        assert arm_names == {"arm_0", "arm_1", "arm_2", "arm_3"}

    def test_base_link_geometry_is_box(self, pipeline):
        links = pipeline.generate_links({"arm_length_mm": 250})
        base = next(lk for lk in links if lk.name == "base_link")
        assert base.geometry_type == "box"

    def test_arm_geometry_is_cylinder(self, pipeline):
        links = pipeline.generate_links({"arm_length_mm": 250})
        arm = next(lk for lk in links if lk.name == "arm_0")
        assert arm.geometry_type == "cylinder"

    def test_base_mass_minimum_150g(self, pipeline):
        # Tiny frame should still be at least 150g
        links = pipeline.generate_links(
            {"arm_length_mm": 10, "center_body": {"length": 1, "width": 1, "height": 1}}
        )
        base = next(lk for lk in links if lk.name == "base_link")
        assert base.mass_kg >= 0.15

    def test_empty_frame_uses_defaults(self, pipeline):
        links = pipeline.generate_links({})
        assert any(lk.name == "base_link" for lk in links)


# ─────────────────────────────────────────────────────────────
# 4. generate_joints tests
# ─────────────────────────────────────────────────────────────


class TestGenerateJoints:
    def test_quad_joint_count(self, pipeline, quad_design):
        propulsion = quad_design["propulsion"]
        frame = quad_design["frame"]
        joints = pipeline.generate_joints(propulsion, frame, "quadcopter")
        # 4 arm joints + 4 motor mounts + 4 propeller joints = 12
        assert len(joints) == 12

    def test_hex_joint_count(self, pipeline, hex_design):
        propulsion = hex_design["propulsion"]
        frame = hex_design["frame"]
        joints = pipeline.generate_joints(propulsion, frame, "hexacopter")
        # 6 arm joints + 6 motor mounts + 6 propeller joints = 18 (previously 16)
        assert len(joints) == 18

    def test_arm_joints_are_fixed(self, pipeline, quad_design):
        joints = pipeline.generate_joints(
            quad_design["propulsion"], quad_design["frame"], "quadcopter"
        )
        arm_joints = [j for j in joints if j.name.startswith("arm_")]
        assert all(j.joint_type == "fixed" for j in arm_joints)

    def test_prop_joints_are_continuous(self, pipeline, quad_design):
        joints = pipeline.generate_joints(
            quad_design["propulsion"], quad_design["frame"], "quadcopter"
        )
        prop_joints = [j for j in joints if j.name.startswith("prop_")]
        assert all(j.joint_type == "continuous" for j in prop_joints)

    def test_motor_mounts_are_fixed(self, pipeline, quad_design):
        joints = pipeline.generate_joints(
            quad_design["propulsion"], quad_design["frame"], "quadcopter"
        )
        mount_joints = [j for j in joints if "mount" in j.name]
        assert all(j.joint_type == "fixed" for j in mount_joints)

    def test_prop_spin_alternates(self, pipeline, quad_design):
        joints = pipeline.generate_joints(
            quad_design["propulsion"], quad_design["frame"], "quadcopter"
        )
        prop_joints = sorted(
            [j for j in joints if j.name.startswith("prop_")], key=lambda j: j.name
        )
        spins = [j.axis_xyz[2] for j in prop_joints]
        # Even idx CCW (+1), odd CW (-1)
        assert spins[0] == 1.0
        assert spins[1] == -1.0

    def test_empty_propulsion_gives_only_arm_joints(self, pipeline):
        joints = pipeline.generate_joints([], {"arm_length_mm": 250}, "quadcopter")
        assert len(joints) == 4  # only arm joints


# ─────────────────────────────────────────────────────────────
# 5. validate_urdf tests
# ─────────────────────────────────────────────────────────────


class TestValidateUrdf:
    def _write_urdf(self, tmp_dir: Path, content: str) -> str:
        p = tmp_dir / "test.urdf"
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_valid_minimal_urdf(self, pipeline, tmp_path):
        urdf = """<?xml version="1.0"?>
<robot name="drone">
  <link name="base_link">
    <inertial><mass value="1.0"/><inertia ixx="0.1" ixy="0" ixz="0" iyy="0.1" iyz="0" izz="0.1"/></inertial>
  </link>
  <link name="motor_0"/>
  <joint name="j0" type="fixed">
    <parent link="base_link"/><child link="motor_0"/>
  </joint>
</robot>"""
        path = self._write_urdf(tmp_path, urdf)
        result = pipeline.validate_urdf(path)
        assert result.valid

    def test_missing_base_link_is_error(self, pipeline, tmp_path):
        urdf = """<?xml version="1.0"?><robot name="x"><link name="foo"/></robot>"""
        path = self._write_urdf(tmp_path, urdf)
        result = pipeline.validate_urdf(path)
        assert not result.valid
        assert any("base_link" in e for e in result.errors)

    def test_wrong_root_tag_is_error(self, pipeline, tmp_path):
        urdf = """<?xml version="1.0"?><model name="x"><link name="base_link"/></model>"""
        path = self._write_urdf(tmp_path, urdf)
        result = pipeline.validate_urdf(path)
        assert not result.valid

    def test_invalid_xml_is_error(self, pipeline, tmp_path):
        path = self._write_urdf(tmp_path, "<robot><unclosed>")
        result = pipeline.validate_urdf(path)
        assert not result.valid
        assert any("XML" in e or "parse" in e.lower() for e in result.errors)

    def test_unknown_joint_parent_is_error(self, pipeline, tmp_path):
        urdf = """<?xml version="1.0"?>
<robot name="x">
  <link name="base_link"/>
  <link name="child"/>
  <joint name="j" type="fixed">
    <parent link="does_not_exist"/><child link="child"/>
  </joint>
</robot>"""
        path = self._write_urdf(tmp_path, urdf)
        result = pipeline.validate_urdf(path)
        assert not result.valid

    def test_summary_contains_status(self, pipeline, tmp_path):
        urdf = """<?xml version="1.0"?><robot name="x"><link name="base_link"/></robot>"""
        path = self._write_urdf(tmp_path, urdf)
        result = pipeline.validate_urdf(path)
        assert "VALID" in result.summary or "INVALID" in result.summary

    def test_orphaned_link_produces_warning(self, pipeline, tmp_path):
        urdf = """<?xml version="1.0"?>
<robot name="x">
  <link name="base_link"/>
  <link name="orphan"/>
</robot>"""
        path = self._write_urdf(tmp_path, urdf)
        result = pipeline.validate_urdf(path)
        assert len(result.warnings) > 0


# ─────────────────────────────────────────────────────────────
# 6. attach_meshes tests
# ─────────────────────────────────────────────────────────────


class TestAttachMeshes:
    def _minimal_robot(self) -> ET.Element:
        root = ET.Element("robot", name="test")
        for name in ["base_link", "arm_0", "motor_0", "prop_0"]:
            link = ET.SubElement(root, "link", name=name)
            vis = ET.SubElement(link, "visual")
            geom = ET.SubElement(vis, "geometry")
            ET.SubElement(geom, "box", size="0.1 0.1 0.1")
        return root

    def test_attach_body_mesh(self, pipeline):
        root = self._minimal_robot()
        pipeline.attach_meshes(root, ["/some/path/quadcopter_body.stl"])
        base = root.find(".//link[@name='base_link']")
        mesh_elem = base.find(".//mesh")
        assert mesh_elem is not None
        assert "quadcopter_body.stl" in mesh_elem.get("filename", "")

    def test_attach_motor_mesh(self, pipeline):
        root = self._minimal_robot()
        pipeline.attach_meshes(root, ["/path/motor.stl"])
        motor = root.find(".//link[@name='motor_0']")
        mesh_elem = motor.find(".//mesh")
        assert mesh_elem is not None

    def test_no_mesh_match_leaves_geometry_unchanged(self, pipeline):
        root = self._minimal_robot()
        pipeline.attach_meshes(root, [])
        base = root.find(".//link[@name='base_link']")
        box_elem = base.find(".//box")
        assert box_elem is not None  # original box geometry intact


# ─────────────────────────────────────────────────────────────
# 7. End-to-end convert() tests
# ─────────────────────────────────────────────────────────────


class TestConvert:
    def test_convert_produces_urdf_file(self, pipeline, quad_design, tmp_dir):
        result = pipeline.convert(quad_design, tmp_dir)
        assert result.urdf_path.exists()

    def test_convert_produces_sdf_file(self, pipeline, quad_design, tmp_dir):
        result = pipeline.convert(quad_design, tmp_dir)
        assert result.sdf_path is not None
        assert result.sdf_path.exists()

    def test_convert_urdf_is_valid(self, pipeline, quad_design, tmp_dir):
        result = pipeline.convert(quad_design, tmp_dir)
        assert result.validation.valid, f"Errors: {result.validation.errors}"

    def test_convert_links_count_quad(self, pipeline, quad_design, tmp_dir):
        result = pipeline.convert(quad_design, tmp_dir)
        # 5 structural (base + 4 arms) + 4*2 motor/prop = 13
        assert len(result.links) == 13

    def test_convert_joints_count_quad(self, pipeline, quad_design, tmp_dir):
        result = pipeline.convert(quad_design, tmp_dir)
        # 4 arm + 4 mounts + 4 props = 12
        assert len(result.joints) == 12

    def test_convert_hexacopter_links(self, pipeline, hex_design, tmp_dir):
        result = pipeline.convert(hex_design, tmp_dir)
        # 1 base_link + 6 arms + 6 motors + 6 props = 19 (previously 17)
        assert len(result.links) == 19

    def test_convert_creates_output_dir(self, pipeline, quad_design, tmp_path):
        deep_dir = tmp_path / "a" / "b" / "c"
        result = pipeline.convert(quad_design, deep_dir)
        assert result.urdf_path.exists()

    def test_convert_design_name_matches_topology(self, pipeline, quad_design, tmp_dir):
        result = pipeline.convert(quad_design, tmp_dir)
        assert result.design_name == "quadcopter"

    def test_convert_urdf_has_robot_root(self, pipeline, quad_design, tmp_dir):
        result = pipeline.convert(quad_design, tmp_dir)
        tree = ET.parse(result.urdf_path)
        root = tree.getroot()
        assert root.tag == "robot"

    def test_convert_urdf_has_base_link(self, pipeline, quad_design, tmp_dir):
        result = pipeline.convert(quad_design, tmp_dir)
        tree = ET.parse(result.urdf_path)
        links = {lk.get("name") for lk in tree.findall(".//link")}
        assert "base_link" in links

    def test_convert_empty_propulsion(self, pipeline, tmp_dir):
        design = {
            "topology": "quadcopter",
            "frame": {"arm_length_mm": 250},
            "propulsion": [],
        }
        result = pipeline.convert(design, tmp_dir)
        assert result.validation.valid

    def test_convert_minimal_design(self, pipeline, tmp_dir):
        result = pipeline.convert({}, tmp_dir)
        assert result.urdf_path.exists()

    def test_sdf_has_model_element(self, pipeline, quad_design, tmp_dir):
        result = pipeline.convert(quad_design, tmp_dir)
        tree = ET.parse(result.sdf_path)
        root = tree.getroot()
        assert root.tag == "sdf"
        assert root.find("model") is not None

    def test_parse_prop_radius_5inch(self, pipeline):
        r = pipeline._parse_prop_radius("5045")
        expected = (5.0 * 25.4) / 2000.0
        assert math.isclose(r, expected, rel_tol=1e-6)

    def test_backwards_compat_alias(self, quad_design, tmp_dir):
        """JSONToURDFConverter should work identically to JsonToUrdfPipeline."""
        converter = JSONToURDFConverter(meshes_dir=Path("/nonexistent"))
        result = converter.convert(quad_design, tmp_dir)
        assert isinstance(result, URDFResult)
        assert result.validation.valid
