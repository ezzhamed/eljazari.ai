/**
 * eljazari.ai — Typed API client
 *
 * All endpoints map 1-to-1 to the FastAPI backend.
 * Auth token is read from localStorage key "dp_token".
 */

import { API_BASE_URL as BASE } from './config'
import { readSessionValue, writeSessionValue } from './session-storage'
import type { FlightValidation } from '@eljazari/shared-types'

// ── Types ─────────────────────────────────────────────────────────────────────

export interface ProjectResponse {
  id:         string
  name:       string
  prompt:     string
  status:     string
  user_id:    string
  created_at: string | null
}

export interface ProjectListResponse {
  items: ProjectResponse[]
  total: number
}

export interface DesignResponse {
  project_id:  string
  design_tree: Record<string, unknown> | null
  status:      string
  urdf_path:   string | null
}

export interface CodeResponse {
  project_id:    string
  code:          string | null
  language:      string
  iteration:     number
  safety_report: Record<string, unknown> | null
  status:        string
}

export interface SimulationObservation {
  timestamp_s: number
  description: string
  event_type:  string
  severity:    string
}

export interface SimulationResult {
  simulation_id: string
  project_id:    string
  iteration:     number
  status:        string
  passed:        boolean | null
  score:         number | null
  duration_s:    number | null
  observations?: SimulationObservation[]
}

export interface SimulationResultsResponse {
  project_id:   string
  simulations:  SimulationResult[]
  best_score:   number | null
  final_passed: boolean | null
}

export interface PerformanceReportResponse {
  project_id:      string
  overall_score:   number | null
  metrics:         Record<string, unknown>
  recommendations: string[]
  validation?:     FlightValidation | null
}

export interface FlightLogsResponse {
  project_id: string
  logs:       Record<string, unknown>[]
  total:      number
}

export interface TokenResponse {
  access_token: string
  token_type:   string
  user_id:      string
}

// ── Auth helpers ──────────────────────────────────────────────────────────────

/**
 * Tokens are read from localStorage. They get there one of two ways:
 *
 *  1. Development — POST /api/auth/token mints one for any user_id without
 *     checking a credential. Convenience only.
 *  2. Production — that endpoint returns 404 (see apps/api/routers/auth.py).
 *     The app must be handed a token issued by a real identity provider:
 *
 *         import { setToken } from '@/lib/api'
 *         setToken(tokenFromYourIdP)   // signed with the API's API_SECRET_KEY
 *
 * There is no third path: the backend ships no user store, so nothing here can
 * establish an identity on its own. See SECURITY.md.
 */
const TOKEN_KEY = 'dp_token'

/**
 * The anonymous identity this browser has been using.
 *
 * POST /api/auth/token generates a fresh random user_id when the body omits one,
 * and every project is bound to whichever id created it. Minting without
 * remembering the id therefore orphans the previous work permanently: the
 * projects still exist, but requests arrive as a different user and the API
 * answers 404 for all of them. Four separate identities accumulated on one
 * machine that way, splitting seven projects between them with no route back.
 *
 * Persisting the id and asking for it by name makes the identity survive token
 * loss or expiry, which is the difference between "my drones are gone" and
 * simply getting a new token.
 */
const USER_ID_KEY = 'dp_user_id'

export function getToken(): string | null {
  return readSessionValue(TOKEN_KEY)
}

export function getUserId(): string | null {
  return readSessionValue(USER_ID_KEY)
}

export function setUserId(userId: string): void {
  if (userId) writeSessionValue(USER_ID_KEY, userId)
}

/**
 * Store both halves of a freshly issued session.
 *
 * Keeping the token without the id is what made projects disappear: the next
 * mint invented a new user and every earlier project answered 404 under it.
 * Every mint path must go through here, so use this rather than a bare
 * setToken(res.access_token) — there are three call sites and only one of them
 * was persisting the id.
 */
export function setSession(res: { access_token: string; user_id?: string }): void {
  setToken(res.access_token)
  if (res.user_id) setUserId(res.user_id)
}

export function setToken(token: string): void {
  writeSessionValue(TOKEN_KEY, token)
}

export function clearToken(): void {
  writeSessionValue(TOKEN_KEY, null)
}

/** True once a token is available, whoever issued it. */
export function isAuthenticated(): boolean {
  return getToken() !== null
}

/**
 * Set after the dev mint answers 404, so a production deployment is probed once
 * per session instead of on every unauthenticated request.
 */
let devTokenEndpointUnavailable = false

