"""Write URDF and SDF from the same resolved structural links and local inertias."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from pipelines.json_to_urdf import URDFJoint, URDFLink, URDFResult
from pipelines.resolved_drone import ResolvedDroneSpec


def write_structural_model(spec: ResolvedDroneSpec, output_dir: Path, pipeline) -> URDFResult:
    # Revalidate even an in-memory spec: nested models can have been mutated.
    spec = ResolvedDroneSpec.model_validate(spec.model_dump())
    digest = spec.sha256
    output_dir = Path(output_dir) / digest
    output_dir.mkdir(parents=True, exist_ok=True)
    name = f"quad_{digest[:16]}"
    links, joints = [], []
    by_name = {link.name: link for link in spec.links}
    for link in spec.links:
        dims = (
            dict(zip(("x", "y", "z"), link.dimensions))
            if link.shape == "box"
            else {"radius": link.dimensions[0], "length": link.dimensions[1]}
        )
        links.append(
            URDFLink(
                name=link.name, mass_kg=link.mass_kg, geometry_type=link.shape, dimensions=dims
            )
        )
        if link.parent:
            parent = by_name[link.parent]
            # All parents in this generator are the unrotated base or motor links.
            # Keep geometry and inertia local; never apply the mount pose twice.
            if any(parent.pose[3:]):
                raise ValueError("Rotated parent frames require an explicit transform")
            xyz = tuple(link.pose[i] - parent.pose[i] for i in range(3))
            joints.append(
                URDFJoint(
                    name=f"{link.name}_joint",
                    joint_type=link.joint_type,
                    parent=link.parent,
                    child=link.name,
                    origin_xyz=xyz,
                    origin_rpy=link.pose[3:],
                    axis_xyz=(0, 0, 1),
                )
            )
    urdf = pipeline._build_robot_xml(name, links, joints)
    # The legacy writer rounds poses/dimensions to 4 decimals; resolved models
    # use the same precision in both formats so small geometries stay consistent.
    for element, local in zip(urdf.findall("link"), links):
        element.find("inertial/mass").set("value", f"{local.mass_kg:.10g}")
        inertia = element.find("inertial/inertia")
        for key, value in pipeline._inertia_values(local).items():
            inertia.set(key, f"{value:.12g}")
        for geometry in element.findall("*/geometry"):
            if local.geometry_type == "box":
                geometry.find("box").set(
                    "size", " ".join(f"{local.dimensions[k]:.10g}" for k in ("x", "y", "z"))
                )
            else:
                for key in ("radius", "length"):
                    geometry.find("cylinder").set(key, f"{local.dimensions[key]:.10g}")
    for element, joint in zip(urdf.findall("joint"), joints):
        origin = element.find("origin")
        origin.set("xyz", " ".join(f"{v:.10g}" for v in joint.origin_xyz))
        origin.set("rpy", " ".join(f"{v:.10g}" for v in joint.origin_rpy))
    urdf.insert(
        0, ET.Comment(f" structural_only; resolved_spec_sha256={digest}; not PX4 flight-ready ")
    )
    urdf_path = output_dir / "model.urdf"
    pipeline._write_xml(urdf, urdf_path)
    validation = pipeline.validate_urdf(str(urdf_path))
    if not validation.valid:
        raise ValueError("Resolved URDF validation failed: " + "; ".join(validation.errors))

    sdf = ET.Element("sdf", version="1.9")
    sdf.append(
        ET.Comment(f" structural_only; resolved_spec_sha256={digest}; not PX4 flight-ready ")
    )
    model = ET.SubElement(sdf, "model", name=name)
    ET.SubElement(model, "static").text = "false"
    for source, local in zip(spec.links, links):
        element = ET.SubElement(model, "link", name=source.name)
        ET.SubElement(element, "pose").text = " ".join(f"{v:.10g}" for v in source.pose)
        inertial = ET.SubElement(element, "inertial")
        ET.SubElement(inertial, "mass").text = f"{source.mass_kg:.10g}"
        inertia = ET.SubElement(inertial, "inertia")
        for key, value in pipeline._inertia_values(local).items():
            ET.SubElement(inertia, key).text = f"{value:.12g}"
        for kind in ("visual", "collision"):
            child = ET.SubElement(element, kind, name=f"{source.name}_{kind}")
            geometry = ET.SubElement(child, "geometry")
            shape = ET.SubElement(geometry, source.shape)
            if source.shape == "box":
                ET.SubElement(shape, "size").text = " ".join(f"{v:.10g}" for v in source.dimensions)
            else:
                ET.SubElement(shape, "radius").text = str(source.dimensions[0])
                ET.SubElement(shape, "length").text = str(source.dimensions[1])
    for joint in joints:
        kind = "revolute" if joint.joint_type == "continuous" else "fixed"
        element = ET.SubElement(model, "joint", name=joint.name, type=kind)
        ET.SubElement(element, "parent").text = joint.parent
        ET.SubElement(element, "child").text = joint.child
        ET.SubElement(element, "pose", relative_to=joint.child).text = "0 0 0 0 0 0"
        if kind == "revolute":
            axis = ET.SubElement(element, "axis")
            ET.SubElement(axis, "xyz").text = "0 0 1"
            limits = ET.SubElement(axis, "limit")
            ET.SubElement(limits, "lower").text = "-1e16"
            ET.SubElement(limits, "upper").text = "1e16"
    sdf_path = output_dir / "model.sdf"
    pipeline._write_xml(sdf, sdf_path)
    spec_path = output_dir / "resolved_drone.json"
    spec_path.write_text(
        json.dumps(
            {"sha256": digest, "spec": spec.model_dump(mode="json")}, indent=2, allow_nan=False
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest_path = output_dir / "model_manifest.json"
    manifest = {
        "schema_version": 1,
        "resolved_spec_sha256": digest,
        "model_name": name,
        "validation_scope": "structural_only",
        "flight_ready": False,
        "total_mass_g": spec.total_mass_g,
        "files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (urdf_path, sdf_path, spec_path)
        },
        "assumptions": spec.assumptions,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n"
    )
    return URDFResult(
        urdf_path=urdf_path,
        sdf_path=sdf_path,
        links=links,
        joints=joints,
        validation=validation,
        design_name=name,
        resolved_spec_path=spec_path,
        spec_sha256=digest,
        manifest_path=manifest_path,
        validation_scope="structural_only",
    )
