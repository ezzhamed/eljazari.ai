'use client'

import {
  Bar, BarChart, CartesianGrid, Cell, PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { BarChart3, Clock, Gauge, Lightbulb, Radar as RadarIcon, RefreshCw, Target } from 'lucide-react'
import { RestoringState } from '@/components/app/RestoringState'
import { FlightValidationCard } from '@/components/monitor/FlightValidationCard'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { EmptyState } from '@/components/ui/empty-state'
import { PageBody, PageHeader } from '@/components/ui/page'
import { Stat } from '@/components/ui/stat'
import { useDroneStore } from '@/lib/stores/drone-store'

type ReportMetrics = Record<string, number | null | undefined>

/** A metric the report actually contains, or null. Missing is not zero. */
function metric(metrics: ReportMetrics, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = metrics[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return null
}

const TOOLTIP = {
  backgroundColor: 'hsl(var(--popover))',
  border: '1px solid hsl(var(--border))',
  borderRadius: '8px',
  fontSize: '11px',
  color: 'hsl(var(--foreground))',
}

interface IterationRow {
  iteration: number
  score?: number
  passed?: boolean
  evaluation?: { score?: number; passed?: boolean }
}

function IterationChart({ simulations }: { simulations: IterationRow[] }) {
  // Live runs carry the score under `evaluation`, hydrated runs at the top level.
  const data = [...simulations]
    .sort((a, b) => a.iteration - b.iteration)
    .map((s) => ({
      name: `Run ${s.iteration}`,
      score: s.evaluation?.score ?? s.score ?? null,
      passed: s.evaluation?.passed ?? s.passed,
    }))
  const scored = data.filter((d) => d.score != null)

  return (
    <Card>
      <CardHeader title="Score per simulation run" icon={BarChart3} description="Evaluator score; green runs passed the gate" />
      <CardContent className="h-64 flex-none">
        {scored.length === 0 ? (
          <EmptyState icon={BarChart3} title="No scored runs" description="Each simulation run's evaluator score appears here." />
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
              <XAxis dataKey="name" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} axisLine={false} tickLine={false} />
              <YAxis domain={[0, 100]} tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={TOOLTIP} cursor={{ fill: 'hsl(0 0% 100% / 0.03)' }} formatter={(v: number) => [`${v.toFixed(0)} / 100`, 'Score']} />
              <Bar dataKey="score" radius={[4, 4, 0, 0]} maxBarSize={48} isAnimationActive={false}>
                {data.map((entry, i) => (
                  <Cell key={i} fill={entry.passed ? 'hsl(var(--success))' : 'hsl(var(--destructive))'} fillOpacity={0.8} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  )
}

function QualityRadar({ metrics }: { metrics: ReportMetrics }) {
  const axes = [
    { subject: 'Safety', value: metric(metrics, 'safety_score') },
    { subject: 'Path', value: metric(metrics, 'path_efficiency', 'path_completeness') },
    { subject: 'Mission', value: metric(metrics, 'mission_success_rate') },
    { subject: 'Compliance', value: metric(metrics, 'constraint_compliance') },
  ]
    .filter((axis): axis is { subject: string; value: number } => axis.value != null)
    .map((axis) => ({ ...axis, value: axis.value * 100 }))

  return (
    <Card>
      <CardHeader title="Quality" icon={RadarIcon} description="Report metrics, 0–100" />
      <CardContent className="h-64 flex-none">
        {axes.length < 3 ? (
          <EmptyState icon={RadarIcon} title="Not enough metrics" description="The report needs at least three of safety, path, mission and compliance to draw this." />
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <RadarChart data={axes} outerRadius="68%" margin={{ top: 8, right: 36, bottom: 8, left: 36 }}>
              <PolarGrid stroke="hsl(var(--border))" />
              <PolarAngleAxis dataKey="subject" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
              {/* Without an explicit radius scale Recharts collapsed every value to the centre. */}
              <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
              <Radar dataKey="value" stroke="hsl(var(--primary))" fill="hsl(var(--primary))" fillOpacity={0.15} strokeWidth={2} isAnimationActive={false} />
              <Tooltip contentStyle={TOOLTIP} formatter={(v: number) => [`${v.toFixed(0)} / 100`, 'Value']} />
            </RadarChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  )
}

export default function MonitorPage() {
  const report = useDroneStore((s) => s.performanceReport)
  const simulations = useDroneStore((s) => s.simulations)
  const isRestoring = useDroneStore((s) => s.isRestoring)

  const header = (
    <PageHeader
      title="Performance"
      icon={Gauge}
      description="The Monitor agent's analysis of the flight, and the acceptance gate's verdict on what flew."
      status={
        report ? (
          <Badge tone={report.overall_score == null ? 'neutral' : 'brand'}>
            {report.overall_score == null ? 'No score' : `Score ${report.overall_score.toFixed(0)} / 100`}
          </Badge>
        ) : undefined
      }
    />
  )

  if (!report) {
    return (
      <PageBody>
        {header}
        <Card>
          {isRestoring ? <RestoringState /> : <EmptyState icon={Gauge} title="No performance report yet" description="The report is written after the simulation, once the Monitor agent has analysed the flight." />}
        </Card>
      </PageBody>
    )
  }

  const m = (report.metrics ?? {}) as unknown as ReportMetrics
  const successRate = metric(m, 'mission_success_rate')
  const iterations = metric(m, 'code_iterations_needed')
  const duration = metric(m, 'avg_flight_duration_s')

  return (
    <PageBody>
      {header}

      {report.validation && <FlightValidationCard validation={report.validation} />}

      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <Stat
          label="Overall score"
          icon={Gauge}
          value={report.overall_score != null ? report.overall_score.toFixed(1) : null}
          unit="/ 100"
          tone="brand"
          hint={report.overall_score == null ? 'Not scored: blocked or rejected' : 'Monitor agent'}
        />
        <Stat
          label="Mission success"
          icon={Target}
          value={successRate != null ? (successRate * 100).toFixed(0) : null}
          unit="%"
          tone={successRate == null ? 'default' : successRate >= 0.8 ? 'success' : successRate >= 0.5 ? 'warning' : 'danger'}
        />
        <Stat label="Code versions" icon={RefreshCw} value={iterations != null && iterations > 0 ? iterations : null} hint="Needed to reach this flight" />
        <Stat label="Flight duration" icon={Clock} value={duration != null && duration > 0 ? (duration / 60).toFixed(1) : null} unit="min" hint="Average over runs" />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <IterationChart simulations={simulations as IterationRow[]} />
        <QualityRadar metrics={m} />
      </div>

      <Card>
        <CardHeader title="Recommendations" icon={Lightbulb} description="From the Monitor agent" />
        <CardContent>
          {report.recommendations?.length ? (
            <ol className="space-y-2.5">
              {report.recommendations.map((rec, i) => (
                <li key={i} className="flex gap-3 text-sm leading-6">
                  <span className="mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-surface-raised text-2xs tabular-nums text-muted-foreground">
                    {i + 1}
                  </span>
                  <span className="text-foreground/90" dir="auto">{rec}</span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-sm text-muted-foreground">No recommendations in this report.</p>
          )}
        </CardContent>
      </Card>
    </PageBody>
  )
}
