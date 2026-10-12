'use client'

import { usePathname, useRouter } from 'next/navigation'
import { ChevronRight, PanelRightClose, PanelRightOpen, Plus } from 'lucide-react'
import { PhaseBadge } from '@/components/app/PhaseBadge'
import { navItemFor } from '@/components/app/nav'
import { Button } from '@/components/ui/button'
import { useDroneStore } from '@/lib/stores/drone-store'
import { useUIStore } from '@/lib/stores/ui-store'

/** Where you are (run › page), the run's phase, and the workspace actions. */
export function AppHeader() {
  const pathname = usePathname()
  const router = useRouter()
  const projectName = useDroneStore((s) => s.projectName)
  const projectId = useDroneStore((s) => s.projectId)
  const phase = useDroneStore((s) => s.currentPhase)
  const hitlPending = useDroneStore((s) => s.hitlPending)
  const reset = useDroneStore((s) => s.reset)
  const agentPanelOpen = useUIStore((s) => s.agentPanelOpen)
  const toggleAgentPanel = useUIStore((s) => s.toggleAgentPanel)
  const page = navItemFor(pathname)

  const runName = projectName || (projectId ? `Run ${projectId.slice(0, 8)}` : 'No run')

  return (
    <header className="flex h-14 flex-shrink-0 items-center gap-3 border-b border-border bg-surface/80 px-4 backdrop-blur-xl lg:px-5">
      <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1.5 text-sm">
        <span className="truncate text-muted-foreground" title={runName}>{runName}</span>
        {page && (
          <>
            <ChevronRight className="h-3.5 w-3.5 flex-shrink-0 text-muted-foreground/50" aria-hidden />
            <span className="truncate font-medium text-foreground">{page.label}</span>
          </>
        )}
      </nav>

      <div className="ml-auto flex items-center gap-2">
        <span className="hidden sm:contents">
          <PhaseBadge phase={phase} />
        </span>

        <Button
          variant="ghost"
          size="sm"
          onClick={toggleAgentPanel}
          aria-pressed={agentPanelOpen}
          className="relative h-8 gap-1.5 px-2.5 text-xs text-muted-foreground hover:text-foreground"
          title={agentPanelOpen ? 'Hide agent panel' : 'Show agent panel'}
        >
          {agentPanelOpen ? <PanelRightClose className="h-4 w-4" /> : <PanelRightOpen className="h-4 w-4" />}
          <span className="hidden sm:inline">Agent</span>
          {hitlPending && !agentPanelOpen && (
            <span className="absolute right-1 top-1 h-2 w-2 rounded-full bg-warning" aria-label="Review needed" />
          )}
        </Button>

        <Button
          size="sm"
          onClick={() => {
            reset()
            router.push('/')
          }}
          className="h-8 gap-1.5 px-2.5 text-xs font-semibold sm:px-3"
          aria-label="New run"
        >
          <Plus className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">New run</span>
        </Button>
      </div>
    </header>
  )
}
