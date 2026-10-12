"""Tests for the WebGCS Socket.io handlers (ws/gcs_events.py)

`ws/mavlink_bridge.py` implemented the ground-station behaviour but nothing
imported it, so no handler was registered for the `gcs:*` events the frontend
emits. Socket.io discards unknown events silently, so every control on
/dashboard/gcs appeared to work and did nothing.

Coverage:
- Every event the frontend sends has a handler (the regression that mattered)
- Commands are dispatched through an allow-list, not by dynamic attribute lookup
- Payload shapes match apps/web/components/gcs/
- Project is resolved from the session, never from the payload
- Telemetry is one task per project, and stopping cancels it
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ws import gcs_events

PROJECT_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def bridge():
    """A stand-in MAVLinkBridge wired into the module's registry."""
    mock = MagicMock()
    mock.arm = AsyncMock(return_value={"ok": True, "command": "arm"})
    mock.disarm = AsyncMock(return_value={"ok": True, "command": "disarm"})
    mock.land = AsyncMock(return_value={"ok": True, "command": "land"})
    mock.return_to_launch = AsyncMock(return_value={"ok": True, "command": "rtl"})
    mock.pause_mission = AsyncMock(return_value={"ok": True, "command": "pause"})
    mock.emergency_stop = AsyncMock(return_value={"ok": True, "command": "estop"})
    mock.start_mission = AsyncMock(return_value={"ok": True, "command": "start_mission"})
    mock.takeoff = AsyncMock(return_value={"ok": True, "command": "takeoff"})
    mock.upload_mission = AsyncMock(return_value={"ok": True, "waypoints": 2})
    mock.set_param = AsyncMock(return_value={"ok": True, "param": "MC_ROLL_P"})
    mock.get_pid_params = AsyncMock(return_value={"ok": True, "params": {"MC_ROLL_P": 6.5}})
    mock.connect = AsyncMock(return_value=True)
    mock.start_telemetry_loop = AsyncMock()
    with patch.object(gcs_events, "get_bridge", return_value=mock):
        yield mock


@pytest.fixture
def resolved_project():
    with patch.object(gcs_events, "current_project", AsyncMock(return_value=PROJECT_ID)) as m:
        yield m


