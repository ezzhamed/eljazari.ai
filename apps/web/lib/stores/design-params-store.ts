/**
 * eljazari.ai — live design-parameter overrides
 *
 * The 3D viewer already builds its mesh parametrically from the agent's design
 * tree. This store is the editable layer on top: values the user drags in the
 * parameter panel, kept separate from `useDroneStore().design` so that
 * exploring geometry never overwrites what the Design agent produced. Clearing
 * an override snaps that parameter back to the agent's value.
 *
 * Only parameters that visibly change the model live here. A slider that moves
 * nothing is worse than no slider, so battery capacity and propeller diameter
 * are wired to real geometry in DroneViewer rather than being decorative.
 */

import { create } from 'zustand'
import { immer } from 'zustand/middleware/immer'

export type AirframeKind = 'multirotor' | 'fixed_wing' | 'hybrid'

/** Every parameter the panel can drive. `null` = follow the agent's design. */
export interface DesignParams {
  airframe: AirframeKind | null
  rotorCount: number | null
  armLengthMm: number | null
  propDiameterIn: number | null
  batteryCapacityMah: number | null
}

export const EMPTY_PARAMS: DesignParams = {
  airframe: null,
  rotorCount: null,
  armLengthMm: null,
  propDiameterIn: null,
  batteryCapacityMah: null,
}

/** Slider bounds. Chosen to span real airframes without allowing absurd meshes. */
export const PARAM_RANGES = {
  armLengthMm: { min: 80, max: 900, step: 5, unit: 'mm' },
  propDiameterIn: { min: 2, max: 22, step: 0.5, unit: 'in' },
  batteryCapacityMah: { min: 450, max: 32000, step: 50, unit: 'mAh' },
} as const

/** Rotor counts the mesh renders as a clean radial ring. */
export const ROTOR_COUNTS = [2, 3, 4, 6, 8] as const

export interface ParamPreset {
  id: string
  label: string
  params: DesignParams
}

/** Starting points, mirroring the saved "poses" idea from a CAD viewer. */
export const PRESETS: ParamPreset[] = [
  {
    id: 'racer',
    label: 'FPV racer',
    params: {
      airframe: 'multirotor',
      rotorCount: 4,
      armLengthMm: 125,
      propDiameterIn: 5,
      batteryCapacityMah: 1500,
    },
  },
  {
    id: 'inspection',
    label: 'Inspection quad',
    params: {
      airframe: 'multirotor',
      rotorCount: 4,
      armLengthMm: 350,
      propDiameterIn: 10,
      batteryCapacityMah: 5000,
    },
  },
  {
    id: 'cargo',
    label: 'Cargo hex',
    params: {
      airframe: 'multirotor',
      rotorCount: 6,
      armLengthMm: 520,
      propDiameterIn: 15,
      batteryCapacityMah: 16000,
    },
  },
  {
    id: 'vtol',
    label: 'VTOL hybrid',
    params: {
      airframe: 'hybrid',
      rotorCount: 5,
      armLengthMm: 420,
      propDiameterIn: 13,
      batteryCapacityMah: 12000,
    },
  },
]

interface DesignParamsStore {
  params: DesignParams
  /** Propeller animation, mirroring the viewer's own controls. */
  animateProps: boolean
  propSpeed: number

  setParam: <K extends keyof DesignParams>(key: K, value: DesignParams[K]) => void
  clearParam: (key: keyof DesignParams) => void
  applyPreset: (preset: ParamPreset) => void
  resetAll: () => void
  setAnimateProps: (on: boolean) => void
  setPropSpeed: (speed: number) => void
}

export const useDesignParamsStore = create<DesignParamsStore>()(
  immer((set) => ({
    params: { ...EMPTY_PARAMS },
    animateProps: true,
    propSpeed: 1,

    setParam: (key, value) =>
      set((state) => {
        state.params[key] = value
      }),

    clearParam: (key) =>
      set((state) => {
        state.params[key] = null
      }),

    applyPreset: (preset) =>
      set((state) => {
        state.params = { ...preset.params }
      }),

    resetAll: () =>
      set((state) => {
        state.params = { ...EMPTY_PARAMS }
      }),

    setAnimateProps: (on) =>
      set((state) => {
        state.animateProps = on
      }),

    setPropSpeed: (speed) =>
      set((state) => {
        state.propSpeed = speed
      }),
  })),
)

/** True when the user has overridden anything — drives the Reset affordance. */
export function hasOverrides(params: DesignParams): boolean {
  return Object.values(params).some((value) => value !== null)
}
