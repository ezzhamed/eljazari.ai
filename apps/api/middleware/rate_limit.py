"""eljazari.ai — Rate Limiting via SlowAPI"""

import logging
from typing import Callable

from fastapi import FastAPI

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware
    from slowapi.util import get_remote_address

    _SLOWAPI_AVAILABLE = True
except ImportError:
    _SLOWAPI_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Limiter instance (shared across routers) ──────────────────────────────────

if _SLOWAPI_AVAILABLE:
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=["200/minute"],
        headers_enabled=True,  # adds X-RateLimit-* headers
        strategy="fixed-window",
    )
else:
    # Stub limiter when slowapi is not installed
    class _StubLimiter:  # type: ignore[no-redef]
        def limit(self, *_a, **_kw) -> Callable:
            def decorator(f: Callable) -> Callable:
                return f

            return decorator

        def shared_limit(self, *_a, **_kw) -> Callable:
            return self.limit()

    limiter = _StubLimiter()  # type: ignore[assignment]
    logger.warning("slowapi not installed — rate limiting disabled")


def setup_rate_limiting(app: FastAPI) -> None:
    """Register the rate-limit middleware and exception handler on *app*."""
    if not _SLOWAPI_AVAILABLE:
        return

    app.state.limiter = limiter
    app.add_exception_handler(
        RateLimitExceeded,
        _rate_limit_exceeded_handler,  # type: ignore[arg-type]
    )
    app.add_middleware(SlowAPIMiddleware)
    logger.info("Rate limiting enabled (200 req/min default)")


# ── Per-endpoint limit decorators (import these in routers) ──────────────────

# Projects: 30 creates per minute per IP
projects_create_limit = limiter.limit("30/minute")

# Pipeline: 10 runs per minute per IP
pipeline_run_limit = limiter.limit("10/minute")

# Simulation: 5 starts per minute
simulation_start_limit = limiter.limit("5/minute")

# File downloads: 20 per minute
file_download_limit = limiter.limit("20/minute")

# Dev token issuance: 10 per minute. The endpoint is disabled in production,
# but an unthrottled token mint is worth capping wherever it does run.
auth_token_limit = limiter.limit("10/minute")
