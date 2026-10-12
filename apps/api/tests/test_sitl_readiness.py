"""Tests for PX4 readiness gating and simulation failure reporting.

A real WSL SITL run armed the vehicle and then never took off. PX4's own console
explained why:

    WARN [health_and_arming_checks] Preflight Fail: ekf2 missing data
    INFO [commander] Ready for takeoff!
    WARN [mavlink] REJ. MIS ITEM: partner id mismatch
    WARN [mavlink] Mission sync timeout

GazeboService had considered the simulation "ready" as soon as both processes had
a live PID, which is true milliseconds after spawning them, so the mission raced
PX4's startup. Consecutive identical runs failed differently — COMMAND_DENIED on
arm in one, PROTOCOL_ERROR on mission upload in the next — which is the signature
of a race rather than a logic error.

None of that reached the database: mission_error was produced and then dropped,
so the UI showed a failed simulation with no cause.

Coverage:
- Readiness requires PX4's banner, not just a live process
- Live-but-not-ready does not count as ready
- Non-real runtimes still fall back to the process check
- mission_error and warnings survive into the persisted row
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import gazebo_service
from services.gazebo_service import PX4_READY_BANNER, GazeboService, SimProcess

PX4_BOOTING = """INFO  [init] Gazebo world is ready
WARN  [health_and_arming_checks] Preflight Fail: ekf2 missing data
WARN  [health_and_arming_checks] Preflight Fail: system power unavailable
"""

PX4_READY = PX4_BOOTING + "INFO  [commander] \x1b[32mReady for takeoff!\x1b[0m\n"


@pytest.fixture
def sim(tmp_path):
    s = SimProcess(sim_id="sim-1")
    s.px4_stdout_log = str(tmp_path / "px4.stdout.log")
    s.px4_stderr_log = str(tmp_path / "px4.stderr.log")
    return s


class TestReadinessDetection:
    def test_booting_px4_is_not_ready(self, sim):
        """The failing runs raced exactly this window."""
        open(sim.px4_stdout_log, "w", encoding="utf-8").write(PX4_BOOTING)

        assert GazeboService._px4_reports_ready(sim) is False

    def test_ready_banner_is_detected(self, sim):
        open(sim.px4_stdout_log, "w", encoding="utf-8").write(PX4_READY)

        assert GazeboService._px4_reports_ready(sim) is True

    def test_banner_is_matched_through_ansi_colour_codes(self, sim):
        """PX4 wraps the banner in colour escapes; match on the text alone."""
        open(sim.px4_stdout_log, "w", encoding="utf-8").write(
            "INFO  [commander] \x1b[32m" + PX4_READY_BANNER + "\x1b[0m\n"
        )

        assert GazeboService._px4_reports_ready(sim) is True

    def test_banner_on_stderr_also_counts(self, sim):
        open(sim.px4_stderr_log, "w", encoding="utf-8").write(PX4_READY)

        assert GazeboService._px4_reports_ready(sim) is True

    def test_missing_log_is_not_ready(self, sim):
        assert GazeboService._px4_reports_ready(sim) is False

    def test_unset_log_paths_are_tolerated(self):
        assert GazeboService._px4_reports_ready(SimProcess(sim_id="s")) is False


class TestWaitUntilReady:
    @pytest.mark.asyncio
    async def test_does_not_block_waiting_for_the_banner(self, sim):
        """Regression: blocking here deadlocks the whole simulation.

        PX4 lists "No connection to the GCS" among its preflight gates, so it
        withholds readiness until a client attaches — and the only client is the
        mission, which runs after this returns. An earlier attempt at this fix
        waited for the banner here and hung every run until the timeout;
        measured across runs, `partner IP` and `Ready for takeoff!` appear
        together or not at all.
        """
        svc = GazeboService()
        svc._sims[sim.sim_id] = sim
        svc._runtime = MagicMock(is_real=True, mode="wsl")
        open(sim.px4_stdout_log, "w", encoding="utf-8").write(PX4_BOOTING)

        with patch.object(svc, "is_running", AsyncMock(return_value=True)):
            await asyncio.wait_for(svc._wait_until_ready(sim, timeout_s=30.0), timeout=5.0)

        assert sim.status == "running"

    @pytest.mark.asyncio
    async def test_returns_once_the_banner_appears(self, sim):
        svc = GazeboService()
        svc._sims[sim.sim_id] = sim
        svc._runtime = MagicMock(is_real=True, mode="wsl")

        open(sim.px4_stdout_log, "w", encoding="utf-8").write(PX4_READY)
        with patch.object(svc, "is_running", AsyncMock(return_value=True)):
            await svc._wait_until_ready(sim, timeout_s=5.0)

        assert sim.status == "running"

    @pytest.mark.asyncio
    async def test_mock_runtime_still_uses_the_process_check(self, sim):
        """Mock and docker runtimes never write a PX4 console log."""
        svc = GazeboService()
        svc._sims[sim.sim_id] = sim
        svc._runtime = MagicMock(is_real=False, mode="mock")

        with patch.object(svc, "is_running", AsyncMock(return_value=True)):
            await svc._wait_until_ready(sim, timeout_s=5.0)

        assert sim.status == "running"

    @pytest.mark.asyncio
    async def test_dead_process_never_becomes_ready(self, sim):
        svc = GazeboService()
        svc._sims[sim.sim_id] = sim
        svc._runtime = MagicMock(is_real=True, mode="wsl")

        open(sim.px4_stdout_log, "w", encoding="utf-8").write(PX4_READY)
        with (
            patch.object(svc, "is_running", AsyncMock(return_value=False)),
            patch.object(gazebo_service, "require_real_sitl", return_value=False),
            patch.object(gazebo_service.logger, "warning") as warn,
            pytest.raises(RuntimeError, match="exited before becoming ready"),
        ):
            await svc._wait_until_ready(sim, timeout_s=0.1)

        # A dead process is never ready, banner or not. Both processes are gone
        # (gz_process / px4_process are None), so this must fail fast rather than
        # report "running" and let execute_mission cascade into timeouts.
        assert warn.called
        assert sim.status == "failed"
        assert sim.sim_id not in svc._sims
        assert not any("best-effort mode" in str(c) for c in warn.call_args_list)


class TestFailureReporting:
    """mission_error reached the UI as an unexplained failure before this."""

    def test_mission_error_is_stored(self):
        from graph.nodes import build_raw_telemetry

        raw = build_raw_telemetry(
            points=[{"alt_m": 0.0}],
            telemetry_source="real_wsl_sitl",
            mission_error="MissionError: PROTOCOL_ERROR; origin: upload_mission()",
            warnings=["Mission sync timeout"],
        )

        assert "PROTOCOL_ERROR" in raw["mission_error"]
        assert raw["warnings"] == ["Mission sync timeout"]
        assert raw["telemetry_source"] == "real_wsl_sitl"
        assert raw["points"] == [{"alt_m": 0.0}]

    def test_a_clean_run_stores_no_error(self):
        raw = _build(mission_error="", warnings=None)

        assert raw["mission_error"] is None
        assert raw["warnings"] == []

    def test_empty_error_is_normalised_to_none(self):
        """An empty string would render as a blank "error" in the UI."""
        assert _build(mission_error="   ".strip())["mission_error"] is None

    def test_warnings_are_copied_not_aliased(self):
        original = ["first"]
        raw = _build(warnings=original)
        original.append("second")

        assert raw["warnings"] == ["first"]

    def test_the_simulation_node_forwards_the_failure_cause(self):
        """The node extracted mission_error and then did not pass it on."""
        import inspect

        from graph import nodes

        source = inspect.getsource(nodes.run_simulation)
        assert "mission_error=mission_error" in source
        assert "warnings=warnings" in source


def _build(**kwargs):
    from graph.nodes import build_raw_telemetry

    kwargs.setdefault("points", [])
    kwargs.setdefault("telemetry_source", "real_wsl_sitl")
    return build_raw_telemetry(**kwargs)


class TestMissionRetries:
    """The waiting PX4 needs happens after connecting, so it lives in the mission."""

    def test_the_template_retries_a_rejected_upload(self):
        """PX4 rejects the first upload while its mission manager settles."""
        source = inspect_source_of("pipelines.mavsdk_templates")

        assert "mission.upload_retry" in source
        assert "upload_mission(plan)" in source

    def test_the_template_retries_a_denied_arm(self):
        """PX4 becomes armable seconds after the mission connects, not before."""
        source = inspect_source_of("pipelines.mavsdk_templates")

        assert "arm.retry" in source
        assert "arm.ok" in source


def inspect_source_of(module_path: str) -> str:
    import importlib
    import inspect

    return inspect.getsource(importlib.import_module(module_path))
