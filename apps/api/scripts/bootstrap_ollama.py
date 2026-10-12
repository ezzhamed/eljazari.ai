from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from config.settings import settings


def _request_json(base_url: str, path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = None
    headers = {"Content-Type": "application/json"}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        method = "POST"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


import shutil
import subprocess


def wait_for_ollama(base_url: str, wait_seconds: int) -> None:
    # Try once
    try:
        _request_json(base_url, "/api/tags", timeout=3)
        return
    except Exception:
        # Not running. Let's try to start it!
        print("[ollama] Ollama is not running. Attempting to start 'ollama serve' automatically...")
        if shutil.which("ollama"):
            try:
                # Start 'ollama serve' in background
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True if sys.platform != "win32" else False,
                )
                print("[ollama] Launched 'ollama serve' background process.")
            except Exception as e:
                print(f"[ollama] Failed to launch 'ollama serve': {e}", file=sys.stderr)
        else:
            print(
                "[ollama] 'ollama' executable not found on PATH. Please install Ollama.",
                file=sys.stderr,
            )

    deadline = time.time() + wait_seconds
    last_error: str | None = None
    while time.time() < deadline:
        try:
            _request_json(base_url, "/api/tags", timeout=5)
            return
        except Exception as exc:  # pragma: no cover - network timing
            last_error = str(exc)
            time.sleep(2)
    raise RuntimeError(f"Ollama not ready after {wait_seconds}s: {last_error}")


def ensure_models(base_url: str, models: list[str], pull_missing: bool) -> int:
    tags = _request_json(base_url, "/api/tags")
    installed = {item.get("name") for item in tags.get("models", [])}
    missing = [model for model in models if model not in installed]

    if not missing:
        print(f"[ollama] all required models are available: {', '.join(models)}")
        return 0

    if not pull_missing:
        print(f"[ollama] missing models: {', '.join(missing)}", file=sys.stderr)
        return 1

    for model in missing:
        print(f"[ollama] pulling {model} ...")
        _request_json(
            base_url, "/api/pull", payload={"model": model, "stream": False}, timeout=1800
        )

    print(f"[ollama] models ready: {', '.join(models)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or pull required Ollama models")
    parser.add_argument("--base-url", default=settings.ollama_base_url)
    parser.add_argument("--pull-missing", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=120)
    parser.add_argument("models", nargs="*", help="Override model list")
    args = parser.parse_args()

    models = args.models or [
        settings.ollama_design_model,
        settings.ollama_default_model,
        settings.ollama_code_model,
        settings.ollama_eval_model,
    ]
    try:
        wait_for_ollama(args.base_url, args.wait_seconds)
        return ensure_models(args.base_url, models, args.pull_missing)
    except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"[ollama] bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
