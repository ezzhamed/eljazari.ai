'use client'

import dynamic from 'next/dynamic'
import { useId, useState } from 'react'
import { Activity, Eye, Map as MapIcon, MapPin, Radar, ShieldCheck, TerminalSquare, ArrowUpRight, ChevronDown, Layers, Navigation } from 'lucide-react'
import TelemetryCharts from '@/components/charts/TelemetryCharts'
import { Badge, type BadgeTone } from '@/components/ui/badge'
import { Card, CardHeader } from '@/components/ui/card'
import { EmptyState } from '@/components/ui/empty-state'
import { PageBody, PageHeader } from '@/components/ui/page'
import { Segmented } from '@/components/ui/segmented'
import { Stat } from '@/components/ui/stat'
import { useDroneStore, type SimStatus } from '@/lib/stores/drone-store'

const SimulationTerminal = dynamic(() => import('@/components/terminal/SimulationTerminal'), { ssr: false })
const FlightPathViewer = dynamic(() => import('@/components/drone-viewer/FlightPathViewer'), {
  ssr: false,
  loading: () => <div className="flex h-full items-center justify-center text-sm text-muted-foreground">Loading the 3D view…</div>,
})

/** The backend's default `max_simulation_iterations` (config/settings.py). */
const MAX_ITERATIONS = 5

