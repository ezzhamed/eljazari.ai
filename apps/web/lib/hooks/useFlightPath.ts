'use client'

/**
 * useFlightPath — derives 3D path geometry and drone animation state
 * from the live telemetry stream in DroneStore.
 *
 * Provides:
 *  - scaledPoints: THREE.Vector3[] ready for TubeGeometry / Line
 *  - dronePosition / droneRotation: current drone pose along the path
 *  - altitudeProfile: [{distance, altitude}] for the 2D sidebar chart
 *  - waypointInfos: per-point metadata (altitude, distance, hover info)
 *  - playback controls: start / pause / reset / seek
 */

import { useMemo, useState, useCallback, useEffect, useRef } from 'react'
import * as THREE from 'three'
import { useDroneStore } from '@/lib/stores/drone-store'
import { computeScaleFactor } from '@/components/drone-viewer/controls'

// ─────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────

export interface WaypointInfo {
  index:             number
  position:          THREE.Vector3   // scaled (viewer coords)
  realAltitude_m:    number
  realNorth_m:       number
  realEast_m:        number
  distanceFromStart: number          // real metres
  batteryPct?:       number
  /** Horizontal speed over ground from the sample's velocity, m/s. */
  groundSpeed_ms?:   number
  /** Yaw from the sample's attitude, 0–360°. */
  heading_deg?:      number
  flightMode?:       string
}

export interface AltitudePoint {
  distance: number    // metres along path
  altitude: number    // metres AGL
}

export interface UseFlightPathReturn {
  /** Original (unscaled) path points */
  rawPoints:       THREE.Vector3[]
  /** Viewer-scaled path points */
  scaledPoints:    THREE.Vector3[]
  /** Uniform scale applied to all coordinates */
  scale:           number
  /** Index of drone position along path */
  currentIndex:    number
  /** Drone world position (scaled) */
  dronePosition:   THREE.Vector3
  /** Drone heading derived from path direction */
  droneRotation:   THREE.Euler
  /** True while animation is playing */
  isAnimating:     boolean
  /** True when animation has reached the end */
  isFinished:      boolean
  /** Total mission path length in real metres */
  totalDistance:   number
  /** 2D altitude profile data */
  altitudeProfile: AltitudePoint[]
  /** Per-waypoint metadata */
  waypointInfos:   WaypointInfo[]
  /** 0–1 playback progress */
  progress:        number
  /** True while the aircraft is flying now, rather than being replayed */
  isLive:          boolean

  startAnimation:  () => void
  pauseAnimation:  () => void
  resetAnimation:  () => void
  /** Jump to a specific point index */
  seekTo:          (index: number) => void
}

// ─────────────────────────────────────────────────────────────
// Hook
// ─────────────────────────────────────────────────────────────

