'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { useState } from 'react'
import { Bot, Check, MessageSquareText, Send, X } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { useDroneStore, type AgentActivity } from '@/lib/stores/drone-store'
import { useUIStore } from '@/lib/stores/ui-store'

const STATUS_TONE = { running: 'brand', done: 'neutral', error: 'danger' } as const

function time(ms: number) {
  if (!ms) return ''
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/** Approve or send changes when the pipeline pauses for a human review. */
export function ReviewCard({ inline = false }: { inline?: boolean }) {
  const phase = useDroneStore((s) => s.currentPhase)
  const hitlRequest = useDroneStore((s) => s.hitlRequest)
  const approveDesign = useDroneStore((s) => s.approveDesign)
  const approveCode = useDroneStore((s) => s.approveCode)
  const requestModification = useDroneStore((s) => s.requestModification)
  const [feedback, setFeedback] = useState('')
  const [writing, setWriting] = useState(false)

  const isDesign = hitlRequest?.phase === 'design_review' || phase === 'review_design'
  const subject = isDesign ? 'design' : 'mission code'

  const send = () => {
    if (!feedback.trim()) return
    void requestModification(feedback.trim())
    setFeedback('')
    setWriting(false)
  }

  return (
    <div className="rounded-xl border border-warning/30 bg-warning/[0.06] p-4">
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 animate-pulse rounded-full bg-warning" />
        <p className="text-sm font-medium text-foreground">Review the {subject}</p>
      </div>
      <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
        The pipeline is paused until you approve the {subject} or ask for changes.{' '}
        {!inline && (
          <Link
            href={isDesign ? '/dashboard/design' : '/dashboard/code'}
            className="text-brand-300 underline-offset-2 hover:underline"
          >
            Open the {subject}
          </Link>
        )}
      </p>

      {writing ? (
        <div className="mt-3 space-y-2">
          <textarea
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send()
            }}
            rows={3}
            dir="auto"
            autoFocus
            placeholder={isDesign ? 'e.g. use a 4S battery and keep it under 1.5 kg' : 'e.g. hover for 30 s instead of 20 s'}
            className="focus-ring w-full resize-none rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/60"
          />
          <div className="flex gap-2">
            <Button size="sm" onClick={send} disabled={!feedback.trim()} className="h-8 flex-1 gap-1.5 text-xs">
              <Send className="h-3.5 w-3.5" /> Send changes
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setWriting(false)} className="h-8 text-xs">
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-3 flex gap-2">
          <Button
            size="sm"
            onClick={() => void (isDesign ? approveDesign() : approveCode())}
            className="h-8 flex-1 gap-1.5 text-xs font-semibold"
          >
            <Check className="h-3.5 w-3.5" /> Approve
          </Button>
          <Button size="sm" variant="outline" onClick={() => setWriting(true)} className="h-8 flex-1 gap-1.5 text-xs">
            <MessageSquareText className="h-3.5 w-3.5" /> Request changes
          </Button>
        </div>
      )}
    </div>
  )
}

function ActivityRow({ activity }: { activity: AgentActivity }) {
  return (
    <li className="rounded-lg border border-border bg-card px-3 py-2.5">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">{activity.agent}</span>
        <span className="text-2xs tabular-nums text-muted-foreground/70">{time(activity.timestamp)}</span>
        <Badge tone={STATUS_TONE[activity.status] ?? 'neutral'} dot={activity.status === 'running' ? 'pulse' : false}>
          {activity.status}
        </Badge>
      </div>
      <p className="mt-1.5 break-words text-xs leading-5 text-muted-foreground" dir="auto">
        {activity.message}
      </p>
    </li>
  )
}

/** Right-hand panel: the request, any pending review, and what the agents are doing. */
export function AgentPanel() {
  const userPrompt = useDroneStore((s) => s.userPrompt)
  const activityFeed = useDroneStore((s) => s.activityFeed)
  const hitlPending = useDroneStore((s) => s.hitlPending)
  const setAgentPanelOpen = useUIStore((s) => s.setAgentPanelOpen)
  const phase = useDroneStore((s) => s.currentPhase)
  const pathname = usePathname()
  // The stage page shows the same card inline; one copy is enough.
  const reviewPage = phase === 'review_design' ? '/dashboard/design' : phase === 'review_code' ? '/dashboard/code' : null
  const reviewShownOnPage = reviewPage !== null && pathname === reviewPage

  return (
    <aside
      aria-label="Agent activity"
      className="fixed inset-y-0 right-0 z-40 flex w-[min(340px,100vw)] flex-col border-l border-border bg-surface shadow-2xl shadow-black/60 xl:static xl:z-auto xl:h-full xl:w-[340px] xl:flex-shrink-0 xl:shadow-none"
    >
      <div className="flex h-12 flex-shrink-0 items-center gap-2 border-b border-border px-4">
        <Bot className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h2 className="text-sm font-medium">Agent</h2>
        {activityFeed.length > 0 && (
          <span className="text-2xs tabular-nums text-muted-foreground">{activityFeed.length} events</span>
        )}
        <button
          type="button"
          onClick={() => setAgentPanelOpen(false)}
          className="focus-ring ml-auto rounded-md p-1 text-muted-foreground transition-colors hover:bg-white/[0.05] hover:text-foreground"
          aria-label="Close agent panel"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        <section>
          <p className="eyebrow mb-1.5">Request</p>
          <p className="text-sm leading-6 text-foreground/90" dir="auto">
            {userPrompt || <span className="text-muted-foreground">No run yet. Start one from the prompt on the home page.</span>}
          </p>
        </section>

        {hitlPending && !reviewShownOnPage && <ReviewCard />}

        <section>
          <p className="eyebrow mb-2">Activity</p>
          {activityFeed.length === 0 ? (
            <EmptyState
              icon={Bot}
              title="No agent activity yet"
              description="Each agent reports here as it starts and finishes its stage."
              className="rounded-xl border border-dashed border-border py-8"
            />
          ) : (
            <ul className="space-y-2">
              {activityFeed.map((activity, i) => (
                <ActivityRow key={`${activity.timestamp}-${i}`} activity={activity} />
              ))}
            </ul>
          )}
        </section>
      </div>
    </aside>
  )
}
