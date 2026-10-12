'use client'

import { create } from 'zustand'
import { immer } from 'zustand/middleware/immer'
import { api, ApiError } from '@/lib/api'
import { rememberProjectId } from '@/components/app/ProjectHydrator'
import type {
  PipelinePhase,
  DroneDesign,
  DroneDesignTree,
  SimulationRun,
  PerformanceReport,
  SafetyReport,
  TelemetryPoint,
  SemanticObservation,
  HITLRequest,
} from '@eljazari/shared-types'

// ── Types ──────────────────────────────────────────────────────────────────────

export type DesignStatus = 'pending' | 'generating' | 'ready' | 'approved'
export type CodeStatus   = 'pending' | 'generating' | 'ready' | 'approved'
export type SimStatus    = 'idle' | 'running' | 'completed' | 'failed'

export interface AgentActivity {
  agent: string
  status: 'running' | 'done' | 'error'
  message: string
  timestamp: number
}

export interface DroneProjectStore {
  // ── Project ─────────────────────────────────────────────────
  projectId:    string | null
  projectName:  string
  userPrompt:   string
  currentPhase: PipelinePhase

  // ── Design ──────────────────────────────────────────────────
  design:       DroneDesign | null
  designStatus: DesignStatus

  // ── Code ────────────────────────────────────────────────────
  generatedCode: string
  safetyReport:  SafetyReport | null
  codeStatus:    CodeStatus
  codeIteration: number

  // ── Simulation ───────────────────────────────────────────────
  simulations:          SimulationRun[]
  simulationStatus:     SimStatus
  telemetryStream:      TelemetryPoint[]
  flightHome:           [number, number] | null
  /**
   * True while telemetry is arriving from a flight that is happening now.
   *
   * The viewer behaves differently in the two cases and cannot infer which it
   * is from the points alone: a live run must follow the newest sample and can
   * only draw the path it has so far, while a finished run is scrubbed. Set by
   * the socket handler when a point carries `live`, cleared when the
   * simulation completes.
   */
  isLive:               boolean
  semanticObservations: SemanticObservation[]
  currentIteration:     number

  // ── Monitor ──────────────────────────────────────────────────
  performanceReport: PerformanceReport | null

  // ── HITL ─────────────────────────────────────────────────────
  hitlRequest:   HITLRequest | null
  hitlPending:   boolean

  // ── Activity feed ────────────────────────────────────────────
  activityFeed: AgentActivity[]
  pipelineLogs: string[]
  /**
   * Lines ever appended to `pipelineLogs`, including those the ring buffer
   * has since dropped. Consumers that echo the log incrementally (the
   * simulation terminal) need this: once the buffer is full, length stops
   * changing and can no longer tell them anything new arrived.
   */
  pipelineLogTotal: number

  // ── Loading ──────────────────────────────────────────────────
  isLoading: boolean
  /**
   * True until a saved run is restored after a (re)load, or until the
   * ProjectHydrator finds there is none. Starts true so the first render does
   * not claim "no design yet" before the saved project id has been read.
   */
  isRestoring: boolean
  error:     string | null

  // ── Actions ──────────────────────────────────────────────────
  setProjectId:       (id: string) => void
  setPhase:           (phase: PipelinePhase) => void
  setDesign:          (design: DroneDesign) => void
  setDesignStatus:    (s: DesignStatus) => void
  setGeneratedCode:   (code: string) => void
  setSafetyReport:    (r: SafetyReport) => void
  setCodeStatus:      (s: CodeStatus) => void
  addSimulation:      (sim: SimulationRun) => void
  /** Attach the evaluator's verdict to the run with this iteration (or the latest run). */
  setSimulationEvaluation: (iteration: number, evaluation: { passed: boolean; score: number; feedback_for_code_agent: string }) => void
  setSimulationStatus:(s: SimStatus) => void
  addTelemetry:       (point: TelemetryPoint, live?: boolean) => void
  /** Append several points in one store update — see ws-store's telemetry buffer. */
  addTelemetryBatch:  (points: TelemetryPoint[], live?: boolean) => void
  setLive:            (live: boolean) => void
  addObservation:     (obs: SemanticObservation) => void
  setPerformanceReport:(r: PerformanceReport) => void
  setHitlRequest:     (r: HITLRequest | null) => void
  addLog:             (log: string) => void
  addActivity:        (a: AgentActivity) => void

