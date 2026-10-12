"""Tests for Socket.io connection auth (ws/events.py)

The handshake and the REST middleware must agree on when an unauthenticated
caller is allowed in. They previously did not: REST consulted
`settings.anonymous_auth_enabled` (which honours `ALLOW_ANONYMOUS_AUTH`) while
the socket consulted `settings.debug`. With `DEBUG=true` and
`ALLOW_ANONYMOUS_AUTH=false`, every REST route rejected anonymous callers while
the socket still accepted them and handed out the shared anonymous identity.

Coverage:
- A valid token is accepted and its subject stored on the session
- An invalid token is rejected regardless of anonymous settings
- Tokenless connections follow anonymous_auth_enabled, not debug
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest

from middleware.auth import ANONYMOUS_USER_ID, create_access_token
from ws import events


@pytest.fixture
def saved_session():
    """Capture what on_connect stores for the socket."""
    with patch.object(events.sio, "save_session", new_callable=AsyncMock) as mock:
        yield mock


def _anonymous_auth(enabled: bool):
    from config.settings import Settings

    return patch.object(
        Settings,
        "anonymous_auth_enabled",
        new_callable=PropertyMock,
        return_value=enabled,
    )


class TestTokenAuth:
    @pytest.mark.asyncio
    async def test_valid_token_is_accepted(self, saved_session):
        user_id = str(uuid.uuid4())
        token = create_access_token(user_id=user_id)

        result = await events.on_connect("sid1", {}, {"token": token})

        assert result is True
        saved_session.assert_awaited_once_with("sid1", {"user_id": user_id})

    @pytest.mark.asyncio
    async def test_invalid_token_is_rejected(self, saved_session):
        with _anonymous_auth(True):
            result = await events.on_connect("sid2", {}, {"token": "garbage"})

        assert result is False
        saved_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_token_never_falls_back_to_anonymous(self, saved_session):
        """A bad token must fail closed, not degrade to the anonymous identity."""
        with _anonymous_auth(True):
            result = await events.on_connect("sid3", {}, {"token": "garbage"})

        assert result is False


class TestAnonymousHandshake:
    @pytest.mark.asyncio
    async def test_tokenless_connection_allowed_when_anonymous_enabled(self, saved_session):
        with _anonymous_auth(True):
            result = await events.on_connect("sid4", {}, None)

        assert result is True
        saved_session.assert_awaited_once_with("sid4", {"user_id": ANONYMOUS_USER_ID})

    @pytest.mark.asyncio
    async def test_tokenless_connection_rejected_when_anonymous_disabled(self, saved_session):
        """Regression: the socket used to gate on settings.debug.

        DEBUG=true is forced here on purpose — that is the combination that was
        broken. With ALLOW_ANONYMOUS_AUTH=false locking down every REST route,
        the old `elif settings.debug` branch still admitted the socket and
        handed it the shared anonymous identity. Without pinning debug=True this
        test passes against the buggy code too, since debug is false by default.
        """
        with _anonymous_auth(False), patch.object(events.settings, "debug", True):
            assert events.settings.debug is True  # the dangerous combination
            result = await events.on_connect("sid5", {}, None)

        assert result is False
        saved_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_auth_dict_is_treated_as_tokenless(self, saved_session):
        with _anonymous_auth(False):
            result = await events.on_connect("sid6", {}, {})

        assert result is False
