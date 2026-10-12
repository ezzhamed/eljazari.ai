'use client'

import Link from 'next/link'
import dynamic from 'next/dynamic'
import { ArrowRight, Battery, Box, Braces, Cpu, Gauge, Layers, Package, Weight } from 'lucide-react'
import { ReviewCard } from '@/components/app/AgentPanel'
import { RestoringState } from '@/components/app/RestoringState'
import { Badge, type BadgeTone } from '@/components/ui/badge'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { EmptyState } from '@/components/ui/empty-state'
import { JsonTree } from '@/components/ui/json-tree'
import { KeyValueList, type KeyValueItem } from '@/components/ui/key-value'
import { PageBody, PageHeader } from '@/components/ui/page'
import { Stat } from '@/components/ui/stat'
import { METHOD_LABEL, fmt, readDesign } from '@/lib/design-view'
import { useDroneStore, type DesignStatus } from '@/lib/stores/drone-store'

const DroneViewer = dynamic(() => import('@/components/drone-viewer/DroneViewer'), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">Loading the model…</div>
  ),
})

const STATUS: Record<DesignStatus, { label: string; tone: BadgeTone }> = {
  pending: { label: 'Not started', tone: 'neutral' },
  generating: { label: 'Generating', tone: 'brand' },
  ready: { label: 'Ready for review', tone: 'warning' },
  approved: { label: 'Approved', tone: 'success' },
}

/** Rows the design actually has; an optional field it lacks is left out rather than shown as "—". */
function present(items: (KeyValueItem & { optional?: boolean })[]): KeyValueItem[] {
  return items.filter((item) => !item.optional || (item.value !== null && item.value !== undefined && item.value !== ''))
}

function Group({ title, items }: { title: string; items: KeyValueItem[] }) {
  return (
    <div>
      <p className="eyebrow mb-2">{title}</p>
      <KeyValueList items={items} />
    </div>
  )
}

export default function DesignPage() {
  const design = useDroneStore((s) => s.design)
  const designStatus = useDroneStore((s) => s.designStatus)
  const phase = useDroneStore((s) => s.currentPhase)
  const hitlPending = useDroneStore((s) => s.hitlPending)
  const isRestoring = useDroneStore((s) => s.isRestoring)
  const view = readDesign(design)
  const status = STATUS[designStatus] ?? STATUS.pending
  const estimate = view?.estimate
  const noEstimate = 'Not kept after a reload'

  return (
    <PageBody>
      <PageHeader
        title="Design"
        icon={Layers}
        description={
          view?.method
            ? `How it was made: ${METHOD_LABEL[view.method] ?? view.method}.`
            : 'The airframe and components the Design agent chose, and its performance estimate.'
        }
        status={<Badge tone={status.tone} dot={designStatus === 'generating' ? 'pulse' : designStatus !== 'pending'}>{status.label}</Badge>}
      />

      {hitlPending && phase === 'review_design' && <ReviewCard inline />}

      {!view && isRestoring ? (
        <Card>
          <RestoringState />
        </Card>
      ) : !view ? (
        <Card>
          <EmptyState
            icon={Layers}
            title="No design yet"
            description="The design appears here when the Design agent finishes. Start a run from the home page."
          />
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
            <Stat label="All-up weight" icon={Weight} value={estimate?.totalWeightG != null ? Math.round(estimate.totalWeightG).toLocaleString('en-US') : null} unit="g" hint={estimate ? 'Design agent estimate' : noEstimate} />
            <Stat label="Hover time" icon={Battery} value={estimate?.hoverMin != null ? estimate.hoverMin.toFixed(1) : null} unit="min" hint={estimate ? 'Design agent estimate' : noEstimate} />
            <Stat label="Thrust to weight" icon={Gauge} value={estimate?.thrustToWeight != null ? estimate.thrustToWeight.toFixed(2) : null} hint={estimate ? '2 or more to hover safely' : noEstimate} />
            <Stat label="Payload" icon={Package} value={view.payload.installedG != null ? Math.round(view.payload.installedG) : null} unit="g" hint={view.payload.maxG != null ? `Rated for ${Math.round(view.payload.maxG)} g` : undefined} />
          </div>

          <div className="grid gap-4 2xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <Card>
              <CardHeader
                title="Airframe and components"
                icon={Cpu}
                description="As the Design agent specified them. The catalogue parts chosen after approval are not shown here yet."
              />
              <CardContent className="grid gap-x-10 gap-y-5 md:grid-cols-2">
                <Group
                  title="Frame"
                  items={present([
                    { label: 'Topology', value: view.topology?.replace(/_/g, ' ') },
                    { label: 'Geometry', value: view.frame.type },
                    { label: 'Arm length', value: fmt(view.frame.armLengthMm, 'mm') },
                    { label: 'Wheelbase', value: fmt(view.frame.wheelbaseMm, 'mm'), optional: true },
                    { label: 'Centre body', value: view.frame.body, optional: true },
                    { label: 'Material', value: view.frame.material, optional: true },
                    { label: 'Mass', value: fmt(view.frame.massG, 'g'), optional: true },
                  ])}
                />
                <Group
                  title="Propulsion"
                  items={[
                    { label: 'Rotors', value: view.rotorCount },
                    { label: 'Motor', value: view.motor, mono: true },
                    { label: 'Propeller', value: view.propeller, mono: true },
                  ]}
                />
                <Group
                  title="Battery"
                  items={present([
                    { label: 'Chemistry', value: view.battery.chemistry },
                    { label: 'Cells', value: view.battery.cells != null ? `${view.battery.cells}S` : null },
                    { label: 'Capacity', value: fmt(view.battery.capacityMah, 'mAh') },
                    { label: 'Mass', value: fmt(view.battery.massG, 'g') },
                    { label: 'Nominal voltage', value: fmt(view.battery.voltage, 'V', 1), optional: true },
                  ])}
                />
                <div className="space-y-5">
                  <Group
                    title="Payload"
                    items={present([
                      { label: 'Installed', value: fmt(view.payload.installedG, 'g') },
                      { label: 'Maximum', value: fmt(view.payload.maxG, 'g') },
                      { label: 'Mount', value: view.payload.mount },
                      { label: 'Description', value: view.payload.description, optional: true },
                    ])}
                  />
                  <div>
                    <p className="eyebrow mb-2">Avionics</p>
                    {view.avionics.length ? (
                      <div className="flex flex-wrap gap-1.5">
                        {view.avionics.map((item) => <Badge key={item}>{item}</Badge>)}
                      </div>
                    ) : (
                      <p className="text-xs text-muted-foreground">None listed</p>
                    )}
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card className="min-h-[380px] overflow-hidden">
              <CardHeader
                title="Model preview"
                icon={Box}
                actions={
                  <Link href="/dashboard/viewer" className="focus-ring inline-flex items-center gap-1 rounded text-xs text-muted-foreground hover:text-foreground">
                    Open 3D model <ArrowRight className="h-3 w-3" />
                  </Link>
                }
              />
              <div className="relative min-h-0 flex-1">
                <div className="absolute inset-0">
                  <DroneViewer />
                </div>
              </div>
            </Card>
          </div>

          <Card>
            <CardHeader title="Design tree" icon={Braces} description="The Design agent's output as stored" />
            <CardContent className="max-h-[480px] overflow-auto font-mono text-xs leading-relaxed">
              <JsonTree data={design?.design_tree} />
            </CardContent>
          </Card>
        </>
      )}
    </PageBody>
  )
}
