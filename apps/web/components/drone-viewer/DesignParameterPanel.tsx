'use client'

/**
 * DesignParameterPanel — live geometry controls for the 3D viewer.
 *
 * Every control here changes the rendered model immediately, with no backend
 * round-trip: the mesh is already parametric, so the panel only has to feed it
 * numbers (see resolve-design.ts).
 *
 * Controls are limited to parameters that visibly move geometry. A drone's URDF
 * joints are all fixed apart from the propellers, so joint sliders — the obvious
 * analogue from a robot-arm viewer — would do nothing here. Airframe geometry is
 * the equivalent axis of control.
 *
 * Overrides live beside the Design agent's output rather than replacing it, so
 * "Reset" always restores what the agent produced.
 */

import { useCallback, useMemo, useState } from 'react'
import { useDroneStore } from '@/lib/stores/drone-store'
import {
  PARAM_RANGES,
  PRESETS,
  ROTOR_COUNTS,
  hasOverrides,
  useDesignParamsStore,
  type AirframeKind,
  type DesignParams,
} from '@/lib/stores/design-params-store'
import { resolveDesign } from './resolve-design'

const AIRFRAMES: { id: AirframeKind; label: string }[] = [
  { id: 'multirotor', label: 'Multirotor' },
  { id: 'fixed_wing', label: 'Fixed-wing' },
  { id: 'hybrid', label: 'VTOL' },
]

// ── Primitives ────────────────────────────────────────────────────────────────

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="eyebrow mb-3">
      {children}
    </div>
  )
}

/**
 * Slider paired with a typed numeric field, as in the reference CAD viewer:
 * drag for exploration, type for a precise value.
 */
function ParamSlider({
  label,
  value,
  min,
  max,
  step,
  unit,
  overridden,
  onChange,
  onClear,
}: {
  label: string
  value: number
  min: number
  max: number
  step: number
  unit: string
  overridden: boolean
  onChange: (v: number) => void
  onClear: () => void
}) {
  const [draft, setDraft] = useState<string | null>(null)

  const commit = useCallback(
    (raw: string) => {
      const parsed = Number(raw)
      setDraft(null)
      if (!Number.isFinite(parsed)) return
      onChange(Math.min(max, Math.max(min, parsed)))
    },
    [max, min, onChange],
  )

  return (
    <div className="mb-4">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="truncate text-xs text-white/55">{label}</span>
        <div className="flex items-center gap-1">
          {overridden && (
            <button
              onClick={onClear}
              title="Restore the value from the design agent"
              className="rounded px-1 text-2xs font-bold text-primary/80 hover:text-primary"
            >
              ↺
            </button>
          )}
          <input
            type="number"
            value={draft ?? String(Math.round(value * 100) / 100)}
            min={min}
            max={max}
            step={step}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={(e) => commit(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
            }}
            aria-label={`${label} value`}
            className={`w-16 rounded-md border bg-black/50 px-1.5 py-0.5 text-right text-xs tabular-nums outline-none focus:border-primary ${
              overridden ? 'border-primary/50 text-primary' : 'border-white/10 text-white/80'
            }`}
          />
          <span className="w-8 text-2xs text-white/30">{unit}</span>
        </div>
      </div>
      <input
        type="range"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(Number(e.target.value))}
        aria-label={label}
        className="h-1 w-full cursor-pointer appearance-none rounded-full bg-white/10 accent-primary"
      />
      <div className="mt-1 flex justify-between text-2xs text-white/20">
        <span>{min}</span>
        <span>{max}</span>
      </div>
    </div>
  )
}