  // Async pipeline actions
  startProject:        (prompt: string) => Promise<void>
  approveDesign:       () => Promise<void>
  approveCode:         () => Promise<void>
  requestModification: (feedback: string) => Promise<void>
  /**
   * Phase 8.6 — hydrate the store from the REST API for an existing project.
   *
   * Sub-pages (`/dashboard/design`, `/code`, `/simulate`, `/monitor`,
   * `/viewer`) only receive live data via Socket.io. When a user navigates
   * to one of them AFTER the relevant phase has already fired, the store is
   * empty and the page shows an idle scaffold — even though the project is
   * complete in Postgres. `hydrateFromBackend(projectId)` fills the gap by
   * GETing every persisted resource and seeding the store. Idempotent and
   * safe to call on every mount.
   */
  hydrateFromBackend: (projectId: string) => Promise<void>
  reset:               () => void
}

// ── Initial state ──────────────────────────────────────────────────────────────

/**
 * Translate the backend's design vocabulary into the store's.
 *
 * These are two different vocabularies and were being bridged with a bare
 * `as DesignStatus`, which happily produced values — 'generated', 'converted' —
 * that no UI branch tests for. The API returns the raw DB enum
 * (generated | validated | converted | approved | rejected); the store speaks
 * pending | generating | ready | approved. Unknown values return null so the
 * caller leaves the existing state alone instead of writing something invalid.
 */
function designStatusFromBackend(raw?: string): DesignStatus | null {
  switch ((raw ?? '').toLowerCase()) {
    case 'pending':   return 'pending'
    case 'generated':                    // produced, waiting on the human gate
    case 'validated': return 'ready'
    case 'converted':                    // approved and already turned into URDF
    case 'approved':  return 'approved'
    default:          return null
  }
}

/** Same bridge for code. The API derives this from safety_validated. */
function codeStatusFromBackend(raw?: string): CodeStatus | null {
  switch ((raw ?? '').toLowerCase()) {
    case 'pending':   return 'pending'
    case 'generated':
    case 'validated': return 'ready'
    default:          return null
  }
}

function phaseFromProjectStatus(status?: string): PipelinePhase | null {
  switch ((status ?? '').toLowerCase()) {
    case 'completed': return 'complete'
    case 'monitoring': return 'monitoring'
    case 'simulating':
    case 'testing': return 'simulation'
    case 'coding': return 'code_generation'
    case 'designing': return 'design'
    default: return null
  }
}

/** The flight-logs endpoint pages at 100; a two-minute flight is ~170 points. */
const TELEMETRY_PAGE = 100

const initialState = {
  projectId:    null,
  projectName:  '',
  userPrompt:   '',
  currentPhase: 'idle' as PipelinePhase,

  design:       null,
  designStatus: 'pending' as DesignStatus,

  generatedCode: '',
  safetyReport:  null,
  codeStatus:    'pending' as CodeStatus,
  codeIteration: 0,

  simulations:          [],
  simulationStatus:     'idle' as SimStatus,
  telemetryStream:      [],
  flightHome:           null as [number, number] | null,
  isLive:               false,
  semanticObservations: [],
  currentIteration:     0,

  performanceReport: null,

  hitlRequest: null,
  hitlPending: false,

  activityFeed: [],
  pipelineLogs: [],
  pipelineLogTotal: 0,

  isLoading: false,
  isRestoring: true,
  error:     null,
}

// ── Store ──────────────────────────────────────────────────────────────────────

// ── Normalize incoming telemetry so UI never hits null fields ──────────────────

