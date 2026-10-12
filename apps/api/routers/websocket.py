"""eljazari.ai — Legacy WebSocket shim

The real-time layer now lives in ws/socket_manager.py + ws/events.py
(Socket.io via python-socketio).

This module is kept for backward-compatibility if something imports
`routers.websocket.manager`. New code should use ws.socket_manager.broadcast_to_project.
"""

from fastapi import APIRouter

# Re-export the Socket.io broadcast helper so old import paths still work
from ws.socket_manager import broadcast_to_project, sio  # noqa: F401

router = APIRouter()

# No FastAPI routes registered here — Socket.io is mounted at the ASGI level
# in main.py as a separate ASGI app on the path /socket.io/
