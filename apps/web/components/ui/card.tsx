import * as React from 'react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

/** A bordered panel. Every block of content on a page sits in one. */
export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('flex min-w-0 flex-col rounded-xl border border-border bg-card', className)}
      {...props}
    />
  )
}

interface CardHeaderProps {
  title: React.ReactNode
  icon?: LucideIcon
  description?: React.ReactNode
  /** Right-aligned: a badge, a toggle, a link. */
  actions?: React.ReactNode
  className?: string
}

export function CardHeader({ title, icon: Icon, description, actions, className }: CardHeaderProps) {
  return (
    <div
      className={cn(
        'flex min-h-[3rem] flex-shrink-0 items-center gap-3 border-b border-border px-4 py-2.5',
        className,
      )}
    >
      {Icon && <Icon className="h-4 w-4 flex-shrink-0 text-muted-foreground" aria-hidden />}
      <div className="min-w-0 flex-1">
        <h2 className="truncate text-sm font-medium text-foreground">{title}</h2>
        {description && <p className="line-clamp-2 text-2xs text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex flex-shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}

export function CardContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('min-h-0 flex-1 p-4', className)} {...props} />
}
