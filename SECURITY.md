# Security Policy

## Supported Versions

eljazari.ai is alpha software. Security fixes go to the `main` branch and the
latest release only.

| Version                    | Supported |
|----------------------------|-----------|
| Latest release and `main`  | ✅ Yes    |
| Older releases             | ❌ No     |

## Reporting a Vulnerability

If you discover a security vulnerability in eljazari.ai, please report it responsibly:

1. **Do NOT** open a public GitHub issue.
2. **Report it privately on GitHub**: open the repository's **Security** tab and choose
   **Report a vulnerability** ([direct link](https://github.com/ezzhamed/eljazari.ai/security/advisories/new)).
   Only the maintainers can see the report.
3. Include:
   - A description of the vulnerability
   - Steps to reproduce the issue
   - Potential impact assessment
   - Any suggested fixes (optional)

This is a volunteer-maintained project. We aim to acknowledge a report within **7 days** and to agree on a fix and disclosure date with you after that.

## Known limitation: there is no identity provider

> **eljazari.ai does not authenticate users.** Read this before exposing the API
> to anything but localhost.

The API verifies JWTs, but nothing in this repository *issues* them against a
verified identity. There is no `users` table, no password storage, and no login
flow. A token's `sub` claim scopes requests to a `user_id`; it does not prove
who the caller is.

`POST /api/auth/token` signs a token for whatever `user_id` it is handed, with
no credential check. It is a development convenience and is:

- **disabled automatically** when `ENVIRONMENT=production`
- **rejected at startup** if `ENABLE_DEV_TOKEN_ENDPOINT=true` is set in
  production — the process refuses to boot rather than serve an open token mint
- rate limited to 10 requests/minute wherever it does run

`ALLOW_ANONYMOUS_AUTH=false` closes the anonymous path on both REST and the
Socket.io handshake, but on its own it is **not** sufficient: with the dev token
endpoint reachable, anyone can mint a token for any `user_id` — including one
that already owns projects — and bypass it entirely.

**To deploy this for real**, put a real identity provider in front of the API
(OIDC, SSO, or your own login service) and have it issue the JWTs that
`middleware.auth` verifies, signed with `API_SECRET_KEY`. Treat project
ownership as tenant scoping, not as an access-control boundary you can rely on
before that is in place.

### Wiring the frontend to your identity provider

The web client reads its bearer token from `localStorage['dp_token']`. In
development it fills that key from the dev mint; in production, hand it a token
your provider issued:

```ts
import { setToken, isAuthenticated } from '@/lib/api'

setToken(tokenFromYourIdP)   // must be signed with the API's API_SECRET_KEY
```

If no token is present and the dev mint is disabled, API calls reject with
`AuthUnavailableError` rather than emitting an unauthenticated request — so the
UI can route to sign-in instead of showing a 401 the user cannot act on. The
client probes the mint once per session, not before every request.

## Security Measures

eljazari.ai implements the following security practices:

- **JWT verification** with configurable expiry (issuance is *not* provided —
  see the limitation above)
- **Rate Limiting** via SlowAPI (200 req/min default)
- **Security Headers**: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, `Strict-Transport-Security`
- **CORS** and **Trusted Host** validation
- **Input Validation** via Pydantic models
- **Safe-LLM Supervision Layer** — 3-pillar safety system for all AI-generated drone code
- **Mission sandbox** — generated mission code runs only if it is byte-identical to the
  approved version, inside unprivileged Linux namespaces with no network, no service
  secrets, a read-only filesystem view and no capabilities; the Docker runtime refuses to
  execute it (see `docs/MISSION_SANDBOX.md`)
- **Docker Security**: `no-new-privileges`, `cap_drop: ALL`, read-only filesystems
- **No secrets in source code** — all credentials via environment variables

## Responsible Disclosure

We follow responsible disclosure practices. Security researchers who report valid vulnerabilities will be credited in our release notes (unless they prefer to remain anonymous).
