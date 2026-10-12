# Production Checklist

## 1. Secrets and environment

- Generate a strong `API_SECRET_KEY` (>= 32 chars, no default in code):
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
- Set `ENVIRONMENT=production`
- Disable anonymous access with `ALLOW_ANONYMOUS_AUTH=false`
- Set `ACCESS_TOKEN_EXPIRE_HOURS=24` or stricter

> **Authentication is not included.** This project has no user store and no
> login flow. `ALLOW_ANONYMOUS_AUTH=false` closes the anonymous path but does
> not give you authentication — you must front the API with an identity
> provider (OIDC/SSO/your own) that issues JWTs signed with `API_SECRET_KEY`.
>
> The development endpoint `POST /api/auth/token` mints a token for any
> `user_id` with no credential check. It is disabled automatically when
> `ENVIRONMENT=production`; setting `ENABLE_DEV_TOKEN_ENDPOINT=true` there makes
> the API refuse to start. Do not work around this. See [SECURITY.md](../SECURITY.md).
- Set explicit `CORS_ORIGINS` — wildcards (`*`) are rejected automatically by the server in production mode
- Set `TRUSTED_HOSTS` (must not be empty in production)
- Set Flower credentials: `FLOWER_USER` and `FLOWER_PASSWORD` (the dashboard is now auth-protected and bound to 127.0.0.1:5555)

## 2. Real AircraftVerse dataset

- Place the real dataset at `data/aircraftverse/designs.json`
- Validate it:

```bash
cd apps/api
python -m scripts.bootstrap_aircraftverse
```

- Enforce it in production:

```bash
AIRCRAFTVERSE_REQUIRE_REAL=true
AIRCRAFTVERSE_RAG_MODE=persistent
```

## 3. Ollama model provisioning

- Start Ollama
- Pull or validate required models:

```bash
cd apps/api
python -m scripts.bootstrap_ollama --pull-missing
```

- Required models default to:
  - `OLLAMA_DEFAULT_MODEL`
  - `OLLAMA_CODE_MODEL`
  - `OLLAMA_EVAL_MODEL`

## 4. Linux simulation stack

- Use Linux host or Linux VM
- Start core services:

```bash
docker compose -f infrastructure/docker-compose.yml up -d postgres redis ollama api celery_worker web
```

- Start the Linux simulation profile:

```bash
docker compose -f infrastructure/docker-compose.yml --profile simulation up -d simulation
```

## 5. Optional bootstrap profile

- Restore dataset from backup when available:

```bash
docker compose -f infrastructure/docker-compose.yml --profile bootstrap run --rm dataset-bootstrap
```

- Pull Ollama models from inside the stack:

```bash
docker compose -f infrastructure/docker-compose.yml --profile bootstrap run --rm ollama-bootstrap
```

## 6. Reverse proxy and TLS

- Default Nginx config is HTTP-only at `infrastructure/nginx/nginx.conf`
- Add certs under `infrastructure/nginx/ssl/`
- After TLS is terminated at Nginx or ingress, enable:

```bash
ENABLE_HTTPS_REDIRECT=true
```

## 7. Final validation

- API health: `GET /health`
- WebSocket path: `/socket.io/`
- Smoke flow:
  - create project
  - approve design
  - approve code
  - confirm simulation and monitor report complete