@pytest.fixture
def broadcast():
    with patch.object(gcs_events, "broadcast_to_project", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture(autouse=True)
def _clear_tasks():
    gcs_events._telemetry_tasks.clear()
    yield
    gcs_events._telemetry_tasks.clear()


class TestHandlersExist:
    """The regression: these events had no handler at all."""

    @pytest.mark.parametrize(
        "handler",
        [
            "on_gcs_start",
            "on_gcs_stop",
            "on_gcs_command",
            "on_gcs_mission_upload",
            "on_gcs_set_param",
            "on_gcs_get_params",
        ],
    )
    def test_handler_is_defined(self, handler):
        assert callable(getattr(gcs_events, handler))

    def test_main_imports_the_module(self):
        """Registration happens on import — the bug was that nothing imported it."""
        import inspect

        import main

        source = inspect.getsource(main)
        assert "ws.gcs_events" in source


class TestCommandDispatch:
    @pytest.mark.parametrize(
        "command,method",
        [
            ("arm", "arm"),
            ("disarm", "disarm"),
            ("land", "land"),
            ("rtl", "return_to_launch"),
            ("pause", "pause_mission"),
            ("estop", "emergency_stop"),
        ],
    )
    @pytest.mark.asyncio
    async def test_command_routes_to_bridge(
        self, bridge, resolved_project, broadcast, command, method
    ):
        result = await gcs_events.on_gcs_command("sid", {"command": command})

        assert result["ok"] is True
        getattr(bridge, method).assert_awaited_once()

    @pytest.mark.asyncio
    async def test_takeoff_passes_altitude(self, bridge, resolved_project, broadcast):
        await gcs_events.on_gcs_command("sid", {"command": "takeoff", "altitude_m": 25})

        bridge.takeoff.assert_awaited_once_with(25.0)

    @pytest.mark.asyncio
    async def test_takeoff_falls_back_to_configured_altitude(
        self, bridge, resolved_project, broadcast
    ):
        await gcs_events.on_gcs_command("sid", {"command": "takeoff"})

        bridge.takeoff.assert_awaited_once_with(gcs_events.settings.gcs_takeoff_altitude_m)

    @pytest.mark.asyncio
    async def test_command_is_echoed_to_the_room(self, bridge, resolved_project, broadcast):
        """Every tab's EventLog logs the command, not just the sender's."""
        await gcs_events.on_gcs_command("sid", {"command": "arm"})

        broadcast.assert_awaited_once_with(PROJECT_ID, "gcs:command", {"command": "arm"})

    @pytest.mark.asyncio
    async def test_unknown_command_is_rejected(self, bridge, resolved_project, broadcast):
        """Dispatch is an allow-list — these arm motors on real hardware."""
        result = await gcs_events.on_gcs_command("sid", {"command": "self_destruct"})

        assert result["ok"] is False
        assert "unknown command" in result["error"]
        broadcast.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dunder_attribute_is_not_dispatchable(self, bridge, resolved_project, broadcast):
        result = await gcs_events.on_gcs_command("sid", {"command": "__init__"})

        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_empty_command_is_rejected(self, bridge, resolved_project, broadcast):
        assert (await gcs_events.on_gcs_command("sid", {}))["ok"] is False


class TestAuthorization:
    @pytest.mark.asyncio
    async def test_command_requires_a_joined_project(self, bridge, broadcast):
        with patch.object(
            gcs_events,
            "current_project",
            AsyncMock(side_effect=PermissionError("join_project required")),
        ):
            result = await gcs_events.on_gcs_command("sid", {"command": "arm"})

        assert result["ok"] is False
        bridge.arm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_project_comes_from_session_not_payload(self, bridge, broadcast):
        """A caller must not be able to target another project by payload."""
        with patch.object(
            gcs_events, "current_project", AsyncMock(return_value=PROJECT_ID)
        ) as resolver:
            await gcs_events.on_gcs_command(
                "sid", {"command": "arm", "project_id": "22222222-2222-2222-2222-222222222222"}
            )

        resolver.assert_awaited_once_with("sid")
        assert broadcast.await_args.args[0] == PROJECT_ID


class TestMissionUpload:
    @pytest.mark.asyncio
    async def test_waypoints_forwarded_unchanged(self, bridge, resolved_project):
        waypoints = [
            {"lat": 24.7136, "lon": 46.6753, "alt_m": 20, "hover_s": 0},
            {"lat": 24.7140, "lon": 46.6760, "alt_m": 25, "hover_s": 5},
        ]

        result = await gcs_events.on_gcs_mission_upload("sid", {"waypoints": waypoints})

        assert result["ok"] is True
        bridge.upload_mission.assert_awaited_once_with(waypoints)

    @pytest.mark.asyncio
    async def test_empty_waypoints_rejected(self, bridge, resolved_project):
        assert (await gcs_events.on_gcs_mission_upload("sid", {"waypoints": []}))["ok"] is False
        bridge.upload_mission.assert_not_awaited()


class TestParameters:
    @pytest.mark.asyncio
    async def test_set_param_coerces_numeric_value(self, bridge, resolved_project):
        await gcs_events.on_gcs_set_param("sid", {"name": "MC_ROLL_P", "value": "6.5"})

        bridge.set_param.assert_awaited_once_with("MC_ROLL_P", 6.5)

    @pytest.mark.asyncio
    async def test_set_param_rejects_non_numeric(self, bridge, resolved_project):
        result = await gcs_events.on_gcs_set_param("sid", {"name": "MC_ROLL_P", "value": "high"})

        assert result["ok"] is False
        bridge.set_param.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_param_requires_a_name(self, bridge, resolved_project):
        assert (await gcs_events.on_gcs_set_param("sid", {"value": 1.0}))["ok"] is False

    @pytest.mark.asyncio
    async def test_get_params_broadcasts_shape_the_ui_expects(
        self, bridge, resolved_project, broadcast
    ):
        """ParameterTuning reads data.params — see components/gcs/ParameterTuning.tsx."""
        await gcs_events.on_gcs_get_params("sid", {})

        broadcast.assert_awaited_once_with(
            PROJECT_ID, "gcs:params_update", {"params": {"MC_ROLL_P": 6.5}}
        )


class TestTelemetryLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_one_task(self, bridge, resolved_project):
        result = await gcs_events.on_gcs_start("sid", {})

        assert result["ok"] is True
        assert PROJECT_ID in gcs_events._telemetry_tasks
        await gcs_events.on_gcs_stop("sid", {})

    @pytest.mark.asyncio
    async def test_second_start_does_not_spawn_another_task(self, bridge, resolved_project):
        """Several tabs watch the same drone; each must not open its own stream."""
        await gcs_events.on_gcs_start("sid", {})
        first = gcs_events._telemetry_tasks[PROJECT_ID]

        result = await gcs_events.on_gcs_start("sid2", {})

        assert result["status"] == "already_running"
        assert gcs_events._telemetry_tasks[PROJECT_ID] is first
        assert bridge.connect.await_count == 1
        await gcs_events.on_gcs_stop("sid", {})

    @pytest.mark.asyncio
    async def test_failed_connect_starts_no_task(self, bridge, resolved_project):
        bridge.connect = AsyncMock(return_value=False)

        result = await gcs_events.on_gcs_start("sid", {})

        assert result["ok"] is False
        assert PROJECT_ID not in gcs_events._telemetry_tasks

    @pytest.mark.asyncio
    async def test_stop_cancels_and_removes_the_task(self, bridge, resolved_project):
        async def _forever():
            await asyncio.sleep(3600)

        bridge.start_telemetry_loop = _forever
        await gcs_events.on_gcs_start("sid", {})
        task = gcs_events._telemetry_tasks[PROJECT_ID]

        with patch.object(gcs_events, "remove_bridge", new_callable=AsyncMock):
            result = await gcs_events.on_gcs_stop("sid", {})

        assert result["ok"] is True
        assert task.cancelled() or task.done()
        assert PROJECT_ID not in gcs_events._telemetry_tasks


async def test_stub_telemetry_says_it_is_synthetic():
    """Without MAVSDK the bridge invents an orbit; the page must be able to tell."""
    from ws import mavlink_bridge

    bridge = mavlink_bridge.MAVLinkBridge(PROJECT_ID, "udp://:14540")
    with patch.object(mavlink_bridge, "broadcast_to_project", new_callable=AsyncMock) as sent:
        await bridge._emit_stub_telemetry()
    project, event, payload = sent.await_args.args
    assert (project, event) == (PROJECT_ID, "mavlink_telemetry")
    assert payload["synthetic"] is True
