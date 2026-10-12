import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  vi.resetModules()
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('authentication without persistent browser storage', () => {
  it('keeps the session usable when the storage getter is blocked', async () => {
    const auth = await import('../api')
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
      throw new DOMException('Storage blocked', 'SecurityError')
    })
    expect(auth.getToken()).toBeNull()
    auth.setSession({ access_token: 'session-token', user_id: 'session-user' })
    expect(auth.getToken()).toBe('session-token')
    expect(auth.getUserId()).toBe('session-user')
    auth.clearToken()
    expect(auth.getToken()).toBeNull()
    expect(auth.getUserId()).toBe('session-user')
  })

  it('retains a newly issued token after a storage quota failure', async () => {
    const auth = await import('../api')
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Storage full', 'QuotaExceededError')
    })
    const fetchMock = vi.fn(async (url: RequestInfo | URL, options?: RequestInit) => {
      if (String(url).includes('/api/auth/token')) {
        return new Response(JSON.stringify({ access_token: 'issued-token', user_id: 'user' }))
      }
      expect(options?.headers).toMatchObject({ Authorization: 'Bearer issued-token' })
      return new Response(JSON.stringify({ items: [], total: 0 }))
    })
    vi.stubGlobal('fetch', fetchMock)
    await auth.api.listProjects()
    await auth.api.listProjects()
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/api/auth/token'))).toHaveLength(1)
    expect(auth.getUserId()).toBe('user')
  })

  it('does not revive a cleared token when persistent removal fails', async () => {
    const auth = await import('../api')
    auth.setToken('old-token')
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('Storage blocked', 'SecurityError')
    })
    auth.clearToken()
    expect(auth.getToken()).toBeNull()
    expect(auth.isAuthenticated()).toBe(false)
    expect(localStorage.getItem('dp_token')).toBe('old-token')
  })
})
