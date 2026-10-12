'use client'

import { useDroneStore } from '@/lib/stores/drone-store'
import { STAGES, stageStatus, type StageId, type StageStatus } from '@/lib/pipeline'

/**
 * Status of every pipeline stage for the current run.
 *
 * On an error the store no longer says which phase was running, so the failed
 * stage is the first one whose output is missing.
 */
export function useStageStatuses(): Record<StageId, StageStatus> {
  const phase = useDroneStore((s) => s.currentPhase)
  const hasDesign = useDroneStore((s) => s.design !== null)
  const hasCode = useDroneStore((s) => s.generatedCode.length > 0)
  const hasSafety = useDroneStore((s) => s.safetyReport !== null)
  const hasFlight = useDroneStore((s) => s.simulations.length > 0 && s.simulationStatus !== 'failed')
  const hasReport = useDroneStore((s) => s.performanceReport !== null)

  let failed: StageId | null = null
  if (phase === 'error') {
    const produced: Record<StageId, boolean> = {
      design: hasDesign,
      // The model is built from the design before code generation starts.
      urdf_conversion: hasDesign && hasCode,
      code_generation: hasCode,
      safety_check: hasSafety,
      simulation: hasFlight,
      monitoring: hasReport,
    }
    failed = STAGES.find((stage) => !produced[stage.id])?.id ?? 'monitoring'
  }

  return Object.fromEntries(
    STAGES.map((stage) => [stage.id, stageStatus(stage.id, phase, failed)]),
  ) as Record<StageId, StageStatus>
}
