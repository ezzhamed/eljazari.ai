/**
 * eljazari.ai - Socket.io client store (Zustand)
 *
 * Connects to the python-socketio server at /socket.io/ and
 * pipes every server -> client event into the DroneStore.
 */
import { useEffect } from 'react'
import { create } from 'zustand'
import { io, Socket } from 'socket.io-client'
import { useDroneStore } from './drone-store'
import { api, getToken } from '@/lib/api'
import { WS_BASE_URL } from '@/lib/config'

interface PhaseChangedPayload { phase: string; project_id: string; feedback?: string; restarted?: boolean }
interface DesignReadyPayload { project_id: string; data: Record<string, unknown> }
interface CodeReadyPayload { project_id: string; data: { code: string; iteration?: number } }
interface SafetyReportPayload { project_id: string; data: Record<string, unknown> }
interface TelemetryPayload { project_id: string; iteration: number; point: Record<string, unknown> }
interface ObservationPayload { project_id: string; timestamp_s: number; description: string; event_type: string; severity: string }
interface SimCompletePayload { project_id: string; sim_id: string; iteration: number; passed: boolean; duration_s: number }
interface SimStatusPayload { status: string; project_id: string; iteration?: number }
interface SimEvaluatedPayload { project_id: string; iteration: number; passed: boolean; score: number | null; status: string; feedback?: string }
interface MonitorUpdatePayload { project_id: string; data: Record<string, unknown> }
interface AgentLogPayload { project_id: string; agent: string; status: string; message: string }
interface ErrorPayload { project_id: string; phase?: string; message: string }

interface WSStore {
  socket: Socket | null
  connected: boolean
  projectId: string | null
  connect: (projectId: string) => void
  disconnect: () => void
}

const WS_URL = WS_BASE_URL
const autoApprovalSent = new Set<string>()
const autoApprovalInFlight = new Set<string>()

/** Prefix used to recognise our own connection errors so `connect` can clear them. */
const WS_ERROR_PREFIX = 'WebSocket connection failed'

/**
 * How many consecutive failed handshakes before telling the user.
 *
 * socket.io retries every second, and a stall in the backend's event loop while
 * an agent runs can push one handshake past its timeout. Reporting the first
 * failure turned a blip into a permanent banner.
 */
const CONNECT_FAILURES_BEFORE_ALERT = 3

let connectFailures = 0

/**
 * Telemetry arrives two ways: one point every 100 ms while the aircraft is
 * flying, and a burst of the whole recording (hundreds of points, back to
 * back in one tick) when a run finishes or a page loads mid-flight.
 *
 * Pushing each point through the store individually turned that burst into
 * hundreds of synchronous re-renders of every telemetry subscriber, and the
 * flight-path hook's effects (which set state on every length change) tripped
 * React's nested-update limit — "Maximum update depth exceeded", thrown from
 * inside the socket handler. Buffering the points and flushing once per frame
 * makes a burst cost one render, and the live stream at most 60 a second.
 */
const telemetryBuffer: { point: Record<string, unknown>; live: boolean }[] = []
let telemetryFlushScheduled = false

function flushTelemetryNow() {
  telemetryFlushScheduled = false
  if (telemetryBuffer.length === 0) return
  const batch = telemetryBuffer.splice(0, telemetryBuffer.length)
  const live = batch.some((b) => b.live)
  useDroneStore.getState().addTelemetryBatch(batch.map((b) => b.point) as any[], live)
}

function scheduleTelemetryFlush() {
  if (telemetryFlushScheduled) return
  telemetryFlushScheduled = true
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(flushTelemetryNow)
  else setTimeout(flushTelemetryNow, 16)
}

function queueTelemetry(point: Record<string, unknown>, live: boolean) {
  telemetryBuffer.push({ point, live })
  scheduleTelemetryFlush()
}

function logActivity(agent: string, status: 'running' | 'done' | 'error', message: string) {
  const store = useDroneStore.getState()
  store.addActivity({ agent, status, message, timestamp: Date.now() })
  store.addLog(`[${agent}] ${message}`)
}

function setReviewState(phase: 'review_design' | 'review_code', payload: Record<string, unknown>) {
  useDroneStore.setState((state: any) => {
    state.currentPhase = phase
    state.hitlPending = true
    state.hitlRequest = {
      id: `${payload.project_id ?? 'project'}:${phase}`,
      project_id: payload.project_id,
      phase: phase === 'review_design' ? 'design_review' : 'code_review',
      data: payload,
      message: phase === 'review_design' ? 'Design approval required' : 'Code approval required',
    }
    if (phase === 'review_design') {
      state.designStatus = 'ready'
    } else {
      state.codeStatus = 'ready'
    }
  })
}

