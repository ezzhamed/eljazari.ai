/**
 * Pipeline stages as the interface presents them.
 *
 * The same six stages, their labels and the "which one is running" rule were
 * written out separately in the landing page, the header, the agent panel and
 * two unused timelines, and had drifted (Model / URDF / Generate 3D Models).
 * Everything that shows pipeline progress reads it from here.
 */

import type { PipelinePhase } from '@eljazari/shared-types'

export type StageId =
  | 'design'
  | 'urdf_conversion'
  | 'code_generation'
  | 'safety_check'
  | 'simulation'
  | 'monitoring'

export type StageStatus = 'done' | 'active' | 'review' | 'pending' | 'error'

export interface Stage {
  id: StageId
  label: string
  /** What the agent does in this stage, in one line. */
  description: string
}

export const STAGES: Stage[] = [
  { id: 'design', label: 'Design', description: 'Airframe, components and performance estimate' },
  { id: 'urdf_conversion', label: '3D model', description: 'Simulation model built from the design' },
  { id: 'code_generation', label: 'Mission code', description: 'PX4 / MAVSDK mission written for the request' },
  { id: 'safety_check', label: 'Safety', description: 'Static checks and injected safety protocols' },
  { id: 'simulation', label: 'Simulation', description: 'Flight in PX4 SITL, checked by the acceptance gate' },
  { id: 'monitoring', label: 'Performance', description: 'Flight analysis and recommendations' },
]

const PHASE_LABELS: Record<PipelinePhase, string> = {
  idle: 'No run',
  design: 'Designing',
  review_design: 'Design review',
  urdf_conversion: 'Building model',
  code_generation: 'Writing code',
  safety_check: 'Safety check',
  review_code: 'Code review',
  simulation: 'Simulating',
  monitoring: 'Analysing',
  complete: 'Complete',
  error: 'Error',
}

export function phaseLabel(phase: PipelinePhase | string): string {
  return PHASE_LABELS[phase as PipelinePhase] ?? String(phase)
}

/** The stage a phase belongs to; review gates belong to the stage they review. */
export function stageOf(phase: PipelinePhase | string): StageId | null {
  if (phase === 'review_design') return 'design'
  if (phase === 'review_code') return 'safety_check'
  return STAGES.some((s) => s.id === phase) ? (phase as StageId) : null
}

export function isWorking(phase: PipelinePhase | string): boolean {
  return phase !== 'idle' && phase !== 'complete' && phase !== 'error'
}

export function isReviewPhase(phase: PipelinePhase | string): boolean {
  return phase === 'review_design' || phase === 'review_code'
}

/**
 * Status of one stage given the run's current phase.
 *
 * An error is shown on the stage that was running; the store does not keep the
 * phase an error happened in, so `failedStage` passes it when a caller knows it.
 */
export function stageStatus(
  stage: StageId,
  phase: PipelinePhase | string,
  failedStage?: StageId | null,
): StageStatus {
  if (phase === 'complete') return 'done'
  if (phase === 'idle') return 'pending'
  const order = STAGES.map((s) => s.id)
  if (phase === 'error') {
    if (!failedStage) return 'pending'
    const failed = order.indexOf(failedStage)
    const index = order.indexOf(stage)
    return index < failed ? 'done' : index === failed ? 'error' : 'pending'
  }
  const current = stageOf(phase)
  if (!current) return 'pending'
  const index = order.indexOf(stage)
  const active = order.indexOf(current)
  if (index < active) return 'done'
  if (index > active) return 'pending'
  return isReviewPhase(phase) ? 'review' : 'active'
}

/** Combined status for a page that covers several stages (e.g. code + safety). */
export function combinedStatus(statuses: StageStatus[]): StageStatus {
  for (const s of ['error', 'review', 'active'] as const) {
    if (statuses.includes(s)) return s
  }
  return statuses.every((s) => s === 'done') ? 'done' : 'pending'
}