const STATUS: Record<SimStatus, { label: string; tone: BadgeTone }> = {
  idle: { label: 'Not started', tone: 'neutral' },
  running: { label: 'Flying', tone: 'brand' },
  completed: { label: 'Completed', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
}

const OBSERVATION_TONE: Record<string, BadgeTone> = {
  info: 'neutral',
  event: 'info',
  warning: 'warning',
  critical: 'danger',
}

type MapView = '3d' | '2d'

/** Top-down trace of the telemetry, north up, home marked H. */
function FlightMap2D() {
  const telemetry = useDroneStore((s) => s.telemetryStream)
  const gridId = useId()
  const SIZE = 500
  const PAD = 20
  const points = telemetry.map(p => ({ x: p.position?.east_m ?? NaN, y: p.position?.north_m ?? NaN })).filter(p => Number.isFinite(p.x) && Number.isFinite(p.y))
  const xs = points.map((p) => p.x)
  const ys = points.map((p) => p.y)
  const minX = Math.min(0, ...xs)
  const maxX = Math.max(0, ...xs)
  const minY = Math.min(0, ...ys)
  const maxY = Math.max(0, ...ys)
  // One scale for both axes, so a square flies as a square.
  const span = Math.max(maxX - minX, maxY - minY, 10) * 1.2
  const centerX = (minX + maxX) / 2
  const centerY = (minY + maxY) / 2
  const sx = (x: number) => PAD + ((x - centerX + span / 2) / span) * (SIZE - PAD * 2)
  const sy = (y: number) => SIZE - PAD - ((y - centerY + span / 2) / span) * (SIZE - PAD * 2)
  const d = points.length > 1 ? `M ${points.map((p) => `${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(' L ')}` : ''
  const last = points[points.length - 1]

  if (points.length === 0) {
    return <EmptyState icon={MapPin} title="No track yet" description="The flight's ground track draws here." />
  }

  return (
    <svg viewBox={`0 0 ${SIZE} ${SIZE}`} className="h-full w-full bg-[#0b121c] p-6" role="img" aria-label="Top-down flight track">
      <defs>
        <pattern id={gridId} width="30" height="30" patternUnits="userSpaceOnUse">
          <path d="M 30 0 L 0 0 0 30" fill="none" stroke="hsl(0 0% 100% / 0.05)" strokeWidth="0.5" />
        </pattern>
      </defs>
      <text x={SIZE - 24} y={30} fill="#87d4e1" fontSize={12}>N ↑</text>
      <rect width={SIZE} height={SIZE} fill={`url(#${gridId})`} />
      {d && <path d={d} fill="none" stroke="#87d4e1" strokeWidth="2" opacity="0.85" />}
      <circle cx={sx(0)} cy={sy(0)} r={5} fill="none" stroke="hsl(var(--muted-foreground))" strokeWidth={1.2} />
      <text x={sx(0) + 8} y={sy(0) + 3} fill="hsl(var(--muted-foreground))" fontSize={8}>H</text>
      {last && <circle cx={sx(last.x)} cy={sy(last.y)} r={4} fill="#87d4e1" />}
    </svg>
  )
}

export default function SimulatePage() {
  const simulations = useDroneStore((s) => s.simulations)
  const simulationStatus = useDroneStore((s) => s.simulationStatus)
  const telemetry = useDroneStore((s) => s.telemetryStream)
  const observations = useDroneStore((s) => s.semanticObservations)
  const currentIteration = useDroneStore((s) => s.currentIteration)
  const logCount = useDroneStore((s) => s.pipelineLogs.length)
  const [mapView, setMapView] = useState<MapView>('3d')
  const [showLogs, setShowLogs] = useState(false)
  const latest = simulations[simulations.length - 1]
  // Only the gate's evaluation is a verdict; `simulation_complete` always reports passed=false.
  const verdict = latest?.evaluation?.passed
  const lastPoint = telemetry[telemetry.length - 1]
  const status = STATUS[simulationStatus] ?? STATUS.idle
  const attempt = simulationStatus === 'running' ? currentIteration + 1 : currentIteration

  return <PageBody className="gap-5">
    <PageHeader title="Simulation" icon={Radar}
      description="Follow the aircraft. Inspect the flight. Review the result."
      status={<Badge tone={status.tone} dot={simulationStatus === 'running' ? 'pulse' : simulationStatus !== 'idle'}>{status.label}</Badge>}
      actions={<div className="flex items-center gap-3 rounded-lg border border-border bg-card px-3 py-2">
        <span className="text-2xs text-muted-foreground">Attempt <span className="ml-1 font-mono text-foreground">{attempt.toString().padStart(2, '0')}</span><span className="text-muted-foreground/50"> / {MAX_ITERATIONS.toString().padStart(2, '0')}</span></span>
        <div className="flex gap-1" aria-hidden>{Array.from({ length: MAX_ITERATIONS }, (_, i) => <span key={i} className={`h-3 w-1 rounded-full ${i < attempt ? 'bg-brand-400' : 'bg-white/10'}`} />)}</div>
      </div>} />

    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      <Stat label="Flight acceptance" icon={ShieldCheck} value={verdict == null ? 'Pending' : verdict ? 'Passed' : 'Rejected'}
        tone={verdict == null ? 'muted' : verdict ? 'success' : 'danger'} hint={latest ? `Attempt ${latest.iteration} · Mission comparison` : 'Evaluated after the flight'} />
      <Stat label="Telemetry buffer" icon={Activity} value={telemetry.length || null} unit="samples" hint="Latest recorded positions" />
      <Stat label="Current altitude" icon={ArrowUpRight} value={lastPoint?.position?.alt_m?.toFixed(1)} unit="m" hint="Latest telemetry sample" />
      <Stat label="Flight events" icon={Eye} value={observations.length || null} hint="Mode changes and observations" />
    </div>

    <Card className="overflow-hidden border-white/10 shadow-[0_12px_48px_-24px_rgba(0,0,0,0.7)]">
      <CardHeader title="Flight path" icon={Navigation} description="Aircraft position and recorded trajectory"
        className="min-h-16 max-sm:grid max-sm:grid-cols-[1rem_minmax(0,1fr)] max-sm:[&>div:last-child]:col-span-2 max-sm:[&>div:last-child]:justify-self-start"
        actions={<Segmented<MapView> label="Flight path view" value={mapView} onChange={setMapView}
          options={[{ value: '3d', label: <><Layers className="h-3.5 w-3.5" />3D view</> }, { value: '2d', label: <><MapIcon className="h-3.5 w-3.5" />Top-down</> }]} />} />
      <div className="relative h-[540px] min-h-[460px] sm:h-[600px]">
        <div className="absolute inset-0">{mapView === '3d' ? <FlightPathViewer /> : <FlightMap2D />}</div>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border bg-card px-4 py-2.5 text-[10px] text-muted-foreground">
        <span className="flex items-center gap-2"><span className="h-1.5 w-1.5 rounded-full bg-brand-400/70" />PX4 software-in-the-loop</span>
        <span>Simulation evidence · Not hardware validation</span>
      </div>
    </Card>

    <div className="flex items-center gap-3 pt-2"><Activity className="h-3.5 w-3.5 text-brand-300" /><h2 className="text-sm font-medium">Flight telemetry</h2><div className="h-px flex-1 bg-border" /><span className="text-2xs text-muted-foreground">Recorded samples</span></div>
    <TelemetryCharts />

    <Card className="overflow-hidden">
      <CardHeader title="Flight events" icon={Eye} description="Observations from the flight monitor, newest first"
        actions={<span className="rounded-md border border-border px-2 py-0.5 font-mono text-2xs text-muted-foreground">{observations.length} events</span>} />
      {observations.length === 0 ? <div className="flex items-center gap-4 px-5 py-7">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-border bg-background"><Eye className="h-4 w-4 text-muted-foreground" /></div>
        <div><p className="text-xs font-medium">A clear view of every event</p><p className="mt-1 text-xs text-muted-foreground">Flight-mode changes, warnings and monitor observations will appear here.</p></div>
      </div> : <ol className="max-h-72 divide-y divide-border/60 overflow-y-auto">
        {[...observations].reverse().map((obs, i) => {
          const o = obs as unknown as { timestamp_s?: number; timestamp_ms?: number; description: string; event_type: string; severity: string }
          const seconds = o.timestamp_s ?? (o.timestamp_ms != null ? o.timestamp_ms / 1000 : null)
          return <li key={i} className="flex flex-wrap items-start gap-3 px-5 py-3 text-xs">
            <span className="w-14 shrink-0 pt-0.5 font-mono text-2xs tabular-nums text-muted-foreground">{seconds != null ? `${seconds.toFixed(1)} s` : '—'}</span>
            <Badge tone={OBSERVATION_TONE[o.severity] ?? 'neutral'} className="shrink-0 capitalize">{o.event_type.replace(/_/g, ' ')}</Badge>
            <span className="min-w-0 flex-1 leading-5 text-foreground/90" dir="auto">{o.description}</span>
          </li>
        })}
      </ol>}
    </Card>

    <Card className="overflow-hidden">
      <button type="button" onClick={() => setShowLogs(!showLogs)} aria-expanded={showLogs} aria-controls="simulation-console"
        className="focus-ring flex w-full items-center gap-3 px-4 py-4 text-left hover:bg-white/[0.02]">
        <TerminalSquare className="h-4 w-4 text-muted-foreground" /><span className="flex-1 text-xs font-medium">Simulator console</span>
        <span className="font-mono text-2xs text-muted-foreground">{logCount} lines</span><ChevronDown className={`h-4 w-4 text-muted-foreground transition-transform ${showLogs ? 'rotate-180' : ''}`} />
      </button>
      <div id="simulation-console" hidden={!showLogs}>
        {showLogs && <div className="h-64 border-t border-border bg-card p-3"><SimulationTerminal /></div>}
      </div>
    </Card>
  </PageBody>
}
