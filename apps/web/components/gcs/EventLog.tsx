'use client'

/**
 * EventLog — scrollable filtered event log for the WebGCS.
 *
 * Sources:
 *  - telemetryStream state changes (battery warnings, altitude warnings)
 *  - simulationStatus transitions
 *  - currentPhase transitions
 *  - Manual entries pushed via socket 'gcs:event' events
 *
 * Features:
 *  - Category filter chips: ALL / INFO / WARN / ERROR / CMD
 *  - Auto-scroll to latest entry
 *  - Timestamp + severity badge per row
 *  - Max 200 entries (ring buffer)
 */

import { useEffect, useRef, useState, useCallback } from 'react'
import { useDroneStore } from '@/lib/stores/drone-store'
import { useWSStore } from '@/lib/stores/ws-store'

type Severity = 'info' | 'warn' | 'error' | 'cmd'

interface LogEntry {
  id:        number
  ts:        string     // HH:MM:SS.mmm
  severity:  Severity
  message:   string
  source:    string
}

let _counter = 0
function makeEntry(severity: Severity, message: string, source = 'system'): LogEntry {
  const now = new Date()
  return {
    id: ++_counter,
    ts: now.toTimeString().slice(0, 8) + '.' + String(now.getMilliseconds()).padStart(3, '0'),
    severity,
    message,
    source,
  }
}

const SEV_COLORS: Record<Severity, string> = {
  info:  'text-success',
  warn:  'text-warning',
  error: 'text-destructive',
  cmd:   'text-info',
}

const SEV_BG: Record<Severity, string> = {
  info:  'bg-success/10 border-success/30',
  warn:  'bg-warning/10 border-warning/30',
  error: 'bg-destructive/10 border-destructive/30',
  cmd:   'bg-info/10 border-info/30',
}

const MAX_ENTRIES = 200
const INITIAL_ENTRY: LogEntry = {
  id: 0,
  ts: '00:00:00.000',
  severity: 'info',
  message: 'EventLog initialised',
  source: 'gcs',
}

// ── Public component ──────────────────────────────────────────

interface EventLogProps {
  className?: string
}

