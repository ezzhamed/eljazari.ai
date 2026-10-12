'use client'

/**
 * ParameterTuning — Phase 4.2 WebGCS PID parameter panel
 *
 * Allows operators to read and write PX4 PID gains in real-time:
 *   Roll  P / Roll-rate  P / I / D
 *   Pitch P / Pitch-rate P / I / D
 *   Yaw   P / Yaw-rate   P / I / D
 *   Hover throttle (MPC_THR_HOVER)
 *
 * Communicates with the backend via socket.io events:
 *   gcs:get_params     → server fetches all PID params
 *   gcs:set_param      → { name, value } sets one param
 *   gcs:params_update  ← server replies with current values
 */

import { useCallback, useEffect, useState } from 'react'
import { useWSStore } from '@/lib/stores/ws-store'

// ── PID parameter definitions ─────────────────────────────────────────────────

interface PIDParam {
  name:    string    // PX4 param name
  label:   string    // UI label
  min:     number
  max:     number
  step:    number
  default: number
}

interface PIDGroup {
  title:  string
  color:  string
  params: PIDParam[]
}

const PID_GROUPS: PIDGroup[] = [
  {
    title: 'Roll',
    color: 'text-warning',
    params: [
      { name: 'MC_ROLL_P',     label: 'Angle P',  min: 0, max: 20,   step: 0.1,  default: 6.5 },
      { name: 'MC_ROLLRATE_P', label: 'Rate P',   min: 0, max: 0.5,  step: 0.001, default: 0.15 },
      { name: 'MC_ROLLRATE_I', label: 'Rate I',   min: 0, max: 0.5,  step: 0.001, default: 0.05 },
      { name: 'MC_ROLLRATE_D', label: 'Rate D',   min: 0, max: 0.02, step: 0.0001, default: 0.003 },
    ],
  },
  {
    title: 'Pitch',
    color: 'text-info',
    params: [
      { name: 'MC_PITCH_P',     label: 'Angle P', min: 0, max: 20,   step: 0.1,  default: 6.5 },
      { name: 'MC_PITCHRATE_P', label: 'Rate P',  min: 0, max: 0.5,  step: 0.001, default: 0.15 },
      { name: 'MC_PITCHRATE_I', label: 'Rate I',  min: 0, max: 0.5,  step: 0.001, default: 0.05 },
      { name: 'MC_PITCHRATE_D', label: 'Rate D',  min: 0, max: 0.02, step: 0.0001, default: 0.003 },
    ],
  },
  {
    title: 'Yaw',
    color: 'text-success',
    params: [
      { name: 'MC_YAW_P',     label: 'Angle P', min: 0, max: 10,   step: 0.1,  default: 2.8 },
      { name: 'MC_YAWRATE_P', label: 'Rate P',  min: 0, max: 0.6,  step: 0.001, default: 0.2 },
      { name: 'MC_YAWRATE_I', label: 'Rate I',  min: 0, max: 0.1,  step: 0.001, default: 0.1 },
      { name: 'MC_YAWRATE_D', label: 'Rate D',  min: 0, max: 0.01, step: 0.0001, default: 0.0 },
    ],
  },
  {
    title: 'Throttle',
    color: 'text-brand-300',
    params: [
      { name: 'MPC_THR_HOVER', label: 'Hover', min: 0.1, max: 0.9, step: 0.01, default: 0.5 },
    ],
  },
]

// ── Sub-component: single param row ──────────────────────────────────────────

interface ParamRowProps {
  param:    PIDParam
  value:    number
  dirty:    boolean
  onChange: (name: string, val: number) => void
}

function ParamRow({ param, value, dirty, onChange }: ParamRowProps) {
  const [inputVal, setInputVal] = useState(value.toString())

  useEffect(() => {
    setInputVal(value.toFixed(param.step < 0.001 ? 4 : param.step < 0.01 ? 3 : param.step < 0.1 ? 2 : 1))
  }, [value, param.step])

  const handleSlider = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = parseFloat(e.target.value)
    onChange(param.name, v)
  }

  const handleInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    setInputVal(e.target.value)
    const v = parseFloat(e.target.value)
    if (!isNaN(v) && v >= param.min && v <= param.max) {
      onChange(param.name, v)
    }
  }

  const sliderPct = ((value - param.min) / (param.max - param.min)) * 100

  return (
    <div className="grid grid-cols-[80px_1fr_60px] items-center gap-2">
      <span className={`text-[9px] font-mono text-muted-foreground truncate ${dirty ? 'text-warning' : ''}`}>
        {param.label}{dirty ? ' *' : ''}
      </span>
      {/* Custom slider */}
      <div className="relative h-4 flex items-center">
        <div className="absolute inset-x-0 h-0.5 bg-success/10 rounded" />
        <div className="absolute left-0 h-0.5 bg-success rounded"
          style={{ width: `${sliderPct}%` }} />
        <input
          type="range"
          min={param.min}
          max={param.max}
          step={param.step}
          value={value}
          onChange={handleSlider}
          className="absolute inset-0 w-full opacity-0 cursor-pointer h-full"
        />
        {/* Thumb */}
        <div className="absolute w-3 h-3 rounded-full bg-success border border-success -translate-x-1/2 pointer-events-none"
          style={{ left: `${sliderPct}%` }} />
      </div>
      {/* Numeric input */}
      <input
        type="number"
        min={param.min}
        max={param.max}
        step={param.step}
        value={inputVal}
        onChange={handleInput}
        className="w-full bg-background border border-border rounded px-1 py-0.5 text-[9px] font-mono text-success tabular-nums text-right focus:outline-none focus:border-brand-500"
      />
    </div>
  )
}

// ── Public component ───────────────────────────────────────────────────────────

