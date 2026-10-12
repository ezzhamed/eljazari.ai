'use client'

/**
 * TelemetryDashboard — Phase 4.2 WebGCS instrument panel
 *
 * Panels:
 *  1. Compass rose  — magnetic heading, cardinal directions
 *  2. Altitude tape — current altitude + vertical speed
 *  3. Airspeed indicator — speed m/s + mode badge
 *  4. Battery gauge — voltage + % + estimated time remaining
 *  5. GPS status    — satellites, HDOP, fix type
 *  6. Motor bars    — 4 motor RPM (from telemetry or placeholder)
 */

import { useEffect, useRef, useState } from 'react'
import { useDroneStore } from '@/lib/stores/drone-store'
import { useWSStore } from '@/lib/stores/ws-store'

// ── Helpers ────────────────────────────────────────────────────────────────────

function useLatestTelemetry() {
  const { telemetryStream } = useDroneStore()
  return telemetryStream[telemetryStream.length - 1] as any
}

// ── 1. Compass Rose ────────────────────────────────────────────────────────────

function CompassRose({ heading = 0 }: { heading: number }) {
  const SIZE = 120
  const CX = SIZE / 2
  const CY = SIZE / 2
  const R = 50

  const cardinals = ['N', 'E', 'S', 'W']
  const majors    = [0, 45, 90, 135, 180, 225, 270, 315]
  const minors    = [22.5, 67.5, 112.5, 157.5, 202.5, 247.5, 292.5, 337.5]

  return (
    <div className="flex flex-col items-center gap-1">
      <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`}>
        {/* Outer ring */}
        <circle cx={CX} cy={CY} r={R + 4} fill="#121212" stroke="#b89b6e40" strokeWidth="2" />
        {/* Tick marks */}
        {majors.map(deg => {
          const rad = ((deg - heading - 90) * Math.PI) / 180
          const r1 = R - 2, r2 = R + 2
          return (
            <line key={deg}
              x1={CX + r1 * Math.cos(rad)} y1={CY + r1 * Math.sin(rad)}
              x2={CX + r2 * Math.cos(rad)} y2={CY + r2 * Math.sin(rad)}
              stroke="#cdb892" strokeWidth="1.5"
            />
          )
        })}
        {minors.map(deg => {
          const rad = ((deg - heading - 90) * Math.PI) / 180
          const r1 = R, r2 = R + 2
          return (
            <line key={deg}
              x1={CX + r1 * Math.cos(rad)} y1={CY + r1 * Math.sin(rad)}
              x2={CX + r2 * Math.cos(rad)} y2={CY + r2 * Math.sin(rad)}
              stroke="#5f4e30" strokeWidth="1"
            />
          )
        })}
        {/* Cardinal labels (rotate with heading) */}
        {cardinals.map((label, i) => {
          const deg = i * 90
          const rad = ((deg - heading - 90) * Math.PI) / 180
          const lr = R - 12
          return (
            <text key={label}
              x={CX + lr * Math.cos(rad)}
              y={CY + lr * Math.sin(rad) + 4}
              textAnchor="middle"
              fill={label === 'N' ? '#cdb892' : '#8f8f8f'}
              fontSize={label === 'N' ? '10' : '8'}
              fontFamily="monospace"
              fontWeight="bold"
            >
              {label}
            </text>
          )
        })}
        {/* Fixed heading pointer (top) */}
        <polygon
          points={`${CX},${CY - R - 6} ${CX - 4},${CY - R + 2} ${CX + 4},${CY - R + 2}`}
          fill="#cdb892"
        />
        {/* Center dot */}
        <circle cx={CX} cy={CY} r={3} fill="#cdb892" />
      </svg>
      <div className="text-sm font-mono font-bold text-success tabular-nums">
        {Math.round(heading).toString().padStart(3, '0')}°
      </div>
    </div>
  )
}

// ── 2. Altitude Tape ────────────────────────────────────────────────────────────

function AltitudeTape({ alt_m = 0, vspeed_ms = 0 }: { alt_m: number; vspeed_ms: number }) {
  const lines: number[] = []
  const base = Math.floor(alt_m / 5) * 5
  for (let i = base + 20; i >= base - 20; i -= 5) lines.push(i)

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="text-[9px] font-mono text-muted-foreground uppercase">ALT</div>
      <div className="relative bg-background border border-border rounded overflow-hidden"
        style={{ width: 52, height: 100 }}>
        {/* Tape lines */}
        <div className="absolute inset-0 flex flex-col justify-center" style={{ gap: 0 }}>
          {lines.map(v => {
            const offset = (v - alt_m) * (100 / 40)  // 100px for 40m span
            const isZero = v === 0
            return (
              <div key={v} className="absolute w-full flex items-center"
                style={{ top: `calc(50% - ${offset}px - 8px)` }}>
                <div className={`h-px flex-1 ${isZero ? 'bg-success' : 'bg-success/10'}`} style={{ marginLeft: v % 10 === 0 ? 0 : 20 }} />
                {v % 10 === 0 && (
                  <span className="text-[8px] font-mono text-muted-foreground px-1 flex-shrink-0">{v}</span>
                )}
              </div>
            )
          })}
        </div>
        {/* Current value box */}
        <div className="absolute inset-x-0 top-1/2 -translate-y-1/2 flex items-center justify-center">
          <div className="bg-background border border-brand-500 rounded px-1 py-0.5">
            <span className="text-xs font-mono font-bold text-success tabular-nums">
              {alt_m.toFixed(1)}
            </span>
          </div>
        </div>
      </div>
      <div className={`text-[9px] font-mono tabular-nums ${
        Math.abs(vspeed_ms) > 1 ? (vspeed_ms > 0 ? 'text-success' : 'text-warning') : 'text-muted-foreground/80'
      }`}>
        {vspeed_ms > 0 ? '↑' : vspeed_ms < 0 ? '↓' : '→'} {Math.abs(vspeed_ms).toFixed(1)}m/s
      </div>
    </div>
  )
}

// ── 3. Speed Indicator ─────────────────────────────────────────────────────────

function SpeedIndicator({ speed_ms = 0, flight_mode = '' }: { speed_ms: number; flight_mode: string }) {
  const maxSpeed = 20
  const pct = Math.min(speed_ms / maxSpeed, 1)
  const angle = -135 + pct * 270  // -135° to +135°
  const cx = 50, cy = 50, r = 38

  const ticks = [0, 5, 10, 15, 20]

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="text-[9px] font-mono text-muted-foreground uppercase">SPD</div>
      <svg width={100} height={80} viewBox="0 0 100 80">
        {/* Arc background */}
        <path
          d={describeArc(cx, cy, r, -135, 135)}
          fill="none" stroke="#1f1f1f" strokeWidth="6"
        />
        {/* Speed arc (green→yellow→red) */}
        {pct > 0 && (
          <path
            d={describeArc(cx, cy, r, -135, -135 + pct * 270)}
            fill="none"
            stroke={pct > 0.8 ? '#d65252' : pct > 0.6 ? '#ebad47' : '#56b388'}
            strokeWidth="5"
            strokeLinecap="round"
          />
        )}
        {/* Ticks */}
        {ticks.map(v => {
          const a = (-135 + (v / maxSpeed) * 270) * Math.PI / 180
          const r1 = r - 4, r2 = r + 2
          return (
            <g key={v}>
              <line
                x1={cx + r1 * Math.cos(a)} y1={cy + r1 * Math.sin(a)}
                x2={cx + r2 * Math.cos(a)} y2={cy + r2 * Math.sin(a)}
                stroke="#cdb89280" strokeWidth="1.2"
              />
              <text x={cx + (r - 12) * Math.cos(a)} y={cy + (r - 12) * Math.sin(a) + 3}
                textAnchor="middle" fill="#cdb89260" fontSize="7" fontFamily="monospace">{v}</text>
            </g>
          )
        })}
        {/* Needle */}
        {(() => {
          const a = angle * Math.PI / 180
          return <line x1={cx} y1={cy} x2={cx + (r - 6) * Math.cos(a)} y2={cy + (r - 6) * Math.sin(a)}
            stroke="#cdb892" strokeWidth="1.5" strokeLinecap="round" />
        })()}
        <circle cx={cx} cy={cy} r={3} fill="#cdb892" />
        {/* Value */}
        <text x={cx} y={cy + 18} textAnchor="middle" fill="#cdb892" fontSize="11" fontFamily="monospace" fontWeight="bold">
          {speed_ms.toFixed(1)}
        </text>
        <text x={cx} y={cy + 26} textAnchor="middle" fill="#9c8154" fontSize="6" fontFamily="monospace">m/s</text>
      </svg>
      {flight_mode && (
        <div className="text-[9px] font-mono bg-info/10 border border-info/30 text-info rounded px-1.5 py-0.5">
          {flight_mode}
        </div>
      )}
    </div>
  )
}

function describeArc(cx: number, cy: number, r: number, startAngle: number, endAngle: number): string {
  const toRad = (d: number) => (d * Math.PI) / 180
  const x1 = cx + r * Math.cos(toRad(startAngle))
  const y1 = cy + r * Math.sin(toRad(startAngle))
  const x2 = cx + r * Math.cos(toRad(endAngle))
  const y2 = cy + r * Math.sin(toRad(endAngle))
  const large = endAngle - startAngle > 180 ? 1 : 0
  return `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`
}

// ── 4. Battery Gauge ───────────────────────────────────────────────────────────

function BatteryGauge({
  pct,
  voltage_v = 0,
  remaining_s = 0,
}: {
  pct: number | null
  voltage_v: number
  remaining_s: number
}) {
  const color = pct == null ? '#3d3d3d' : pct < 20 ? '#d65252' : pct < 40 ? '#ebad47' : '#56b388'
  const mins = remaining_s > 0 ? Math.floor(remaining_s / 60) : null
  const secs = remaining_s > 0 ? Math.floor(remaining_s % 60) : null

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="text-[9px] font-mono text-muted-foreground uppercase">BATT</div>
      {/* Battery body */}
      <div className="relative" style={{ width: 32, height: 68 }}>
        {/* Terminal nub */}
        <div className="absolute top-0 left-1/2 -translate-x-1/2 w-4 h-2 rounded-t"
          style={{ background: color }} />
        {/* Outer shell */}
        <div className="absolute top-2 inset-x-0 bottom-0 rounded border-2"
          style={{ borderColor: color, background: '#0e0e0e' }}>
          {/* Fill */}
          <div className="absolute bottom-0 inset-x-0 rounded-sm transition-all duration-300"
            style={{ height: `${pct ?? 0}%`, background: color, opacity: 0.3 }} />
          {/* % label */}
          <div className="absolute inset-0 flex items-center justify-center">
            <span className="text-[8px] font-mono font-bold" style={{ color: pct == null ? '#8a8a8a' : color }}>{pct == null ? '—' : `${Math.round(pct)}%`}</span>
          </div>
        </div>
      </div>
      {/* Voltage */}
      {voltage_v > 0 && (
        <div className="text-[9px] font-mono text-warning tabular-nums">{voltage_v.toFixed(1)}V</div>
      )}
      {/* Time remaining */}
      {mins !== null && (
        <div className="text-[9px] font-mono text-muted-foreground tabular-nums">{mins}:{String(secs).padStart(2,'0')}</div>
      )}
    </div>
  )
}

// ── 5. GPS Status ──────────────────────────────────────────────────────────────

function GPSStatus({
  satellites = 0,
  hdop = 0,
  fix_type = '',
}: {
  satellites: number
  hdop: number
  fix_type: string
}) {
  const fixOk = fix_type.includes('3D') || fix_type.includes('FIXED')
  const fixLabel = fixOk ? '3D FIX' : fix_type || 'NO DATA'
  const satColor = satellites >= 8 ? '#56b388' : satellites >= 6 ? '#ebad47' : '#d65252'
  const hdopColor = hdop < 1.5 ? '#56b388' : hdop < 2.5 ? '#ebad47' : '#d65252'

  // Satellite signal bars (visual)
  const bars = Array.from({ length: 5 }, (_, i) => i < Math.round(satellites / 3))

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="text-[9px] font-mono text-muted-foreground uppercase">GPS</div>
      {/* Fix status */}
      <div className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded border ${
        fixOk
          ? 'bg-success/10 border-success/30 text-success'
          : 'bg-destructive/10 border-destructive/30 text-destructive'
      }`}>
        {fixLabel}
      </div>
      {/* Signal bars */}
      <div className="flex items-end gap-0.5 h-5">
        {bars.map((active, i) => (
          <div key={i}
            className="rounded-sm transition-colors"
            style={{
              width: 5,
              height: 4 + i * 3,
              background: active ? satColor : '#262626',
            }}
          />
        ))}
      </div>
      <div className="flex flex-col items-center gap-0.5">
        <span className="text-[9px] font-mono tabular-nums" style={{ color: satColor }}>{satellites} SATS</span>
        <span className="text-[9px] font-mono tabular-nums" style={{ color: hdopColor }}>HDOP {hdop.toFixed(1)}</span>
      </div>
    </div>
  )
}

