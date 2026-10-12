"""eljazari.ai — Auth Router (development token issuance)

> **This is not authentication.** `POST /api/auth/token` signs a JWT for whatever
> `user_id` it is handed, without checking a password, a session, or anything
> else. It exists so local development and tests can obtain a bearer token
> without standing up an identity provider.
>
> eljazari.ai ships **no identity provider and no user store** — there is no
> `users` table. Tokens only scope requests to a `user_id`; they do not prove
> who the caller is. Anyone who can reach this endpoint can mint a token for any
> `user_id`, including one that already owns projects.
>
> The endpoint is therefore disabled whenever `ENVIRONMENT=production`, and the
> application refuses to start if it is explicitly enabled there. To run this in
> production, front the API with a real identity provider (OIDC, SSO, or your
> own login service) and have it issue the JWTs that `middleware.auth` verifies.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from config.settings import settings
from middleware.auth import create_access_token
from middleware.rate_limit import auth_token_limit

router = APIRouter()


class TokenRequest(BaseModel):
    """Development-only token request. Any user_id is accepted."""

    user_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="UUID identifying the caller. Not verified against anything.",
    )


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Issue a development JWT (disabled in production)",
)
@auth_token_limit
async def issue_token(request: Request, response: Response, body: TokenRequest) -> TokenResponse:
    """
    Issue an unauthenticated JWT for local development.

    **Dev usage:** POST /api/auth/token → `{ access_token, user_id }`,
    then send `Authorization: Bearer <token>`.

    Returns 404 when disabled so a production deployment does not advertise
    that an open token mint exists.
    """
    if not settings.dev_token_endpoint_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    user_id = body.user_id.strip() or str(uuid.uuid4())
    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="user_id must be a valid UUID",
        )

    token = create_access_token(user_id=user_id)
    return TokenResponse(access_token=token, user_id=user_id)
