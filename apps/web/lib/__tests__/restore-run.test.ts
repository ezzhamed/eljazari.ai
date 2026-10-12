import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api'
import { useDroneStore } from '../stores/drone-store'

// After a reload the pages read an empty store until the saved run is fetched.
// They must say "loading" for that window, not "no design yet".
describe('restoring a saved run', () => {
  beforeEach(() => {
    useDroneStore.getState().reset()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('is flagged while the saved run loads and fills design, code and report', async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    vi.spyOn(api, 'getProject').mockImplementation(async () => {
      await gate
      return { id: 'p1', name: 'Run', prompt: 'hover', status: 'completed' } as never
    })
    vi.spyOn(api, 'getDesign').mockResolvedValue({ design_tree: { topology: 'quadcopter' }, status: 'approved' } as never)
    vi.spyOn(api, 'getCode').mockResolvedValue({ code: 'print(1)', status: 'approved', safety_report: null } as never)
    vi.spyOn(api, 'getSimulationResults').mockResolvedValue({ simulations: [] } as never)
    vi.spyOn(api, 'getFlightLogs').mockResolvedValue({ logs: [], total: 0 } as never)
    vi.spyOn(api, 'getPerformanceReport').mockResolvedValue({
      overall_score: 96, metrics: {}, recommendations: [], validation: { status: 'passed' },
    } as never)

    const restoring = useDroneStore.getState().hydrateFromBackend('p1')
    expect(useDroneStore.getState().isRestoring).toBe(true)
    release()
    await restoring

    const s = useDroneStore.getState()
    expect(s.isRestoring).toBe(false)
    expect(s.design?.design_tree).toEqual({ topology: 'quadcopter' })
    expect(s.generatedCode).toBe('print(1)')
    expect(s.performanceReport?.overall_score).toBe(96)
  })

  it('clears the flag when the project cannot be loaded', async () => {
    vi.spyOn(api, 'getProject').mockRejectedValue(new ApiError(404, 'not found'))
    await useDroneStore.getState().hydrateFromBackend('p1')
    const s = useDroneStore.getState()
    expect(s.isRestoring).toBe(false)
    expect(s.error).toMatch(/different session/)
  })
})
