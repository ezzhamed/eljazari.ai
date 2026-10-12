/**
 * eljazari.ai — frontend runtime configuration
 *
 * Single source of truth for backend URLs. These defaults were previously
 * duplicated across lib/api.ts, lib/stores/ws-store.ts and
 * app/api/agents/route.ts, and drifted: two of them pointed at port 8010 while
 * the README, docker-compose and .env.example all use 8000. The drift was
 * invisible to anyone with an apps/web/.env.local pinning 8010, and broke every
 * request for anyone cloning the repo fresh.
 *
 * Next.js inlines NEXT_PUBLIC_* at build time and only reads env files from
 * this directory (apps/web), never from the repository root — so setting these
 * in the root .env has no effect on the frontend. See apps/web/.env.example.
 */

/** Port the FastAPI backend listens on by default (see README quick start). */
const DEFAULT_BACKEND_PORT = 8000
const DEFAULT_BACKEND_ORIGIN = `http://localhost:${DEFAULT_BACKEND_PORT}`

/** Base URL for REST calls to the FastAPI backend. */
export const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? DEFAULT_BACKEND_ORIGIN

/**
 * Base URL for the Socket.io connection.
 *
 * Falls back to the REST origin because the two are the same process — Socket.io
 * is mounted on the FastAPI app at /socket.io. Pass an http(s) origin, not
 * ws(s): the Socket.io client negotiates the upgrade itself.
 */
export const WS_BASE_URL =
  process.env.NEXT_PUBLIC_WS_URL ?? process.env.NEXT_PUBLIC_API_URL ?? DEFAULT_BACKEND_ORIGIN
