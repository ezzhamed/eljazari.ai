import { describe, expect, it } from 'vitest'
import { fmt, readDesign } from '@/lib/design-view'

// The shape agents/design_models.py produces, as stored for a real run on 2026-09-30.
const LIVE = {
  design_tree: {
    topology: 'quadcopter',
    frame: { type: 'X', arm_length_mm: 381, center_body: { length: 180, width: 120, height: 45 } },
    propulsion: [
      { motor: 'MN3508-380KV', propeller: '1344', position: 'front_right' },
      { motor: 'MN3508-380KV', propeller: '1344', position: 'front_left' },
      { motor: 'MN3508-380KV', propeller: '1344', position: 'rear_left' },
      { motor: 'MN3508-380KV', propeller: '1344', position: 'rear_right' },
    ],
    battery: { type: 'LiPo', cells: 6, capacity_mah: 17000, weight_g: 480 },
    payload: { max_weight_g: 500, installed_weight_g: 150, mount_type: 'gimbal' },
    electronics: { flight_controller: 'PX4', gps: true, compass: true },
  },
  performance_estimate: { hover_time_s: 1566, max_speed_ms: 18.2, max_distance_m: 900, total_weight_g: 1314, thrust_to_weight_ratio: 2.03 },
  design_generation: { method: 'rule_based', repair_attempts: 0 },
}

describe('readDesign', () => {
  it('reads the tree the backend actually stores', () => {
    const d = readDesign(LIVE)!
    expect(d.topology).toBe('quadcopter')
    expect(d.rotorCount).toBe(4)
    expect(d.motor).toBe('MN3508-380KV')
    expect(d.propeller).toBe('1344')
    expect(d.frame).toMatchObject({ type: 'X', armLengthMm: 381, body: '180 × 120 × 45 mm' })
    expect(d.battery).toMatchObject({ chemistry: 'LiPo', cells: 6, capacityMah: 17000, massG: 480, voltage: null })
    expect(d.payload).toMatchObject({ installedG: 150, maxG: 500, mount: 'gimbal' })
    expect(d.avionics).toEqual(['PX4 flight controller', 'GPS', 'Compass'])
    expect(d.estimate).toMatchObject({ totalWeightG: 1314, hoverMin: 26.1, thrustToWeight: 2.03 })
    expect(d.method).toBe('rule_based')
  })

  it('has no estimate for a design reloaded from the API', () => {
    const d = readDesign({ design_tree: LIVE.design_tree })!
    expect(d.estimate).toBeNull()
    expect(d.rotorCount).toBe(4)
  })

  it('still reads the older shape, and invents nothing it lacks', () => {
    const d = readDesign({
      design_tree: {
        frame: { type: 'quadrotor', wheelbase_mm: 450, material: 'carbon', mass_g: 300 },
        motors: [{ kv_rating: 920 }, { kv_rating: 920 }],
        propellers: [{ diameter_inch: 10, pitch_inch: 4.5 }],
        battery: { cells: 4, capacity_mah: 5000, mass_g: 520, voltage_nominal: 14.8 },
        estimated_mass_kg: 1.2,
        estimated_flight_time_min: 18,
      },
    })!
    expect(d.rotorCount).toBe(2)
    expect(d.motor).toBe('920 KV')
    expect(d.propeller).toBe('10 × 4.5 in')
    expect(d.battery).toMatchObject({ massG: 520, voltage: 14.8 })
    expect(d.estimate).toMatchObject({ totalWeightG: 1200, hoverMin: 18, thrustToWeight: null })
    expect(d.payload.installedG).toBeNull()
  })

  it('returns null without a design, and fmt keeps missing values missing', () => {
    expect(readDesign(null)).toBeNull()
    expect(readDesign({})).toBeNull()
    expect(fmt(null, 'g')).toBeNull()
    expect(fmt(1314, 'g')).toBe('1,314 g')
  })
})