// ── 6. Motor Bars ──────────────────────────────────────────────────────────────

function MotorBars({ motor_rpms = [] }: { motor_rpms: number[] }) {
  const MAX_RPM = 8000
  // Default 4 motors if no data
  const motors = motor_rpms.length > 0
    ? motor_rpms.slice(0, 8)
    : [0, 0, 0, 0]

  return (
    <div className="flex flex-col gap-1">
      <div className="text-[9px] font-mono text-muted-foreground uppercase text-center">MOTORS</div>
      <div className={`grid gap-1 ${motors.length > 4 ? 'grid-cols-4' : 'grid-cols-2'}`}>
        {motors.map((rpm, i) => {
          const pct = Math.min(rpm / MAX_RPM, 1)
          const col = pct > 0.9 ? '#d65252' : pct > 0.7 ? '#ebad47' : pct > 0.1 ? '#56b388' : '#262626'
          return (
            <div key={i} className="flex flex-col items-center gap-0.5">
              <div className="text-[8px] font-mono text-muted-foreground/80">M{i + 1}</div>
              {/* Vertical bar */}
              <div className="relative bg-background rounded overflow-hidden border border-border"
                style={{ width: 20, height: 40 }}>
                <div className="absolute bottom-0 inset-x-0 rounded-sm transition-all duration-200"
                  style={{ height: `${pct * 100}%`, background: col, opacity: 0.6 }} />
              </div>
              <div className="text-[7px] font-mono tabular-nums" style={{ color: col }}>
                {rpm > 0 ? `${Math.round(rpm / 100) * 100}` : '---'}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Armed / Flight mode badge ──────────────────────────────────────────────────

function StatusBadges({ armed, flight_mode = '' }: { armed: boolean | null; flight_mode: string }) {
  return (
    <div className="flex gap-1.5 justify-center flex-wrap">
      <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded border ${
        armed
          ? 'bg-success/10 border-success/30 text-success'
          : 'bg-white/[0.04] border-border text-muted-foreground'
      }`}>
        {armed == null ? 'NO DATA' : armed ? 'ARMED' : 'DISARMED'}
      </span>
      {flight_mode && (
        <span className="text-[10px] font-mono px-2 py-0.5 rounded border border-info/30 text-info bg-info/10">
          {flight_mode}
        </span>
      )}
    </div>
  )
}

// ── Public component ───────────────────────────────────────────────────────────

interface TelemetryDashboardProps {
  className?: string
}

export default function TelemetryDashboard({ className = '' }: TelemetryDashboardProps) {
  const latest = useLatestTelemetry()
  const { socket } = useWSStore()

  // Merged telemetry (prefer MAVLink live over sim stream)
  const [mavTel, setMavTel] = useState<any>({})

  useEffect(() => {
    if (!socket) return
    const handler = (data: any) => {
      setMavTel((prev: any) => ({ ...prev, ...data }))
    }
    socket.on('mavlink_telemetry', handler)
    return () => { socket.off('mavlink_telemetry', handler) }
  }, [socket])

  // Resolved values (prefer MAVLink, fallback to sim store)
  const heading   = mavTel.yaw_deg     ?? latest?.attitude?.yaw_deg    ?? 0
  const alt_m     = mavTel.alt_m       ?? latest?.position?.alt_m      ?? 0
  const speed_ms  = mavTel.speed_ms    ?? latest?.speed_ms             ?? (latest?.velocity
    ? Math.sqrt(
        Math.pow(latest.velocity.north_ms ?? 0, 2) +
        Math.pow(latest.velocity.east_ms ?? 0, 2)
      )
    : 0)
  const vspeed_ms = latest?.velocity?.down_ms ? -latest.velocity.down_ms : (mavTel.vspeed_ms ?? 0)
  const batt_pct: number | null = mavTel.battery_pct ?? latest?.battery?.percent ?? latest?.battery_pct ?? null
  const voltage_v = mavTel.voltage_v   ?? latest?.battery?.voltage_v   ?? 0
  const remaining_s = latest?.battery?.remaining_s ?? (mavTel.remaining_s ?? 0)
  const satellites  = mavTel.satellites ?? latest?.gps_satellites ?? 0
  const hdop        = mavTel.hdop       ?? latest?.hdop            ?? 0
  const fix_type    = mavTel.fix_type   ?? ''
  const flight_mode = mavTel.flight_mode ?? latest?.flight_mode ?? ''
  const armed: boolean | null = mavTel.armed ?? latest?.armed ?? null
  const motor_rpms: number[] = latest?.motor_rpms ?? mavTel.motor_rpms ?? []

  return (
    <div className={`rounded-xl border border-border bg-card overflow-hidden ${className}`}>
      {/* Header */}
      <div className="flex min-h-[3rem] items-center justify-between gap-3 border-b border-border px-4 py-2.5">
        <span className="text-sm font-medium text-foreground">Telemetry</span>
        <StatusBadges armed={armed} flight_mode={flight_mode} />
      </div>

      {/* Instruments grid */}
      <div className="p-3 grid grid-cols-3 gap-3">
        {/* Row 1: Compass | Altitude | Speed */}
        <CompassRose heading={heading} />
        <AltitudeTape alt_m={alt_m} vspeed_ms={vspeed_ms} />
        <SpeedIndicator speed_ms={speed_ms} flight_mode={flight_mode} />
      </div>

      <div className="px-3 pb-3 grid grid-cols-3 gap-3">
        {/* Row 2: Battery | GPS | Motors */}
        <BatteryGauge pct={batt_pct} voltage_v={voltage_v} remaining_s={remaining_s} />
        <GPSStatus satellites={satellites} hdop={hdop} fix_type={fix_type} />
        <MotorBars motor_rpms={motor_rpms} />
      </div>

      {/* Numeric readout strip */}
      <div className="px-3 pb-3 grid grid-cols-4 gap-1">
        {[
          { label: 'ALT', value: `${alt_m.toFixed(1)}m`,          color: 'text-info' },
          { label: 'SPD', value: `${speed_ms.toFixed(1)}m/s`,     color: 'text-info' },
          { label: 'HDG', value: `${Math.round(heading)}°`,        color: 'text-success' },
          { label: 'BAT', value: batt_pct == null ? '—' : `${batt_pct.toFixed(0)}%`, color: batt_pct == null ? 'text-muted-foreground' : batt_pct < 20 ? 'text-destructive' : 'text-success' },
        ].map(({ label, value, color }) => (
          <div key={label} className="flex flex-col items-center bg-background/80 rounded py-1 border border-border/60">
            <span className="text-[8px] font-mono text-muted-foreground/80 uppercase">{label}</span>
            <span className={`text-[10px] font-mono font-bold tabular-nums ${color}`}>{value}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
