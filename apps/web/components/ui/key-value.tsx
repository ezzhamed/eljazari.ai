import * as React from 'react'
import { cn } from '@/lib/utils'

export interface KeyValueItem {
  label: string
  value: React.ReactNode
  /** Monospace for identifiers and code-like values. */
  mono?: boolean
}

/** Label / value rows. Empty values read "—". */
export function KeyValueList({ items, className }: { items: KeyValueItem[]; className?: string }) {
  return (
    <dl className={cn('divide-y divide-border/60', className)}>
      {items.map((item) => {
        const empty = item.value === null || item.value === undefined || item.value === ''
        return (
          <div key={item.label} className="flex items-baseline justify-between gap-4 py-2 text-xs first:pt-0 last:pb-0">
            <dt className="flex-shrink-0 text-muted-foreground">{item.label}</dt>
            <dd
              className={cn(
                'min-w-0 break-words text-right font-medium tabular-nums',
                empty ? 'text-muted-foreground' : 'text-foreground',
                item.mono && 'font-mono',
              )}
            >
              {empty ? '—' : item.value}
            </dd>
          </div>
        )
      })}
    </dl>
  )
}