export default function EventLog({ className = '' }: EventLogProps) {
  const { telemetryStream, simulationStatus, currentPhase } = useDroneStore()
  const { socket } = useWSStore()
  const [entries, setEntries] = useState<LogEntry[]>([INITIAL_ENTRY])
  const [filter, setFilter] = useState<Severity | 'all'>('all')
  const [autoScroll, setAutoScroll] = useState(true)
  const listRef = useRef<HTMLDivElement>(null)

  const push = useCallback((entry: LogEntry) => {
    setEntries(prev => {
      const next = [...prev, entry]
      return next.length > MAX_ENTRIES ? next.slice(-MAX_ENTRIES) : next
    })
  }, [])

  // Watch simulationStatus
  const prevStatus = useRef(simulationStatus)
  useEffect(() => {
    if (simulationStatus !== prevStatus.current) {
      push(makeEntry('info', `Simulation status → ${simulationStatus}`, 'simulator'))
      prevStatus.current = simulationStatus
    }
  }, [simulationStatus, push])

  // Watch currentPhase
  const prevPhase = useRef(currentPhase)
  useEffect(() => {
    if (currentPhase !== prevPhase.current) {
      const sev: Severity = currentPhase === 'error' ? 'error' : 'info'
      push(makeEntry(sev, `Phase → ${currentPhase}`, 'orchestrator'))
      prevPhase.current = currentPhase
    }
  }, [currentPhase, push])

  // Watch telemetry for warnings
  const prevTelLen = useRef(0)
  useEffect(() => {
    const latest = telemetryStream[telemetryStream.length - 1]
    if (!latest || telemetryStream.length === prevTelLen.current) return
    prevTelLen.current = telemetryStream.length

    if (latest.battery_pct != null && latest.battery_pct < 20) {
      push(makeEntry('warn', `LOW BATTERY: ${latest.battery_pct.toFixed(0)}%`, 'telemetry'))
    }
    if ((latest.position?.alt_m ?? 0) > 120) {
      push(makeEntry('warn', `HIGH ALTITUDE: ${(latest.position?.alt_m ?? 0).toFixed(1)} m`, 'telemetry'))
    }
    if (latest.speed_ms != null && latest.speed_ms > 18) {
      push(makeEntry('warn', `HIGH SPEED: ${latest.speed_ms.toFixed(1)} m/s`, 'telemetry'))
    }
  }, [telemetryStream, push])

  // Socket events
  useEffect(() => {
    if (!socket) return
    const handler = (data: { severity?: Severity; message: string; source?: string }) => {
      push(makeEntry(data.severity ?? 'info', data.message, data.source ?? 'server'))
    }
    const cmdHandler = (data: { command: string }) => {
      push(makeEntry('cmd', `CMD sent: ${data.command.toUpperCase()}`, 'gcs'))
    }
    socket.on('gcs:event', handler)
    socket.on('gcs:command', cmdHandler)
    return () => {
      socket.off('gcs:event', handler)
      socket.off('gcs:command', cmdHandler)
    }
  }, [socket, push])

  // Auto-scroll
  useEffect(() => {
    if (autoScroll && listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight
    }
  }, [entries, autoScroll])

  const visible = filter === 'all' ? entries : entries.filter(e => e.severity === filter)

  return (
    <div className={`rounded-xl border border-border bg-card flex flex-col overflow-hidden ${className}`}>
      {/* Header */}
      <div className="flex min-h-[3rem] items-center justify-between gap-3 border-b border-border px-4 py-2.5 flex-shrink-0">
        <span className="text-sm font-medium text-foreground">Event log</span>
        <div className="flex items-center gap-2">
          <span className="text-2xs tabular-nums text-muted-foreground">{entries.length} entries</span>
          <button
            onClick={() => setAutoScroll(v => !v)}
            className={`text-2xs font-medium px-2 py-1 rounded-md border transition-colors ${
              autoScroll
                ? 'border-success/30 text-success'
                : 'border-border text-muted-foreground/80'
            }`}
          >
            {autoScroll ? 'Auto-scroll' : 'Scroll paused'}
          </button>
          <button
            onClick={() => {
              const rows = ['timestamp,severity,source,message']
              entries.forEach(e => rows.push(`"${e.ts}","${e.severity}","${e.source}","${e.message.replace(/"/g, '""')}"`) )
              const blob = new Blob([rows.join('\n')], { type: 'text/csv' })
              const url  = URL.createObjectURL(blob)
              const a    = document.createElement('a')
              a.href     = url
              a.download = `gcs-log-${new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')}.csv`
              a.click()
              URL.revokeObjectURL(url)
            }}
            className="text-2xs font-medium px-2 py-1 rounded-md border border-border text-muted-foreground/80 hover:text-success transition-colors"
          >
            Export CSV
          </button>
          <button
            onClick={() => setEntries([makeEntry('info', 'Log cleared', 'gcs')])}
            className="text-2xs font-medium px-2 py-1 rounded-md border border-border text-muted-foreground/80 hover:text-destructive transition-colors"
          >
            Clear
          </button>
        </div>
      </div>

      {/* Filter chips */}
      <div className="flex gap-1.5 px-3 py-2 border-b border-border/60 flex-shrink-0">
        {(['all', 'info', 'warn', 'error', 'cmd'] as const).map(f => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`text-[10px] font-mono px-2 py-0.5 rounded-full border transition-colors uppercase ${
              filter === f
                ? f === 'all'
                  ? 'bg-success/10 border-success/30 text-success'
                  : `${SEV_BG[f as Severity]} ${SEV_COLORS[f as Severity]} border`
                : 'border-border text-muted-foreground/80'
            }`}
          >
            {f}
          </button>
        ))}
      </div>

      {/* Log entries */}
      <div
        ref={listRef}
        className="flex-1 overflow-y-auto min-h-0 space-y-0.5 p-2"
        style={{ maxHeight: 280 }}
        onScroll={e => {
          const el = e.currentTarget
          const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 20
          setAutoScroll(atBottom)
        }}
      >
        {visible.length === 0 ? (
          <div className="text-center text-muted-foreground/60 text-xs font-mono py-8">No entries</div>
        ) : (
          visible.map(entry => (
            <div
              key={entry.id}
              className={`flex items-start gap-2 px-2 py-1 rounded text-[10px] font-mono border ${SEV_BG[entry.severity]}`}
            >
              <span className="text-muted-foreground/80 flex-shrink-0 tabular-nums">{entry.ts}</span>
              <span className={`flex-shrink-0 uppercase font-bold w-10 ${SEV_COLORS[entry.severity]}`}>
                {entry.severity}
              </span>
              <span className="text-muted-foreground flex-shrink-0">[{entry.source}]</span>
              <span className="text-foreground/80 break-all">{entry.message}</span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
