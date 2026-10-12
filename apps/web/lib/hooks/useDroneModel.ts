'use client'

/**
 * useDroneModel — local UI state for the 3D DroneViewer.
 *
 * Manages view mode, animation speed, annotation/axes/grid visibility,
 * selected component, and screenshot callback registration.
 */

import { useState, useCallback, useRef } from 'react'
import { useDesignParamsStore } from '@/lib/stores/design-params-store'
import type { ViewMode, CameraPreset } from '@/components/drone-viewer/controls'

// ─────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────

export interface DroneModelControls {
  /** Current render mode */
  viewMode:          ViewMode
  /** Propeller animation speed (degrees/sec equivalent) */
  propSpeed:         number
  /** Show component annotation labels */
  showAnnotations:   boolean
  /** Show world-axes helper */
  showAxes:          boolean
  /** Show ground grid */
  showGrid:          boolean
  /** Show propeller disc animation */
  animateProps:      boolean
  /** Currently highlighted/selected component id */
  selectedComponent: string | null
  /** Active camera preset */
  cameraPreset:      CameraPreset

  // ── Setters ─────────────────────────────────────────────────
  setViewMode:           (m: ViewMode) => void
  setPropSpeed:          (s: number) => void
  toggleAnnotations:     () => void
  toggleAxes:            () => void
  toggleGrid:            () => void
  toggleAnimateProps:    () => void
  setSelectedComponent:  (id: string | null) => void
  setCameraPreset:       (p: CameraPreset) => void

  /** Register a screenshot function from inside the Canvas */
  registerScreenshot:    (fn: () => void) => void
  /** Trigger a screenshot (calls the registered function) */
  takeScreenshot:        () => void
}

// ─────────────────────────────────────────────────────────────
// Hook
// ─────────────────────────────────────────────────────────────

export function useDroneModel(): DroneModelControls {
  const [viewMode,          setViewMode]          = useState<ViewMode>('solid')
  const [showAnnotations,   setShowAnnotations]    = useState<boolean>(false)
  const [showAxes,          setShowAxes]            = useState<boolean>(true)
  const [showGrid,          setShowGrid]            = useState<boolean>(true)
  const [selectedComponent, setSelectedComponent]  = useState<string | null>(null)
  const [cameraPreset,      setCameraPreset]        = useState<CameraPreset>('isometric')

  // Propeller animation is shared state, not local: the viewer's own toolbar and
  // the design parameter panel both drive it, and they must agree.
  const propSpeed        = useDesignParamsStore(s => s.propSpeed)
  const setPropSpeed     = useDesignParamsStore(s => s.setPropSpeed)
  const animateProps     = useDesignParamsStore(s => s.animateProps)
  const setAnimateProps  = useDesignParamsStore(s => s.setAnimateProps)

  // Screenshot callback registered from inside Canvas
  const screenshotFnRef = useRef<(() => void) | null>(null)

  const registerScreenshot = useCallback((fn: () => void) => {
    screenshotFnRef.current = fn
  }, [])

  const takeScreenshot = useCallback(() => {
    screenshotFnRef.current?.()
  }, [])

  return {
    viewMode,
    propSpeed,
    showAnnotations,
    showAxes,
    showGrid,
    animateProps,
    selectedComponent,
    cameraPreset,

    setViewMode,
    setPropSpeed,
    toggleAnnotations:  () => setShowAnnotations(v => !v),
    toggleAxes:         () => setShowAxes(v => !v),
    toggleGrid:         () => setShowGrid(v => !v),
    // The store setter takes a value, not React's functional updater.
    toggleAnimateProps: () => setAnimateProps(!animateProps),
    setSelectedComponent,
    setCameraPreset,

    registerScreenshot,
    takeScreenshot,
  }
}
