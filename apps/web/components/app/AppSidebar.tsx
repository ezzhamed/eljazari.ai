'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { Logo } from '@/components/brand/Logo'
import { StageIndicator } from '@/components/app/StageIndicator'
import { NAV_SECTIONS, navItemFor } from '@/components/app/nav'
import { useStageStatuses } from '@/lib/hooks/useStageStatuses'
import { combinedStatus } from '@/lib/pipeline'
import { useDroneStore } from '@/lib/stores/drone-store'
import { useWSStore } from '@/lib/stores/ws-store'
import { cn } from '@/lib/utils'

/**
 * Workspace navigation. The pipeline pages carry their stage's status, so the
 * sidebar is also the run's progress list. Below `lg` it narrows to icons.
 */
export function AppSidebar() {
  const pathname = usePathname()
  const statuses = useStageStatuses()
  const projectId = useDroneStore((s) => s.projectId)
  const connected = useWSStore((s) => s.connected)
  const active = navItemFor(pathname)

  // The socket only opens once there is a run to follow, so "offline" with no
  // run loaded would suggest an outage that is not happening.
  const link = !projectId
    ? { label: 'No active run', dot: 'bg-white/25', text: 'text-muted-foreground' }
    : connected
      ? { label: 'Live', dot: 'bg-success animate-pulse', text: 'text-success' }
      : { label: 'Reconnecting…', dot: 'bg-warning animate-pulse', text: 'text-warning' }

  return (
    <aside className="flex h-full w-16 flex-shrink-0 flex-col border-r border-border bg-surface lg:w-60">
      <div className="flex h-14 flex-shrink-0 items-center justify-center border-b border-border px-3 lg:justify-start lg:px-4">
        <Link href="/" className="focus-ring rounded-md transition-opacity hover:opacity-80" title="eljazari.ai home">
          <span className="lg:hidden">
            <Logo size={28} showWordmark={false} />
          </span>
          <span className="hidden lg:block">
            <Logo size={28} />
          </span>
        </Link>
      </div>

      <nav aria-label="Workspace" className="flex-1 space-y-5 overflow-y-auto px-2 py-4 lg:px-3">
        {NAV_SECTIONS.map((section) => (
          <div key={section.title}>
            <p className="eyebrow mb-1.5 hidden px-2 lg:block">{section.title}</p>
            <ul className="space-y-0.5">
              {section.items.map((item) => {
                const selected = active?.href === item.href
                const status = item.stages ? combinedStatus(item.stages.map((id) => statuses[id])) : null
                const Icon = item.icon
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      title={item.label}
                      aria-current={selected ? 'page' : undefined}
                      className={cn(
                        'focus-ring group relative flex h-9 items-center justify-center gap-3 rounded-lg px-2 text-sm transition-colors lg:justify-start',
                        selected
                          ? 'bg-surface-raised text-foreground'
                          : 'text-muted-foreground hover:bg-white/[0.04] hover:text-foreground',
                      )}
                    >
                      {selected && (
                        <span aria-hidden className="absolute inset-y-2 left-0 w-0.5 rounded-full bg-brand-400" />
                      )}
                      <Icon className={cn('h-4 w-4 flex-shrink-0', selected && 'text-brand-300')} aria-hidden />
                      <span className="hidden flex-1 truncate lg:block">{item.label}</span>
                      {status && (
                        <StageIndicator
                          status={status}
                          className="absolute right-1 top-1 scale-75 lg:static lg:scale-100"
                        />
                      )}
                    </Link>
                  </li>
                )
              })}
            </ul>
          </div>
        ))}
      </nav>

      <div className="flex-shrink-0 border-t border-border px-3 py-3 lg:px-4" title={link.label}>
        <div className="flex items-center justify-center gap-2 lg:justify-start">
          <span className={cn('h-2 w-2 flex-shrink-0 rounded-full', link.dot)} />
          <span className={cn('hidden text-xs lg:inline', link.text)}>{link.label}</span>
          {projectId && (
            <span className="ml-auto hidden truncate font-mono text-2xs text-muted-foreground/70 lg:inline" title={projectId}>
              {projectId.slice(0, 8)}
            </span>
          )}
        </div>
      </div>
    </aside>
  )
}
