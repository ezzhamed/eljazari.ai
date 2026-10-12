/**
 * Tests for resolve-design.ts — the merge of the Design agent's tree with the
 * user's live parameter overrides.
 *
 * This is the layer the 3D mesh reads, so a mistake here shows up as a model
 * that silently disagrees with the numbers on screen.
 */

import { describe, expect, it } from 'vitest'
import { EMPTY_PARAMS, type DesignParams } from '@/lib/stores/design-params-store'
import { resolveDesign } from '../resolve-design'

const IN_TO_M = 0.0254

const agentTree = {
  topology: 'quadcopter',
  frame: { type: 'X', arm_length_mm: 360 },
  battery: { capacity_mah: 5200, cells: 4 },
  propulsion: [{ motor: 'x', propeller: '1045' }, {}, {}, {}],
}

function params(overrides: Partial<DesignParams> = {}): DesignParams {
  return { ...EMPTY_PARAMS, ...overrides }
}

describe('following the agent', () => {
  it('uses the agent design when nothing is overridden', () => {
    const r = resolveDesign(agentTree, params())

    expect(r.kind).toBe('multirotor')
    expect(r.rotorCount).toBe(4)
    expect(r.label).toBe('Quadcopter')
    expect(r.armLengthM).toBeCloseTo(0.36)
    expect(r.overridden.size).toBe(0)
  })

  it('falls back to reference values with no design at all', () => {
    const r = resolveDesign(null, params())

    expect(r.armLengthM).toBeCloseTo(0.35)
    expect(r.rotorCount).toBe(4)
    expect(r.batteryScale).toBeCloseTo(1)
  })
})

describe('overrides', () => {
  it('replaces only the overridden value', () => {
    const r = resolveDesign(agentTree, params({ armLengthMm: 500 }))

    expect(r.armLengthM).toBeCloseTo(0.5)
    expect(r.rotorCount).toBe(4) // still the agent's
    expect([...r.overridden]).toEqual(['armLengthMm'])
  })

  it('relabels when the rotor count changes', () => {
    expect(resolveDesign(agentTree, params({ rotorCount: 6 })).label).toBe('Hexacopter')
    expect(resolveDesign(agentTree, params({ rotorCount: 8 })).label).toBe('Octocopter')
  })

  it('switches airframe kind', () => {
    const r = resolveDesign(agentTree, params({ airframe: 'hybrid' }))

    expect(r.kind).toBe('hybrid')
    expect(r.label).toBe('VTOL hybrid')
  })

  it('tracks every overridden key', () => {
    const r = resolveDesign(
      agentTree,
      params({ airframe: 'hybrid', rotorCount: 6, armLengthMm: 400 }),
    )

    expect(r.overridden.size).toBe(3)
  })
})

describe('propeller diameter', () => {
  it('reports the diameter it is given, in metres', () => {
    // Regression: propDiameterM was fed to the mesh as a radius while
    // SpinningProp sizes its box by full span, so a 10 in prop rendered 5 in
    // wide and the panel's number disagreed with the model.
    const r = resolveDesign(agentTree, params({ propDiameterIn: 10 }))

    expect(r.propDiameterM).toBeCloseTo(10 * IN_TO_M)
  })

  it('derives a sane default from arm length when the design has none', () => {
    const bare = { frame: { arm_length_mm: 360 } }
    const r = resolveDesign(bare, params())

    // Matches the span the mesh used before propeller size was adjustable.
    expect(r.propDiameterM).toBeCloseTo(0.36 * 0.72)
    expect(r.propDiameterM / IN_TO_M).toBeGreaterThan(9)
    expect(r.propDiameterM / IN_TO_M).toBeLessThan(11)
  })

  it('reads a millimetre diameter resolved from the AircraftVerse corpus', () => {
    const tree = {
      ...agentTree,
      propulsion: [{ motor: 'kde', propeller: 'apc_15x8E', prop_diameter_mm: 381 }],
    }

    expect(resolveDesign(tree, params()).propDiameterM).toBeCloseTo(0.381, 3)
  })

  it('parses the compact frontend propeller code', () => {
    const tree = { ...agentTree, propulsion: [{ propeller: '5045' }] }

    expect(resolveDesign(tree, params()).propDiameterM / IN_TO_M).toBeCloseTo(50, 0)
  })

  it('parses a cross-notation propeller name', () => {
    const tree = { ...agentTree, propulsion: [{ propeller: '15x8E' }] }

    expect(resolveDesign(tree, params()).propDiameterM / IN_TO_M).toBeCloseTo(15)
  })
})

describe('battery scale', () => {
  it('is 1 at the reference pack', () => {
    const tree = { ...agentTree, battery: { capacity_mah: 5000 } }

    expect(resolveDesign(tree, params()).batteryScale).toBeCloseTo(1)
  })

  it('grows sub-linearly so a huge pack stays renderable', () => {
    // Cube root: a 10x pack is ~2.15x longer, not 10x.
    const r = resolveDesign(agentTree, params({ batteryCapacityMah: 50000 }))

    expect(r.batteryScale).toBeCloseTo(Math.cbrt(10), 2)
    expect(r.batteryScale).toBeLessThan(3)
  })

  it('shrinks for a small pack', () => {
    expect(
      resolveDesign(agentTree, params({ batteryCapacityMah: 625 })).batteryScale,
    ).toBeLessThan(1)
  })
})

describe('arm scale', () => {
  it('is 1 at the 350 mm reference airframe', () => {
    const tree = { frame: { arm_length_mm: 350 } }

    expect(resolveDesign(tree, params()).armScale).toBeCloseTo(1)
  })

  it('scales linearly with arm length', () => {
    expect(resolveDesign(agentTree, params({ armLengthMm: 700 })).armScale).toBeCloseTo(2)
  })
})
