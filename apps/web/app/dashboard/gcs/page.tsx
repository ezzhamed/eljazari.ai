'use client'

/**
 * Ground control (/dashboard/gcs)
 *
 * Map and mission controls on the left, instruments and PID tuning on the
 * right, the event log underneath. Live MAVLink telemetry is preferred when the
 * bridge streams it; otherwise the panels show the simulation's telemetry.
 */

import dynamic from 'next/dynamic'
import { useEffect, useState } from 'react'
import { AlertTriangle, Gamepad2 } from 'lucide-react'
import AttitudeIndicator from '@/components/gcs/AttitudeIndicator'
import EventLog from '@/components/gcs/EventLog'
import MissionControls from '@/components/gcs/MissionControls'
import ParameterTuning from '@/components/gcs/ParameterTuning'
import TelemetryDashboard from '@/components/gcs/TelemetryDashboard'
import { Badge } from '@/components/ui/badge'
import { PageBody, PageHeader } from '@/components/ui/page'
import { useWSStore } from '@/lib/stores/ws-store'

const LeafletMap = dynamic(() => import('@/components/gcs/LeafletMap'), {
  ssr: false,
  loading: () => <div className="h-[460px] animate-pulse rounded-xl border border-border bg-card" />,
})

/** A MAVLink sample within this window counts as "receiving". */
const FRESH_MS = 3000

/**
 * Start the backend's MAVLink bridge while this page is open, and report
 * whether telemetry is actually arriving (the old banner said "MAVLink Bridge
 * Active" whether or not anything was connected).
 */
function useMavlinkStatus(): 'no_socket' | 'waiting' | 'receiving' | 'synthetic' {
  const { socket, connected } = useWSStore()
  const [lastSample, setLastSample] = useState({ at: 0, synthetic: false })
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!socket || !connected) return
    let stopped = false
    let retry: number | undefined
    // The server handles events concurrently, so a start sent right after
    // connecting can arrive before the project join has finished and be
    // refused. Retry a few times on a refusal instead of silently giving up.
    const start = (attempt: number) => {
      socket.emit('gcs:start', {}, (reply?: { ok?: boolean }) => {
        if (stopped || reply?.ok !== false || attempt >= 5) return
        retry = window.setTimeout(() => start(attempt + 1), 1000)
      })
    }
    start(1)
    // A server without MAVSDK streams a made-up orbit and marks it `synthetic`.
    const onSample = (data: { synthetic?: boolean }) =>
      setLastSample({ at: Date.now(), synthetic: data?.synthetic === true })
    socket.on('mavlink_telemetry', onSample)
    return () => {
      stopped = true
      window.clearTimeout(retry)
      socket.off('mavlink_telemetry', onSample)
      socket.emit('gcs:stop', {})
    }
  }, [socket, connected])

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  if (!socket || !connected) return 'no_socket'
  if (now - lastSample.at >= FRESH_MS) return 'waiting'
  return lastSample.synthetic ? 'synthetic' : 'receiving'
}

const MAVLINK = {
  no_socket: { label: 'Backend not connected', tone: 'danger' },
  waiting: { label: 'MAVLink: no telemetry', tone: 'warning' },
  receiving: { label: 'MAVLink: receiving', tone: 'success' },
  synthetic: { label: 'Synthetic telemetry', tone: 'warning' },
} as const

export default function GCSPage() {
  const mavlinkStatus = useMavlinkStatus()
  const mavlink = MAVLINK[mavlinkStatus]

  return (
    <PageBody>
      <PageHeader
        title="Ground control"
        icon={Gamepad2}
        description="Map, instruments and commands for a vehicle on the MAVLink bridge. Without live MAVLink, the panels show the simulation's telemetry."
        status={<Badge tone={mavlink.tone} dot={mavlink.tone === 'success' ? 'pulse' : true}>{mavlink.label}</Badge>}
      />

      {mavlinkStatus === 'synthetic' && (
        <div role="status" className="flex items-start gap-2.5 rounded-xl border border-warning/30 bg-warning/[0.06] px-4 py-3 text-sm text-warning">
          <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0" aria-hidden />
          <span>
            The server has no MAVSDK, so the bridge is sending a made-up orbit for interface testing. The map,
            instruments and command replies below are not from a vehicle.
          </span>
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-5">
        <div className="flex min-w-0 flex-col gap-4 xl:col-span-3">
          <LeafletMap className="min-h-[460px]" />
          <div className="grid gap-4 lg:grid-cols-2">
            <MissionControls />
            <AttitudeIndicator className="h-full" />
          </div>
        </div>

        <div className="flex min-w-0 flex-col gap-4 xl:col-span-2">
          <TelemetryDashboard />
          <ParameterTuning className="flex-1" />
        </div>

        <div className="xl:col-span-5">
          <EventLog className="h-72" />
        </div>
      </div>
    </PageBody>
  )
}
