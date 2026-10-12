'use client'

/**
 * AttitudeIndicator — Artificial horizon (AHRS) SVG instrument.
 *
 * Shows:
 *  - Pitch ladder (horizon line + pitch angle tapes)
 *  - Roll arc with bank angle indicator
 *  - Aircraft symbol (fixed crosshair)
 *  - Digital readouts: roll °, pitch °, yaw °
 */

import { useDroneStore } from '@/lib/stores/drone-store'

const CX = 80   // SVG centre X
const CY = 80   // SVG centre Y
const R  = 70   // instrument radius
const SIZE = 160

interface AttitudeIndicatorProps {
  className?: string
  /** Override values instead of reading from store (for storybook / testing) */
  roll?:  number
  pitch?: number
  yaw?:   number
}

export default function AttitudeIndicator({
  className = '',
  roll:  rollProp,
  pitch: pitchProp,
  yaw:   yawProp,
}: AttitudeIndicatorProps) {
  const { telemetryStream } = useDroneStore()
  const latest = telemetryStream[telemetryStream.length - 1]

  const roll  = rollProp  ?? latest?.attitude?.roll_deg  ?? 0
  const pitch = pitchProp ?? latest?.attitude?.pitch_deg ?? 0
  const yaw   = yawProp   ?? latest?.attitude?.yaw_deg   ?? 0

  // Pixels-per-degree for pitch
  const PPD = 1.8

  // Horizon vertical shift due to pitch (positive pitch → horizon moves down)
  const pitchOffset = pitch * PPD

  // Horizon line endpoints (long enough to always span the circle)
  const horizonLen = R + 20
  const rollRad    = (roll * Math.PI) / 180

  // Sky / ground: two halves of the circle, rotated by roll
  const skyColor    = '#0a1e3a'
  const groundColor = '#2a1400'

  return (
    <div className={`rounded-xl border border-border bg-card overflow-hidden ${className}`}>
      {/* Header */}
      <div className="flex min-h-[3rem] items-center justify-between gap-3 border-b border-border px-4 py-2.5">
        <span className="text-sm font-medium text-foreground">Attitude</span>
        <span className="text-[10px] font-mono text-muted-foreground/80">AHRS</span>
      </div>

      {/* SVG instrument */}
      <div className="flex justify-center py-2">
        <svg viewBox={`0 0 ${SIZE} ${SIZE}`} width={SIZE} height={SIZE}>
          <defs>
            <clipPath id="att-clip">
              <circle cx={CX} cy={CY} r={R} />
            </clipPath>
          </defs>

          {/* Outer ring */}
          <circle cx={CX} cy={CY} r={R + 2} fill="none" stroke="#2a2a2a" strokeWidth="3" />

          {/* Sky + ground fill (rotated group) */}
          <g clipPath="url(#att-clip)" transform={`rotate(${-roll}, ${CX}, ${CY})`}>
            {/* Ground (bottom half) */}
            <rect x={CX - R - 5} y={CY - pitchOffset} width={(R + 5) * 2} height={R + 5 + pitchOffset}
              fill={groundColor} />
            {/* Sky (top half) */}
            <rect x={CX - R - 5} y={CY - R - 5} width={(R + 5) * 2} height={R + 5 - pitchOffset}
              fill={skyColor} />

            {/* Pitch ladder */}
            {[-30, -20, -10, 10, 20, 30].map(deg => {
              const y   = CY - pitchOffset - deg * PPD
              const len = Math.abs(deg) === 30 ? 24 : Math.abs(deg) === 20 ? 20 : 14
              return (
                <g key={deg}>
                  <line x1={CX - len} y1={y} x2={CX + len} y2={y}
                    stroke="#ffffff40" strokeWidth="0.8" />
                  <text x={CX - len - 4} y={y + 3} textAnchor="end"
                    fill="#ffffff50" fontSize="6" fontFamily="monospace">{deg}</text>
                </g>
              )
            })}

            {/* Horizon line */}
            <line
              x1={CX - horizonLen} y1={CY - pitchOffset}
              x2={CX + horizonLen} y2={CY - pitchOffset}
              stroke="#ffffff" strokeWidth="1.5"
            />
          </g>

          {/* Roll arc */}
          <g>
            {[-60, -45, -30, -20, -10, 0, 10, 20, 30, 45, 60].map(deg => {
              const a = ((deg - 90) * Math.PI) / 180
              const r1 = R - 4, r2 = deg % 30 === 0 ? R - 10 : R - 7
              return (
                <line
                  key={deg}
                  x1={CX + r2 * Math.cos(a)} y1={CY + r2 * Math.sin(a)}
                  x2={CX + r1 * Math.cos(a)} y2={CY + r1 * Math.sin(a)}
                  stroke="#cdb892" strokeWidth={deg % 30 === 0 ? 1.5 : 0.8}
                />
              )
            })}
            {/* Bank index (rotating triangle) */}
            <g transform={`rotate(${-roll}, ${CX}, ${CY})`}>
              <polygon
                points={`${CX},${CY - R + 6} ${CX - 5},${CY - R + 14} ${CX + 5},${CY - R + 14}`}
                fill="#cdb892"
              />
            </g>
          </g>

          {/* Fixed aircraft symbol */}
          <g>
            {/* Left wing */}
            <line x1={CX - 28} y1={CY} x2={CX - 10} y2={CY} stroke="#ebad47" strokeWidth="2" />
            <line x1={CX - 10} y1={CY} x2={CX - 10} y2={CY + 6} stroke="#ebad47" strokeWidth="2" />
            {/* Right wing */}
            <line x1={CX + 28} y1={CY} x2={CX + 10} y2={CY} stroke="#ebad47" strokeWidth="2" />
            <line x1={CX + 10} y1={CY} x2={CX + 10} y2={CY + 6} stroke="#ebad47" strokeWidth="2" />
            {/* Centre dot */}
            <circle cx={CX} cy={CY} r={2.5} fill="#ebad47" />
          </g>
        </svg>
      </div>

      {/* Digital readouts */}
      <div className="grid grid-cols-3 gap-1 px-3 pb-3 font-mono text-xs">
        {[
          { label: 'ROLL',  value: roll,  unit: '°', color: 'text-warning' },
          { label: 'PITCH', value: pitch, unit: '°', color: 'text-info' },
          { label: 'YAW',   value: yaw,   unit: '°', color: 'text-success' },
        ].map(({ label, value, unit, color }) => (
          <div key={label} className="flex flex-col items-center bg-background/80 rounded py-1">
            <span className="text-[9px] text-muted-foreground/80">{label}</span>
            <span className={`tabular-nums font-bold ${color}`}>
              {value.toFixed(1)}{unit}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
