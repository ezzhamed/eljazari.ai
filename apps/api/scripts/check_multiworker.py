"""Verify F09 across real processes with PostgreSQL and Redis (run on Linux/WSL).

1. Two processes race for one project's run lock at the same moment: exactly
   one may own it. Another process can take it after they exit.
2. A process holding the lock is SIGKILLed: PostgreSQL releases the lock with
   its connection, so a new process can own the project.
3. A Socket.IO server in one process delivers, to its connected client, an
   event raised by a different process through the Redis message queue.

Usage from apps/api inside WSL:

    ~/droneprompt-api-venv/bin/python scripts/check_multiworker.py
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
QUEUE_URL = os.environ.get("CHECK_SOCKETIO_QUEUE", "redis://localhost:6379/3")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "CHECKPOINT_BACKEND": "postgres",
            "ELJAZARI_SOCKETIO_MESSAGE_QUEUE": QUEUE_URL,
            "PYTHONPATH": str(API_ROOT),
        }
    )
    return env


def _child(code: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        cwd=API_ROOT,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


HOLD_LOCK = """
import asyncio, sys, time
from services.run_lock import project_run_lock
async def main(project, start_at, hold_s):
    await asyncio.sleep(max(0.0, start_at - time.time()))
    async with project_run_lock(project) as owned:
        print("OWNED" if owned else "REFUSED", flush=True)
        await asyncio.sleep(hold_s if owned else 0)
asyncio.run(main(sys.argv[1], float(sys.argv[2]), float(sys.argv[3])))
"""

SERVER = """
import asyncio, sys
import socketio, uvicorn
from ws.socket_manager import sio
@sio.event
async def connect(sid, environ, auth=None):
    await sio.enter_room(sid, "project:" + sys.argv[2])
config = uvicorn.Config(socketio.ASGIApp(sio), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
asyncio.run(uvicorn.Server(config).serve())
"""

EMITTER = """
import asyncio, sys
from ws.socket_manager import broadcast_to_project
asyncio.run(broadcast_to_project(sys.argv[1], "probe", {"from_pid": int(sys.argv[2])}))
print("EMITTED", flush=True)
"""


def check_concurrent_lock() -> dict:
    project = str(uuid.uuid4())
    start_at = time.time() + 2.0
    racers = [_child(HOLD_LOCK, project, str(start_at), "3") for _ in range(2)]
    outcomes = sorted(p.communicate(timeout=60)[0].strip() for p in racers)
    after = _child(HOLD_LOCK, project, "0", "0").communicate(timeout=60)[0].strip()
    return {
        "racers": outcomes,
        "after_exit": after,
        "ok": outcomes == ["OWNED", "REFUSED"] and after == "OWNED",
    }


def check_crash_releases_lock() -> dict:
    project = str(uuid.uuid4())
    holder = _child(HOLD_LOCK, project, "0", "300")
    assert holder.stdout is not None
    first = holder.stdout.readline().strip()
    blocked = _child(HOLD_LOCK, project, "0", "0").communicate(timeout=60)[0].strip()
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=30)
    time.sleep(1.0)  # the server notices the closed connection
    after = _child(HOLD_LOCK, project, "0", "0").communicate(timeout=60)[0].strip()
    return {
        "holder": first,
        "while_held": blocked,
        "after_kill": after,
        "ok": (first, blocked, after) == ("OWNED", "REFUSED", "OWNED"),
    }


async def check_cross_process_events() -> dict:
    import socketio

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    project = str(uuid.uuid4())
    server = _child(SERVER, str(port), project)
    received: list[dict] = []
    client = socketio.AsyncClient()

    @client.on("probe")
    async def on_probe(data):
        received.append(data)

    try:
        for _ in range(50):
            try:
                await client.connect(f"http://127.0.0.1:{port}", transports=["websocket"])
                break
            except Exception:
                await asyncio.sleep(0.2)
        await asyncio.sleep(0.5)
        emitter = _child(EMITTER, project, str(os.getpid()))
        emitted = emitter.communicate(timeout=60)[0].strip()
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.1)
    finally:
        if client.connected:
            await client.disconnect()
        server.terminate()
        server.wait(timeout=30)
    return {
        "server_pid": server.pid,
        "emitter": emitted,
        "received": received,
        "ok": emitted == "EMITTED" and received == [{"from_pid": os.getpid()}],
    }


def main() -> int:
    if not sys.platform.startswith("linux"):
        print("Run this inside WSL/Linux where PostgreSQL and Redis are local.")
        return 2
    report = {
        "concurrent_lock": check_concurrent_lock(),
        "crash_releases_lock": check_crash_releases_lock(),
        "cross_process_events": asyncio.run(check_cross_process_events()),
    }
    print(json.dumps(report, indent=2))
    return 0 if all(item["ok"] for item in report.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
