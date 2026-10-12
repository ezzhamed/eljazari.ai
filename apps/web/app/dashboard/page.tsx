'use client'

/**
 * Overview — the run at a glance.
 *
 * The model (or its flight path) on the left; the stage list, the figures worth
 * checking, and the safety result on the right. Detail lives on each stage's
 * own page. Every figure is read from the run; a value that does not exist yet
 * reads "—" (this page used to show a made-up 98.1 score once a simulation had
 * passed and before the report arrived).
 */

import Link from 'next/link'
import dynamic from 'next/dynamic'
import { useEffect, useRef, useState } from 'react'
import {
  Activity, ArrowRight, Battery, Box, Gauge, LayoutDashboard, Route, ShieldCheck, Weight,
} from 'lucide-react'
import { PhaseBadge } from '@/components/app/PhaseBadge'
import { RestoringState } from '@/components/app/RestoringState'
import { StageIndicator } from '@/components/app/StageIndicator'
import { NAV_ITEMS } from '@/components/app/nav'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { EmptyState } from '@/components/ui/empty-state'
import { KeyValueList } from '@/components/ui/key-value'
import { PageBody, PageHeader } from '@/components/ui/page'
import { Segmented } from '@/components/ui/segmented'
import { Stat } from '@/components/ui/stat'
import { useStageStatuses } from '@/lib/hooks/useStageStatuses'
import { fmt, readDesign } from '@/lib/design-view'
import { STAGES } from '@/lib/pipeline'
import { useDroneStore } from '@/lib/stores/drone-store'

const DroneViewer = dynamic(() => import('@/components/drone-viewer/DroneViewer'), {
  ssr: false,
  loading: () => <CanvasLoading label="Loading the model" />,
})

const FlightPathViewer = dynamic(() => import('@/components/drone-viewer/FlightPathViewer'), {
  ssr: false,
  loading: () => <CanvasLoading label="Loading the flight path" />,
})

function CanvasLoading({ label }: { label: string }) {
  return (
    <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-brand-400 border-t-transparent" />
      {label}
    </div>
  )
}

type View = 'model' | 'flight'

const GATE = {
  passed: { label: 'Passed', tone: 'success' },
  rejected_by_checks: { label: 'Rejected', tone: 'danger' },
  blocked: { label: 'Blocked', tone: 'danger' },
  unknown: { label: 'Unknown', tone: 'muted' },
} as const

