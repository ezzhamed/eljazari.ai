'use client'

import { useEffect } from 'react'
import { AlertCircle } from 'lucide-react'
import { AgentPanel } from '@/components/app/AgentPanel'
import { AppHeader } from '@/components/app/AppHeader'
import { AppSidebar } from '@/components/app/AppSidebar'
import { useDroneStore } from '@/lib/stores/drone-store'
import { useUIStore } from '@/lib/stores/ui-store'

/**
 * Workspace shell: navigation on the left, the page in the middle, and the
 * agent panel on the right (collapsible; it opens by itself when a review is
 * needed, since the pipeline waits on it).
 *
 * Heights are explicit (h-screen, then min-h-0 down the tree): the 3D canvases
 * measure their container on mount and need a definite size to measure.
 */
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const agentPanelOpen = useUIStore((s) => s.agentPanelOpen)
  const setAgentPanelOpen = useUIStore((s) => s.setAgentPanelOpen)
  const loadPreferences = useUIStore((s) => s.loadPreferences)
  const revealAgentPanel = useUIStore((s) => s.revealAgentPanel)
  const hitlPending = useDroneStore((s) => s.hitlPending)
  const error = useDroneStore((s) => s.error)

  useEffect(() => loadPreferences(), [loadPreferences])
  useEffect(() => {
    if (hitlPending) revealAgentPanel()
  }, [hitlPending, revealAgentPanel])

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      <AppSidebar />

      <div className="flex min-w-0 flex-1 flex-col">
        <AppHeader />

        {error && (
          <div role="alert" className="flex flex-shrink-0 items-start gap-2.5 border-b border-destructive/20 bg-destructive/[0.06] px-5 py-2.5 text-sm text-destructive">
            <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" aria-hidden />
            <span className="min-w-0 break-words">{error}</span>
          </div>
        )}

        <div className="flex min-h-0 flex-1">
          <main className="relative min-w-0 flex-1 overflow-y-auto">{children}</main>
          {agentPanelOpen && (
            <>
              {/* Below xl the panel overlays the page; a click outside closes it. */}
              <button
                type="button"
                aria-label="Close agent panel"
                onClick={() => setAgentPanelOpen(false)}
                className="fixed inset-0 z-30 bg-black/50 backdrop-blur-[1px] xl:hidden"
              />
              <AgentPanel />
            </>
          )}
        </div>
      </div>
    </div>
  )
}
