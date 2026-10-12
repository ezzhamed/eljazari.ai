'use client'

import { History } from 'lucide-react'
import { EmptyState } from '@/components/ui/empty-state'

/** Shown instead of a page's "nothing yet" state while a saved run is restored. */
export function RestoringState({ className }: { className?: string }) {
  return (
    <EmptyState
      icon={History}
      title="Loading this run"
      description="Restoring the saved design, code and results from the server."
      className={className}
    />
  )
}
