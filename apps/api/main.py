"""eljazari.ai - FastAPI Application Entry Point"""

import asyncio
import logging
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from config.logging import setup_logging
from config.settings import settings

setup_logging()
logger = logging.getLogger(__name__)


def _build_allowed_hosts() -> list[str]:
    hosts = [host.strip() for host in settings.trusted_hosts if host.strip()]
    if not settings.is_production and "testserver" not in hosts:
        hosts.append("testserver")
    return hosts


def ensure_docker_services():
    api_dir = Path(__file__).parent
    root_dir = api_dir.parent.parent

    compose_path = root_dir / "infrastructure" / "docker-compose.dev.yml"
    if not compose_path.exists():
        compose_path = root_dir / "infrastructure" / "docker-compose.yml"

    if not compose_path.exists():
        logger.warning("Docker compose file not found at %s", compose_path)
        return

    if not shutil.which("docker"):
        logger.warning("Docker executable not found on PATH. Cannot auto-start services.")
        return

    try:
        subprocess.run(
            ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )
    except Exception:
        logger.warning("Docker daemon is not running. Please start Docker Desktop.", exc_info=True)
        return

    logger.info("Attempting to auto-start Docker services: postgres, redis...")
    try:
        # We start postgres and redis. If already running, docker compose up is a no-op.
        cmd = ["docker", "compose", "-f", str(compose_path), "up", "-d", "postgres", "redis"]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        logger.info("Docker services checked/started successfully.")
    except Exception as e:
        logger.warning("Failed to start Docker services: %s", e, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("eljazari.ai API starting up")

    if settings.auto_start_docker_services:
        ensure_docker_services()

    dataset_path = settings.resolved_aircraftverse_path
    if settings.aircraftverse_require_real and not dataset_path.exists():
        raise RuntimeError(f"AircraftVerse dataset is required but missing: {dataset_path}")
    if dataset_path.exists():
        logger.info("AircraftVerse dataset detected at %s", dataset_path)
    else:
        logger.warning("AircraftVerse dataset not found at %s", dataset_path)

    from db.session import engine
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Database connection OK")
    except Exception as exc:
        logger.warning("Database not reachable at startup: %s", exc)

    if settings.resolved_checkpoint_backend == "postgres":
        from graph.workflow import get_workflow_async

        try:
            async with get_workflow_async(setup=True):
                logger.info("Durable PostgreSQL checkpoints ready")
        except Exception:
            await engine.dispose()
            raise
    else:
        logger.warning("Memory checkpoints enabled: state will be lost on process restart")

    # Importing these registers their @sio.on handlers as a side effect.
    # gcs_events was missing entirely, which is why every gcs:* event the
    # frontend sent was silently discarded.
    import ws.events  # noqa: F401
    import ws.gcs_events  # noqa: F401
    from ws.socket_manager import bind_server_loop

    logger.info("Socket.io event handlers registered (pipeline + GCS)")
    # Pipelines emit from worker threads; route their events through this loop.
    bind_server_loop(asyncio.get_running_loop())

    yield

    bind_server_loop(None)

    from db.session import engine as _engine

    await _engine.dispose()
    logger.info("eljazari.ai API shutting down")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0-alpha.1",
    description=(
        "Build and test a drone from a prompt - multi-agent drone design and simulation platform.\n\n"
        "Auth: POST `/api/auth/token` -> `{ access_token }` -> "
        "`Authorization: Bearer <token>`\n\n"
        "WebSocket: connect via Socket.io to `/socket.io/`, emit `join_project` "
        "with `{ project_id }` to subscribe."
    ),
    lifespan=lifespan,
    docs_url="/docs" if settings.enable_api_docs else None,
    redoc_url="/redoc" if settings.enable_api_docs else None,
    openapi_url="/openapi.json" if settings.enable_api_docs else None,
)

if settings.enable_https_redirect:
    app.add_middleware(HTTPSRedirectMiddleware)

allowed_hosts = _build_allowed_hosts()
if allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

from middleware.rate_limit import setup_rate_limiting

setup_rate_limiting(app)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=()",
    )
    if settings.enable_https_redirect:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=63072000; includeSubDomains; preload",
        )
    return response


from fastapi.responses import JSONResponse  # noqa: E402
from sqlalchemy.exc import DBAPIError, OperationalError  # noqa: E402


@app.exception_handler(OperationalError)
@app.exception_handler(DBAPIError)
@app.exception_handler(ConnectionRefusedError)
async def database_unavailable(request: Request, exc: Exception):
    """Answer a lost database with a 503 the browser can actually read.

    An unhandled exception escapes every middleware, CORS included, so the
    frontend saw only a blocked cross-origin request and rendered the browser's
    own "Failed to fetch" with no idea why. This turns it into a normal
    response with a message that says what to start.
    """
    logger.error("Database unavailable on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": (
                "The database is not reachable. Start Postgres "
                "(infrastructure/dev-services.ps1 on Windows, or docker compose up) "
                "and try again."
            ),
            "code": "database_unavailable",
        },
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    response = JSONResponse(
        status_code=500,
        content={"detail": "Internal server error.", "code": "internal_error"},
    )
    # Starlette runs the catch-all handler outside the middleware stack, so
    # CORSMiddleware never sees this response; add the header ourselves or the
    # dashboard cannot read the error it is being sent.
    origin = request.headers.get("origin")
    if origin and (origin in settings.cors_origins or "*" in settings.cors_origins):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    return response


from routers import agents, auth, code, design, files, monitor, projects, simulation  # noqa: E402

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(design.router, prefix="/api/projects", tags=["design"])
app.include_router(code.router, prefix="/api/projects", tags=["code"])
app.include_router(simulation.router, prefix="/api/projects", tags=["simulation"])
app.include_router(monitor.router, prefix="/api/projects", tags=["monitor"])
app.include_router(files.router, prefix="/api/projects", tags=["files"])


@app.get("/health", tags=["health"])
async def health():
    return {
        "status": "ok",
        "service": "eljazari-api",
        "version": app.version,
        "environment": settings.environment,
        "dataset": "real" if settings.resolved_aircraftverse_path.exists() else "fallback",
    }


try:
    import socketio
    from ws.socket_manager import sio

    socket_app = socketio.ASGIApp(
        socketio_server=sio,
        other_asgi_app=app,
        socketio_path="/socket.io",
    )
    logger.info("Socket.io ASGI app mounted at /socket.io/")
except ImportError:
    socket_app = app  # type: ignore[assignment]
    logger.warning(
        "python-socketio not installed - run: pip install python-socketio[asyncio_client]"
    )
