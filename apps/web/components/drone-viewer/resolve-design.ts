/**
 * Merge the Design agent's tree with the user's live parameter overrides into
 * the concrete numbers the mesh renders.
 *
 * The agent's design stays the baseline; an override replaces one value without
 * touching the rest, so clearing it snaps back to what the agent produced.
 * Every field here drives visible geometry — see design-params-store.ts.
 */

import type { DesignParams } from '@/lib/stores/design-params-store'
import { resolveTopology, type TopologyKind } from './topology'

export interface ResolvedDesign {
  kind: TopologyKind
  rotorCount: number
  label: string
  /** Metres, as the mesh works in metres. */
  armLengthM: number
  /** Metres, tip to tip. */
  propDiameterM: number
  /** Relative to the 350 mm reference airframe. */
  armScale: number
  /** Battery block scale, 1.0 at the 5000 mAh reference pack. */
  batteryScale: number
  /** Which values the user is currently overriding. */
  overridden: Set<keyof DesignParams>
}

const REFERENCE_ARM_MM = 350
const REFERENCE_BATTERY_MAH = 5000
const IN_TO_M = 0.0254

function multirotorLabel(n: number): string {
  switch (n) {
    case 2:
      return 'Bicopter'
    case 3:
      return 'Tricopter'
    case 6:
      return 'Hexacopter'
    case 8:
      return 'Octocopter'
    default:
      return 'Quadcopter'
  }
}

function labelFor(kind: TopologyKind, rotorCount: number): string {
  if (kind === 'fixed_wing') return 'Fixed-wing'
  if (kind === 'hybrid') return 'VTOL hybrid'
  return multirotorLabel(rotorCount)
}

/** Read the agent's battery capacity from either schema shape. */
function agentBatteryMah(tree: unknown): number | null {
  const battery = ((tree ?? {}) as Record<string, unknown>).battery as
    | Record<string, unknown>
    | undefined
  const value = battery?.capacity_mah ?? battery?.capacityMah
  return typeof value === 'number' && value > 0 ? value : null
}

/** Read the agent's propeller diameter, which the schemas express in inches. */
function agentPropDiameterIn(tree: unknown): number | null {
  const t = (tree ?? {}) as Record<string, unknown>
  const list = (Array.isArray(t.propulsion) ? t.propulsion : t.motors) as
    | Record<string, unknown>[]
    | undefined
  const first = list?.[0]
  if (!first) return null

  // Backend records resolve propellers against the AircraftVerse corpus and
  // carry a millimetre diameter; the store shape uses an inch string such as
  // "5045" (5.0 in) or "15x8E".
  const mm = first.prop_diameter_mm
  if (typeof mm === 'number' && mm > 0) return mm / 1000 / IN_TO_M

  const name = typeof first.propeller === 'string' ? first.propeller : ''
  const cross = name.match(/(\d+(?:\.\d+)?)\s*[xX]/)
  if (cross) return parseFloat(cross[1])
  const compact = name.match(/(\d{4})/)
  if (compact) return parseInt(compact[1].slice(0, 2), 10)

  return null
}

export function resolveDesign(tree: unknown, params: DesignParams): ResolvedDesign {
  const base = resolveTopology(tree)

  const kind = params.airframe ?? base.kind
  const rotorCount = params.rotorCount ?? base.rotorCount

  const agentArmMm = ((tree ?? {}) as { frame?: { arm_length_mm?: number } })?.frame
    ?.arm_length_mm
  const armLengthMm = params.armLengthMm ?? agentArmMm ?? REFERENCE_ARM_MM

  // Falls back to a diameter proportional to the arm, which is what the mesh
  // assumed before propeller size was adjustable.
  const propDiameterIn =
    params.propDiameterIn ??
    agentPropDiameterIn(tree) ??
    (armLengthMm / 1000) * 0.72 / IN_TO_M

  const batteryMah =
    params.batteryCapacityMah ?? agentBatteryMah(tree) ?? REFERENCE_BATTERY_MAH

  const overridden = new Set(
    (Object.keys(params) as (keyof DesignParams)[]).filter((key) => params[key] !== null),
  )

  return {
    kind,
    rotorCount,
    label: labelFor(kind, rotorCount),
    armLengthM: armLengthMm / 1000,
    propDiameterM: propDiameterIn * IN_TO_M,
    armScale: armLengthMm / REFERENCE_ARM_MM,
    // Cube-root so a 10x capacity pack is ~2.15x longer, not 10x.
    batteryScale: Math.cbrt(batteryMah / REFERENCE_BATTERY_MAH),
    overridden,
  }
}