interface ParameterTuningProps {
  className?: string
}

export default function ParameterTuning({ className = '' }: ParameterTuningProps) {
  const { socket } = useWSStore()

  // Current param values (server → local)
  const [params, setParams] = useState<Record<string, number>>(() => {
    const defaults: Record<string, number> = {}
    PID_GROUPS.forEach(g => g.params.forEach(p => { defaults[p.name] = p.default }))
    return defaults
  })

  // Dirty (locally changed but not yet sent)
  const [dirty, setDirty] = useState<Set<string>>(new Set())

  // Active group tab
  const [activeGroup, setActiveGroup] = useState(0)

  const [status, setStatus] = useState<'idle' | 'loading' | 'saving' | 'saved' | 'error'>('idle')

  // Listen for param updates from server
  useEffect(() => {
    if (!socket) return
    const handler = (data: { params: Record<string, number> }) => {
      setParams(prev => ({ ...prev, ...data.params }))
      setDirty(new Set())
      setStatus('idle')
    }
    socket.on('gcs:params_update', handler)
    return () => { socket.off('gcs:params_update', handler) }
  }, [socket])

  // Fetch all params from server
  const fetchParams = useCallback(() => {
    if (!socket) return
    setStatus('loading')
    socket.emit('gcs:get_params', {})
  }, [socket])

  // Change a single param value locally
  const handleChange = useCallback((name: string, value: number) => {
    setParams(prev => ({ ...prev, [name]: value }))
    setDirty(prev => new Set(prev).add(name))
  }, [])

  // Send one param to server immediately
  const sendParam = useCallback((name: string) => {
    if (!socket) return
    const value = params[name]
    socket.emit('gcs:set_param', { name, value })
    setDirty(prev => { const n = new Set(prev); n.delete(name); return n })
  }, [socket, params])

  // Send all dirty params
  const sendAll = useCallback(() => {
    if (!socket || dirty.size === 0) return
    setStatus('saving')
    dirty.forEach(name => {
      socket.emit('gcs:set_param', { name, value: params[name] })
    })
    setDirty(new Set())
    setTimeout(() => setStatus('saved'), 800)
    setTimeout(() => setStatus('idle'), 2500)
  }, [socket, dirty, params])

  // Reset group to defaults
  const resetGroup = useCallback((group: PIDGroup) => {
    setParams(prev => {
      const next = { ...prev }
      group.params.forEach(p => { next[p.name] = p.default })
      return next
    })
    setDirty(prev => {
      const n = new Set(prev)
      group.params.forEach(p => n.add(p.name))
      return n
    })
  }, [])

  const group = PID_GROUPS[activeGroup]

  return (
    <div className={`rounded-xl border border-border bg-card overflow-hidden flex flex-col ${className}`}>
      {/* Header */}
      <div className="flex min-h-[3rem] items-center justify-between gap-3 border-b border-border px-4 py-2.5 flex-shrink-0">
        <span className="text-sm font-medium text-foreground">PID tuning</span>
        <div className="flex items-center gap-1.5">
          <button
            onClick={fetchParams}
            disabled={status === 'loading'}
            className="text-[10px] font-mono px-2 py-0.5 rounded border border-border text-muted-foreground hover:text-success hover:border-brand-500/40 transition-colors disabled:opacity-40"
          >
            {status === 'loading' ? '⟳ FETCHING…' : '⟳ READ'}
          </button>
          {dirty.size > 0 && (
            <button
              onClick={sendAll}
              disabled={status === 'saving'}
              className="text-[10px] font-mono px-2 py-0.5 rounded border border-warning/30 text-warning hover:bg-warning/15 transition-colors"
            >
              {status === 'saving' ? '↑ SENDING…' : `↑ SEND (${dirty.size})`}
            </button>
          )}
          {status === 'saved' && (
            <span className="text-[10px] font-mono text-success">✓ SAVED</span>
          )}
          {status === 'error' && (
            <span className="text-[10px] font-mono text-destructive">✕ ERROR</span>
          )}
        </div>
      </div>

      {/* Group tabs */}
      <div className="flex border-b border-border/60">
        {PID_GROUPS.map((g, i) => (
          <button
            key={g.title}
            onClick={() => setActiveGroup(i)}
            className={`flex-1 py-1.5 text-[10px] font-mono transition-colors ${
              activeGroup === i
                ? `${g.color} bg-background border-b-2 border-current`
                : 'text-muted-foreground/80 hover:text-foreground'
            }`}
          >
            {g.title}
          </button>
        ))}
      </div>

      {/* Param rows */}
      <div className="flex-1 p-3 space-y-2.5 overflow-y-auto">
        {group.params.map(param => (
          <div key={param.name} className="space-y-0.5">
            <div className="flex items-center justify-between">
              <span className="text-[8px] font-mono text-muted-foreground/60">{param.name}</span>
              {dirty.has(param.name) && (
                <button
                  onClick={() => sendParam(param.name)}
                  className="text-[8px] font-mono text-warning hover:text-warning border border-warning/30 rounded px-1"
                >
                  SEND
                </button>
              )}
            </div>
            <ParamRow
              param={param}
              value={params[param.name] ?? param.default}
              dirty={dirty.has(param.name)}
              onChange={handleChange}
            />
          </div>
        ))}
      </div>

      {/* Footer */}
      <div className="px-3 py-2 border-t border-border/60 flex items-center justify-between">
        <button
          onClick={() => resetGroup(group)}
          className="text-[9px] font-mono text-muted-foreground/80 hover:text-warning transition-colors"
        >
          Reset {group.title} defaults
        </button>
        <span className="text-[9px] font-mono text-muted-foreground/60">
          {socket ? '● Connected' : '○ Offline'}
        </span>
      </div>
    </div>
  )
}