function autoApproveReview(phase: 'review_design' | 'review_code', projectId: string) {
  const key = `${projectId}:${phase}`
  if (autoApprovalSent.has(key) || autoApprovalInFlight.has(key)) return
  autoApprovalInFlight.add(key)

  window.setTimeout(async () => {
    // Re-check after delay in case another event already handled it
    if (autoApprovalSent.has(key)) {
      autoApprovalInFlight.delete(key)
      return
    }
    autoApprovalSent.add(key)
    try {
      if (phase === 'review_design') {
        logActivity('HITL', 'running', 'Auto-approving generated design and continuing to URDF.')
        await api.approveDesign(projectId)
      } else {
        logActivity('HITL', 'running', 'Auto-approving safety-checked code and starting simulation.')
        await api.approveCode(projectId)
      }
    } catch (err) {
      // 409 = pipeline already advanced past this phase — not a real error
      const status = (err as any)?.response?.status ?? (err as any)?.status
      if (status === 409 || status === 400) return
      const message = err instanceof Error ? err.message : 'Auto approval failed'
      useDroneStore.setState((state: any) => {
        state.error = message
      })
      logActivity('HITL', 'error', message)
    } finally {
      autoApprovalInFlight.delete(key)
    }
  }, 700)
}

export const useWSStore = create<WSStore>((set, get) => ({
  socket: null,
  connected: false,
  projectId: null,

  connect: (projectId: string) => {
    const { socket: existing, projectId: currentProjectId } = get()

    if (existing) {
      if (currentProjectId && currentProjectId !== projectId) {
        existing.emit('leave_project', { project_id: currentProjectId })
      }
      existing.disconnect()
    }

    const token = getToken()
    const socket = io(WS_URL, {
      path: '/socket.io',
      transports: ['websocket', 'polling'],
      auth: token ? { token } : {},
      reconnectionAttempts: 10,
      reconnectionDelay: 1_000,
    })

    socket.on('connect', () => {
      set({ connected: true })
      connectFailures = 0
      socket.emit('join_project', { project_id: projectId })
      // Clear a stale connection error. socket.io reconnects on its own, and an
      // attempt that timed out before succeeding used to leave "WebSocket
      // connection failed: timeout" on screen for the rest of the run, over a
      // connection that was by then working perfectly.
      useDroneStore.setState((state: any) => {
        if (typeof state.error === 'string' && state.error.startsWith(WS_ERROR_PREFIX)) {
          state.error = null
        }
      })
      logActivity('WebSocket', 'done', 'Connected to backend live pipeline events.')
    })

    socket.on('disconnect', () => {
      set({ connected: false })
      logActivity('WebSocket', 'error', 'Disconnected from backend live events.')
    })

    socket.on('connect_error', (err) => {
      connectFailures += 1
      console.warn(`[WS] connect error (attempt ${connectFailures}):`, err.message)
      logActivity('WebSocket', 'running', `Reconnecting (attempt ${connectFailures})…`)

      // A single timeout is not worth alarming anyone with. The backend's event
      // loop stalls in bursts while an agent runs, so the first handshake can
      // exceed socket.io's timeout and the next one succeeds — measured
      // locally at 314ms, 328ms, 3744ms, 3217ms, 2761ms back to back. Surface
      // it only once the retries have genuinely failed to get through.
      if (connectFailures < CONNECT_FAILURES_BEFORE_ALERT) return
      useDroneStore.setState((state: any) => {
        state.error =
          `${WS_ERROR_PREFIX}: ${err.message}. Live updates are paused; ` +
          'the pipeline is still running and the page will catch up on reconnect.'
      })
    })

    const store = () => useDroneStore.getState()

    socket.on('phase_changed', (d: PhaseChangedPayload) => {
      if (d.phase === 'review_design' || d.phase === 'review_code') {
        setReviewState(d.phase, d as unknown as Record<string, unknown>)
        // Let the user review and approve manually
        logActivity('HITL', 'running', d.phase === 'review_design'
          ? 'Design ready — waiting for your approval.'
          : 'Code ready — waiting for your approval.')
      } else {
        store().setPhase(d.phase as any)
        useDroneStore.setState((state: any) => {
          if (d.phase === 'design') state.designStatus = 'generating'
          if (d.phase === 'code_generation') state.codeStatus = 'generating'
          if (d.phase === 'simulation') state.simulationStatus = 'running'
          if (d.phase === 'complete') {
            state.hitlPending = false
            state.hitlRequest = null
          }
        })
      }
      store().addLog(`[phase] -> ${d.phase}${d.restarted ? ' (restarted)' : ''}`)
    })

    socket.on('design_ready', (d: DesignReadyPayload) => {
      store().setDesign(d.data as any)
      store().setDesignStatus('ready')
      store().addLog('[design] Design ready')
      store().addActivity({
        agent: 'Design Agent',
        status: 'done',
        message: 'Design tree received from backend.',
        timestamp: Date.now(),
      })
    })

    socket.on('code_ready', (d: CodeReadyPayload) => {
      store().setGeneratedCode(d.data.code)
      if (d.data.iteration != null) {
        useDroneStore.setState((state: any) => {
          state.codeIteration = d.data.iteration
        })
      }
      store().setCodeStatus('ready')
      store().addLog(`[code] Code ready (iter ${d.data.iteration ?? 1})`)
      store().addActivity({
        agent: 'Code Agent',
        status: 'done',
        message: 'Mission code received from backend.',
        timestamp: Date.now(),
      })
    })

    socket.on('safety_report', (d: SafetyReportPayload) => {
      store().setSafetyReport(d.data as any)
      store().addLog('[safety] Safety report received')
      store().addActivity({
        agent: 'SafetyLayer',
        status: 'done',
        message: 'Safety report received from backend.',
        timestamp: Date.now(),
      })
    })

    socket.on('simulation_telemetry', (d: TelemetryPayload) => {
      // `live` marks a sample the backend streamed while the aircraft was
      // flying, as opposed to the burst it replays for a finished run. The
      // viewer needs the distinction: it can only follow, and only draw the
      // path so far, when the flight is still happening.
      queueTelemetry(d.point, (d as { live?: boolean }).live === true)
    })

    socket.on('semantic_observation', (d: ObservationPayload) => {
      store().addObservation(d as any)
      if (d.severity === 'critical' || d.severity === 'warning') {
        store().addLog(`[obs] ${d.description}`)
      }
    })

    socket.on('simulation_complete', (d: SimCompletePayload) => {
      // Points still waiting for the next frame belong to this flight; land
      // them first, or a buffered live sample would flip isLive back on.
      flushTelemetryNow()
      // The flight is over: hand the viewer back to the reader for scrubbing.
      store().setLive(false)
      store().setSimulationStatus('completed')
      store().addSimulation({
        id: d.sim_id,
        project_id: d.project_id,
        mission_id: '',
        iteration: d.iteration,
        status: 'complete',
        passed: d.passed,
        duration_s: d.duration_s,
        telemetry: [],
        semantic_observations: [],
      } as any)
      store().addLog(`[sim] Complete iter=${d.iteration} passed=${d.passed}`)
    })

    socket.on('sim_evaluated', (d: SimEvaluatedPayload) => {
      store().setSimulationEvaluation(d.iteration, {
        passed: d.passed,
        score: d.score ?? 0,
        feedback_for_code_agent: d.feedback ?? '',
      })
      if (d.score != null) {
        store().addLog(`[eval] iter=${d.iteration} score=${d.score.toFixed(0)}/100 passed=${d.passed}`)
      }
    })

    socket.on('sim_status', (d: SimStatusPayload) => {
      store().setSimulationStatus(d.status as any)
      if (d.iteration != null) {
        useDroneStore.setState((state: any) => {
          if (state.currentIteration !== d.iteration) {
            state.telemetryStream = []
            state.flightHome = null
          }
          state.currentIteration = d.iteration
        })
      }
    })

    socket.on('monitor_update', (d: MonitorUpdatePayload) => {
      store().setPerformanceReport(d.data as any)
      store().addActivity({
        agent: 'MonitorAgent',
        status: 'done',
        message: 'Performance report received from backend.',
        timestamp: Date.now(),
      })
    })

    socket.on('agent_log', (d: AgentLogPayload) => {
      store().addActivity({
        agent: d.agent,
        status: d.status as any,
        message: d.message,
        timestamp: Date.now(),
      })
      store().addLog(`[${d.agent}] ${d.message}`)
    })

    socket.on('error', (d: ErrorPayload) => {
      useDroneStore.setState((state: any) => {
        state.error = d.message
      })
      store().addLog(`[error] ${d.phase ? `(${d.phase}) ` : ''}${d.message}`)
    })

    set({ socket, projectId })
  },

  disconnect: () => {
    const { socket, projectId } = get()
    if (socket) {
      if (projectId) socket.emit('leave_project', { project_id: projectId })
      socket.disconnect()
    }
    autoApprovalSent.clear()
    autoApprovalInFlight.clear()
    telemetryBuffer.length = 0
    set({ socket: null, connected: false, projectId: null })
  },
}))

export function useConnectWS(projectId: string | null): void {
  const connect = useWSStore((state) => state.connect)
  const disconnect = useWSStore((state) => state.disconnect)

  useEffect(() => {
    const current = useWSStore.getState().projectId
    if (!projectId) {
      if (current) disconnect()
      return
    }
    if (current !== projectId) connect(projectId)
  }, [connect, disconnect, projectId])
}
