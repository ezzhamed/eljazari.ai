/**
 * The Design agent's output, read into the figures the pages show.
 *
 * The backend stores the agent's own tree (agents/design_models.py:
 * `propulsion[]` of motor/propeller strings, `battery.weight_g`,
 * `payload.installed_weight_g`, ...), while the shared TypeScript type still
 * describes an older shape (`motors[]`, `mass_g`, `estimated_mass_kg`). The
 * pages read the old names and showed "—" for nearly everything, and the
 * overview filled the motor count with a default 4. This reads either shape,
 * and returns null for anything neither contains.
 *
 * The estimate is the Design agent's calculator, sent with the live
 * `design_ready` event; it is not stored with the design, so a reloaded run has
 * none. The catalogue components chosen after approval, and their sourced
 * performance, are not sent to the browser at all yet.
 */

type Json = Record<string, unknown>

export interface DesignView {
  topology: string | null
  rotorCount: number | null
  frame: {
    type: string | null
    armLengthMm: number | null
    wheelbaseMm: number | null
    body: string | null
    material: string | null
    massG: number | null
  }
  motor: string | null
  propeller: string | null
  battery: {
    chemistry: string | null
    cells: number | null
    capacityMah: number | null
    massG: number | null
    voltage: number | null
  }
  payload: {
    installedG: number | null
    maxG: number | null
    mount: string | null
    description: string | null
  }
  avionics: string[]
  estimate: {
    totalWeightG: number | null
    hoverMin: number | null
    maxSpeedMs: number | null
    maxDistanceM: number | null
    thrustToWeight: number | null
  } | null
  /** How the design was produced: rag_llm, llm_only or rule_based. */
  method: string | null
}

function obj(value: unknown): Json {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Json) : {}
}

function num(...values: unknown[]): number | null {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return null
}

function str(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return null
}

export function readDesign(design: unknown): DesignView | null {
  const record = obj(design)
  const tree = obj(record.design_tree ?? design)
  if (Object.keys(tree).length === 0) return null

  const frame = obj(tree.frame)
  const body = obj(frame.center_body ?? frame.body_dimensions)
  const propulsion = Array.isArray(tree.propulsion) ? tree.propulsion.map(obj) : []
  const motors = Array.isArray(tree.motors) ? tree.motors.map(obj) : []
  const propellers = Array.isArray(tree.propellers) ? tree.propellers.map(obj) : []
  const battery = obj(tree.battery)
  const payload = obj(tree.payload)
  const electronics = obj(tree.electronics)
  const sensors = Array.isArray(tree.sensors) ? tree.sensors.map(obj) : []
  const estimate = obj(record.performance_estimate)
  const generation = obj(record.design_generation)

  const firstMotor = propulsion[0] ?? {}
  const legacyMotor = motors[0] ?? {}
  const legacyProp = propellers[0] ?? {}
  const kv = num(legacyMotor.kv_rating, legacyMotor.kv)
  const bodyLength = num(body.length, body.length_mm)
  const bodyWidth = num(body.width, body.width_mm)
  const bodyHeight = num(body.height, body.height_mm)

  const avionics = [
    str(electronics.flight_controller) && `${electronics.flight_controller} flight controller`,
    electronics.gps === true ? 'GPS' : null,
    electronics.compass === true ? 'Compass' : null,
    ...sensors.map((s) => str(s.model, s.type)),
  ].filter((x): x is string => Boolean(x))

  const legacyMassKg = num(tree.estimated_mass_kg)
  const legacyFlightMin = num(tree.estimated_flight_time_min)
  const hoverS = num(estimate.hover_time_s)
  const hasEstimate = Object.keys(estimate).length > 0 || legacyMassKg != null || legacyFlightMin != null

  return {
    topology: str(tree.topology),
    rotorCount: propulsion.length || motors.length || null,
    frame: {
      type: str(frame.type),
      armLengthMm: num(frame.arm_length_mm),
      wheelbaseMm: num(frame.wheelbase_mm),
      body: bodyLength && bodyWidth ? `${bodyLength} × ${bodyWidth}${bodyHeight ? ` × ${bodyHeight}` : ''} mm` : null,
      material: str(frame.material),
      massG: num(frame.mass_g, frame.weight_g),
    },
    motor: str(firstMotor.motor) ?? (kv != null ? `${kv} KV` : null),
    propeller:
      str(firstMotor.propeller) ??
      (num(legacyProp.diameter_inch) != null ? `${legacyProp.diameter_inch} × ${legacyProp.pitch_inch} in` : null),
    battery: {
      chemistry: str(battery.type),
      cells: num(battery.cells),
      capacityMah: num(battery.capacity_mah),
      massG: num(battery.weight_g, battery.mass_g),
      voltage: num(battery.voltage_nominal),
    },
    payload: {
      installedG: num(payload.installed_weight_g, payload.mass_g),
      maxG: num(payload.max_weight_g),
      mount: str(payload.mount_type),
      description: str(payload.description),
    },
    avionics,
    estimate: hasEstimate
      ? {
          totalWeightG: num(estimate.total_weight_g) ?? (legacyMassKg != null ? legacyMassKg * 1000 : null),
          hoverMin: hoverS != null ? hoverS / 60 : legacyFlightMin,
          maxSpeedMs: num(estimate.max_speed_ms),
          maxDistanceM: num(estimate.max_distance_m),
          thrustToWeight: num(estimate.thrust_to_weight_ratio),
        }
      : null,
    method: str(generation.method),
  }
}

/** "1,314 g"-style formatting; null stays null so the UI shows "—". */
export function fmt(value: number | null | undefined, unit: string, digits = 0): string | null {
  if (value == null || !Number.isFinite(value)) return null
  return `${value.toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits })} ${unit}`
}

export const METHOD_LABEL: Record<string, string> = {
  rag_llm: 'Language model with AircraftVerse references',
  llm_only: 'Language model without references',
  rule_based: 'Deterministic baseline (the model’s design was not usable)',
}