/** Exposed for tests; production code has no reason to reset this. */
export function _resetAuthProbe(): void {
  devTokenEndpointUnavailable = false
}

async function issueAnonymousToken(): Promise<string | null> {
  if (devTokenEndpointUnavailable) return null
  try {
    // Ask for the id this browser already used, so a re-mint returns to the
    // same identity instead of stranding everything it created.
    const knownUserId = getUserId()
    const tokenRes = await fetch(`${BASE}/api/auth/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(knownUserId ? { user_id: knownUserId } : {}),
    })
    // 404 is the documented "disabled in production" response; 501 covers a
    // deployment that chose to advertise the endpoint as unimplemented.
    if (tokenRes.status === 404 || tokenRes.status === 501) {
      devTokenEndpointUnavailable = true
      return null
    }
    if (!tokenRes.ok) return null
    const { access_token, user_id } = await tokenRes.json() as TokenResponse
    setToken(access_token)
    if (user_id) setUserId(user_id)
    return access_token
  } catch {
    return null
  }
}

// ── Fetch wrapper ─────────────────────────────────────────────────────────────

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) {
    super(message)
    this.name = 'ApiError'
  }
}

/**
 * No token, and none obtainable — the backend has no identity provider wired up.
 *
 * Distinct from a plain 401 so the UI can prompt for sign-in rather than
 * reporting a request failure the user cannot act on.
 */
export class AuthUnavailableError extends ApiError {
  constructor() {
    super(
      401,
      'Not authenticated, and this deployment has no way to issue a token. ' +
        'The development endpoint POST /api/auth/token is disabled outside development. ' +
        'Supply a token from your identity provider via setToken().',
    )
    this.name = 'AuthUnavailableError'
  }
}

/**
 * A backend that is down, or a 500 that escaped CORS, reaches the page only as
 * the browser's bare "Failed to fetch" — a TypeError with no status — which
 * then sat under the prompt box telling the user nothing. Name the cause and
 * where the app was trying to go.
 */
async function fetchOrExplain(url: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init)
  } catch (err) {
    if (err instanceof TypeError) {
      throw new ApiError(
        0,
        `Cannot reach the backend at ${BASE}. Make sure the API server is running ` +
          '(run.cmd, or `uvicorn main:socket_app --port 8010` in apps/api).',
        err,
      )
    }
    throw err
  }
}

async function fetchAPI<T>(
  path: string,
  options: RequestInit = {},
  authenticated = true,
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string> ?? {}),
  }

  if (authenticated) {
    const token = getToken() ?? await issueAnonymousToken()
    if (token) {
      headers['Authorization'] = `Bearer ${token}`
    } else if (devTokenEndpointUnavailable) {
      // Nothing can issue a token here. Fail with something actionable instead
      // of sending an unauthenticated request and surfacing the resulting 401.
      throw new AuthUnavailableError()
    }
  }

  let res = await fetchOrExplain(`${BASE}${path}`, { ...options, headers })

  // Auto-refresh expired token: on 401, clear old token, issue a new one, retry
  if (res.status === 401 && authenticated) {
    clearToken()
    try {
      const accessToken = await issueAnonymousToken()
      if (accessToken) {
        headers['Authorization'] = `Bearer ${accessToken}`
        res = await fetchOrExplain(`${BASE}${path}`, { ...options, headers })
      } else if (devTokenEndpointUnavailable) {
        // The token was rejected and cannot be replaced — the caller has to
        // re-authenticate against their identity provider.
        throw new AuthUnavailableError()
      }
    } catch (err) {
      if (err instanceof AuthUnavailableError) throw err
      // Any other refresh failure falls through to the error below.
    }
  }

  if (!res.ok) {
    let body: unknown
    try { body = await res.json() } catch { body = await res.text() }
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? String((body as Record<string, unknown>).detail)
        : String(body)
    throw new ApiError(res.status, `[${res.status}] ${detail}`, body)
  }

  // 204 No Content
  if (res.status === 204) return undefined as unknown as T

  return res.json() as Promise<T>
}

async function downloadBlob(path: string): Promise<Blob> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  const res = await fetch(`${BASE}${path}`, { headers })
  if (!res.ok) throw new ApiError(res.status, `Download failed: ${res.status}`)
  return res.blob()
}

// ── API surface ───────────────────────────────────────────────────────────────

export const api = {

  // ── Auth ────────────────────────────────────────────────────────────────────

  /**
   * Issue a dev JWT token. Call once on app mount if no token is stored.
   *
   * Defaults to the identity this browser has already used. Omitting it makes
   * the backend generate a fresh random user_id, which silently orphans every
   * project the previous one created.
   */
  issueToken: (userId?: string) =>
    fetchAPI<TokenResponse>('/api/auth/token', {
      method: 'POST',
      body: JSON.stringify({ user_id: userId ?? getUserId() ?? undefined }),
    }, false),


  // ── Projects ────────────────────────────────────────────────────────────────

  createProject: (name: string, prompt: string) =>
    fetchAPI<ProjectResponse>('/api/projects/', {
      method: 'POST',
      body: JSON.stringify({ name, prompt }),
    }),

  listProjects: (offset = 0, limit = 50) =>
    fetchAPI<ProjectListResponse>(`/api/projects/?offset=${offset}&limit=${limit}`),

  getProject: (projectId: string) =>
    fetchAPI<ProjectResponse>(`/api/projects/${projectId}`),

  deleteProject: (projectId: string) =>
    fetchAPI<void>(`/api/projects/${projectId}`, { method: 'DELETE' }),


  // ── Design ──────────────────────────────────────────────────────────────────

  getDesign: (projectId: string) =>
    fetchAPI<DesignResponse>(`/api/projects/${projectId}/design`),

  approveDesign: (projectId: string) =>
    fetchAPI<{ status: string }>(`/api/projects/${projectId}/design/approve`, {
      method: 'POST',
    }),

  modifyDesign: (projectId: string, feedback: string) =>
    fetchAPI<{ status: string }>(`/api/projects/${projectId}/design/modify`, {
      method: 'POST',
      body: JSON.stringify({ feedback }),
    }),


  // ── Code ────────────────────────────────────────────────────────────────────

  getCode: (projectId: string) =>
    fetchAPI<CodeResponse>(`/api/projects/${projectId}/code`),

  approveCode: (projectId: string) =>
    fetchAPI<{ status: string }>(`/api/projects/${projectId}/code/approve`, {
      method: 'POST',
    }),

  modifyCode: (projectId: string, feedback: string) =>
    fetchAPI<{ status: string }>(`/api/projects/${projectId}/code/modify`, {
      method: 'POST',
      body: JSON.stringify({ feedback }),
    }),


  // ── Simulation ──────────────────────────────────────────────────────────────

  startSimulation: (projectId: string) =>
    fetchAPI<{ status: string; iteration: number }>(`/api/projects/${projectId}/simulate/start`, {
      method: 'POST',
    }),

  stopSimulation: (projectId: string) =>
    fetchAPI<{ status: string }>(`/api/projects/${projectId}/simulate/stop`, {
      method: 'POST',
    }),

  getSimulationResults: (projectId: string) =>
    fetchAPI<SimulationResultsResponse>(`/api/projects/${projectId}/simulate/results`),


  // ── Monitor ─────────────────────────────────────────────────────────────────

  getPerformanceReport: (projectId: string) =>
    fetchAPI<PerformanceReportResponse>(`/api/projects/${projectId}/monitor/report`),

  getFlightLogs: (projectId: string, limit = 100, offset = 0) =>
    fetchAPI<FlightLogsResponse>(
      `/api/projects/${projectId}/monitor/logs?limit=${limit}&offset=${offset}`
    ),


  // ── Files ────────────────────────────────────────────────────────────────────

  downloadUrdf: (projectId: string) =>
    downloadBlob(`/api/projects/${projectId}/files/urdf`),

  downloadCode: (projectId: string) =>
    downloadBlob(`/api/projects/${projectId}/files/code`),

  downloadReport: (projectId: string) =>
    downloadBlob(`/api/projects/${projectId}/files/report`),


  // ── Legacy aliases (keep existing callers working) ───────────────────────────

  runPipeline: (projectId: string, prompt: string) =>
    fetchAPI<{ status: string; project_id: string }>('/api/agents/run', {
      method: 'POST',
      body: JSON.stringify({ project_id: projectId, prompt }),
    }),

  approvePipeline: (projectId: string, approved: boolean, feedback = '') =>
    fetchAPI<{ status: string }>('/api/agents/approve', {
      method: 'POST',
      body: JSON.stringify({ project_id: projectId, approved, feedback }),
    }),
}

// ── Download helpers ──────────────────────────────────────────────────────────

/** Trigger a browser file download from a Blob. */
export function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a   = document.createElement('a')
  a.href     = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
