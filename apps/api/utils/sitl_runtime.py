"""Runtime selection helpers for PX4/Gazebo SITL."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from pathlib import PureWindowsPath

from config.env_names import getenv as env_getenv
from config.settings import settings

_SUPPORTED_RUNTIMES = {"auto", "native", "wsl", "mock", "docker"}
_IGNORED_WSL_DISTROS = {"docker-desktop", "docker-desktop-data"}
_CACHE_ENV = "SITL_CACHE"
_FORCE_ENV = "FORCE_SITL"
_REQUIRE_REAL_ENV = "DRONEPROMPT_REQUIRE_REAL_SITL"
_SKIP_WSL_EXEC_PROBE_ENV = "SKIP_WSL_EXEC_PROBE"
_SKIP_DOCKER_PROBE_ENV = "SKIP_DOCKER_PROBE"
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SITLRuntime:
    """Resolved runtime for launching PX4/Gazebo."""

    mode: str
    host_platform: str
    is_real: bool
    diagnostics: tuple[str, ...] = ()
    px4_dir: str = ""
    worlds_path: str = ""
    wsl_distro: str = ""
    wsl_user: str = ""
    wsl_python_bin: str = "python3"
    wsl_gz_bin: str = "gz"

    def wsl_prefix(self) -> list[str]:
        """Return the base `wsl.exe` prefix for this runtime."""
        if self.mode != "wsl" or not self.wsl_distro:
            raise RuntimeError("WSL prefix requested for a non-WSL runtime")
        prefix = ["wsl.exe", "-d", self.wsl_distro]
        if self.wsl_user:
            prefix.extend(["-u", self.wsl_user])
        prefix.append("--")
        return prefix


def require_real_sitl() -> bool:
    """Return True when the caller explicitly forbids mock SITL fallback."""
    raw_value = os.getenv(_REQUIRE_REAL_ENV)
    if raw_value is not None:
        return raw_value.strip().lower() in _TRUE_VALUES
    return bool(getattr(settings, "sitl_require_real", False))


def _force_runtime_setting() -> str:
    forced = _prefixed_override(
        _FORCE_ENV,
        getattr(settings, "sitl_force_runtime", ""),
    ).lower()
    return forced if forced in _SUPPORTED_RUNTIMES else ""


def _mock_runtime_or_raise(
    host_platform: str, diagnostics: list[str] | tuple[str, ...]
) -> SITLRuntime:
    diagnostic_items = tuple(item for item in diagnostics if item)
    if require_real_sitl():
        detail = "; ".join(diagnostic_items) or "No diagnostic detail was provided."
        raise RuntimeError(
            "Real PX4/Gazebo SITL was required by DRONEPROMPT_REQUIRE_REAL_SITL=true "
            f"but is unavailable. {detail}"
        )
    return SITLRuntime(
        mode="mock",
        host_platform=host_platform,
        is_real=False,
        diagnostics=diagnostic_items,
    )


def resolve_sitl_runtime() -> SITLRuntime:
    """Resolve the most appropriate SITL runtime for this host."""
    requested = _force_runtime_setting() or _runtime_setting()
    host_platform = platform.system()

    if requested == "mock":
        return _mock_runtime_or_raise(
            host_platform,
            ("SITL runtime explicitly set to mock.",),
        )

    if requested == "docker":
        image, probe_error = _probe_docker_sim_image()
        if image:
            from services.docker_sim import CONTAINER_PX4_DIR, CONTAINER_WORLDS

            return SITLRuntime(
                mode="docker",
                host_platform=host_platform,
                is_real=True,
                px4_dir=CONTAINER_PX4_DIR,
                worlds_path=CONTAINER_WORLDS,
                diagnostics=(f"Docker SITL: one simulation container per run from {image}",),
            )
        diagnostics = ["Docker SITL was requested but the simulation image is not available."]
        if probe_error:
            diagnostics.append(probe_error)
        diagnostics.append(
            "Build it with `docker build -f infrastructure/Dockerfile.sim -t "
            f"{settings.sitl_docker_image} .` or set ELJAZARI_SITL_DOCKER_IMAGE."
        )
        return _mock_runtime_or_raise(host_platform, diagnostics)

    if host_platform == "Linux":
        return SITLRuntime(
            mode="native",
            host_platform=host_platform,
            is_real=True,
            px4_dir=_string_override("PX4_DIR", settings.sitl_native_px4_dir),
            worlds_path=_string_override("GAZEBO_WORLDS_PATH", settings.sitl_native_worlds_path),
        )

    if host_platform == "Windows" and requested in {"auto", "wsl"}:
        configured_distro = _prefixed_override("WSL_DISTRO", settings.sitl_wsl_distro)
        diagnostics: list[str] = []
        forced = _force_runtime_setting()
        if forced == "wsl":
            cached_distros = _read_cached_wsl_distros()
            distro = configured_distro or _first_usable_wsl_distro(cached_distros) or "Ubuntu"
            diagnostics.append("DRONEPROMPT_FORCE_SITL=wsl set; skipping wsl.exe inspection.")
            return _wsl_runtime_if_accessible(host_platform, distro, diagnostics)

        distro_names, error = _list_wsl_distros()
        if error:
            cached_distros = _read_cached_wsl_distros()
            distro = (
                configured_distro
                if configured_distro and configured_distro in cached_distros
                else _first_usable_wsl_distro(cached_distros)
            )
            diagnostics.append(error)
            if distro:
                diagnostics.append(
                    "Using cached WSL distro because live wsl.exe inspection failed."
                )
                return _wsl_runtime_if_accessible(host_platform, distro, diagnostics)

            diagnostics.append(
                "WSL could not be inspected, so real SITL is unavailable in this session."
            )
            return _mock_runtime_or_raise(host_platform, diagnostics)
        if configured_distro and configured_distro not in distro_names:
            diagnostics.append(f"Configured WSL distro '{configured_distro}' was not found.")
            return _mock_runtime_or_raise(host_platform, diagnostics)
        distro = configured_distro or _first_usable_wsl_distro(distro_names)
        if not distro:
            if distro_names:
                diagnostics.append(
                    "No usable WSL distro was found. Install Ubuntu or set DRONEPROMPT_WSL_DISTRO."
                )
            else:
                diagnostics.append(
                    "No usable WSL distro was detected. Install Ubuntu and PX4/Gazebo inside it."
                )
            return _mock_runtime_or_raise(host_platform, diagnostics)
        _write_cached_wsl_distros(distro_names)
        return _wsl_runtime_if_accessible(host_platform, distro, diagnostics)

    diagnostics = [f"Runtime '{requested}' is not available on host platform '{host_platform}'."]
    if host_platform == "Windows":
        diagnostics.append(
            "Use DRONEPROMPT_SITL_RUNTIME=wsl after installing an Ubuntu WSL distro."
        )
    return _mock_runtime_or_raise(host_platform, diagnostics)


def windows_to_wsl_path(path: str) -> str:
    """Convert a Windows path to a WSL `/mnt/<drive>/...` path."""
    if not path:
        return path
    normalized = path.replace("\\", "/")
    if normalized.startswith("/"):
        return normalized
    pure = PureWindowsPath(path)
    drive = pure.drive.rstrip(":").lower()
    if not drive:
        return normalized
    parts = list(pure.parts)
    if parts and parts[0].rstrip("\\/") == pure.drive:
        parts = parts[1:]
    parts = [part.strip("\\/") for part in parts if part.strip("\\/")]
    if not parts:
        return f"/mnt/{drive}"
    return f"/mnt/{drive}/" + "/".join(parts)


def _runtime_setting() -> str:
    requested = _prefixed_override("SITL_RUNTIME", settings.sitl_runtime).lower()
    if requested not in _SUPPORTED_RUNTIMES:
        return "auto"
    return requested


def _wsl_runtime(
    host_platform: str,
    distro: str,
    diagnostics: list[str] | tuple[str, ...] = (),
) -> SITLRuntime:
    return SITLRuntime(
        mode="wsl",
        host_platform=host_platform,
        is_real=True,
        diagnostics=tuple(diagnostics),
        px4_dir=_prefixed_override("WSL_PX4_DIR", settings.sitl_wsl_px4_dir),
        worlds_path=_prefixed_override(
            "WSL_WORLDS_PATH",
            settings.sitl_wsl_worlds_path,
        ),
        wsl_distro=distro,
        wsl_user=_prefixed_override("WSL_USER", settings.sitl_wsl_user),
        wsl_python_bin=_prefixed_override(
            "WSL_PYTHON_BIN",
            settings.sitl_wsl_python_bin,
        ),
        wsl_gz_bin=_prefixed_override(
            "WSL_GZ_BIN",
            settings.sitl_wsl_gz_bin,
        ),
    )


def _wsl_runtime_if_accessible(
    host_platform: str,
    distro: str,
    diagnostics: list[str],
) -> SITLRuntime:
    probe_user = _prefixed_override("WSL_USER", settings.sitl_wsl_user)
    probe_error = _probe_wsl_execution(distro, probe_user)
    if probe_error:
        diagnostics.append(probe_error)
        diagnostics.append("WSL distro was detected, but this process cannot execute WSL commands.")
        return _mock_runtime_or_raise(host_platform, diagnostics)
    return _wsl_runtime(host_platform, distro, diagnostics)


def _string_override(env_name: str, fallback: str) -> str:
    """Read an environment variable by its exact name."""
    try:
        value = os.getenv(env_name)
    except Exception:
        value = None
    return (value or fallback or "").strip()


def _prefixed_override(env_suffix: str, fallback: str) -> str:
    """Read ELJAZARI_<suffix>, accepting the legacy DRONEPROMPT_<suffix> spelling."""
    try:
        value = env_getenv(env_suffix)
    except Exception:
        value = None
    return (value or fallback or "").strip()


def _probe_docker_sim_image() -> tuple[str | None, str | None]:
    """Return (image, error): the simulation image the docker runtime starts per run.

    Honours ``ELJAZARI_SKIP_DOCKER_PROBE=1`` for hosts where the image is pulled
    lazily; the first run then fails if it is missing.
    """
    image = settings.sitl_docker_image
    skip = _prefixed_override(_SKIP_DOCKER_PROBE_ENV, "").lower()
    if skip in {"1", "true", "yes", "on"}:
        return (image, None)
    if not shutil.which("docker"):
        return (None, "docker CLI was not found on PATH.")
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return (None, f"Failed to run docker image inspect: {exc}")
    if proc.returncode != 0:
        return (None, (proc.stderr or "").strip() or f"image {image} not found")
    return (image, None)


def _probe_wsl_execution(distro: str, user: str = "") -> str | None:
    skip_probe = _prefixed_override(_SKIP_WSL_EXEC_PROBE_ENV, "").lower()
    if skip_probe in {"1", "true", "yes", "on"}:
        return None
    if not shutil.which("wsl.exe"):
        return "wsl.exe was not found on PATH."

    cmd = ["wsl.exe", "-d", distro]
    if user:
        cmd.extend(["-u", user])
    cmd.extend(["--", "sh", "-lc", "printf DRONEPROMPT_WSL_OK"])
    # A cold WSL distro can take 10-20 s to boot on its first command, so a
    # single short probe produces false negatives that silently drop the run to
    # mock telemetry. Retry a few times with a generous timeout: the first
    # attempt warms the distro and a later one succeeds. This removes the need
    # for DRONEPROMPT_SKIP_WSL_EXEC_PROBE in normal use.
    last_detail = ""
    for _attempt in range(3):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            last_detail = "timed out (distro may still be booting)"
            continue
        except Exception as exc:
            return f"Failed to execute WSL distro '{distro}': {exc}"

        stdout_cleaned = (proc.stdout or "").replace("\x00", "")
        stderr_cleaned = (proc.stderr or "").replace("\x00", "")
        if proc.returncode == 0 and "DRONEPROMPT_WSL_OK" in stdout_cleaned:
            return None
        last_detail = (
            stderr_cleaned.strip() or stdout_cleaned.strip() or f"exit code {proc.returncode}"
        )

    return f"wsl.exe could not execute distro '{distro}' after 3 attempts: {last_detail}"


def _cache_path() -> Path:
    override = _prefixed_override(_CACHE_ENV, "")
    if override:
        return Path(override)
    return Path.home() / ".dronekit" / "sitl_runtime.json"


def _read_cached_wsl_distros() -> list[str]:
    try:
        path = _cache_path()
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        distros = data.get("distros", [])
        if not isinstance(distros, list):
            return []
        return [str(item).strip() for item in distros if str(item).strip()]
    except Exception:
        return []


def _write_cached_wsl_distros(distros: list[str]) -> None:
    usable = [distro for distro in distros if distro.lower() not in _IGNORED_WSL_DISTROS]
    if not usable:
        return
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"distros": usable}, indent=2), encoding="utf-8")
    except Exception:
        return


def _list_wsl_distros() -> tuple[list[str], str | None]:
    if not shutil.which("wsl.exe"):
        return [], "wsl.exe was not found on PATH."
    try:
        proc = subprocess.run(
            ["wsl.exe", "-l", "-q"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return [], f"Failed to inspect WSL distros: {exc}"

    stdout_cleaned = (proc.stdout or "").replace("\x00", "")
    stderr_cleaned = (proc.stderr or "").replace("\x00", "")
    lines = [line.strip(" *\r\n\t") for line in stdout_cleaned.splitlines()]
    distros = [line for line in lines if line and line.upper() != "NAME"]
    if proc.returncode != 0:
        error = stderr_cleaned.strip() or stdout_cleaned.strip()
        if not error:
            error = f"wsl.exe returned exit code {proc.returncode}"
        return [], error
    return distros, None


def _first_usable_wsl_distro(distros: list[str]) -> str:
    for distro in distros:
        if distro.lower() not in _IGNORED_WSL_DISTROS:
            return distro
    return ""
