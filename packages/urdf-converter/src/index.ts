/**
 * @eljazari/urdf-converter
 * Converts a DroneDesignTree representation into URDF and SDF robot descriptions.
 */

import { DroneDesign, DronePropeller } from "@eljazari/shared-types";

// ─────────────────────────────────────────────────────────────
// Physics & Geometry Constants
// ─────────────────────────────────────────────────────────────

/** Material density of drone frame/body (carbon fiber/plastic approximation in kg/m^3) */
export const CARBON_FIBER_DENSITY_KG_M3 = 1400.0;

/** Minimum backup body mass in kg if dimensions are too small */
export const MIN_BODY_MASS_KG = 0.15;

/** Default arm length in mm if not specified */
export const DEFAULT_ARM_LENGTH_MM = 250.0;

/** Arm tube radius in meters */
export const ARM_RADIUS_M = 0.008;

/** Minimum arm mass in kg to avoid zero mass links */
export const MIN_ARM_MASS_KG = 0.01;

/** Standard drone motor mass in kg */
export const MOTOR_MASS_KG = 0.055;

/** Standard drone motor radius in meters */
export const MOTOR_RADIUS_M = 0.014;

/** Standard drone motor length/height in meters */
export const MOTOR_LENGTH_M = 0.025;

/** Standard propeller mass in kg */
export const PROPELLER_MASS_KG = 0.012;

/** Standard propeller height/thickness in meters */
export const PROPELLER_LENGTH_M = 0.003;

/** Default propeller size in inches if spec is invalid/missing */
export const DEFAULT_PROPELLER_INCHES = 5.0;

/** Z-offset for motor relative to arm/body plane in meters */
export const MOTOR_MOUNT_Z_OFFSET_M = 0.02;

/** Z-offset for propeller joint relative to motor mount in meters */
export const PROP_JOINT_Z_OFFSET_M = 0.015;

/** Backup battery weight in grams if unspecified */
export const DEFAULT_BATTERY_WEIGHT_G = 480.0;

/** Dry frame/avionics weight overhead in grams */
export const AVIONICS_OVERHEAD_WEIGHT_G = 200.0;

/** Default center body length in mm */
export const DEFAULT_BODY_LENGTH_MM = 180.0;

/** Default center body width in mm */
export const DEFAULT_BODY_WIDTH_MM = 120.0;

/** Default center body height in mm */
export const DEFAULT_BODY_HEIGHT_MM = 45.0;

/** Default inertias for base link simulation */
export const DEFAULT_INERTIA_IXX = 0.025;
export const DEFAULT_INERTIA_IYY = 0.025;
export const DEFAULT_INERTIA_IZZ = 0.045;

export interface URDFLink {
  name: string;
  mass_kg: number;
  origin_xyz: [number, number, number];
  origin_rpy: [number, number, number];
  mesh_path?: string;
  geometry_type: "box" | "cylinder" | "sphere";
  dimensions: Record<string, number>;
}

export interface URDFJoint {
  name: string;
  joint_type: "fixed" | "continuous" | "revolute";
  parent: string;
  child: string;
  origin_xyz: [number, number, number];
  origin_rpy: [number, number, number];
  axis_xyz: [number, number, number];
}

