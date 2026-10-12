import { beforeEach, describe, expect, it } from 'vitest'
import { useDroneStore } from '../stores/drone-store'

describe('drone-store telemetry normalization', () => {
  beforeEach(() => {
    useDroneStore.getState().reset()
  })

  it('normalizes flat Python telemetry points correctly', () => {
    const store = useDroneStore.getState()

    const flatPoint = {
      timestamp_s: 2.5,
      north_m: 14.2,
      east_m: -8.5,
      alt_m: 12.0,
      lat: 24.7136,
      lon: 46.6753,
      vel_north_ms: 3.5,
      vel_east_ms: 1.2,
      vel_down_ms: 0.1,
      speed_ms: 3.7,
      roll_deg: 2.1,
      pitch_deg: -1.0,
      yaw_deg: 85.4,
      battery_percent: 94.5,
      battery_voltage: 12.2,
      flight_mode: 'MISSION',
      armed: true,
    }

    store.addTelemetry(flatPoint as any, true)

    const latest = useDroneStore.getState().telemetryStream[0]
    expect(latest).toBeDefined()
    expect(latest.timestamp_ms).toBe(2500)
    expect(latest.position?.north_m).toBe(14.2)
    expect(latest.position?.east_m).toBe(-8.5)
    expect(latest.position?.alt_m).toBe(12.0)
    expect(latest.position?.lat).toBe(24.7136)
    expect(latest.position?.lon).toBe(46.6753)
    expect(latest.attitude?.yaw_deg).toBe(85.4)
    expect(latest.velocity?.north_ms).toBe(3.5)
    expect(latest.battery?.percent).toBe(94.5)
    expect(latest.battery_pct).toBe(94.5)
    expect(latest.speed_ms).toBe(3.7)
    expect(latest.armed).toBe(true)
    expect(latest.flight_mode).toBe('MISSION')
    expect(useDroneStore.getState().isLive).toBe(true)
  })

  it('preserves nested telemetry points if already structured', () => {
    const store = useDroneStore.getState()

    const nestedPoint = {
      timestamp_ms: 1000,
      position: {
        north_m: 5.0,
        east_m: 10.0,
        alt_m: 15.0,
        lat: 0,
        lon: 0,
      },
      attitude: {
        roll_deg: 0,
        pitch_deg: 0,
        yaw_deg: 90,
      },
      velocity: {
        north_ms: 1,
        east_ms: 2,
        down_ms: 0,
      },
      battery: {
        voltage_v: 12.4,
        percent: 88,
        remaining_s: 400,
      },
      flight_mode: 'AUTO.TAKEOFF',
      armed: true,
    }

    store.addTelemetry(nestedPoint as any, false)

    const latest = useDroneStore.getState().telemetryStream[0]
    expect(latest.position?.north_m).toBe(5.0)
    expect(latest.position?.east_m).toBe(10.0)
    expect(latest.position?.alt_m).toBe(15.0)
    expect(latest.attitude?.yaw_deg).toBe(90)
    expect(latest.battery?.percent).toBe(88)
    expect(latest.flight_mode).toBe('AUTO.TAKEOFF')
    expect(useDroneStore.getState().isLive).toBe(false)
  })
})

describe('drone-store batching and de-duplication', () => {
  beforeEach(() => {
    useDroneStore.getState().reset()
  })

  it('appends a telemetry batch in one update and keeps the last 500 points', () => {
    const store = useDroneStore.getState()
    const points = Array.from({ length: 620 }, (_, i) => ({ timestamp_s: i, alt_m: i }))

    store.addTelemetryBatch(points as any[], false)

    const stream = useDroneStore.getState().telemetryStream
    expect(stream).toHaveLength(500)
    expect(stream[0].position?.alt_m).toBe(120)
    expect(stream[499].position?.alt_m).toBe(619)
    expect(useDroneStore.getState().isLive).toBe(false)
  })

  it('counts every log line even once the ring buffer is full', () => {
    const store = useDroneStore.getState()
    for (let i = 0; i < 230; i++) store.addLog(`line ${i}`)

    const state = useDroneStore.getState()
    expect(state.pipelineLogs).toHaveLength(200)
    expect(state.pipelineLogTotal).toBe(230)
  })

  it('replaces a simulation that arrives twice for the same iteration', () => {
    const store = useDroneStore.getState()
    const base = { project_id: 'p', mission_id: '', status: 'complete', telemetry: [], semantic_observations: [] }

    store.addSimulation({ ...base, id: 'live', iteration: 1, passed: true } as any)
    store.addSimulation({ ...base, id: 'hydrated', iteration: 1, passed: true, duration_s: 63 } as any)

    const sims = useDroneStore.getState().simulations
    expect(sims).toHaveLength(1)
    expect((sims[0] as any).duration_s).toBe(63)
  })

  it('attaches the evaluator verdict to the matching run, or the latest one', () => {
    const store = useDroneStore.getState()
    const base = { project_id: 'p', mission_id: '', status: 'complete', telemetry: [], semantic_observations: [] }
    store.addSimulation({ ...base, id: 'a', iteration: 1, passed: false } as any)

    store.setSimulationEvaluation(1, { passed: true, score: 85, feedback_for_code_agent: '' })
    expect(useDroneStore.getState().simulations[0].evaluation?.score).toBe(85)
    expect(useDroneStore.getState().simulations[0].passed).toBe(true)

    // Unknown iteration: falls back to the newest run rather than being lost.
    store.setSimulationEvaluation(0, { passed: true, score: 90, feedback_for_code_agent: '' })
    expect(useDroneStore.getState().simulations[0].evaluation?.score).toBe(90)
  })

  it('collapses a repeated activity line into one entry', () => {
    const store = useDroneStore.getState()
    store.addActivity({ agent: 'HITL', status: 'running', message: 'waiting', timestamp: 1 })
    store.addActivity({ agent: 'HITL', status: 'running', message: 'waiting', timestamp: 2 })
    store.addActivity({ agent: 'HITL', status: 'done', message: 'waiting', timestamp: 3 })

    const feed = useDroneStore.getState().activityFeed
    expect(feed).toHaveLength(2)
    expect(feed[1].timestamp).toBe(2)
  })
})


describe('flight home', () => {
  beforeEach(() => useDroneStore.getState().reset())

  it('keeps home after the display buffer discards the takeoff sample', () => {
    const store = useDroneStore.getState()
    store.addTelemetryBatch(Array.from({ length: 600 }, (_, i) => ({
      timestamp_s: i, lat: i / 1000, lon: i / 1000,
    })) as any, true)
    expect(useDroneStore.getState().telemetryStream).toHaveLength(500)
    expect(useDroneStore.getState().flightHome).toEqual([0, 0])
  })

  it('clears home when switching projects', () => {
    const store = useDroneStore.getState()
    store.setProjectId('first')
    store.addTelemetry({ timestamp_s: 0, lat: 1, lon: 2 } as any)
    store.setProjectId('second')
    expect(useDroneStore.getState().flightHome).toBeNull()
    expect(useDroneStore.getState().telemetryStream).toEqual([])
  })
})

 it('does not turn a missing GPS fix into home at zero degrees', () => {
   useDroneStore.getState().reset()
   useDroneStore.getState().addTelemetry({ timestamp_s: 0, north_m: 1, east_m: 2 } as any)
   expect(useDroneStore.getState().flightHome).toBeNull()
 })
