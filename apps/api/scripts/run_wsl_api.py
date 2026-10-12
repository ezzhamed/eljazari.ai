"""Run the API locally on Linux/WSL with durable checkpoints and one worker.

Use the dedicated Linux virtual environment. Existing .env credentials are
loaded by Settings; optional .env.wsl.local contains machine-specific overrides.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

API_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = API_ROOT.parents[1]


def configure_environment() -> None:
    if sys.platform != "linux":
        raise RuntimeError("Run this launcher with Linux Python inside WSL")
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env.wsl.local", override=False)
    defaults = {
        "CHECKPOINT_BACKEND": "postgres",
        "AUTO_START_DOCKER_SERVICES": "false",
        "STORAGE_PATH": str(PROJECT_ROOT / "data" / "storage"),
        "AIRCRAFTVERSE_PATH": str(PROJECT_ROOT / "data" / "aircraftverse" / "designs.json"),
        "AIRCRAFTVERSE_CHROMA_PATH": str(PROJECT_ROOT / "data" / "storage" / "chroma-wsl-0.6.3"),
        "ELJAZARI_FORCE_SITL": "native",
        "ELJAZARI_SITL_RUNTIME": "native",
        "ELJAZARI_REQUIRE_REAL_SITL": "true",
        "ELJAZARI_NATIVE_PX4_DIR": str(Path.home() / "PX4-Autopilot"),
        "ELJAZARI_NATIVE_WORLDS_PATH": str(
            Path.home() / "PX4-Autopilot" / "Tools" / "simulation" / "gz" / "worlds"
        ),
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    sys.path.insert(0, str(API_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="More than one also enables the Redis Socket.IO queue (SOCKETIO_MESSAGE_QUEUE)",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    os.environ.setdefault("ELJAZARI_API_WORKERS", str(args.workers))
    if args.workers > 1:
        os.environ.setdefault("ELJAZARI_SOCKETIO_MESSAGE_QUEUE", "redis://localhost:6379/3")
    configure_environment()
    import uvicorn

    uvicorn.run("main:socket_app", host="127.0.0.1", port=args.port, workers=args.workers)


if __name__ == "__main__":
    main()