export default function OverviewPage() {
  const phase = useDroneStore((s) => s.currentPhase)
  const userPrompt = useDroneStore((s) => s.userPrompt)
  const design = useDroneStore((s) => s.design)
  const safetyReport = useDroneStore((s) => s.safetyReport)
  const telemetryCount = useDroneStore((s) => s.telemetryStream.length)
  const lastAlt = useDroneStore((s) => s.telemetryStream[s.telemetryStream.length - 1]?.position?.alt_m)
  const report = useDroneStore((s) => s.performanceReport)
  const latestRun = useDroneStore((s) => s.simulations[s.simulations.length - 1])
  const isRestoring = useDroneStore((s) => s.isRestoring)
  const statuses = useStageStatuses()

  // Follow the run (model while designing, path once it flies) until the
  // reader picks a view; after that the choice is theirs.
  const [view, setView] = useState<View>('model')
  const pinned = useRef(false)
  useEffect(() => {
    if (!pinned.current) setView(['simulation', 'monitoring', 'complete'].includes(phase) ? 'flight' : 'model')
  }, [phase])

  if (phase === 'idle' && isRestoring) {
    return (
      <PageBody className="justify-center">
        <Card className="mx-auto w-full max-w-lg">
          <RestoringState />
        </Card>
      </PageBody>
    )
  }

  if (phase === 'idle') {
    return (
      <PageBody className="justify-center">
        <Card className="mx-auto w-full max-w-lg">
          <EmptyState
            icon={LayoutDashboard}
            title="No run on the bench"
            description="Describe a drone and its mission. The agents design it, build its model, write and check its mission code, and fly it in PX4 SITL."
            action={
              <Button asChild size="sm" className="gap-1.5">
                <Link href="/">Start from a prompt <ArrowRight className="h-3.5 w-3.5" /></Link>
              </Button>
            }
          />
        </Card>
      </PageBody>
    )
  }

  const aircraft = readDesign(design)
  const estimate = aircraft?.estimate
  // The estimate arrives with the live design event and is not stored with it.
  const estimateHint = aircraft && !estimate ? 'Not kept after a reload' : 'Design agent estimate'
  const gate = report?.validation?.status ? GATE[report.validation.status] : null
  // Only the gate's evaluation is a verdict; `simulation_complete` always reports passed=false.
  const verdict = latestRun?.evaluation?.passed
  const pageFor = (stageId: string) => NAV_ITEMS.find((item) => item.stages?.includes(stageId as never))

  return (
    <PageBody>
      <PageHeader
        title="Overview"
        icon={LayoutDashboard}
        status={<PhaseBadge phase={phase} />}
        description={
          userPrompt ? (
            <span className="line-clamp-2" dir="auto" title={userPrompt}>{userPrompt}</span>
          ) : undefined
        }
      />

      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <Stat label="All-up weight" icon={Weight} value={estimate?.totalWeightG != null ? Math.round(estimate.totalWeightG).toLocaleString('en-US') : null} unit="g" hint={estimateHint} />
        <Stat label="Hover time" icon={Battery} value={estimate?.hoverMin != null ? estimate.hoverMin.toFixed(1) : null} unit="min" hint={estimateHint} />
        <Stat
          label="Acceptance gate"
          icon={ShieldCheck}
          value={gate ? gate.label : verdict === undefined ? null : verdict ? 'Passed' : 'Rejected'}
          tone={gate ? gate.tone : verdict === undefined ? 'muted' : verdict ? 'success' : 'danger'}
          hint={latestRun ? `Simulation run ${latestRun.iteration}` : 'After the simulation'}
        />
        <Stat
          label="Score"
          icon={Gauge}
          value={report?.overall_score != null ? report.overall_score.toFixed(1) : null}
          unit="/ 100"
          tone="brand"
          hint={report ? 'Monitor agent' : 'After the flight analysis'}
        />
      </div>

      <div className="grid min-h-0 gap-4 2xl:grid-cols-[minmax(0,1fr)_340px]">
        <Card className="min-h-[480px] overflow-hidden">
          <CardHeader
            title={view === 'model' ? 'Aircraft model' : 'Flight path'}
            icon={view === 'model' ? Box : Route}
            description={view === 'model' ? 'Built from the design; drag to rotate' : `${telemetryCount} telemetry samples`}
            actions={
              <Segmented<View>
                label="Canvas view"
                value={view}
                onChange={(next) => {
                  pinned.current = true
                  setView(next)
                }}
                options={[
                  { value: 'model', label: 'Model' },
                  { value: 'flight', label: 'Path' },
                ]}
              />
            }
          />
          <div className="relative min-h-0 flex-1">
            <div className="absolute inset-0">{view === 'model' ? <DroneViewer /> : <FlightPathViewer />}</div>
          </div>
        </Card>

        <div className="grid min-w-0 content-start items-start gap-4 lg:grid-cols-3 2xl:flex 2xl:flex-col">
          <Card>
            <CardHeader title="Pipeline" icon={Activity} />
            <ol className="divide-y divide-border/60">
              {STAGES.map((stage) => {
                const target = pageFor(stage.id)
                return (
                  <li key={stage.id}>
                    <Link
                      href={target?.href ?? '/dashboard'}
                      className="focus-ring group flex items-center gap-3 px-4 py-2.5 transition-colors hover:bg-white/[0.03]"
                    >
                      <StageIndicator status={statuses[stage.id]} />
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm text-foreground">{stage.label}</span>
                        <span className="block truncate text-2xs text-muted-foreground">{stage.description}</span>
                      </span>
                      <ArrowRight className="h-3.5 w-3.5 text-muted-foreground/0 transition-colors group-hover:text-muted-foreground" />
                    </Link>
                  </li>
                )
              })}
            </ol>
          </Card>

          <Card>
            <CardHeader
              title="Safety"
              icon={ShieldCheck}
              actions={
                safetyReport ? (
                  <Badge tone={safetyReport.passed ? 'success' : 'danger'}>
                    {safetyReport.passed ? 'Passed' : 'Failed'}
                  </Badge>
                ) : (
                  <Badge>Waiting</Badge>
                )
              }
            />
            <CardContent>
              <KeyValueList
                items={[
                  { label: 'Risk level', value: safetyReport?.risk_level },
                  { label: 'Issues found', value: safetyReport ? safetyReport.issues.length : null },
                  { label: 'Protocols injected', value: safetyReport ? safetyReport.injected_protocols.length : null },
                ]}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader title="Aircraft" icon={Box} />
            <CardContent>
              <KeyValueList
                items={[
                  { label: 'Topology', value: aircraft?.topology?.replace(/_/g, ' ') },
                  { label: 'Rotors', value: aircraft?.rotorCount },
                  { label: 'Motor', value: aircraft?.motor, mono: true },
                  { label: 'Battery', value: aircraft?.battery.cells != null ? `${aircraft.battery.cells}S · ${fmt(aircraft.battery.capacityMah, 'mAh') ?? '—'}` : null },
                  { label: 'Last altitude', value: lastAlt != null ? `${lastAlt.toFixed(1)} m` : null },
                ]}
              />
            </CardContent>
          </Card>
        </div>
      </div>
    </PageBody>
  )
}
