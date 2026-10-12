import * as React from 'react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

interface PageHeaderProps {
  title: string
  description?: React.ReactNode
  icon?: LucideIcon
  /** Next to the title: the page's status badge. */
  status?: React.ReactNode
  /** Right-aligned controls. */
  actions?: React.ReactNode
}

/** Title row shared by every workspace page. */
export function PageHeader({ title, description, icon: Icon, status, actions }: PageHeaderProps) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
      <div className="flex min-w-0 items-start gap-3">
        {Icon && (
          <span className="mt-0.5 flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg border border-border bg-surface-raised">
            <Icon className="h-4 w-4 text-brand-300" aria-hidden />
          </span>
        )}
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-lg font-semibold tracking-tight text-foreground">{title}</h1>
            {status}
          </div>
          {description && (
            <p className="mt-0.5 max-w-2xl text-sm text-muted-foreground">{description}</p>
          )}
        </div>
      </div>
      {actions && <div className="flex flex-shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}

/**
 * Scrolling page body with the standard padding.
 *
 * `fill` pages (a 3D canvas, an editor) take the full height and scroll inside
 * their own panels instead.
 */
export function PageBody({
  className,
  fill = false,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { fill?: boolean }) {
  return (
    <div
      className={cn(
        'mx-auto flex w-full max-w-[1600px] flex-col gap-5 p-5 lg:p-6',
        fill ? 'h-full min-h-0' : 'min-h-full',
        className,
      )}
      {...props}
    />
  )
}
