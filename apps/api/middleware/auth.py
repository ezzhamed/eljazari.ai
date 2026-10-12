"""eljazari.ai - JWT authentication middleware and dependencies."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    import jwt
    from jwt import InvalidTokenError as JWTError
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("PyJWT[crypto] is required: pip install PyJWT[crypto]") from exc

from config.settings import settings

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
_bearer = HTTPBearer(auto_error=False)

# Identity assigned to unauthenticated callers when anonymous access is enabled.
# Shared with the Socket.io handshake so both transports agree on who "anonymous"
# is — they previously hardcoded the same literal in two places and gated it on
# two different settings.
ANONYMOUS_USER_ID = "00000000-0000-0000-0000-000000000001"


def create_access_token(user_id: str, expires_delta: timedelta | None = None) -> str:
    """Create a signed JWT access token."""
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(hours=settings.access_token_expire_hours)
    )
    payload = {
        "sub": user_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.api_secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT. Raises HTTPException on failure."""
    try:
        payload = jwt.decode(
            token,
            settings.api_secret_key,
            algorithms=[ALGORITHM],
        )
        if payload.get("sub") is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing subject",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


class CurrentUser:
    """Parsed token claims injected into route handlers."""

    def __init__(self, user_id: str, raw: dict) -> None:
        self.user_id = user_id
        self.raw = raw

    @property
    def user_uuid(self) -> uuid.UUID:
        return uuid.UUID(self.user_id)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    """Validate Bearer token and return CurrentUser."""
    if credentials is None:
        if settings.anonymous_auth_enabled:
            return CurrentUser(user_id=ANONYMOUS_USER_ID, raw={})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials)
    return CurrentUser(user_id=payload["sub"], raw=payload)


async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser | None:
    if credentials is None:
        return None
    try:
        payload = decode_token(credentials.credentials)
        return CurrentUser(user_id=payload["sub"], raw=payload)
    except HTTPException:
        return None


AuthUser = Annotated[CurrentUser, Depends(get_current_user)]
