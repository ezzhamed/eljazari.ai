import { act, renderHook, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useFlightPath } from '../hooks/useFlightPath'
import { useDroneStore } from '../stores/drone-store'

function recording(count: number, altitude = 15) {
  for (let i = 0; i < count; i++) {
    useDroneStore.getState().addTelemetry({ north_m: i * .1, east_m: 0, alt_m: altitude, timestamp_s: i * .1 } as any, false)
  }
}
beforeEach(() => { vi.useFakeTimers(); useDroneStore.getState().reset() })
afterEach(() => { cleanup(); vi.useRealTimers() })

it('applies playback speed relative to the same recording base rate', () => {
  recording(500)
  const { result, rerender } = renderHook(({ speed }) => useFlightPath(speed), { initialProps: { speed: 1 } })
  act(() => vi.advanceTimersByTime(50))
  expect(result.current.currentIndex).toBe(2)
  rerender({ speed: 2 })
  act(() => vi.advanceTimersByTime(50))
  expect(result.current.currentIndex).toBe(6)
  rerender({ speed: 4 })
  act(() => vi.advanceTimersByTime(50))
  expect(result.current.currentIndex).toBe(14)
})

it('frames a vertical-only flight within the viewing volume', () => {
  recording(2, 100)
  const { result } = renderHook(() => useFlightPath())
  expect(result.current.scaledPoints[0].y).toBeLessThanOrEqual(5)
  expect(result.current.waypointInfos[0].realAltitude_m).toBe(100)
})

it('resets safely if a recording is removed after seeking', () => {
  recording(30)
  const { result } = renderHook(() => useFlightPath())
  act(() => { result.current.pauseAnimation(); result.current.seekTo(25) })
  act(() => useDroneStore.getState().reset())
  expect(result.current.currentIndex).toBe(0)
  expect(result.current.scaledPoints).toHaveLength(0)
  expect(result.current.isAnimating).toBe(false)
})
