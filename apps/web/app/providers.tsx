'use client'

import { QueryClientProvider } from '@tanstack/react-query'
import { useEffect } from 'react'
import { queryClient } from '@/lib/hooks/useProject'
import { getToken, setSession, clearToken, api } from '@/lib/api'

// ── Auto-token bootstrap ──────────────────────────────────────────────────────

function isTokenExpired(token: string): boolean {
  try {
    const payload = JSON.parse(atob(token.split('.')[1]))
    return payload.exp * 1000 < Date.now()
  } catch {
    return true
  }
}

function TokenBootstrap() {
  useEffect(() => {
    const token = getToken()
    if (!token || isTokenExpired(token)) {
      if (token) clearToken()
      api.issueToken().then(setSession).catch(() => {
        // Silently fail in dev when API is not running
      })
    }
  }, [])
  return null
}

// ── Providers ─────────────────────────────────────────────────────────────────

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      <TokenBootstrap />
      {children}
    </QueryClientProvider>
  )
}