function SegmentedControl<T extends string | number>({
  options,
  value,
  onChange,
}: {
  options: { id: T; label: string }[]
  value: T
  onChange: (v: T) => void
}) {
  return (
    <div className="flex gap-1 rounded-xl border border-white/10 bg-black/40 p-1">
      {options.map((opt) => (
        <button
          key={String(opt.id)}
          onClick={() => onChange(opt.id)}
          className={`flex-1 rounded-lg px-2 py-1.5 text-xs font-bold transition ${
            value === opt.id
              ? 'bg-primary text-primary-foreground'
              : 'text-white/50 hover:text-white'
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}

// ── Panel ─────────────────────────────────────────────────────────────────────

export default function DesignParameterPanel({ className = '' }: { className?: string }) {
  const design = useDroneStore((s) => s.design)
  const { params, animateProps, propSpeed } = useDesignParamsStore()
  const { setParam, clearParam, applyPreset, resetAll, setAnimateProps, setPropSpeed } =
    useDesignParamsStore()

  const [copied, setCopied] = useState(false)

  const tree = design?.design_tree
  const resolved = useMemo(() => resolveDesign(tree, params), [tree, params])
  const dirty = hasOverrides(params)

  const set = useCallback(
    <K extends keyof DesignParams>(key: K) =>
      (value: DesignParams[K]) =>
        setParam(key, value),
    [setParam],
  )

  /** Copy the resolved geometry, so it can be pasted back to the agent. */
  const copyParameters = useCallback(() => {
    const payload = {
      airframe: resolved.kind,
      rotor_count: resolved.rotorCount,
      arm_length_mm: Math.round(resolved.armLengthM * 1000),
      prop_diameter_in: Math.round((resolved.propDiameterM / 0.0254) * 10) / 10,
      battery_capacity_mah:
        params.batteryCapacityMah ?? (tree as { battery?: { capacity_mah?: number } })?.battery?.capacity_mah ?? null,
    }
    navigator.clipboard?.writeText(JSON.stringify(payload, null, 2))
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }, [resolved, params, tree])

  const rotorDisabled = resolved.kind === 'fixed_wing'

  return (
    <div className={`flex flex-col overflow-y-auto ${className}`}>
      <div className="mb-4 flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold">{resolved.label}</div>
          <div className="text-2xs text-white/35">
            {dirty ? `${resolved.overridden.size} parameter(s) overridden` : 'Following agent design'}
          </div>
        </div>
        {dirty && (
          <button
            onClick={resetAll}
            className="rounded-lg border border-white/10 px-2 py-1 text-2xs font-bold text-white/60 hover:border-primary/50 hover:text-primary"
          >
            Reset all
          </button>
        )}
      </div>

      {/* ── Airframe ── */}
      <SectionTitle>Airframe</SectionTitle>
      <SegmentedControl
        options={AIRFRAMES}
        value={resolved.kind}
        onChange={(v) => setParam('airframe', v)}
      />

      <div className="mb-4 mt-3">
        <div className="mb-1.5 text-xs text-white/55">
          Rotors {rotorDisabled && <span className="text-white/25">— fixed-wing uses one tractor prop</span>}
        </div>
        <SegmentedControl
          options={ROTOR_COUNTS.map((n) => ({ id: n, label: String(n) }))}
          value={resolved.rotorCount as (typeof ROTOR_COUNTS)[number]}
          onChange={(v) => setParam('rotorCount', v)}
        />
      </div>

      {/* ── Geometry ── */}
      <SectionTitle>Geometry</SectionTitle>
      <ParamSlider
        label="Arm length"
        value={resolved.armLengthM * 1000}
        {...PARAM_RANGES.armLengthMm}
        overridden={resolved.overridden.has('armLengthMm')}
        onChange={set('armLengthMm')}
        onClear={() => clearParam('armLengthMm')}
      />
      <ParamSlider
        label="Propeller diameter"
        value={resolved.propDiameterM / 0.0254}
        {...PARAM_RANGES.propDiameterIn}
        overridden={resolved.overridden.has('propDiameterIn')}
        onChange={set('propDiameterIn')}
        onClear={() => clearParam('propDiameterIn')}
      />

      {/* ── Power ── */}
      <SectionTitle>Power</SectionTitle>
      <ParamSlider
        label="Battery capacity"
        value={
          params.batteryCapacityMah ??
          (tree as { battery?: { capacity_mah?: number } })?.battery?.capacity_mah ??
          5000
        }
        {...PARAM_RANGES.batteryCapacityMah}
        overridden={resolved.overridden.has('batteryCapacityMah')}
        onChange={set('batteryCapacityMah')}
        onClear={() => clearParam('batteryCapacityMah')}
      />

      <button
        onClick={copyParameters}
        className="mb-5 w-full rounded-xl border border-white/10 bg-white/[0.03] py-2 text-xs font-bold text-white/70 transition hover:border-primary/40 hover:text-primary"
      >
        {copied ? '✓ Copied' : 'Copy parameters'}
      </button>

      {/* ── Presets ── */}
      <SectionTitle>Presets</SectionTitle>
      <div className="mb-5 space-y-1">
        {PRESETS.map((preset) => (
          <button
            key={preset.id}
            onClick={() => applyPreset(preset)}
            className="flex w-full items-center justify-between rounded-xl border border-white/10 bg-white/[0.02] px-3 py-2 text-left text-xs transition hover:border-primary/40 hover:bg-white/[0.05]"
          >
            <span className="text-white/75">{preset.label}</span>
            <span className="text-2xs text-white/30">
              {preset.params.rotorCount}× · {preset.params.armLengthMm} mm
            </span>
          </button>
        ))}
      </div>

      {/* ── Animation ── */}
      <SectionTitle>Animation</SectionTitle>
      <div className="mb-3 flex items-center justify-between">
        <span className="text-xs text-white/55">Spin propellers</span>
        <button
          role="switch"
          aria-checked={animateProps}
          aria-label="Spin propellers"
          onClick={() => setAnimateProps(!animateProps)}
          className={`h-5 w-9 rounded-full p-0.5 transition ${animateProps ? 'bg-primary' : 'bg-white/15'}`}
        >
          <span
            className={`block h-4 w-4 rounded-full bg-white transition-transform ${
              animateProps ? 'translate-x-4' : ''
            }`}
          />
        </button>
      </div>
      <ParamSlider
        label="Rotation speed"
        value={propSpeed}
        min={0}
        max={30}
        step={0.5}
        unit="×"
        overridden={false}
        onChange={setPropSpeed}
        onClear={() => setPropSpeed(1)}
      />
    </div>
  )
}