function normalizeTelemetryPoint(raw: any): TelemetryPoint {
  if (!raw) raw = {}

  const timestamp_ms =
    raw.timestamp_ms ??
    (raw.timestamp_s != null ? Math.round(raw.timestamp_s * 1000) : 0)

  const north_m = raw.position?.north_m ?? raw.north_m ?? 0
  const east_m  = raw.position?.east_m  ?? raw.east_m  ?? 0
  const alt_m   = raw.position?.alt_m   ?? raw.alt_m   ?? 0
  const lat     = raw.position?.lat     ?? raw.lat     ?? Number.NaN
  const lon     = raw.position?.lon     ?? raw.lon     ?? Number.NaN

  const roll_deg  = raw.attitude?.roll_deg  ?? raw.roll_deg  ?? 0
  const pitch_deg = raw.attitude?.pitch_deg ?? raw.pitch_deg ?? 0
  const yaw_deg   = raw.attitude?.yaw_deg   ?? raw.yaw_deg   ?? 0

  const vel_north_ms = raw.velocity?.north_ms ?? raw.vel_north_ms ?? 0
  const vel_east_ms  = raw.velocity?.east_ms  ?? raw.vel_east_ms  ?? 0
  const vel_down_ms  = raw.velocity?.down_ms  ?? raw.vel_down_ms  ?? 0

  const battery_percent =
    raw.battery?.percent ??
    raw.battery_pct ??
    raw.battery_percent ??
    100

  const battery_voltage =
    raw.battery?.voltage_v ??
    raw.battery_voltage ??
    12.6

  const speed_ms =
    raw.speed_ms ??
    Math.sqrt(vel_north_ms ** 2 + vel_east_ms ** 2)

  return {
    timestamp_ms,
    position: {
      lat,
      lon,
      alt_m,
      north_m,
      east_m,
    },
    attitude: {
      roll_deg,
      pitch_deg,
      yaw_deg,
    },
    velocity: {
      north_ms: vel_north_ms,
      east_ms: vel_east_ms,
      down_ms: vel_down_ms,
    },
    battery: {
      voltage_v: battery_voltage,
      percent: battery_percent,
      remaining_s: raw.battery?.remaining_s ?? 0,
    },
    flight_mode: raw.flight_mode ?? '',
    armed: raw.armed ?? false,
    speed_ms,
    battery_pct: battery_percent,
  }
}

