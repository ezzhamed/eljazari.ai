"""F08: generated mission code runs only approved, fingerprinted and sandboxed."""

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graph.nodes import _code_sha256, code_approval_block_reason
from pipelines.simulation_acceptance import assess_simulation
from services.px4_service import PX4Service, mission_isolation_mode
from utils import mission_sandbox as sb
from utils.sitl_runtime import SITLRuntime

HOSTILE_MISSION = """
import json, os, socket

def attempt(fn):
    try:
        return fn()
    except Exception as exc:
        return type(exc).__name__

def main():
    grpc = os.listdir("/run/mavsdk")
    def relay():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect("/run/mavsdk/" + grpc[0]); s.sendall(b"ping"); return s.recv(8).decode()
    report = {
        "env": sorted(os.environ),
        "secret_file": attempt(lambda: open(os.environ.get("PLANTED", "/nonexistent")).read()),
        "tcp": attempt(lambda: socket.create_connection(("127.0.0.1", 45679), timeout=2)),
        "write_root": attempt(lambda: open("/pwned", "w")),
        "root": sorted(os.listdir("/")),
        "pids": sorted(p for p in os.listdir("/proc") if p.isdigit()),
        "relay": attempt(relay),
    }
    print("REPORT " + json.dumps(report, default=str))
    return 0
"""


