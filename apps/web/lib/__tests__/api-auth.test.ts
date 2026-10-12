/**
 * Tests for the API client's auth handling (lib/api.ts).
 *
 * The client mints its own token from POST /api/auth/token. That endpoint is
 * disabled outside development (apps/api/routers/auth.py), so in production the
 * client used to POST a doomed request before every call and then surface a
 * bare "[401] Missing Authorization header" the user could not act on.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  AuthUnavailableError,
  _resetAuthProbe,
  api,
  clearToken,
  getToken,
  isAuthenticated,
  setToken,
} from '../api'

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response
}

/** Signature matches the global fetch so mock.calls stays typed as [url, init]. */
type FetchArgs = [url: RequestInfo | URL, init?: RequestInit]

function headersOf(call: FetchArgs | undefined): Record<string, string> {
  return (call?.[1]?.headers ?? {}) as Record<string, string>
}

/** Backend with the dev mint disabled, as in production. */
function productionBackend() {
  return vi.fn(async (...[url]: FetchArgs) => {
    if (String(url).includes('/api/auth/token')) {
      return jsonResponse({ detail: 'Not Found' }, 404)
    }
    return jsonResponse({ detail: 'Missing Authorization header' }, 401)
  })
}

/** Backend with the dev mint enabled, as in development. */
function developmentBackend() {
  return vi.fn(async (...[url]: FetchArgs) => {
    if (String(url).includes('/api/auth/token')) {
      return jsonResponse({ access_token: 'dev-token', token_type: 'bearer', user_id: 'u1' })
    }
    return jsonResponse({ id: 'p1', name: 'n', prompt: 'p', status: 's', user_id: 'u1', created_at: null })
  })
}

beforeEach(() => {
  localStorage.clear()
  _resetAuthProbe()
  vi.restoreAllMocks()
})

describe('token storage', () => {
  it('round-trips a token', () => {
    expect(getToken()).toBeNull()
    expect(isAuthenticated()).toBe(false)

    setToken('external-token')

    expect(getToken()).toBe('external-token')
    expect(isAuthenticated()).toBe(true)
  })

  it('clears a token', () => {
    setToken('t')
    clearToken()
    expect(getToken()).toBeNull()
  })
})

describe('development: the dev mint is available', () => {
  it('obtains a token and attaches it', async () => {
    const fetchMock = developmentBackend()
    vi.stubGlobal('fetch', fetchMock)

    await api.getProject('p1')

    expect(getToken()).toBe('dev-token')
    const projectCall = fetchMock.mock.calls.find(c => String(c[0]).includes('/api/projects/'))
    expect(headersOf(projectCall)).toMatchObject({ Authorization: 'Bearer dev-token' })
  })
})

describe('production: the dev mint returns 404', () => {
  it('throws AuthUnavailableError rather than a bare 401', async () => {
    vi.stubGlobal('fetch', productionBackend())

    await expect(api.getProject('p1')).rejects.toBeInstanceOf(AuthUnavailableError)
  })

  it('explains how to authenticate', async () => {
    vi.stubGlobal('fetch', productionBackend())

    await expect(api.getProject('p1')).rejects.toThrow(/setToken/)
  })

  it('probes the mint once, not on every request', async () => {
    // The regression: an unauthenticated app POSTed to a 404 before every call.
    const fetchMock = productionBackend()
    vi.stubGlobal('fetch', fetchMock)

    for (const _ of [1, 2, 3]) {
      await api.getProject('p1').catch(() => undefined)
    }

    const tokenCalls = fetchMock.mock.calls.filter(c => String(c[0]).includes('/api/auth/token'))
    expect(tokenCalls).toHaveLength(1)
  })

  it('uses an externally supplied token and never calls the mint', async () => {
    const fetchMock = vi.fn(async (...[_url]: FetchArgs) => jsonResponse({ id: 'p1' }))
    vi.stubGlobal('fetch', fetchMock)
    setToken('token-from-idp')

    await api.getProject('p1')

    expect(fetchMock.mock.calls.some(c => String(c[0]).includes('/api/auth/token'))).toBe(false)
    expect(headersOf(fetchMock.mock.calls[0])).toMatchObject({ Authorization: 'Bearer token-from-idp' })
  })

  it('reports auth is unavailable when a stored token is rejected', async () => {
    const fetchMock = vi.fn(async (...[url]: FetchArgs) => {
      if (String(url).includes('/api/auth/token')) return jsonResponse({}, 404)
      return jsonResponse({ detail: 'Invalid or expired token' }, 401)
    })
    vi.stubGlobal('fetch', fetchMock)
    setToken('expired-token')

    await expect(api.getProject('p1')).rejects.toBeInstanceOf(AuthUnavailableError)
    expect(getToken()).toBeNull() // the dead token is not left behind
  })
})
