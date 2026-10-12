import * as React from 'react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

const TONES = {
  default: 'text-foreground',
  brand: 'text-brand-300',
  success: 'text-success',
  warning: 'text-warning',
  danger: 'text-destructive',
  muted: 'text-muted-foreground',
} as const

interface StatProps {
  label: string
  value: React.ReactNode
  unit?: string
  /** One line under the value: where it came from, or what it is measured against. */
  hint?: React.ReactNode
  icon?: LucideIcon
  tone?: keyof typeof TONES
  className?: string
}

/** One number with its label. A missing value is shown as "—", never as zero. */
export function Stat({ label, value, unit, hint, icon: Icon, tone = 'default', className }: StatProps) {
  const empty = value === null || value === undefined || value === ''
  return (
    <div className={cn('min-w-0 rounded-xl border border-border bg-card px-4 py-3.5', className)}>
      <div className="flex items-center gap-2">
        {Icon && <Icon className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />}
        <p className="truncate text-2xs font-medium text-muted-foreground">{label}</p>
      </div>
      <p className={cn('mt-1.5 truncate text-xl font-semibold tabular-nums', empty ? TONES.muted : TONES[tone])}>
        {empty ? '—' : value}
        {!empty && unit && <span className="ml-1 text-xs font-normal text-muted-foreground">{unit}</span>}
      </p>
      {hint && <p className="mt-1 line-clamp-2 text-2xs text-muted-foreground">{hint}</p>}
    </div>
  )
}
