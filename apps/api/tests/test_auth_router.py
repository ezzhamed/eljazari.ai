"""Tests for the development token endpoint (routers/auth.py)

`POST /api/auth/token` signs a JWT for whatever user_id it is given, without
checking any credential. eljazari.ai ships no user store, so this endpoint is a
development convenience and must not be reachable in production — otherwise
`ALLOW_ANONYMOUS_AUTH=false` is meaningless, because anyone can mint a token for
any identity, including one that already owns projects.

Coverage:
- Serves tokens when enabled, 404 when disabled
- The 404 does not advertise that the endpoint exists
- A minted token verifies and carries the requested subject
- Invalid UUIDs are rejected
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import PropertyMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.auth import decode_token
from middleware.rate_limit import setup_rate_limiting
from routers import auth as auth_router


@pytest.fixture
def client() -> TestClient:
    """A minimal app with only the auth router — no DB, no lifespan."""
    app = FastAPI()
    setup_rate_limiting(app)
    app.include_router(auth_router.router, prefix="/api/auth")
    return TestClient(app)


@contextmanager
def dev_token_endpoint(enabled: bool):
    """Override the computed setting. It is a property on Settings, not a field."""
    with patch.object(
        type(auth_router.settings),
        "dev_token_endpoint_enabled",
        new_callable=PropertyMock,
        return_value=enabled,
    ):
        yield


class TestEndpointEnabled:
    def test_issues_a_token(self, client: TestClient):
        with dev_token_endpoint(True):
            user_id = str(uuid.uuid4())
            response = client.post("/api/auth/token", json={"user_id": user_id})

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["user_id"] == user_id
        assert decode_token(body["access_token"])["sub"] == user_id

    def test_generates_a_user_id_when_omitted(self, client: TestClient):
        with dev_token_endpoint(True):
            response = client.post("/api/auth/token", json={})

        assert response.status_code == 200
        uuid.UUID(response.json()["user_id"])  # raises if not a UUID

    def test_rejects_a_non_uuid_user_id(self, client: TestClient):
        with dev_token_endpoint(True):
            response = client.post("/api/auth/token", json={"user_id": "not-a-uuid"})

        assert response.status_code == 422


class TestEndpointDisabled:
    """The production posture: the open token mint must be unreachable."""

    def test_returns_404_when_disabled(self, client: TestClient):
        with dev_token_endpoint(False):
            response = client.post("/api/auth/token", json={"user_id": str(uuid.uuid4())})

        assert response.status_code == 404

    def test_404_does_not_reveal_the_endpoint(self, client: TestClient):
        """A disabled mint should look absent, not merely switched off."""
        with dev_token_endpoint(False):
            response = client.post("/api/auth/token", json={})

        assert response.json() == {"detail": "Not Found"}

    def test_issues_no_token_when_disabled(self, client: TestClient):
        with dev_token_endpoint(False):
            response = client.post("/api/auth/token", json={"user_id": str(uuid.uuid4())})

        assert "access_token" not in response.text