@pytest.fixture
def short_dir():
    """Unix socket paths are limited to 108 bytes; stage where the runner does."""
    path = Path(tempfile.mkdtemp(prefix="dp-sbx-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _layout(tmp_path, workdir, code="print('hi')\n"):
    mission = tmp_path / "m.py"
    mission.write_text(code, encoding="utf-8")
    return sb.prepare(workdir, mission, sb.sha256_file(mission))


def test_prepare_refuses_a_mission_that_is_not_the_approved_one(tmp_path, short_dir):
    mission = tmp_path / "m.py"
    mission.write_text("print('x')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="approved fingerprint"):
        sb.prepare(tmp_path / "w", mission, "0" * 64)
    layout = _layout(tmp_path, short_dir)
    names = sorted(p.name for p in layout.mission_dir.iterdir())
    assert names == ["mavsdk_runtime_adapter.py", "mission.py", "mission_sandbox_entry.py"]


def test_init_script_isolates_filesystem_privileges_and_environment(
    tmp_path, short_dir, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("LANG", "C.UTF-8")
    layout = _layout(tmp_path, short_dir)
    text = sb.init_script_text(
        layout,
        python_exe="/venv/bin/python",
        entry_args=["/mission/mission_sandbox_entry.py", "--sha256", layout.mission_sha256],
        runtime_dirs=["/venv"],
        env=sb.sandbox_env(),
    )
    assert text.startswith("set -eu\n")
    assert "pivot_root . .oldroot" in text and "umount -l /.oldroot" in text
    assert "mount -o remount,bind,ro /" in text
    assert "--no-new-privs" in text and "--bounding-set=-all" in text
    assert "exec env -i " in text
    assert "secret" not in text and "DATABASE_URL" not in text
    assert "LANG=C.UTF-8" in text
    # Only listed directories are bound; the repo, /mnt and home never appear.
    binds = [line for line in text.splitlines() if line.startswith("mount --rbind")]
    sources = sorted(line.split()[2] for line in binds)
    assert "/venv" in sources and shlex.quote(str(layout.mission_dir)) in sources
    staged = shlex.quote(str(layout.mission_dir))
    assert not any(src.startswith(("/mnt", "/home", "/root")) for src in sources if src != staged)
    grpc_bind = f"mount --bind {shlex.quote(str(layout.grpc_dir))} "
    assert grpc_bind in text  # the only writable host path
    assert set(sb.sandbox_env()) <= {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ"}


def test_isolation_override_is_development_only(monkeypatch):
    from config.settings import settings

    monkeypatch.delenv("DRONEPROMPT_ALLOW_UNSANDBOXED_MISSIONS", raising=False)
    assert mission_isolation_mode() == "sandbox"
    monkeypatch.setenv("DRONEPROMPT_ALLOW_UNSANDBOXED_MISSIONS", "true")
    monkeypatch.setattr(settings, "environment", "development")
    assert mission_isolation_mode() == "none"
    monkeypatch.setattr(settings, "environment", "production")
    assert mission_isolation_mode() == "sandbox"


def _service(mode, **kwargs):
    runtime = SITLRuntime(mode=mode, host_platform="Linux", is_real=True, **kwargs)
    with patch("services.px4_service.resolve_sitl_runtime", return_value=runtime):
        return PX4Service()


@pytest.mark.asyncio
async def test_code_must_match_its_approved_fingerprint_before_anything_runs():
    service = _service("native")
    with patch.object(PX4Service, "_execute_wsl_mission", new=AsyncMock()) as runner:
        with pytest.raises(ValueError, match="approved fingerprint"):
            await service.execute_mission("print(1)", "s", expected_code_sha256="0" * 64)
    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_docker_runtime_does_not_execute_unsandboxed_code(monkeypatch):
    """Docker once ran mission code in the API process. It now runs it only through
    the runner and its sandbox inside the run's own container; with none, nothing runs."""
    monkeypatch.delenv("DRONEPROMPT_ALLOW_UNSANDBOXED_MISSIONS", raising=False)
    monkeypatch.setenv("DRONEPROMPT_REQUIRE_REAL_SITL", "true")
    service = _service("docker")
    with (
        patch.object(PX4Service, "_run_code_subprocess", new=AsyncMock()) as direct,
        patch.object(PX4Service, "_collect_telemetry_stream", new=AsyncMock()) as collector,
        patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn,
    ):
        result = await service.execute_mission("print(1)", "s")
    direct.assert_not_awaited()
    collector.assert_not_awaited()
    spawn.assert_not_awaited()
    assert "No simulation container is registered" in result.mission_error
    assert not result.points


@pytest.mark.asyncio
async def test_runner_gets_the_fingerprint_sandbox_flag_and_no_service_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("JWT_SECRET", "secret")
    monkeypatch.delenv("DRONEPROMPT_ALLOW_UNSANDBOXED_MISSIONS", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    service = _service("native")
    seen = {}

    async def fake_exec(*cmd, **kwargs):
        seen["cmd"], seen["env"] = list(cmd), kwargs.get("env")
        proc = MagicMock()
        proc.returncode = 1
        proc.wait = AsyncMock(return_value=1)
        return proc

    code_path = tmp_path / "mission.py"
    code_path.write_bytes(b"print('approved')\n")
    with (
        patch("asyncio.create_subprocess_exec", new=fake_exec),
        patch.object(PX4Service, "_stream_mission_output", new=AsyncMock(return_value=("", ""))),
    ):
        await service._execute_wsl_mission(
            code_path=str(code_path),
            sim_id="s",
            mavlink_port=14550,
            mavsdk_address=None,
            timeout_s=30,
        )
    cmd = seen["cmd"]
    assert cmd[cmd.index("--isolation") + 1] == "sandbox"
    expected = hashlib.sha256(code_path.read_bytes()).hexdigest()
    assert cmd[cmd.index("--mission-sha256") + 1] == expected
    assert "DATABASE_URL" not in seen["env"] and "JWT_SECRET" not in seen["env"]


def test_acceptance_rejects_unsandboxed_missions():
    from tests.test_simulation_acceptance import good_state

    assert assess_simulation(good_state())["passed"]
    rejected = assess_simulation(good_state(mission_isolation="none"))
    assert not rejected["passed"]
    assert "Mission code did not run in the isolated sandbox" in rejected["reasons"]
    assert rejected["retryable_with_code_change"] is False


@pytest.mark.asyncio
async def test_approval_is_bound_to_the_exact_code_and_cleared_by_regeneration():
    from graph.nodes import generate_code, human_review_code

    approved = await human_review_code(
        {"generated_code": "print(1)\n", "human_approval_needed": False}
    )
    assert approved["approved_code_sha256"] == _code_sha256("print(1)\n")
    rejected = await human_review_code(
        {"generated_code": "print(1)\n", "human_approval_needed": True}
    )
    assert rejected["approved_code_sha256"] is None
    with patch("agents.code_agent.CodeGenerationAgent") as agent:
        agent.return_value.generate = AsyncMock(side_effect=RuntimeError("boom"))
        regenerated = await generate_code({"project_id": "p", "design_tree": {}})
    assert regenerated["approved_code_sha256"] is None
    assert code_approval_block_reason(None, "x")
    assert "differs" in code_approval_block_reason(_code_sha256("x"), "y")
    assert code_approval_block_reason(_code_sha256("x"), "x") is None


@pytest.mark.parametrize("approved", [None, "0" * 64])
@pytest.mark.asyncio
async def test_unapproved_or_changed_code_is_never_simulated(approved):
    from graph.edges import route_after_run_simulation
    from graph.nodes import run_simulation

    with patch("agents.test_agent.SimulationTestAgent") as agent:
        result = await run_simulation(
            {"project_id": "p", "generated_code": "print(1)\n", "approved_code_sha256": approved}
        )
    agent.assert_not_called()
    assert result["simulation_status"] == "blocked"
    assert result["simulation_success"] is False
    assert route_after_run_simulation(result) == "generate_monitor_report"


@pytest.mark.asyncio
async def test_manual_start_requires_the_recorded_approval():
    from fastapi import HTTPException

    from routers.simulation import start_simulation

    record = MagicMock(code_content="print(1)\n", version=1)
    project = MagicMock(id="11111111-1111-1111-1111-111111111111")
    orchestrator = MagicMock()
    orchestrator.get_state = AsyncMock(return_value={"approved_code_sha256": None})
    background = MagicMock()
    with (
        patch("routers.simulation.require_project_access", new=AsyncMock(return_value=project)),
        patch("routers.simulation.GeneratedCodeRepository") as repo,
        patch("services.pipeline_runtime.get_orchestrator", return_value=orchestrator),
    ):
        repo.return_value.get_latest_for_project = AsyncMock(return_value=record)
        endpoint = (
            start_simulation.__wrapped__
            if hasattr(start_simulation, "__wrapped__")
            else start_simulation
        )
        with pytest.raises(HTTPException) as refused:
            await endpoint(MagicMock(), MagicMock(), "p", background, MagicMock(), MagicMock())
        assert refused.value.status_code == 409
        background.add_task.assert_not_called()
        orchestrator.get_state = AsyncMock(
            return_value={
                "approved_code_sha256": _code_sha256("print(1)\n"),
                "simulation_model_config": {"resolved_drone_sha256": "a" * 64},
            }
        )
        await endpoint(MagicMock(), MagicMock(), "p", background, MagicMock(), MagicMock())
    args = background.add_task.call_args.args
    assert args[4] == {"resolved_drone_sha256": "a" * 64}
    assert args[5] == _code_sha256("print(1)\n")


# CI sets this so the containment test fails, rather than skips, on a runner
# that cannot build the sandbox.
_SANDBOX_REQUIRED = os.environ.get("ELJAZARI_REQUIRE_SANDBOX_TEST") == "1"


@pytest.mark.skipif(
    not _SANDBOX_REQUIRED
    and (not sys.platform.startswith("linux") or sb.unsupported_reason() is not None),
    reason="needs Linux with unprivileged user namespaces",
)
@pytest.mark.asyncio
async def test_hostile_mission_is_contained(tmp_path, monkeypatch):
    unsupported = sb.unsupported_reason()
    if unsupported:
        pytest.fail(f"Mission sandbox required but unavailable: {unsupported}")
    planted = tmp_path / "planted-secret.txt"
    planted.write_text("TOPSECRET", encoding="utf-8")
    monkeypatch.setenv("PLANTED", str(planted))
    monkeypatch.setenv("SECRET_TOKEN", "leak-me")

    async def echo(reader, writer):
        writer.write((await reader.read(8)).replace(b"ping", b"pong"))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 45679)
    mission = tmp_path / "mission.py"
    mission.write_text(HOSTILE_MISSION, encoding="utf-8")
    # Unix socket paths are short (108 bytes); stage where the runner does, in /tmp.
    workdir = Path(tempfile.mkdtemp(prefix="dp-sbx-test-", dir="/tmp"))
    layout = sb.prepare(workdir, mission, sb.sha256_file(mission))
    relay = await sb.serve_grpc_relay(layout.socket_path(45679), "127.0.0.1", 45679)
    try:
        proc = await sb.start(sb.write_launch(layout, grpc_port=45679, timeout_s=30))
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
    finally:
        relay.close()
        server.close()
    assert proc.returncode == 0, out.decode(errors="replace")[-500:]
    line = next(x for x in out.decode().splitlines() if x.startswith("REPORT "))
    report = json.loads(line[len("REPORT ") :])
    assert "SECRET_TOKEN" not in report["env"] and "PLANTED" not in report["env"]
    assert report["secret_file"] == "FileNotFoundError"
    assert report["tcp"] == "OSError"  # no network namespace route at all
    assert report["write_root"] == "OSError"  # read-only root
    assert report["pids"] == ["1"]
    assert report["relay"] == "pong"  # the vehicle link is the only channel
    assert "mnt" not in report["root"]

    (layout.mission_dir / "mission.py").chmod(0o644)
    (layout.mission_dir / "mission.py").write_text("print('tampered')\n", encoding="utf-8")
    proc = await sb.start(sb.write_launch(layout, grpc_port=45679, timeout_s=30))
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
    assert proc.returncode == 3
    assert b"tampered" not in out.replace(b"fingerprint mismatch", b"")
    shutil.rmtree(workdir, ignore_errors=True)


def test_socket_path_length_is_checked_before_staging(tmp_path):
    mission = tmp_path / "m.py"
    mission.write_text("print(1)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="too deep"):
        sb.prepare(tmp_path / ("d" * 120), mission, sb.sha256_file(mission))
