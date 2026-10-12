"""eljazari.ai — Socket.io server instance (shared singleton)

Usage in routers / services:
    from ws.socket_manager import sio
    await sio.emit('phase_changed', data, room=f'project:{project_id}')
"""

import asyncio
import logging

try:
    import socketio

    _SOCKETIO_AVAILABLE = True
except ImportError:
    _SOCKETIO_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Socket.io async server ────────────────────────────────────────────────────


def _client_manager():
    """Redis-backed manager when configured, so every worker delivers every event."""
    from config.settings import settings

    url = settings.socketio_message_queue.strip()
    if not url:
        return None
    logger.info("Socket.IO message queue enabled")
    return socketio.AsyncRedisManager(url)


if _SOCKETIO_AVAILABLE:
    sio: socketio.AsyncServer = socketio.AsyncServer(
        client_manager=_client_manager(),
        async_mode="asgi",
        cors_allowed_origins="*",  # locked down to settings.cors_origins in production
        logger=False,
        engineio_logger=False,
        ping_timeout=60,
        ping_interval=25,
    )
else:
    # Stub so imports don't break when python-socketio is not installed
    class _StubSio:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self._sessions: dict[str, dict] = {}

        async def emit(self, *_a, **_kw) -> None: ...
        async def enter_room(self, *_a, **_kw) -> None: ...
        async def leave_room(self, *_a, **_kw) -> None: ...
        async def save_session(self, sid: str, session: dict) -> None:
            self._sessions[sid] = dict(session)

        async def get_session(self, sid: str) -> dict:
            return dict(self._sessions.get(sid, {}))

        def on(self, *_a, **_kw):
            def decorator(f):
                return f

            return decorator

    sio = _StubSio()  # type: ignore[assignment]
    logger.warning("python-socketio not installed — WebSocket disabled")


# ── Room helpers ──────────────────────────────────────────────────────────────


def project_room(project_id: str) -> str:
    """Room name for a project (all tabs watching the same project share it)."""
    return f"project:{project_id}"


# The loop that owns the server's connections (set at startup). Pipelines run on
# worker threads with their own loops; emitting from those loops touched this
# loop's transports from another thread.
_SERVER_LOOP: asyncio.AbstractEventLoop | None = None


def bind_server_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _SERVER_LOOP
    _SERVER_LOOP = loop


async def broadcast_to_project(project_id: str, event: str, data: dict) -> None:
    """Emit *event* with *data* to every socket in the project room."""
    room = project_room(project_id)
    loop = _SERVER_LOOP
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if loop is not None and loop.is_running() and current is not loop:
        future = asyncio.run_coroutine_threadsafe(sio.emit(event, data, room=room), loop)
        await asyncio.wrap_future(future)
    else:
        await sio.emit(event, data, room=room)
    logger.debug("WS broadcast project=%s event=%s", project_id, event)