export interface ValidationResult {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

export interface URDFResult {
  urdf_content: string;
  sdf_content: string;
  links: URDFLink[];
  joints: URDFJoint[];
  validation: ValidationResult;
  design_name: string;
}

// ─────────────────────────────────────────────────────────────
// Inertia helpers
// ─────────────────────────────────────────────────────────────

export function boxInertia(mass: number, lx: number, ly: number, lz: number) {
  return {
    ixx: (mass / 12.0) * (ly * ly + lz * lz),
    iyy: (mass / 12.0) * (lx * lx + lz * lz),
    izz: (mass / 12.0) * (lx * lx + ly * ly),
    ixy: 0.0,
    ixz: 0.0,
    iyz: 0.0,
  };
}

export function cylinderInertia(mass: number, r: number, h: number) {
  return {
    ixx: (mass / 12.0) * (3.0 * r * r + h * h),
    iyy: (mass / 12.0) * (3.0 * r * r + h * h),
    izz: (mass / 2.0) * r * r,
    ixy: 0.0,
    ixz: 0.0,
    iyz: 0.0,
  };
}

// ─────────────────────────────────────────────────────────────
// Motor positions
// ─────────────────────────────────────────────────────────────

export function motorXYTable(topology: string, armM: number): Record<string, [number, number]> {
  const d45 = armM * Math.cos((45 * Math.PI) / 180);
  const d60 = armM * Math.cos((30 * Math.PI) / 180);

  const tables: Record<string, Record<string, [number, number]>> = {
    quadcopter: {
      front_left: [d45, -d45],
      front_right: [d45, d45],
      rear_left: [-d45, -d45],
      rear_right: [-d45, d45],
    },
    hexacopter: {
      front_left: [d60, -armM * 0.5],
      front_right: [d60, armM * 0.5],
      mid_left: [0.0, -armM],
      mid_right: [0.0, armM],
      rear_left: [-d60, -armM * 0.5],
      rear_right: [-d60, armM * 0.5],
    },
    octocopter: {
      front_left: [d45, -d45 * 0.4],
      front_right: [d45, d45 * 0.4],
      mid_left_front: [d45 * 0.4, -armM],
      mid_right_front: [d45 * 0.4, armM],
      mid_left_rear: [-d45 * 0.4, -armM],
      mid_right_rear: [-d45 * 0.4, armM],
      rear_left: [-d45, -d45 * 0.4],
      rear_right: [-d45, d45 * 0.4],
    },
    tricopter: {
      front_left: [d45, -d45],
      front_right: [d45, d45],
      rear_center: [-armM, 0.0],
    },
    bicopter: {
      left: [0.0, -armM],
      right: [0.0, armM],
    },
  };

  return tables[topology.toLowerCase()] || tables["quadcopter"];
}

// ─────────────────────────────────────────────────────────────
// Converter class
// ─────────────────────────────────────────────────────────────

export function convertJsonToUrdf(design: DroneDesign | any): URDFResult {
  const topology = design.topology || "quadcopter";
  const name = topology.replace(/\s+/g, "_");
  const frame = design.frame || {};
  const propulsion = design.propulsion || [];

  // 1. Generate links
  const links = generateLinks(frame);
  links.push(...generateMotorPropLinks(propulsion));

  // 2. Generate joints
  const joints = generateJoints(propulsion, frame, topology);

  // 3. XML URDF construction
  const urdf_content = buildURDFAny(name, links, joints);

  // 4. Companion SDF construction
  const sdf_content = buildSDF(design, name);

  // 5. Basic Validation
  const validation = validateURDF(links, joints);

  return {
    urdf_content,
    sdf_content,
    links,
    joints,
    validation,
    design_name: name,
  };
}

function generateLinks(frame: any): URDFLink[] {
  const cb = frame.center_body || {};
  const lM = (cb.length || DEFAULT_BODY_LENGTH_MM) / 1000.0;
  const wM = (cb.width || DEFAULT_BODY_WIDTH_MM) / 1000.0;
  const hM = (cb.height || DEFAULT_BODY_HEIGHT_MM) / 1000.0;

  // Body Mass
  const bodyMass = Math.max(lM * wM * hM * CARBON_FIBER_DENSITY_KG_M3, MIN_BODY_MASS_KG);

  const links: URDFLink[] = [
    {
      name: "base_link",
      mass_kg: bodyMass,
      origin_xyz: [0, 0, 0],
      origin_rpy: [0, 0, 0],
      geometry_type: "box",
      dimensions: { x: lM, y: wM, z: hM },
    },
  ];

  // Arm Mass
  const armMm = frame.arm_length_mm || DEFAULT_ARM_LENGTH_MM;
  const armLM = armMm / 1000.0;
  const armR = ARM_RADIUS_M;
  const armMass = Math.max(armLM * Math.PI * armR * armR * CARBON_FIBER_DENSITY_KG_M3, MIN_ARM_MASS_KG);

  for (let i = 0; i < 4; i++) {
    const angle = (45.0 + 90.0 * i) * (Math.PI / 180);
    const x = (armLM / 2.0) * Math.cos(angle);
    const y = (armLM / 2.0) * Math.sin(angle);

    links.push({
      name: `arm_${i}`,
      mass_kg: armMass,
      origin_xyz: [x, y, 0.0],
      origin_rpy: [0.0, 0.0, angle],
      geometry_type: "cylinder",
      dimensions: { radius: armR, length: armLM },
    });
  }

  return links;
}

function generateMotorPropLinks(propulsion: any[]): URDFLink[] {
  const links: URDFLink[] = [];
  propulsion.forEach((spec, idx) => {
    // Motor link
    links.push({
      name: `motor_${idx}`,
      mass_kg: MOTOR_MASS_KG,
      origin_xyz: [0, 0, 0],
      origin_rpy: [0, 0, 0],
      geometry_type: "cylinder",
      dimensions: { radius: MOTOR_RADIUS_M, length: MOTOR_LENGTH_M },
    });

    // Propeller radius parsing
    const propRadius = parsePropRadius(spec.propeller || "5045");
    links.push({
      name: `prop_${idx}`,
      mass_kg: PROPELLER_MASS_KG,
      origin_xyz: [0, 0, 0],
      origin_rpy: [0, 0, 0],
      geometry_type: "cylinder",
      dimensions: { radius: propRadius, length: PROPELLER_LENGTH_M },
    });
  });

  return links;
}

function parsePropRadius(spec: string): number {
  const m = spec.match(/(\d+(?:\.\d+)?)[xX]/);
  let inch = DEFAULT_PROPELLER_INCHES;
  if (m) {
    inch = parseFloat(m[1]);
  } else if (spec.match(/^\d{4,}/)) {
    const firstTwo = parseInt(spec.substring(0, 2));
    inch = firstTwo >= 20 ? firstTwo / 10.0 : firstTwo;
  } else {
    const numMatch = spec.match(/(\d+(?:\.\d+)?)/);
    if (numMatch) inch = parseFloat(numMatch[1]);
  }
  return (inch * 25.4) / 2000.0; // inch to meters
}

function generateJoints(propulsion: any[], frame: any, topology: string): URDFJoint[] {
  const joints: URDFJoint[] = [];
  const armM = (frame.arm_length_mm || DEFAULT_ARM_LENGTH_MM) / 1000.0;
  const posTable = motorXYTable(topology, armM);

  // Arm to base link
  for (let i = 0; i < 4; i++) {
    joints.push({
      name: `arm_${i}_joint`,
      joint_type: "fixed",
      parent: "base_link",
      child: `arm_${i}`,
      origin_xyz: [0, 0, 0],
      origin_rpy: [0, 0, 0],
      axis_xyz: [0, 0, 1],
    });
  }

  // Motor and Propeller joints
  propulsion.forEach((spec, idx) => {
    const posKey = spec.position || `motor_${idx}`;
    const xy = posTable[posKey] || [
      armM * 0.707,
      armM * 0.707 * (idx % 2 === 0 ? 1 : -1),
    ];
    const spin = idx % 2 === 0 ? 1.0 : -1.0;

    // Motor mount
    joints.push({
      name: `motor_${idx}_mount`,
      joint_type: "fixed",
      parent: "base_link",
      child: `motor_${idx}`,
      origin_xyz: [xy[0], xy[1], MOTOR_MOUNT_Z_OFFSET_M],
      origin_rpy: [0, 0, 0],
      axis_xyz: [0, 0, 1],
    });

    // Propeller joint
    joints.push({
      name: `prop_${idx}_joint`,
      joint_type: "continuous",
      parent: `motor_${idx}`,
      child: `prop_${idx}`,
      origin_xyz: [0.0, 0.0, PROP_JOINT_Z_OFFSET_M],
      origin_rpy: [0, 0, 0],
      axis_xyz: [0, 0, spin],
    });
  });

  return joints;
}

function buildURDFAny(name: string, links: URDFLink[], joints: URDFJoint[]): string {
  let xml = `<?xml version="1.0" encoding="utf-8"?>\n`;
  xml += `<robot name="${name}">\n`;

  links.forEach((lk) => {
    const dim = lk.dimensions;
    let geom = "";
    if (lk.geometry_type === "box") {
      geom = `<box size="${dim.x.toFixed(4)} ${dim.y.toFixed(4)} ${dim.z.toFixed(4)}" />`;
    } else {
      geom = `<cylinder radius="${dim.radius.toFixed(4)}" length="${dim.length.toFixed(4)}" />`;
    }

    const iv =
      lk.geometry_type === "box"
        ? boxInertia(lk.mass_kg, dim.x, dim.y, dim.z)
        : cylinderInertia(lk.mass_kg, dim.radius, dim.length);

    xml += `  <link name="${lk.name}">\n`;
    xml += `    <inertial>\n`;
    xml += `      <mass value="${lk.mass_kg.toFixed(6)}" />\n`;
    xml += `      <origin xyz="${lk.origin_xyz.map((v) => v.toFixed(4)).join(" ")}" rpy="${lk.origin_rpy.map((v) => v.toFixed(4)).join(" ")}" />\n`;
    xml += `      <inertia ixx="${iv.ixx.toFixed(8)}" ixy="${iv.ixy.toFixed(8)}" ixz="${iv.ixz.toFixed(8)}" iyy="${iv.iyy.toFixed(8)}" iyz="${iv.iyz.toFixed(8)}" izz="${iv.izz.toFixed(8)}" />\n`;
    xml += `    </inertial>\n`;
    xml += `    <visual>\n`;
    xml += `      <origin xyz="${lk.origin_xyz.map((v) => v.toFixed(4)).join(" ")}" rpy="${lk.origin_rpy.map((v) => v.toFixed(4)).join(" ")}" />\n`;
    xml += `      <geometry>\n`;
    xml += `        ${geom}\n`;
    xml += `      </geometry>\n`;
    xml += `      <material name="carbon_fiber">\n`;
    xml += `        <color rgba="0.1 0.1 0.1 1.0" />\n`;
    xml += `      </material>\n`;
    xml += `    </visual>\n`;
    xml += `    <collision>\n`;
    xml += `      <origin xyz="${lk.origin_xyz.map((v) => v.toFixed(4)).join(" ")}" rpy="${lk.origin_rpy.map((v) => v.toFixed(4)).join(" ")}" />\n`;
    xml += `      <geometry>\n`;
    xml += `        ${geom}\n`;
    xml += `      </geometry>\n`;
    xml += `    </collision>\n`;
    xml += `  </link>\n`;
  });

  joints.forEach((jt) => {
    xml += `  <joint name="${jt.name}" type="${jt.joint_type}">\n`;
    xml += `    <parent link="${jt.parent}" />\n`;
    xml += `    <child link="${jt.child}" />\n`;
    xml += `    <origin xyz="${jt.origin_xyz.map((v) => v.toFixed(4)).join(" ")}" rpy="${jt.origin_rpy.map((v) => v.toFixed(4)).join(" ")}" />\n`;
    if (jt.joint_type === "continuous") {
      xml += `    <axis xyz="${jt.axis_xyz.map((v) => v.toFixed(1)).join(" ")}" />\n`;
    }
    xml += `  </joint>\n`;
  });

  xml += `</robot>\n`;
  return xml;
}

function buildSDF(design: any, name: string): string {
  const frame = design.frame || {};
  const cb = frame.center_body || {};
  const lx = (cb.length || DEFAULT_BODY_LENGTH_MM) / 1000.0;
  const ly = (cb.width || DEFAULT_BODY_WIDTH_MM) / 1000.0;
  const lz = (cb.height || DEFAULT_BODY_HEIGHT_MM) / 1000.0;

  const bat = design.battery || {};
  const mass = ((bat.weight_g || DEFAULT_BATTERY_WEIGHT_G) + AVIONICS_OVERHEAD_WEIGHT_G) / 1000.0;

  let sdf = `<?xml version="1.0" ?>\n`;
  sdf += `<sdf version="1.9">\n`;
  sdf += `  <model name="${name}">\n`;
  sdf += `    <static>false</static>\n`;
  sdf += `    <link name="base_link">\n`;
  sdf += `      <pose>0 0 0.1 0 0 0</pose>\n`;
  sdf += `      <inertial>\n`;
  sdf += `        <mass>${mass.toFixed(3)}</mass>\n`;
  sdf += `        <inertia>\n`;
  sdf += `          <ixx>${DEFAULT_INERTIA_IXX}</ixx>\n`;
  sdf += `          <iyy>${DEFAULT_INERTIA_IYY}</iyy>\n`;
  sdf += `          <izz>${DEFAULT_INERTIA_IZZ}</izz>\n`;
  sdf += `          <ixy>0</ixy>\n`;
  sdf += `          <ixz>0</ixz>\n`;
  sdf += `          <iyz>0</iyz>\n`;
  sdf += `        </inertia>\n`;
  sdf += `      </inertial>\n`;
  sdf += `      <visual name="body_visual">\n`;
  sdf += `        <geometry>\n`;
  sdf += `          <box>\n`;
  sdf += `            <size>${lx.toFixed(4)} ${ly.toFixed(4)} ${lz.toFixed(4)}</size>\n`;
  sdf += `          </box>\n`;
  sdf += `        </geometry>\n`;
  sdf += `      </visual>\n`;
  sdf += `      <collision name="body_col">\n`;
  sdf += `        <geometry>\n`;
  sdf += `          <box>\n`;
  sdf += `            <size>${lx.toFixed(4)} ${ly.toFixed(4)} ${lz.toFixed(4)}</size>\n`;
  sdf += `          </box>\n`;
  sdf += `        </geometry>\n`;
  sdf += `      </collision>\n`;
  sdf += `    </link>\n`;
  sdf += `  </model>\n`;
  sdf += `</sdf>\n`;

  return sdf;
}

function validateURDF(links: URDFLink[], joints: URDFJoint[]): ValidationResult {
  const errors: string[] = [];
  const warnings: string[] = [];

  const linkNames = new Set(links.map((l) => l.name));
  if (links.length === 0) {
    errors.push("URDF contains no links.");
  }
  if (!linkNames.has("base_link")) {
    errors.push("Required 'base_link' is missing.");
  }

  const connected = new Set<string>(["base_link"]);
  joints.forEach((jt) => {
    if (!linkNames.has(jt.parent)) {
      errors.push(`Joint '${jt.name}' parent '${jt.parent}' not declared as a link.`);
    }
    if (!linkNames.has(jt.child)) {
      errors.push(`Joint '${jt.name}' child '${jt.child}' not declared as a link.`);
    }
    connected.add(jt.child);
  });

  const orphans = Array.from(linkNames).filter((name) => !connected.has(name));
  if (orphans.length > 0) {
    warnings.push(`Links not connected via any joint: ${orphans.join(", ")}`);
  }

  return {
    valid: errors.length === 0,
    errors,
    warnings,
  };
}

/**
 * @deprecated Use plain function convertJsonToUrdf directly.
 */
export class JsonToUrdfConverter {
  public static convert(design: DroneDesign | any): URDFResult {
    return convertJsonToUrdf(design);
  }
}
