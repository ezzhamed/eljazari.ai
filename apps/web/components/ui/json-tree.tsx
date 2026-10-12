'use client'

import { useState } from 'react'

/** Collapsible JSON view for agent output (design tree, raw reports). */
export function JsonTree({ data, depth = 0 }: { data: unknown; depth?: number }) {
  const [collapsed, setCollapsed] = useState(depth > 1)

  if (data === null || data === undefined) return <span className="text-muted-foreground">null</span>
  if (typeof data === 'boolean') return <span className={data ? 'text-success' : 'text-destructive'}>{String(data)}</span>
  if (typeof data === 'number') return <span className="text-info">{data}</span>
  if (typeof data === 'string') return <span className="text-brand-200">&quot;{data}&quot;</span>

  const isArray = Array.isArray(data)
  const entries: [string, unknown][] = isArray
    ? (data as unknown[]).map((value, i) => [String(i), value])
    : Object.entries(data as Record<string, unknown>)
  const [open, close] = isArray ? ['[', ']'] : ['{', '}']

  if (entries.length === 0) return <span className="text-muted-foreground">{open + close}</span>

  return (
    <span>
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="focus-ring rounded text-muted-foreground hover:text-foreground"
        aria-expanded={!collapsed}
      >
        {open}
        {collapsed && <span className="px-1 text-2xs">{entries.length} {isArray ? 'items' : 'keys'}</span>}
        {collapsed && close}
      </button>
      {!collapsed && (
        <>
          <div className="ml-3 space-y-0.5 border-l border-border pl-3">
            {entries.map(([key, value]) => (
              <div key={key} className="flex flex-wrap gap-1.5">
                <span className={isArray ? 'text-muted-foreground' : 'text-foreground/70'}>{key}:</span>
                <JsonTree data={value} depth={depth + 1} />
              </div>
            ))}
          </div>
          <span className="text-muted-foreground">{close}</span>
        </>
      )}
    </span>
  )
}