export const useDroneStore = create<DroneProjectStore>()(
  immer((set, get) => ({
    ...initialState,

    setProjectId:        (id)     => set((s) => {
      if (s.projectId !== id) { s.telemetryStream = []; s.flightHome = null }
      s.projectId = id
    }),
    setPhase:            (phase)  => set((s) => { s.currentPhase = phase }),
    setDesign:           (design) => set((s) => { s.design = design }),
    setDesignStatus:     (st)     => set((s) => { s.designStatus = st }),
    setGeneratedCode:    (code)   => set((s) => { s.generatedCode = code }),
    setSafetyReport:     (r)      => set((s) => { s.safetyReport = r }),
    setCodeStatus:       (st)     => set((s) => { s.codeStatus = st }),
    setSimulationStatus: (st)     => set((s) => { s.simulationStatus = st }),
    setPerformanceReport:(r)      => set((s) => { s.performanceReport = r }),
    setHitlRequest:      (r)      => set((s) => { s.hitlRequest = r; s.hitlPending = r !== null }),

    addSimulation: (sim) =>
      set((s) => {
        // The same run can arrive twice — once as the live `simulation_complete`
        // event and again when a page hydrates from the REST API — and showed
        // up as two "#1" bars on the monitor page. Replace, don't append.
        const existing = s.simulations.findIndex(
          (x) => x.id === sim.id || x.iteration === sim.iteration,
        )
        if (existing >= 0) s.simulations[existing] = { ...s.simulations[existing], ...sim }
        else s.simulations.push(sim)
      }),

    setSimulationEvaluation: (iteration, evaluation) =>
      set((s) => {
        let sim = s.simulations.find((x) => x.iteration === iteration)
        // The verdict can arrive before the run's own completion event, or for
        // an iteration the store never saw; attach to the newest run instead.
        if (!sim && s.simulations.length > 0) sim = s.simulations[s.simulations.length - 1]
        if (!sim) return
        sim.passed = evaluation.passed
        sim.evaluation = {
          mission_objectives_met: [],
          mission_objectives_failed: [],
          ...(sim.evaluation ?? {}),
          ...evaluation,
        }
      }),

    addTelemetry: (point, live = false) =>
      get().addTelemetryBatch([point], live),

    addTelemetryBatch: (points, live = false) => {
      if (points.length === 0) return
      set((s) => {
        for (const point of points) {
          const normalized = normalizeTelemetryPoint(point)
          const { lat, lon } = normalized.position ?? {}
          if (!s.flightHome && Number.isFinite(lat) && Number.isFinite(lon)) {
            s.flightHome = [lat!, lon!]
          }
          s.telemetryStream.push(normalized)
        }
        if (live) s.isLive = true
        // Keep only last 500 points to prevent memory bloat
        if (s.telemetryStream.length > 500) {
          s.telemetryStream.splice(0, s.telemetryStream.length - 500)
        }
      })
    },

    setLive: (live) => set((s) => { s.isLive = live }),

    addObservation: (obs) =>
      set((s) => { s.semanticObservations.push(obs) }),

    addLog: (log) =>
      set((s) => {
        s.pipelineLogs.push(log)
        s.pipelineLogTotal += 1
        if (s.pipelineLogs.length > 200) s.pipelineLogs.shift()
      }),

    addActivity: (a) =>
      set((s) => {
        // The backend announces a review gate twice (node_complete, then
        // hitl_interrupt); one line in the feed is enough.
        const last = s.activityFeed[0]
        if (last && last.agent === a.agent && last.message === a.message && last.status === a.status) {
          last.timestamp = a.timestamp
          return
        }
        s.activityFeed.unshift(a)
        if (s.activityFeed.length > 50) s.activityFeed.pop()
      }),

    // ── Async pipeline actions ──────────────────────────────────

    startProject: async (prompt: string) => {
      set((s) => {
        s.isLoading = true
        s.error = null
        s.userPrompt = prompt
        s.currentPhase = 'idle'
        s.design = null
        s.designStatus = 'pending'
        s.generatedCode = ''
        s.safetyReport = null
        s.codeStatus = 'pending'
        s.codeIteration = 0
        s.simulations = []
        s.simulationStatus = 'idle'
        s.telemetryStream = []
        s.flightHome = null
        s.semanticObservations = []
        s.currentIteration = 0
        s.performanceReport = null
        s.hitlRequest = null
        s.hitlPending = false
        s.activityFeed = []
        s.pipelineLogs = []
        s.pipelineLogTotal = 0
      })
      try {
        const project = await api.createProject(
          `Drone ${new Date().toLocaleTimeString()}`,
          prompt
        ) as { id: string; name: string }
        rememberProjectId(project.id)
        set((s) => {
          s.projectId   = project.id
          s.projectName = project.name
          s.currentPhase = 'design'
          s.designStatus = 'generating'
          s.isLoading   = false
          s.activityFeed.unshift({
            agent: 'Backend',
            status: 'running',
            message: 'Project created. Listening for live pipeline events.',
            timestamp: Date.now(),
          })
          s.pipelineLogs.push(`[api] project created ${project.id}`)
          s.pipelineLogTotal += 1
        })
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Failed to start project'
        set((s) => { s.error = msg; s.isLoading = false })
      }
    },

    approveDesign: async () => {
      const { projectId } = get()
      if (!projectId) return
      try {
        await api.approveDesign(projectId)
        set((s) => {
          s.designStatus = 'approved'
          s.hitlPending = false
          s.hitlRequest = null
          s.currentPhase = 'urdf_conversion'
        })
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Approval failed'
        set((s) => { s.error = msg })
      }
    },

    approveCode: async () => {
      const { projectId } = get()
      if (!projectId) return
      try {
        await api.approveCode(projectId)
        set((s) => {
          s.codeStatus = 'approved'
          s.hitlPending = false
          s.hitlRequest = null
          s.currentPhase = 'simulation'
          s.simulationStatus = 'running'
        })
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Approval failed'
        set((s) => { s.error = msg })
      }
    },

    requestModification: async (feedback: string) => {
      const { projectId, currentPhase, hitlRequest } = get()
      if (!projectId) return
      try {
        const reviewPhase =
          hitlRequest?.phase === 'design_review' || currentPhase === 'review_design'
            ? 'design'
            : 'code'

        if (reviewPhase === 'design') {
          await api.modifyDesign(projectId, feedback)
        } else {
          await api.modifyCode(projectId, feedback)
        }

        set((s) => {
          s.hitlPending = false
          s.hitlRequest = null
          if (reviewPhase === 'design') {
            s.currentPhase = 'design'
            s.designStatus = 'generating'
          } else {
            s.currentPhase = 'code_generation'
            s.codeStatus = 'generating'
          }
        })
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Request failed'
        set((s) => { s.error = msg })
      }
    },

    hydrateFromBackend: async (projectId: string) => {
      if (!projectId) return
      // Pages show "loading" instead of "nothing yet" until this finishes.
      set((s) => { s.isRestoring = true })
      try {
        // Always set projectId so downstream components know which run to show.
        get().setProjectId(projectId)

        // Which review gate the backend is paused at, if any. The graph stops at
        // human_review_design / human_review_code and waits, but that only ever
        // reached the UI as a live `phase_changed` event. Reload the page, open a
        // sub-page, or simply miss the socket while the design was generating, and
        // the approve button was gone for good while the run sat waiting forever.
        // Rebuild it from persisted state instead.
        let projectStatus = ''

        try {
          const project = await api.getProject(projectId)
          projectStatus = (project.status ?? '').toLowerCase()
          set((s) => {
            s.projectName = project.name || s.projectName
            s.userPrompt = project.prompt || s.userPrompt
            const restoredPhase = phaseFromProjectStatus(project.status)
            if (s.currentPhase === 'idle' && restoredPhase) {
              s.currentPhase = restoredPhase
            }
          })
        } catch (err: unknown) {
          // A 404 here does not mean "not generated yet" — the project row exists,
          // the request simply arrived as a different anonymous user than the one
          // that created it. Swallowing this was why the dashboard rendered an
          // empty page with no explanation while the run sat in the database.
          const status = err instanceof ApiError ? err.status : 0
          const message =
            status === 404
              ? 'This project belongs to a different session on this browser, so it cannot be opened. ' +
                'Start a new run, or restore the previous session id.'
              : err instanceof Error
                ? `Could not load this project: ${err.message}`
                : 'Could not load this project.'
          set((s) => { s.error = message })
          return
        }

        // ── Design ───────────────────────────────────────────────────────
        try {
          const design = await api.getDesign(projectId)
          if (design?.design_tree) {
            set((s) => {
              // Only overwrite when the live store hasn't already received
              // a richer payload from a Socket.io event.
              if (s.currentPhase === 'idle') {
                s.currentPhase = 'design'
              }
              if (!s.design || !s.design.design_tree) {
                const now = new Date().toISOString()
                s.design = {
                  id: `${projectId}:design`,
                  project_id: projectId,
                  name: 'Generated Design',
                  description: '',
                  // The REST projection is wider than DroneDesignTree's
                  // strict schema; treat the API response as the source of
                  // truth for rendering and let the strict type apply to
                  // the UI panes that need it.
                  design_tree: design.design_tree as unknown as DroneDesignTree,
                  created_at: now,
                  updated_at: now,
                }
              }
              const status = designStatusFromBackend(design.status)
              if (status && s.designStatus !== 'approved') {
                s.designStatus = status
              }
              // The gate is open when the design exists but the project has not
              // moved past designing — i.e. the graph is parked on the review node.
              if (status === 'ready' && projectStatus === 'designing') {
                s.currentPhase = 'review_design'
                s.hitlPending = true
                s.hitlRequest = {
                  id: `${projectId}:review_design`,
                  project_id: projectId,
                  phase: 'design_review',
                  data: {},
                  message: 'Design approval required',
                }
              }
            })
          }
        } catch { /* not generated yet — fine */ }

        // ── Code + safety ────────────────────────────────────────────────
        try {
          const code = await api.getCode(projectId)
          if (code?.code) {
            set((s) => {
              if (s.currentPhase === 'idle') {
                s.currentPhase = 'safety_check'
              }
              if (!s.generatedCode && code.code) {
                s.generatedCode = code.code
              }
              if (code.safety_report && !s.safetyReport) {
                s.safetyReport = code.safety_report as unknown as SafetyReport
              }
              const status = codeStatusFromBackend(code.status)
              if (status && s.codeStatus !== 'approved') {
                s.codeStatus = status
              }
              if (status === 'ready' && projectStatus === 'coding') {
                s.currentPhase = 'review_code'
                s.hitlPending = true
                s.hitlRequest = {
                  id: `${projectId}:review_code`,
                  project_id: projectId,
                  phase: 'code_review',
                  data: {},
                  message: 'Code approval required',
                }
              }
            })
          }
        } catch { /* not generated yet — fine */ }

        // ── Simulation results (best iteration only — keep the store small)
        try {
          const sims = await api.getSimulationResults(projectId)
          // One row per iteration. Older runs were persisted twice (once from
          // the graph node, once from the event stream); keep the row that
          // carries the evaluator's score so the chart shows the real number.
          const byIteration = new Map<number, NonNullable<typeof sims>['simulations'][number]>()
          for (const sim of sims?.simulations ?? []) {
            const prev = byIteration.get(sim.iteration)
            if (!prev || (prev.score == null && sim.score != null)) byIteration.set(sim.iteration, sim)
          }
          const items = [...byIteration.values()].sort((a, b) => a.iteration - b.iteration)
          if (items.length > 0) {
            set((s) => {
              if (s.currentPhase === 'idle') {
                s.currentPhase = 'monitoring'
              }
              if (s.simulations.length === 0) {
                s.simulations = items.map((sim) => ({
                  id: sim.simulation_id,
                  project_id: projectId,
                  mission_id: `${projectId}:mission`,
                  iteration: sim.iteration,
                  status: (sim.status as SimulationRun['status']) ?? 'complete',
                  passed: sim.passed ?? false,
                  telemetry: [],
                  semantic_observations: [],
                  evaluation: {
                    passed: sim.passed ?? false,
                    score: sim.score ?? 0,
                    mission_objectives_met: [],
                    mission_objectives_failed: [],
                    feedback_for_code_agent: '',
                  },
                })) as SimulationRun[]
              }
              const latest = items[items.length - 1]
              // The header widget read "idle 0/5" over a finished, passed run.
              if (s.simulationStatus === 'idle' || s.simulationStatus === 'completed') {
                s.simulationStatus = latest.status === 'failed' ? 'failed' : 'completed'
              }
              s.currentIteration = Math.max(s.currentIteration, latest.iteration)
              if (s.semanticObservations.length === 0 && latest.observations?.length) {
                s.semanticObservations = latest.observations as unknown as SemanticObservation[]
              }
            })
          }
        } catch { /* not run yet — fine */ }

        // ── Flight path ─────────────────────────────────────────────────
        // Telemetry used to arrive only as live `simulation_telemetry` socket
        // events, and nothing rebuilt it. Reload the page, come back to a
        // finished project, or just lose the socket for a moment while an agent
        // stalls the event loop, and the flight path was empty forever — the run
        // had flown, the points were in the database, and the viewer had nothing
        // to draw. Same shape of bug as the review gate: live-only state that was
        // never reconstructed.
        try {
          if (get().telemetryStream.length === 0) {
            const first = await api.getFlightLogs(projectId, TELEMETRY_PAGE)
            const total = first?.total ?? 0
            let records = first?.logs ?? []

            // The endpoint pages at 100; a two-minute flight is ~170 points, and
            // drawing the first 100 would silently truncate the path.
            for (let offset = records.length; offset < total; offset += TELEMETRY_PAGE) {
              const next = await api.getFlightLogs(projectId, TELEMETRY_PAGE, offset)
              if (!next?.logs?.length) break
              records = records.concat(next.logs)
            }

            const points = records
              .filter((r) => r.log_type === 'position' && r.data)
              .map((r) => {
                const d = r.data as Record<string, number | undefined>
                return normalizeTelemetryPoint({
                  timestamp_ms: r.timestamp_ms,
                  position: {
                    lat: d.lat ?? Number.NaN,
                    lon: d.lon ?? Number.NaN,
                    alt_m: d.alt_m ?? 0,
                    north_m: d.north_m ?? 0,
                    east_m: d.east_m ?? 0,
                  },
                } as Partial<TelemetryPoint>)
              })
              .sort((a, b) => a.timestamp_ms - b.timestamp_ms)

            if (points.length > 0) {
              set((s) => {
                s.telemetryStream = points
                const home = points.find(p => Number.isFinite(p.position?.lat) && Number.isFinite(p.position?.lon))?.position
                s.flightHome = home ? [home.lat, home.lon] : null
              })
            }
          }
        } catch { /* no flight yet — fine */ }

        // ── Performance report ──────────────────────────────────────────
        try {
          const report = await api.getPerformanceReport(projectId)
          // A blocked or rejected run has no score but still has a verdict to show.
          if (report && (report.overall_score != null || report.validation)) {
            set((s) => {
              if (s.currentPhase === 'idle' || s.currentPhase === 'monitoring') {
                s.currentPhase = 'complete'
              }
              if (s.performanceReport) return
              s.performanceReport = {
                id: `${projectId}:report`,
                project_id: projectId,
                simulation_ids: [],
                overall_score: report.overall_score ?? null,
                metrics: (report.metrics ?? {}) as unknown as PerformanceReport['metrics'],
                recommendations: report.recommendations ?? [],
                generated_at: new Date().toISOString(),
                validation: report.validation ?? null,
              }
            })
          }
        } catch { /* not generated yet — fine */ }
      } finally {
        set((s) => { s.isRestoring = false })
      }
    },

    reset: () => {
      rememberProjectId(null)
      set({ ...initialState, isRestoring: false })
    },
  }))
)

// ── Re-export hook alias for pipeline-store compatibility ─────────────────────
// Existing components that import usePipelineStore continue to work
export const usePipelineStore = useDroneStore
