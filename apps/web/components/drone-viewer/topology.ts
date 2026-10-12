/**
 * Topology resolver — turns any design-tree shape (the store's DroneDesign or
 * the raw backend pydantic projection) into a single, render-ready description.
 *
 * It deliberately reads several possible signals because the WS event payload
 * and the backend REST projection use slightly different schemas:
 *   - `topology`           backend pydantic  ("quadcopter" | … | "fixed_wing")
 *   - `frame.type`         store shape       ("quadrotor" | … | "fixed_wing")
 *   - `motors[]`           store motor list (count)
 *   - `propulsion[]`       backend motor list (count)
 */

export type TopologyKind = 'multirotor' | 'fixed_wing' | 'hybrid'

export interface TopologyLayout {
  kind: TopologyKind
  /** Number of lift/thrust rotors to draw. */
  rotorCount: number
  /** Human label, e.g. "Hexacopter" / "Fixed-wing". */
  label: string
}

const COUNT_BY_NAME: Record<string, number> = {
  bicopter: 2,
  tricopter: 3,
  quadcopter: 4,
  quadrotor: 4,
  hexacopter: 6,
  hexarotor: 6,
  octocopter: 8,
  octorotor: 8,
}

function multirotorLabel(n: number): string {
  switch (n) {
    case 2:  return 'Bicopter'
    case 3:  return 'Tricopter'
    case 6:  return 'Hexacopter'
    case 8:  return 'Octocopter'
    default: return 'Quadcopter'
  }
}

/** Resolve a render-ready topology from any design-tree-ish object. */
export function resolveTopology(tree: unknown): TopologyLayout {
  const t = (tree ?? {}) as Record<string, unknown>
  const frame = (t.frame ?? {}) as Record<string, unknown>
  const raw = `${t.topology ?? ''} ${frame.type ?? ''}`.toLowerCase()

  const motorCount =
    (Array.isArray(t.motors) && t.motors.length) ||
    (Array.isArray(t.propulsion) && t.propulsion.length) ||
    0

  if (/fixed[_\s-]?wing|airplane|\bplane\b|winged|two[_\s-]?wings?|delta/.test(raw)) {
    return { kind: 'fixed_wing', rotorCount: motorCount || 1, label: 'Fixed-wing' }
  }
  if (/hybrid|vtol|tilt/.test(raw)) {
    return { kind: 'hybrid', rotorCount: motorCount || 5, label: 'VTOL hybrid' }
  }

  let n = motorCount
  if (!n) {
    for (const [name, count] of Object.entries(COUNT_BY_NAME)) {
      if (raw.includes(name)) { n = count; break }
    }
  }
  if (!n || n < 2) n = 4
  return { kind: 'multirotor', rotorCount: n, label: multirotorLabel(n) }
}
