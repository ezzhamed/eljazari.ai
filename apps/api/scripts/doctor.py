"""Check that this machine can run a real prompt-to-flight pipeline.

Services, models, the PX4/Gazebo installation, the mission sandbox, disk space
and component versions, each reported as OK / WARN / FAIL. Exit code 1 when
anything FAILs. Run from apps/api on Windows (WSL runtime) or inside Linux/WSL
(native runtime):

    python scripts/doctor.py [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

MIN_FREE_GB = 5.0
Result = tuple[str, str, str]  # (status, check, detail)


def _ok(check: str, detail: str) -> Result:
    return ("OK", check, detail)


def _warn(check: str, detail: str) -> Result:
    return ("WARN", check, detail)


def _fail(check: str, detail: str) -> Result:
    return ("FAIL", check, detail)


def check_python() -> Result:
    version = platform.python_version()
    if sys.version_info < (3, 11):
        return _fail("python", f"{version}; 3.11 or newer is required")
    return _ok("python", version)


def check_settings() -> tuple[Result, object | None]:
    try:
        from config.settings import settings
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return _fail("settings", f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"), None
    detail = (
        f"environment={settings.environment} checkpoints={settings.resolved_checkpoint_backend} "
        f"workers={settings.api_workers}"
    )
    return _ok("settings", detail), settings


async def check_database(settings) -> Result:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            version = (await conn.execute(text("SHOW server_version"))).scalar()
        return _ok("postgresql", f"reachable, server {version}")
    except Exception as exc:  # noqa: BLE001
        detail = f"unreachable: {type(exc).__name__}"
        if settings.resolved_checkpoint_backend == "postgres":
            return _fail("postgresql", detail + " (durable checkpoints need it)")
        return _warn("postgresql", detail + " (in-memory checkpoints in use)")
    finally:
        await engine.dispose()


async def check_redis(settings) -> list[Result]:
    import redis.asyncio as redis

    results = []
    urls = {"redis": settings.redis_url}
    if settings.socketio_message_queue:
        urls["socketio queue"] = settings.socketio_message_queue
    for name, url in urls.items():
        client = redis.from_url(url)
        try:
            await client.ping()
            results.append(_ok(name, "reachable"))
        except Exception as exc:  # noqa: BLE001
            needed = name == "socketio queue" or settings.api_workers > 1
            results.append((_fail if needed else _warn)(name, f"unreachable: {type(exc).__name__}"))
        finally:
            await client.aclose()
    return results


def check_ollama(settings) -> list[Result]:
    import httpx

    try:
        response = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=10)
        response.raise_for_status()
        available = {model["name"] for model in response.json().get("models", [])}
    except Exception as exc:  # noqa: BLE001
        return [_fail("ollama", f"unreachable at {settings.ollama_base_url}: {type(exc).__name__}")]
    results = [_ok("ollama", f"{len(available)} models at {settings.ollama_base_url}")]
    wanted = {
        getattr(settings, key, "")
        for key in ("ollama_design_model", "ollama_code_model", "ollama_eval_model")
    }
    for model in sorted(filter(None, wanted)):
        found = model in available or f"{model}:latest" in available
        results.append(
            (_ok if found else _fail)(f"model {model}", "present" if found else "missing")
        )
    return results


def check_data(settings) -> list[Result]:
    dataset = settings.resolved_aircraftverse_path
    results = [
        (_ok if dataset.exists() else _fail)(
            "aircraftverse", str(dataset) if dataset.exists() else f"missing: {dataset}"
        )
    ]
    storage = Path(settings.storage_path)
    storage.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(storage).free / 1e9
    results.append(
        (_ok if free_gb >= MIN_FREE_GB else _warn)("disk", f"{free_gb:.1f} GB free at {storage}")
    )
    return results


def _run(runtime, command: str, timeout: float = 60) -> tuple[int, str]:
    """Run a shell command where the simulator lives (WSL or this host)."""
    argv = (
        runtime.wsl_prefix() + ["bash", "-lc", command]
        if runtime.mode == "wsl"
        else ["bash", "-lc", command]
    )
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).replace("\x00", "").strip()


def check_simulator() -> list[Result]:
    from utils.sitl_runtime import resolve_sitl_runtime

    try:
        runtime = resolve_sitl_runtime()
    except RuntimeError as exc:
        return [_fail("sitl runtime", str(exc).splitlines()[0][:200])]
    if not runtime.is_real:
        return [_fail("sitl runtime", f"'{runtime.mode}': {'; '.join(runtime.diagnostics)[:200]}")]
    results = [_ok("sitl runtime", f"{runtime.mode} on {runtime.host_platform}")]
    if runtime.mode == "docker":
        # PX4, Gazebo and the mission sandbox are inside the simulation image.
        from config.settings import settings

        image = settings.sitl_docker_image
        try:
            proc = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            found = proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            found = False
        return results + [
            (_ok if found else _fail)(
                "simulation image",
                image
                if found
                else f"{image} not built: python -m scripts.prepare_assets --sim-image",
            )
        ]
    px4 = runtime.px4_dir.rstrip("/")
    code, out = _run(
        runtime,
        f"test -x {shlex.quote(px4)}/build/px4_sitl_default/bin/px4 && "
        f"git -C {shlex.quote(px4)} describe --tags --always 2>/dev/null",
    )
    results.append((_ok if code == 0 else _fail)("px4 sitl build", out or "bin/px4 missing"))
    gz = runtime.wsl_gz_bin if runtime.mode == "wsl" else "gz"
    code, out = _run(runtime, f"{shlex.quote(gz)} sim --versions")
    results.append((_ok if code == 0 else _fail)("gazebo", f"gz sim {out}" if code == 0 else out))
    python = runtime.wsl_python_bin if runtime.mode == "wsl" else sys.executable
    utils_dir = _wsl_path(API_ROOT / "utils") if runtime.mode == "wsl" else str(API_ROOT / "utils")
    probe = (
        "import sys, importlib.metadata as m; "
        f"sys.path.insert(0, {utils_dir!r}); "
        "from mission_sandbox import unsupported_reason; "
        "print(m.version('mavsdk')); print(unsupported_reason() or 'OK')"
    )
    code, out = _run(runtime, f"{shlex.quote(python)} -c {shlex.quote(probe)}")
    lines = out.splitlines()
    if code != 0 or len(lines) < 2:
        results.append(_fail("mission runner", out[-200:] or "python/mavsdk unavailable"))
        return results
    results.append(_ok("mavsdk", f"{lines[-2]} in {python}"))
    sandbox = lines[-1]
    results.append(
        (_ok if sandbox == "OK" else _fail)(
            "mission sandbox", "unprivileged namespaces available" if sandbox == "OK" else sandbox
        )
    )
    return results


def _wsl_path(path: Path) -> str:
    from utils.sitl_runtime import windows_to_wsl_path

    return windows_to_wsl_path(str(path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()
    results: list[Result] = [check_python()]
    settings_result, settings = check_settings()
    results.append(settings_result)
    if settings is not None:
        results.append(asyncio.run(check_database(settings)))
        results.extend(asyncio.run(check_redis(settings)))
        results.extend(check_ollama(settings))
        results.extend(check_data(settings))
    results.extend(check_simulator())
    if args.json:
        print(json.dumps([dict(zip(("status", "check", "detail"), r)) for r in results], indent=2))
    else:
        for status, check, detail in results:
            print(f"[{status:4}] {check:18} {detail}")
    return 1 if any(status == "FAIL" for status, _, _ in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
