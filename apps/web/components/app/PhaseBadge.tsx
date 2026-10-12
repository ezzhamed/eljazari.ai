import type { PipelinePhase } from '@eljazari/shared-types'
import { Badge, type BadgeTone } from '@/components/ui/badge'
import { isReviewPhase, isWorking, phaseLabel } from '@/lib/pipeline'

/** The run's phase as a badge: working, waiting for review, done or failed. */
export function PhaseBadge({ phase }: { phase: PipelinePhase }) {
  const tone: BadgeTone =
    phase === 'complete' ? 'success'
      : phase === 'error' ? 'danger'
        : isReviewPhase(phase) ? 'warning'
          : isWorking(phase) ? 'brand'
            : 'neutral'
  return (
    <Badge tone={tone} dot={isWorking(phase) ? 'pulse' : phase !== 'idle'}>
      {phaseLabel(phase)}
    </Badge>
  )
}
