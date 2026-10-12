import { Check, X } from 'lucide-react'
import type { StageStatus } from '@/lib/pipeline'
import { cn } from '@/lib/utils'

const LABELS: Record<StageStatus, string> = {
  done: 'Done',
  active: 'Running',
  review: 'Needs review',
  pending: 'Not started',
  error: 'Failed',
}

/** Small status mark used in the sidebar and the overview's stage list. */
export function StageIndicator({ status, className }: { status: StageStatus; className?: string }) {
  return (
    <span
      role="img"
      aria-label={LABELS[status]}
      title={LABELS[status]}
      className={cn('flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full', className)}
    >
      {status === 'done' && (
        <span className="flex h-4 w-4 items-center justify-center rounded-full bg-success/15 text-success">
          <Check className="h-2.5 w-2.5" strokeWidth={3} />
        </span>
      )}
      {status === 'error' && (
        <span className="flex h-4 w-4 items-center justify-center rounded-full bg-destructive/15 text-destructive">
          <X className="h-2.5 w-2.5" strokeWidth={3} />
        </span>
      )}
      {status === 'active' && <span className="h-2 w-2 animate-pulse rounded-full bg-brand-400" />}
      {status === 'review' && <span className="h-2 w-2 animate-pulse rounded-full bg-warning" />}
      {status === 'pending' && <span className="h-2 w-2 rounded-full border border-white/20" />}
    </span>
  )
}