export function useFlightPath(
  /** Points advanced per animation tick (50 ms interval). */
  stepPerTick = 1,
): UseFlightPathReturn {
  const telemetryStream = useDroneStore((state) => state.telemetryStream)
  const isLive = useDroneStore((state) => state.isLive)
  const [currentIndex, setCurrentIndex] = useState(0)
  const [isAnimating,  setIsAnimating]  = useState(false)
  const [isFinished,   setIsFinished]   = useState(false)
  const animTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ── Build raw 3D points from telemetry ──────────────────────
  const rawPoints = useMemo<THREE.Vector3[]>(() => {
    return telemetryStream.map(p =>
      new THREE.Vector3(
        p.position?.north_m ?? 0,
        p.position?.alt_m   ?? 0,
        -(p.position?.east_m ?? 0),   // right-hand → Three.js: flip east to -Z
      )
    )
  }, [telemetryStream])

  // ── Auto-scale so the whole path fits in the viewport ───────
  const scale = useMemo(() => {
    if (rawPoints.length === 0) return 1
    const maxDist = Math.max(
      ...rawPoints.map(p => Math.sqrt(p.x ** 2 + p.z ** 2))
    )
    const maxAltitude = Math.max(...rawPoints.map(p => Math.abs(p.y)))
    return computeScaleFactor(Math.max(maxDist, maxAltitude), 5)
  }, [rawPoints])

  const scaledPoints = useMemo(
    () => rawPoints.map(p => new THREE.Vector3(p.x * scale, p.y * scale, p.z * scale)),
    [rawPoints, scale],
  )

  // ── Total distance (real metres) ────────────────────────────
  const totalDistance = useMemo(() => {
    let d = 0
    for (let i = 1; i < rawPoints.length; i++) d += rawPoints[i].distanceTo(rawPoints[i - 1])
    return Math.round(d)
  }, [rawPoints])

  // ── Altitude profile ─────────────────────────────────────────
  const altitudeProfile = useMemo<AltitudePoint[]>(() => {
    let dist = 0
    return rawPoints.map((p, i) => {
      if (i > 0) dist += rawPoints[i].distanceTo(rawPoints[i - 1])
      return { distance: Math.round(dist), altitude: Math.round(p.y) }
    })
  }, [rawPoints])

  // ── Per-waypoint info ────────────────────────────────────────
  const waypointInfos = useMemo<WaypointInfo[]>(() => {
    let cumDist = 0
    return scaledPoints.map((pt, i) => {
      if (i > 0) cumDist += scaledPoints[i].distanceTo(scaledPoints[i - 1])
      const tel = telemetryStream[i]
      return {
        index:             i,
        position:          pt,
        realAltitude_m:    rawPoints[i]?.y ?? 0,
        realNorth_m:       rawPoints[i]?.x ?? 0,
        realEast_m:        -(rawPoints[i]?.z ?? 0),
        distanceFromStart: Math.round(cumDist / Math.max(scale, 0.001)),
        batteryPct:        tel?.battery_pct,
        groundSpeed_ms:    tel?.speed_ms ?? (tel?.velocity ? Math.hypot(tel.velocity.north_ms ?? 0, tel.velocity.east_ms ?? 0) : undefined),
        heading_deg:       tel?.attitude?.yaw_deg != null ? ((tel.attitude.yaw_deg % 360) + 360) % 360 : undefined,
        flightMode:        tel?.flight_mode,
      }
    })
  }, [scaledPoints, rawPoints, scale, telemetryStream])

  // ── Drone pose at currentIndex ──────────────────────────────
  const dronePosition = useMemo<THREE.Vector3>(() => {
    const pt = scaledPoints[currentIndex]
    return pt ? pt.clone() : new THREE.Vector3(0, 0.3, 0)
  }, [scaledPoints, currentIndex])

  const droneRotation = useMemo<THREE.Euler>(() => {
    if (scaledPoints.length < 2 || currentIndex === 0) return new THREE.Euler()
    const prev = scaledPoints[Math.max(0, currentIndex - 1)]
    const curr = scaledPoints[currentIndex]
    if (!curr || !prev) return new THREE.Euler()
    const dir  = new THREE.Vector3().subVectors(curr, prev)
    const yaw  = Math.atan2(dir.x, dir.z)
    return new THREE.Euler(0, yaw, 0)
  }, [scaledPoints, currentIndex])

  const progress = scaledPoints.length > 1
    ? currentIndex / (scaledPoints.length - 1)
    : 0

  // ── Animation ticker ────────────────────────────────────────
  useEffect(() => {
    // A live flight is driven by arriving telemetry, not by a timer; running
    // both would have the ticker fight the stream for the same index.
    if (isLive || !isAnimating || scaledPoints.length === 0) return

    // Adaptive step: ensure long paths (e.g. 500-2500 points) replay smoothly in ~10-15s
    const baseStep = Math.max(1, Math.floor(scaledPoints.length / 200))
    const step = Math.max(1, Math.round(baseStep * stepPerTick))

    animTimerRef.current = setInterval(() => {
      setCurrentIndex(idx => {
        const next = idx + step
        if (next >= scaledPoints.length - 1) {
          // Loop flight path so the drone remains in continuous motion
          return 0
        }
        return next
      })
    }, 50)

    return () => {
      if (animTimerRef.current) clearInterval(animTimerRef.current)
    }
  }, [isLive, isAnimating, scaledPoints.length, stepPerTick])

  // Follow the aircraft while it is flying; auto-play replay once completed.
  useEffect(() => {
    if (isLive) {
      setIsAnimating(false)
      setIsFinished(false)
      setCurrentIndex(Math.max(0, telemetryStream.length - 1))
      return
    }
    if (telemetryStream.length >= 2) {
      // Recording available — auto-play flight path so the user sees the drone
      // actively flying in the 3D environment rather than sitting motionless at origin.
      setIsAnimating(true)
      setIsFinished(false)
      setCurrentIndex(0)
    } else {
      setCurrentIndex(0)
      setIsFinished(false)
      setIsAnimating(false)
    }
  }, [telemetryStream.length, isLive])

  // ── Controls ─────────────────────────────────────────────────
  const startAnimation = useCallback(() => {
    setIsFinished(false)
    setIsAnimating(true)
  }, [])

  const pauseAnimation = useCallback(() => {
    setIsAnimating(false)
  }, [])

  const resetAnimation = useCallback(() => {
    setIsAnimating(false)
    setIsFinished(false)
    setCurrentIndex(0)
  }, [])

  const seekTo = useCallback((index: number) => {
    setCurrentIndex(Math.max(0, Math.min(index, scaledPoints.length - 1)))
  }, [scaledPoints.length])

  return {
    rawPoints,
    scaledPoints,
    scale,
    currentIndex,
    dronePosition,
    droneRotation,
    isAnimating,
    isFinished,
    totalDistance,
    altitudeProfile,
    waypointInfos,
    progress,
    isLive,
    startAnimation,
    pauseAnimation,
    resetAnimation,
    seekTo,
  }
}
