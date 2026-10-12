"""Run one generated mission in an unprivileged Linux sandbox.

The mission is untrusted Python. It gets its own user, mount, network, PID,
IPC and UTS namespaces (``unshare``), a fresh root containing only read-only
system directories, the Python environment and the mission itself, a private
``/tmp``, an allow-listed environment and no capabilities. There is no network
namespace route out: the only way to the vehicle is a Unix socket that the
trusted runner relays to ``mavsdk_server``, which talks to the simulator.

Telemetry is collected by the runner through its own MAVSDK connection, so the
mission can neither read service secrets nor forge the flight record.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import shlex
import shutil
import signal
import site
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SANDBOX_GRPC_DIR = "/run/mavsdk"
SANDBOX_MISSION_DIR = "/mission"
# gRPC parses "unix:<path>"; MAVSDK formats "<address>:<port>", so the port
# becomes part of the socket file name inside the sandbox.
SANDBOX_GRPC_ADDRESS = f"unix:{SANDBOX_GRPC_DIR}/grpc"
ISOLATION_LABEL = "linux_namespaces"
_UNSHARE = [
    "unshare",
    "--user",
    "--map-root-user",
    "--mount",
    "--net",
    "--pid",
    "--ipc",
    "--uts",
    "--fork",
    "--kill-child",
]
_REQUIRED_TOOLS = ("unshare", "setpriv", "mount", "umount", "pivot_root", "findmnt")
_ETC_FILES = ("ld.so.cache", "ld.so.conf", "passwd", "group", "nsswitch.conf", "hosts", "localtime")
_TOP_LEVEL_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
_ENV_PASSTHROUGH = ("LANG", "LC_ALL", "TZ")
_TRUSTED_MODULES = ("mission_sandbox_entry.py", "mavsdk_runtime_adapter.py")
_UNIX_PATH_MAX = 108  # sun_path, including the terminating NUL


def unsupported_reason() -> str | None:
    """Why this host cannot sandbox missions, or None when it can."""
    if not sys.platform.startswith("linux"):
        return "Mission sandboxing needs Linux (run the mission runner inside WSL or Linux)."
    missing = [tool for tool in _REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        return f"Mission sandbox tools are missing: {', '.join(missing)} (util-linux)."
    try:
        probe = subprocess.run(
            [*_UNSHARE, "true"], capture_output=True, timeout=15, check=False, text=True
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Unprivileged user namespaces could not be probed: {exc}"
    if probe.returncode != 0:
        detail = (probe.stderr or "").strip()[:200]
        return f"Unprivileged user namespaces are unavailable: {detail or probe.returncode}"
    return None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def python_runtime_dirs() -> list[str]:
    """Directories outside /usr that the interpreter needs (venv, site-packages)."""
    candidates = {sys.prefix, *site.getsitepackages()}
    user_site = site.getusersitepackages()
    if user_site and Path(user_site).is_dir() and site.ENABLE_USER_SITE:
        candidates.add(user_site)
    dirs = []
    for item in sorted(candidates):
        path = Path(item).resolve()
        if path.is_dir() and not str(path).startswith("/usr/"):
            dirs.append(str(Path(item)))
    # Keep the outermost directories only; a bind covers what lies beneath it.
    return [d for d in dirs if not any(d != o and d.startswith(o.rstrip("/") + "/") for o in dirs)]


@dataclass(frozen=True)
class SandboxLayout:
    workdir: Path
    root: Path
    mission_dir: Path
    grpc_dir: Path
    init_script: Path
    mission_sha256: str

    def socket_path(self, grpc_port: int) -> Path:
        return self.grpc_dir / f"grpc:{grpc_port}"


def prepare(workdir: Path, mission_path: Path, expected_sha256: str) -> SandboxLayout:
    """Stage a verified copy of the mission and the trusted bootstrap files."""
    actual = sha256_file(mission_path)
    if actual != expected_sha256:
        raise ValueError("Mission file does not match the approved fingerprint")
    workdir = Path(workdir)
    root, mission_dir, grpc_dir = workdir / "root", workdir / "mission", workdir / "grpc"
    if len(str(grpc_dir / "grpc:65535").encode()) >= _UNIX_PATH_MAX:
        raise ValueError(f"Sandbox directory {workdir} is too deep for a Unix socket path")
    for path in (root, mission_dir, grpc_dir):
        path.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(mission_path, mission_dir / "mission.py")
    here = Path(__file__).resolve().parent
    for name in _TRUSTED_MODULES:
        shutil.copyfile(here / name, mission_dir / name)
    for path in mission_dir.iterdir():
        path.chmod(0o444)
    init_script = workdir / "sandbox_init.sh"
    return SandboxLayout(workdir, root, mission_dir, grpc_dir, init_script, actual)


def _bind_ro(source: str, target: str, *, is_file: bool = False) -> list[str]:
    make = f": > {target}" if is_file else f"mkdir -p {target}"
    # A user namespace may not hide locked submounts (WSL mounts under /usr),
    # so binds are recursive, and each resulting mount is then made read-only
    # individually: only the per-mount flag may be changed from here.
    return [
        make,
        f"mount --rbind {source} {target}",
        f'findmnt -Rrno TARGET {target} | while read -r m; do mount -o remount,bind,ro "$m"; done',
    ]


def init_script_text(
    layout: SandboxLayout,
    *,
    python_exe: str,
    entry_args: list[str],
    runtime_dirs: list[str],
    env: dict[str, str],
) -> str:
    """POSIX sh run as root of the new user namespace, then as nobody-equivalent."""
    q = shlex.quote
    r = q(str(layout.root))
    lines = [
        "set -eu",
        f"mount -t tmpfs -o mode=0755,size=64m tmpfs {r}",
        *_bind_ro("/usr", f"{r}/usr"),
    ]
    for top in _TOP_LEVEL_LINKS:
        path = Path(top)
        if path.is_symlink():
            lines.append(f"ln -s {q(os.readlink(top))} {r}{top}")
        elif path.is_dir():
            lines.extend(_bind_ro(top, f"{r}{top}"))
    lines.append(f"mkdir -p {r}/etc")
    for name in _ETC_FILES:
        path = Path("/etc") / name
        if path.is_symlink():
            lines.append(f"ln -s {q(os.readlink(path))} {r}/etc/{name}")
        elif path.is_file():
            lines.extend(_bind_ro(str(path), f"{r}/etc/{name}", is_file=True))
    if Path("/etc/ld.so.conf.d").is_dir():
        lines.extend(_bind_ro("/etc/ld.so.conf.d", f"{r}/etc/ld.so.conf.d"))
    lines.append(f"mkdir -p {r}/dev")
    for device in ("null", "zero", "random", "urandom"):
        lines += [f": > {r}/dev/{device}", f"mount --bind /dev/{device} {r}/dev/{device}"]
    for directory in runtime_dirs:
        lines.extend(_bind_ro(q(directory), f"{r}{q(directory)}"))
    lines += [
        *_bind_ro(q(str(layout.mission_dir)), f"{r}{SANDBOX_MISSION_DIR}"),
        f"mkdir -p {r}{SANDBOX_GRPC_DIR}",
        f"mount --bind {q(str(layout.grpc_dir))} {r}{SANDBOX_GRPC_DIR}",
        f"mkdir -p {r}/proc {r}/tmp",
        f"mount -t proc proc {r}/proc",
        f"mount -t tmpfs -o mode=1777,size=64m tmpfs {r}/tmp",
        # Swap roots and drop the host filesystem from this mount namespace.
        f"cd {r}",
        "mkdir .oldroot",
        "pivot_root . .oldroot",
        "cd /",
        "umount -l /.oldroot",
        "rmdir /.oldroot",
        "mount -o remount,bind,ro /",
    ]
    exported = " ".join(f"{key}={q(value)}" for key, value in sorted(env.items()))
    command = " ".join(q(part) for part in [python_exe, "-I", "-B", *entry_args])
    lines.append(
        f"exec env -i {exported} setpriv --no-new-privs --inh-caps=-all "
        f"--ambient-caps=-all --bounding-set=-all -- {command}"
    )
    return "\n".join(lines) + "\n"


def sandbox_env() -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp"}
    for key in _ENV_PASSTHROUGH:
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def write_launch(layout: SandboxLayout, *, grpc_port: int, timeout_s: float) -> list[str]:
    """Write the init script and return the argv that starts the sandbox."""
    entry_args = [
        f"{SANDBOX_MISSION_DIR}/mission_sandbox_entry.py",
        "--mission",
        f"{SANDBOX_MISSION_DIR}/mission.py",
        "--sha256",
        layout.mission_sha256,
        "--grpc-address",
        SANDBOX_GRPC_ADDRESS,
        "--grpc-port",
        str(grpc_port),
        "--timeout",
        f"{timeout_s:g}",
    ]
    text = init_script_text(
        layout,
        python_exe=sys.executable,
        entry_args=entry_args,
        runtime_dirs=python_runtime_dirs(),
        env=sandbox_env(),
    )
    layout.init_script.write_text(text, encoding="utf-8")
    return [*_UNSHARE, "/bin/sh", str(layout.init_script)]


def _die_with_parent() -> None:  # pragma: no cover - runs in the forked child
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG


async def start(argv: list[str]) -> asyncio.subprocess.Process:
    """Start the sandbox with an empty environment; it dies with the runner."""
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        preexec_fn=_die_with_parent,
    )


async def serve_grpc_relay(
    socket_path: Path, target_host: str, target_port: int
) -> asyncio.base_events.Server:
    """Relay the sandbox's Unix socket to mavsdk_server's local TCP port."""

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                target_host, target_port
            )
        except OSError:
            writer.close()
            return
        await asyncio.gather(
            pump(reader, upstream_writer), pump(upstream_reader, writer), return_exceptions=True
        )

    return await asyncio.start_unix_server(handle, path=str(socket_path))
