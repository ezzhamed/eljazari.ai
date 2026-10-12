'use client'

import dynamic from 'next/dynamic'
import { useState } from 'react'
import { Box, Mouse, Route, SlidersHorizontal } from 'lucide-react'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { KeyValueList } from '@/components/ui/key-value'
import { PageBody, PageHeader } from '@/components/ui/page'
import { Segmented } from '@/components/ui/segmented'
import { fmt, readDesign } from '@/lib/design-view'
import { useDroneStore } from '@/lib/stores/drone-store'

function CanvasLoading() {
  return (
    <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-brand-400 border-t-transparent" />
      Loading the 3D view…
    </div>
  )
}

const DroneViewer = dynamic(() => import('@/components/drone-viewer/DroneViewer'), { ssr: false, loading: CanvasLoading })
const FlightPathViewer = dynamic(() => import('@/components/drone-viewer/FlightPathViewer'), { ssr: false, loading: CanvasLoading })
const DesignParameterPanel = dynamic(() => import('@/components/drone-viewer/DesignParameterPanel'), { ssr: false })

type View = 'model' | 'flight'

const CONTROLS = [
  { key: 'Left drag', action: 'Rotate' },
  { key: 'Right drag', action: 'Pan' },
  { key: 'Scroll', action: 'Zoom' },
]

export default function ViewerPage() {
  const [view, setView] = useState<View>('model')
  const aircraft = readDesign(useDroneStore((s) => s.design))

  return (
    <PageBody fill>
      <PageHeader
        title={view === 'flight' ? 'Flight path' : '3D model'}
        icon={Box}
        description={view === 'flight' ? 'Explore the recorded flight, switch cameras and inspect telemetry at any point.' : 'Inspect the aircraft generated from your design. Geometry controls change the preview only.'}
        actions={
          <Segmented<View>
            label="3D view"
            value={view}
            onChange={setView}
            options={[
              { value: 'model', label: <><Box className="h-3.5 w-3.5" /> Model</> },
              { value: 'flight', label: <><Route className="h-3.5 w-3.5" /> Flight path</> },
            ]}
          />
        }
      />

      <div className="grid min-h-0 flex-1 gap-4 2xl:grid-cols-[minmax(0,1fr)_320px]">
        <Card className="min-h-[480px] overflow-hidden">
          <div className="relative min-h-0 flex-1">
            <div className="absolute inset-0">{view === 'model' ? <DroneViewer /> : <FlightPathViewer />}</div>
          </div>
        </Card>

        <div className="grid min-h-0 content-start items-start gap-4 lg:grid-cols-3 2xl:flex 2xl:flex-col 2xl:overflow-y-auto">
          <Card>
            <CardHeader title="Aircraft" icon={Box} description="From the Design agent's tree" />
            <CardContent>
              <KeyValueList
                items={[
                  { label: 'Topology', value: aircraft?.topology?.replace(/_/g, ' ') },
                  { label: 'Arm length', value: fmt(aircraft?.frame.armLengthMm, 'mm') },
                  { label: 'Rotors', value: aircraft?.rotorCount },
                  { label: 'Propeller', value: aircraft?.propeller, mono: true },
                  { label: 'Battery', value: aircraft?.battery.cells != null ? `${aircraft.battery.cells}S · ${fmt(aircraft.battery.capacityMah, 'mAh') ?? '—'}` : null },
                ]}
              />
            </CardContent>
          </Card>

          {view === 'model' && <Card>
            <CardHeader title="Geometry" icon={SlidersHorizontal} description="Viewer only" />
            <CardContent>
              <DesignParameterPanel />
            </CardContent>
          </Card>}

          <Card>
            <CardHeader title="Controls" icon={Mouse} />
            <CardContent>
              <dl className="space-y-2">
                {CONTROLS.map((c) => (
                  <div key={c.key} className="flex items-center justify-between text-xs">
                    <dt>
                      <kbd className="rounded-md border border-border bg-background px-1.5 py-0.5 font-mono text-2xs text-muted-foreground">{c.key}</kbd>
                    </dt>
                    <dd className="text-muted-foreground">{c.action}</dd>
                  </div>
                ))}
              </dl>
            </CardContent>
          </Card>
        </div>
      </div>
    </PageBody>
  )
}
