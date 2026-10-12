/**
 * DroneViewer — shared types, constants and camera helpers.
 * Pure TypeScript — no React or Three.js imports so it can be
 * imported by hooks, components, and tests alike.
 */

// ─────────────────────────────────────────────────────────────
// View modes
// ─────────────────────────────────────────────────────────────

export type ViewMode = 'solid' | 'wireframe' | 'transparent'

export type CameraPreset = 'isometric' | 'top' | 'front' | 'side' | 'orbit'

// ─────────────────────────────────────────────────────────────
// Component colour palette (solid mode)
// ─────────────────────────────────────────────────────────────

export const COMPONENT_COLORS: Record<string, string> = {
  body:       '#1e2d44',
  arm:        '#16263e',
  motor:      '#0f3460',
  propeller:  '#cdb892',
  battery:    '#4a9eff',
  sensor:     '#ff8800',
  payload:    '#ff4466',
  gps:        '#ffcc00',
}

/** Emissive glow colours for each component (accent lighting). */
export const EMISSIVE_COLORS: Record<string, string> = {
  body:       '#001133',
  arm:        '#001122',
  motor:      '#003388',
  propeller:  '#005533',
  battery:    '#002288',
  sensor:     '#441100',
  payload:    '#440011',
  gps:        '#443300',
}

// ─────────────────────────────────────────────────────────────
// Camera presets  [x, y, z]
// ─────────────────────────────────────────────────────────────

export const CAMERA_PRESETS: Record<CameraPreset, [number, number, number]> = {
  isometric: [3,   2.5,  3],
  top:       [0,   6,    0.001],
  front:     [0,   1,    5],
  side:      [5,   1,    0],
  orbit:     [2.5, 1.5,  2.5],
}

// ─────────────────────────────────────────────────────────────
// Drone geometry constants  (all in Three.js units ≈ metres)
// ─────────────────────────────────────────────────────────────

/** Arm positions for standard X-frame quadcopter. */
export const QUAD_ARM_POSITIONS: [number, number, number][] = [
  [ 0.35, 0,  0.35],   // front-right
  [-0.35, 0,  0.35],   // front-left
  [ 0.35, 0, -0.35],   // rear-right
  [-0.35, 0, -0.35],   // rear-left
]

/** +1 = CCW spin, -1 = CW spin (standard X-frame convention). */
export const PROP_SPIN_DIR = [1, -1, -1, 1]

// ─────────────────────────────────────────────────────────────
// Annotation definitions
// ─────────────────────────────────────────────────────────────

export interface AnnotationDef {
  id:          string
  label:       string
  description: string
  position:    [number, number, number]
  component:   keyof typeof COMPONENT_COLORS
}

export const DEFAULT_ANNOTATIONS: AnnotationDef[] = [
  {
    id: 'body',
    label: 'Frame Body',
    description: 'Carbon-fibre central plate, houses FC + GPS',
    position: [0, 0.15, 0],
    component: 'body',
  },
  {
    id: 'motor_0',
    label: 'Motor FR',
    description: '2206-2300KV brushless — CCW',
    position: [0.35, 0.12, 0.35],
    component: 'motor',
  },
  {
    id: 'motor_1',
    label: 'Motor FL',
    description: '2206-2300KV brushless — CW',
    position: [-0.35, 0.12, 0.35],
    component: 'motor',
  },
  {
    id: 'motor_2',
    label: 'Motor RR',
    description: '2206-2300KV brushless — CW',
    position: [0.35, 0.12, -0.35],
    component: 'motor',
  },
  {
    id: 'motor_3',
    label: 'Motor RL',
    description: '2206-2300KV brushless — CCW',
    position: [-0.35, 0.12, -0.35],
    component: 'motor',
  },
  {
    id: 'battery',
    label: 'Battery',
    description: '4S 5000mAh LiPo — 480g',
    position: [0, -0.06, 0],
    component: 'battery',
  },
]

// ─────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────

/** Scale factor so mission distance fits inside the 3D viewport. */
export function computeScaleFactor(
  maxRealDistanceM: number,
  targetVisualRadius = 5,
): number {
  if (maxRealDistanceM <= 0) return 1
  return maxRealDistanceM > targetVisualRadius
    ? targetVisualRadius / maxRealDistanceM
    : 1
}

/** Interpolate a position along a list of points at normalised t ∈ [0,1]. */
export function interpolatePath(
  points: { x: number; y: number; z: number }[],
  t: number,
): { x: number; y: number; z: number } {
  if (points.length === 0) return { x: 0, y: 0, z: 0 }
  if (points.length === 1) return points[0]
  const clamped = Math.max(0, Math.min(1, t))
  const idx = clamped * (points.length - 1)
  const lo  = Math.floor(idx)
  const hi  = Math.min(lo + 1, points.length - 1)
  const frac = idx - lo
  return {
    x: points[lo].x + (points[hi].x - points[lo].x) * frac,
    y: points[lo].y + (points[hi].y - points[lo].y) * frac,
    z: points[lo].z + (points[hi].z - points[lo].z) * frac,
  }
}
