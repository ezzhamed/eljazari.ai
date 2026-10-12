'use client'

/**
 * Project hydrator — remembers the active run and rebuilds it after a reload.
 *
 * The store only ever receives data through Socket.io events fired during
 * pipeline execution, and lives in memory. Reload the tab, open a dashboard
 * sub-page directly, or come back from the landing page, and the store was
 * empty even though the project was still running (or finished) in Postgres.
 *
 * Two jobs, both idempotent:
 *  1. Keep the active project id in localStorage, and restore it on a cold
 *     start. This used to live in the dashboard layout only, so a refresh on
 *     the landing page — where the run is launched and first watched — lost
 *     the run entirely.
 *  2. Fill the store from the REST API for that project. The Socket.io stream
 *     stays the source of truth for new events; this only covers the window
 *     before the socket joins, and everything that happened while it was gone.
 */

import { useEffect } from 'react'
import { useDroneStore } from '@/lib/stores/drone-store'

export const LAST_PROJECT_KEY = 'dp_last_project_id'

function readSavedProjectId(): string | null {
  try {
    return window.localStorage.getItem(LAST_PROJECT_KEY)
  } catch {
    return null
  }
}

export function rememberProjectId(projectId: string | null) {
  try {
    if (projectId) window.localStorage.setItem(LAST_PROJECT_KEY, projectId)
    else window.localStorage.removeItem(LAST_PROJECT_KEY)
  } catch {
    /* private mode / storage disabled — the run simply won't survive a reload */
  }
}

export function ProjectHydrator() {
  const projectId = useDroneStore((s) => s.projectId)
  const setProjectId = useDroneStore((s) => s.setProjectId)
  const hydrateFromBackend = useDroneStore((s) => s.hydrateFromBackend)

  useEffect(() => {
    if (projectId) {
      rememberProjectId(projectId)
      return
    }
    const saved = readSavedProjectId()
    if (saved) setProjectId(saved)
    // Nothing to restore: let the pages show their empty states.
    else useDroneStore.setState({ isRestoring: false })
  }, [projectId, setProjectId])

  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    void (async () => {
      try {
        await hydrateFromBackend(projectId)
      } catch (err) {
        if (!cancelled) {
          // Best-effort: any failure is a no-op so the live socket stream
          // still drives the UI.
          console.warn('[ProjectHydrator] hydrate failed:', err)
        }
      }
    })()
    return () => { cancelled = true }
  }, [projectId, hydrateFromBackend])

  return null
}
