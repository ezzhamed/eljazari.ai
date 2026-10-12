"""F09: one run per project, events across threads/workers, one simulator per host."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.run_lock import advisory_key, project_run_lock


def test_advisory_key_is_stable_signed_64_bit():
    key = advisory_key("11111111-1111-1111-1111-111111111111")
    assert key == advisory_key("11111111-1111-1111-1111-111111111111")
    assert key != advisory_key("22222222-2222-2222-2222-222222222222")
    assert -(2**63) <= key < 2**63


@pytest.mark.asyncio
async def test_lock_is_exclusive_and_released(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "checkpoint_backend", "memory")
    async with project_run_lock("p") as first:
        async with project_run_lock("p") as second:
            async with project_run_lock("other") as other:
                assert (first, second, other) == (True, False, True)
    async with project_run_lock("p") as again:
        assert again is True


@pytest.mark.asyncio
async def test_two_simultaneous_approvals_resume_the_graph_once(monkeypatch):
    """Both requests see an open gate; only the lock owner may act on it."""
    from config.settings import settings
    from services import pipeline_runtime as runtime

    monkeypatch.setattr(settings, "checkpoint_backend", "memory")
    orchestrator = MagicMock()
    orchestrator.is_awaiting_review = AsyncMock(return_value=True)

    async def slow_approve(*_a, **_k):
        await asyncio.sleep(0.2)

    async def no_events(*_a, **_k):
        if False:
            yield

    orchestrator.approve = AsyncMock(side_effect=slow_approve)
    orchestrator.resume = no_events
    with (
        patch.object(runtime, "_ORCHESTRATOR", orchestrator),
        patch.object(runtime, "_persist_hitl_approval", new=AsyncMock()),
        patch.object(runtime, "broadcast_to_project", new=AsyncMock()),
        patch.object(runtime, "_build_worker_session_factory") as factory,
    ):
        engine = MagicMock()
        engine.dispose = AsyncMock()
        factory.return_value = (engine, MagicMock())
        await asyncio.gather(
            runtime._continue_pipeline("p", phase="code", approved=True, feedback=""),
            runtime._continue_pipeline("p", phase="code", approved=True, feedback=""),
        )
    assert orchestrator.approve.await_count == 1


@pytest.mark.asyncio
async def test_new_run_is_refused_while_the_project_is_busy(monkeypatch):
    from config.settings import settings
    from services import pipeline_runtime as runtime

    monkeypatch.setattr(settings, "checkpoint_backend", "memory")
    orchestrator = MagicMock()
    orchestrator.run = MagicMock()
    with patch.object(runtime, "_ORCHESTRATOR", orchestrator):
        async with project_run_lock("busy"):
            await runtime._run_pipeline("busy", "prompt")
    orchestrator.run.assert_not_called()


@pytest.mark.asyncio
async def test_events_from_worker_threads_are_emitted_on_the_server_loop():
    from ws import socket_manager

    server_loop = asyncio.get_running_loop()
    seen = []

    async def record(event, data, room=None):
        seen.append((event, room, asyncio.get_running_loop() is server_loop))

    socket_manager.bind_server_loop(server_loop)
    try:
        with patch.object(socket_manager.sio, "emit", new=record):
            thread = threading.Thread(
                target=lambda: asyncio.run(
                    socket_manager.broadcast_to_project("p", "phase_changed", {"x": 1})
                )
            )
            thread.start()
            while thread.is_alive():
                await asyncio.sleep(0.01)
            await socket_manager.broadcast_to_project("p", "same_loop", {})
    finally:
        socket_manager.bind_server_loop(None)
    assert seen == [("phase_changed", "project:p", True), ("same_loop", "project:p", True)]


@pytest.mark.asyncio
async def test_second_simulator_waits_for_the_host_then_proceeds(monkeypatch, tmp_path):
    from config.settings import settings
    from services import gazebo_service as gz
    from utils.sitl_runtime import SITLRuntime

    monkeypatch.setattr(gz, "_SITL_HOST_LOCK", tmp_path / "sitl.lock")
    monkeypatch.setattr(settings, "sitl_lock_timeout_s", 0.3)
    runtime = SITLRuntime(mode="native", host_platform="Linux", is_real=True, px4_dir="/px4")

    def service():
        instance = gz.GazeboService()
        instance._runtime = runtime
        instance._start_gazebo = AsyncMock()
        instance._start_px4_sitl = AsyncMock()
        instance._wait_until_ready = AsyncMock()
        return instance

    first, second = service(), service()  # separate instances, like separate workers
    sim_id = await first.launch_world({})
    with pytest.raises(RuntimeError, match="Another PX4/Gazebo simulation"):
        await second.launch_world({})
    second._start_gazebo.assert_not_awaited()
    await first.stop_world(sim_id)
    other = await second.launch_world({})
    await second.stop_world(other)

    failing = service()
    failing._start_px4_sitl = AsyncMock(side_effect=RuntimeError("px4 died"))
    with pytest.raises(RuntimeError, match="px4 died"):
        await failing.launch_world({})
    # A failed launch must not keep the host.
    await first.stop_world(await first.launch_world({}))


def test_several_workers_need_a_message_queue_and_durable_checkpoints(monkeypatch):
    from config.settings import Settings

    monkeypatch.setenv("ELJAZARI_API_WORKERS", "2")
    with pytest.raises(ValueError, match="SOCKETIO_MESSAGE_QUEUE"):
        Settings()
    monkeypatch.setenv("ELJAZARI_SOCKETIO_MESSAGE_QUEUE", "redis://localhost:6379/3")
    monkeypatch.setenv("CHECKPOINT_BACKEND", "memory")
    with pytest.raises(ValueError, match="PostgreSQL checkpoints"):
        Settings()
    monkeypatch.setenv("CHECKPOINT_BACKEND", "postgres")
    assert Settings().api_workers == 2


def _v2(msgid: int, payload: bytes = b"\x00" * 9, signed: bool = False) -> bytes:
    header = bytes([0xFD, len(payload), 0x01 if signed else 0x00, 0, 7, 1, 1])
    frame = header + msgid.to_bytes(3, "little") + payload + b"\xaa\xbb"
    return frame + (b"\x00" * 13 if signed else b"")


def test_heartbeat_is_found_in_packed_mavlink_datagrams():
    from services.gazebo_service import contains_mavlink_heartbeat

    attitude = _v2(30, b"\x01" * 28)
    assert contains_mavlink_heartbeat(_v2(0))
    assert contains_mavlink_heartbeat(attitude + _v2(0))  # PX4 packs frames
    assert contains_mavlink_heartbeat(_v2(30, b"\x01" * 28, signed=True) + _v2(0))
    assert contains_mavlink_heartbeat(bytes([0xFE, 9, 3, 1, 1, 0]) + b"\x00" * 11)  # v1
    assert not contains_mavlink_heartbeat(attitude)
    assert not contains_mavlink_heartbeat(b"junk")
    assert not contains_mavlink_heartbeat(b"")


@pytest.mark.asyncio
async def test_docker_mode_runs_the_whole_generated_flight_in_its_own_container(monkeypatch):
    """Container per run: spawn, PX4 and attestation go through docker exec."""
    from services import docker_sim
    from services import gazebo_service as gz
    from utils.sitl_runtime import SITLRuntime

    service = gz.GazeboService()
    service._runtime = SITLRuntime(
        mode="docker", host_platform="Windows", is_real=True, px4_dir="/opt/px4/PX4-Autopilot"
    )
    bundle = MagicMock(bundle_dir="C:/bundles/abc", sdf_path="C:/bundles/abc/model.sdf")
    monkeypatch.setattr(service, "_flight_bundle", lambda config: bundle)
    monkeypatch.setattr(
        "pipelines.simulation_readiness.generated_model_block_reason", lambda config: None
    )
    start = AsyncMock(side_effect=lambda sim_id, world, bundle_dir: f"c-{sim_id}")
    monkeypatch.setattr(docker_sim, "start", start)
    monkeypatch.setattr(docker_sim, "remove_stale", AsyncMock())
    stop = AsyncMock()
    monkeypatch.setattr(docker_sim, "stop", stop)
    calls = []
    for name in ("_spawn_generated_model", "_start_px4_sitl", "_attest_px4_params"):
        monkeypatch.setattr(
            service, name, AsyncMock(side_effect=lambda sim, n=name: calls.append(n))
        )
    monkeypatch.setattr(service, "_wait_until_ready", AsyncMock())

    sim_id = await service.launch_world({"flight_model_manifest_path": "m"})
    sim = service._sims[sim_id]
    assert start.await_args.args[2] == "C:/bundles/abc"
    assert sim.container == f"c-{sim_id}"
    assert calls == ["_spawn_generated_model", "_start_px4_sitl", "_attest_px4_params"]
    assert service._shell_prefix(sim) == ["docker", "exec", f"c-{sim_id}"]
    assert service._runtime_path(bundle.sdf_path) == "/bundle/model.sdf"
    await service.stop_world(sim_id)
    stop.assert_awaited_once_with(sim_id)


def test_docker_run_arguments_confine_the_sandbox_and_mount_the_bundle(monkeypatch, tmp_path):
    from pathlib import Path

    from config.settings import settings
    from services import docker_sim

    monkeypatch.setattr(settings, "sitl_docker_image", "eljazari-sim:test")
    monkeypatch.setattr(settings, "sitl_docker_seccomp_profile", "")
    arguments = docker_sim.run_arguments("abc123", "default", str(tmp_path))
    assert arguments[:2] == ["docker", "run"] and arguments[-1] == "eljazari-sim:test"
    seccomp = next(a for a in arguments if a.startswith("seccomp="))
    profile = Path(seccomp.removeprefix("seccomp="))
    assert profile.name == "sim-sandbox.json" and profile.is_file()
    assert "--privileged" not in arguments and "--cap-add" not in arguments
    assert "SIM_MODE=gazebo" in arguments and "GZ_PARTITION=eljazari_abc123" in arguments
    assert f"{tmp_path.resolve()}:/bundle:ro" in arguments
    assert docker_sim.ROLE_LABEL in arguments
    repo = Path(docker_sim.__file__).resolve().parents[3]
    entrypoint = (repo / "infrastructure" / "sim-entrypoint.sh").read_text(encoding="utf-8")
    assert '"${SIM_MODE:-full}" = "gazebo"' in entrypoint
    dockerfile = (repo / "infrastructure" / "Dockerfile.sim").read_text(encoding="utf-8")
    assert "/opt/eljazari/utils/" in dockerfile and "mavsdk==3.15.3" in dockerfile
    # The image is built from the working tree; a CRLF entrypoint exits with 127
    # (`/usr/bin/env: 'bash\r'`) before Gazebo starts.
    for script in (repo / "infrastructure").glob("*.sh"):
        assert b"\r" not in script.read_bytes(), f"{script.name} has CRLF line endings"
    assert "--rm" not in arguments  # a failed container keeps its logs until stop()


def test_the_mission_runner_accepts_every_runtime_that_starts_it():
    import subprocess
    import sys
    from pathlib import Path

    runner = Path(__file__).resolve().parents[1] / "utils" / "wsl_mission_runner.py"
    usage = subprocess.run(
        [sys.executable, str(runner), "--help"], capture_output=True, text=True, check=True
    ).stdout
    # PX4Service passes its own mode: wsl, native, or docker inside the container.
    assert "{wsl,native,docker}" in usage


def test_the_seccomp_profile_adds_only_the_sandbox_calls():
    import importlib.util
    import json
    from pathlib import Path

    here = Path(__file__).resolve().parents[3] / "infrastructure" / "seccomp"
    spec = importlib.util.spec_from_file_location("make_profile", here / "make_profile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build() == (here / "sim-sandbox.json").read_bytes()
    base = json.loads((here / "moby-default-v28.3.2.json").read_text(encoding="utf-8"))
    derived = json.loads((here / "sim-sandbox.json").read_text(encoding="utf-8"))
    assert derived["syscalls"][: len(base["syscalls"])] == base["syscalls"]
    added = derived["syscalls"][len(base["syscalls"]) :]
    assert [(rule["names"], rule["action"]) for rule in added] == [
        (["mount", "pivot_root", "umount2", "unshare"], "SCMP_ACT_ALLOW")
    ]
    assert {k: v for k, v in derived.items() if k != "syscalls"} == {
        k: v for k, v in base.items() if k != "syscalls"
    }
