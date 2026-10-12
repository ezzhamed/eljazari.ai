/**
 * eljazari.ai — TanStack Query hooks
 *
 * Each hook corresponds to one API resource. Cache keys follow the
 * pattern ['resource', ...ids] so targeted invalidation is easy.
 *
 * Setup: wrap your app with <QueryClientProvider client={queryClient} />.
 * QueryClient is exported from this file for convenience.
 */

import {
  useQuery,
  useMutation,
  useQueryClient,
  QueryClient,
  type UseQueryResult,
  type UseMutationResult,
} from '@tanstack/react-query'
import {
  api,
  type ProjectResponse,
  type ProjectListResponse,
  type DesignResponse,
  type CodeResponse,
  type SimulationResultsResponse,
  type PerformanceReportResponse,
  type FlightLogsResponse,
  getToken,
  setSession,
  triggerDownload,
} from '@/lib/api'

// ── Shared QueryClient ────────────────────────────────────────────────────────

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime:   30_000,   // 30 s
      retry:       2,
      refetchOnWindowFocus: false,
    },
  },
})

// ── Cache key factories ────────────────────────────────────────────────────────

export const keys = {
  projects:            ()              => ['projects']              as const,
  project:             (id: string)    => ['projects', id]          as const,
  design:              (id: string)    => ['design', id]            as const,
  code:                (id: string)    => ['code', id]              as const,
  simulationResults:   (id: string)    => ['simResults', id]        as const,
  performanceReport:   (id: string)    => ['perfReport', id]        as const,
  flightLogs:          (id: string)    => ['flightLogs', id]        as const,
}

// ── Auth ──────────────────────────────────────────────────────────────────────

/**
 * Auto-issues a dev JWT token if none is stored.
 * Call this once in a layout/provider component.
 */
export function useEnsureToken(): void {
  useQuery({
    queryKey: ['auth_token'],
    queryFn: async () => {
      if (getToken()) return { cached: true }
      const res = await api.issueToken()
      setSession(res)
      return res
    },
    staleTime: Infinity,
  })
}

// ── Projects ──────────────────────────────────────────────────────────────────

export function useProjects(offset = 0, limit = 50): UseQueryResult<ProjectListResponse> {
  return useQuery({
    queryKey: [...keys.projects(), offset, limit],
    queryFn:  () => api.listProjects(offset, limit),
  })
}

export function useProject(projectId: string | null): UseQueryResult<ProjectResponse> {
  return useQuery({
    queryKey: keys.project(projectId ?? ''),
    queryFn:  () => api.getProject(projectId!),
    enabled:  Boolean(projectId),
  })
}

export function useCreateProject(): UseMutationResult<
  ProjectResponse,
  Error,
  { name: string; prompt: string }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, prompt }) => api.createProject(name, prompt),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.projects() })
    },
  })
}

export function useDeleteProject(): UseMutationResult<void, Error, string> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (projectId) => api.deleteProject(projectId),
    onSuccess: (_, projectId) => {
      qc.removeQueries({ queryKey: keys.project(projectId) })
      qc.invalidateQueries({ queryKey: keys.projects() })
    },
  })
}

// ── Design ────────────────────────────────────────────────────────────────────

export function useDesign(projectId: string | null): UseQueryResult<DesignResponse> {
  return useQuery({
    queryKey: keys.design(projectId ?? ''),
    queryFn:  () => api.getDesign(projectId!),
    enabled:  Boolean(projectId),
    refetchInterval: (query) =>
      query.state.data?.status === 'pending' ? 3_000 : false,
  })
}

export function useApproveDesign(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => api.approveDesign(projectId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.design(projectId) })
      qc.invalidateQueries({ queryKey: keys.project(projectId) })
    },
  })
}

export function useModifyDesign(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (feedback: string) => api.modifyDesign(projectId, feedback),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.design(projectId) })
    },
  })
}

// ── Code ──────────────────────────────────────────────────────────────────────

export function useCode(projectId: string | null): UseQueryResult<CodeResponse> {
  return useQuery({
    queryKey: keys.code(projectId ?? ''),
    queryFn:  () => api.getCode(projectId!),
    enabled:  Boolean(projectId),
    refetchInterval: (query) =>
      query.state.data?.status === 'pending' ? 3_000 : false,
  })
}

export function useApproveCode(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => api.approveCode(projectId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.code(projectId) })
      qc.invalidateQueries({ queryKey: keys.project(projectId) })
    },
  })
}

export function useModifyCode(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (feedback: string) => api.modifyCode(projectId, feedback),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.code(projectId) })
    },
  })
}

// ── Simulation ────────────────────────────────────────────────────────────────

export function useSimulationResults(
  projectId: string | null
): UseQueryResult<SimulationResultsResponse> {
  return useQuery({
    queryKey: keys.simulationResults(projectId ?? ''),
    queryFn:  () => api.getSimulationResults(projectId!),
    enabled:  Boolean(projectId),
  })
}

export function useStartSimulation(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => api.startSimulation(projectId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.simulationResults(projectId) })
    },
  })
}

export function useStopSimulation(projectId: string) {
  return useMutation({
    mutationFn: () => api.stopSimulation(projectId),
  })
}

// ── Monitor ───────────────────────────────────────────────────────────────────

export function usePerformanceReport(
  projectId: string | null
): UseQueryResult<PerformanceReportResponse> {
  return useQuery({
    queryKey: keys.performanceReport(projectId ?? ''),
    queryFn:  () => api.getPerformanceReport(projectId!),
    enabled:  Boolean(projectId),
  })
}

export function useFlightLogs(
  projectId: string | null,
  limit = 100,
): UseQueryResult<FlightLogsResponse> {
  return useQuery({
    queryKey: [...keys.flightLogs(projectId ?? ''), limit],
    queryFn:  () => api.getFlightLogs(projectId!, limit),
    enabled:  Boolean(projectId),
  })
}

// ── File downloads ────────────────────────────────────────────────────────────

export function useDownloadUrdf(projectId: string) {
  return useMutation({
    mutationFn: async () => {
      const blob = await api.downloadUrdf(projectId)
      triggerDownload(blob, `drone_${projectId.slice(0, 8)}.urdf`)
    },
  })
}

export function useDownloadCode(projectId: string) {
  return useMutation({
    mutationFn: async () => {
      const blob = await api.downloadCode(projectId)
      triggerDownload(blob, `mission_${projectId.slice(0, 8)}.py`)
    },
  })
}

export function useDownloadReport(projectId: string) {
  return useMutation({
    mutationFn: async () => {
      const blob = await api.downloadReport(projectId)
      triggerDownload(blob, `report_${projectId.slice(0, 8)}.pdf`)
    },
  })
}
