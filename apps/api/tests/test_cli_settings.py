"""The CLI must take its models from `.env` like the API does (clean-clone reproduction)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]


def test_importing_the_cli_does_not_override_env_file_settings() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("OLLAMA_", "AIRCRAFTVERSE_", "DRONEPROMPT_OLLAMA", "ELJAZARI_OLLAMA")
        )
    }
    probe = (
        "import os, cli\n"
        "print(sorted(k for k in os.environ if k.startswith(('OLLAMA_', 'AIRCRAFTVERSE_'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == "[]", result.stdout
