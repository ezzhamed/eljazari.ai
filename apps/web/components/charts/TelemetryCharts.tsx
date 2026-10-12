'use client'

import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from 'recharts'
import { Activity } from 'lucide-react'
import { EmptyState } from '@/components/ui/empty-state'
import { useDroneStore } from '@/lib/stores/drone-store'

// ── Common chart wrapper ───────────────────────────────────────────────────────

function ChartCard({ title, children }: {
  title: string
  children: React.ReactNode
}) {
  return (
    <div className="min-w-0 overflow-hidden rounded-xl border border-border bg-card">
      <div className="flex h-10 items-center border-b border-border px-4">
        <span className="text-xs font-medium text-foreground">{title}</span>
      </div>
      <div className="h-40 p-3">{children}</div>
    </div>
  )
}

// Shared tooltip styling
const tooltipStyle = {
  backgroundColor: 'hsl(var(--card))',
  border: '1px solid hsl(var(--border))',
  borderRadius: '6px',
  fontSize: '11px',
  color: 'hsl(var(--foreground))',
}

// ── Individual charts ──────────────────────────────────────────────────────────

export function AltitudeChart() {
  const { telemetryStream } = useDroneStore()
  const data = telemetryStream.map((p, i) => ({
    t:   i,
    alt: p?.position?.alt_m ?? 0,
  }))

  return (
    <ChartCard title="Altitude (m)">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis dataKey="t" hide tick={{ fontSize: 9 }} />
          <YAxis tick={{ fontSize: 9, fill: 'hsl(var(--muted-foreground))' }} />
          <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => [`${v.toFixed(1)}m`, 'Alt']} />
          <ReferenceLine y={0}   stroke="hsl(var(--border))" strokeDasharray="4 2" />
          <ReferenceLine y={120} stroke="hsl(var(--destructive))" strokeDasharray="4 2" label={{ value: 'Max', fontSize: 9, fill: 'hsl(var(--destructive))' }} />
          <Line type="monotone" dataKey="alt" stroke="hsl(var(--primary))" strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  )
}

export function SpeedChart() {
  const { telemetryStream } = useDroneStore()
  const data = telemetryStream.map((p, i) => ({
    t:     i,
    speed: Math.sqrt(
      (p.velocity?.north_ms ?? 0) ** 2 +
      (p.velocity?.east_ms  ?? 0) ** 2
    ),
  }))

  return (
    <ChartCard title="Speed (m/s)">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis dataKey="t" hide />
          <YAxis tick={{ fontSize: 9, fill: 'hsl(var(--muted-foreground))' }} />
          <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => [`${v.toFixed(2)}m/s`, 'Speed']} />
          <ReferenceLine y={15} stroke="hsl(var(--destructive))" strokeDasharray="4 2" label={{ value: 'Limit', fontSize: 9, fill: 'hsl(var(--destructive))' }} />
          <Line type="monotone" dataKey="speed" stroke="hsl(var(--info))" strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  )
}

export function BatteryChart() {
  const { telemetryStream } = useDroneStore()
  const data = telemetryStream.map((p, i) => ({
    t:       i,
    battery: p.battery?.percent ?? 0,
  }))

  return (
    <ChartCard title="Battery (%)">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis dataKey="t" hide />
          <YAxis domain={[0, 100]} tick={{ fontSize: 9, fill: 'hsl(var(--muted-foreground))' }} />
          <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => [`${v.toFixed(1)}%`, 'Battery']} />
          <ReferenceLine y={20} stroke="hsl(var(--destructive))" strokeDasharray="4 2" label={{ value: 'Min', fontSize: 9, fill: 'hsl(var(--destructive))' }} />
          <Line type="monotone" dataKey="battery" stroke="hsl(var(--warning))" strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  )
}

// ── Composite dashboard ────────────────────────────────────────────────────────

export default function TelemetryCharts() {
  const { telemetryStream } = useDroneStore()

  if (telemetryStream.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-card">
        <EmptyState icon={Activity} title="No telemetry yet" description="Altitude, speed and battery plot here as the simulated flight streams in." />
      </div>
    )
  }

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <AltitudeChart />
      <SpeedChart />
      <BatteryChart />
    </div>
  )
}
