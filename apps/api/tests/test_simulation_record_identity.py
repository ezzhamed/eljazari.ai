"""Events must target their committed simulation row, never the newest row."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from graph.nodes import _code_sha256

from graph.nodes import _persist_simulation_record, evaluate_simulation, run_simulation
from models.simulation import SimulationStatus
from services.pipeline_runtime import (
    _persist_simulation,
    _persist_simulation_evaluation,
    _persist_monitor_report,
    _node_messages_from_event,
)
from tests.test_simulation_acceptance import good_state


@pytest.mark.parametrize(
    "consumer", [_persist_simulation, _persist_simulation_evaluation, _persist_monitor_report]
)
@pytest.mark.parametrize(
    "bad", ["missing", "invalid", "other_project", "other_iteration", "deleted"]
)
@pytest.mark.asyncio
async def test_bad_identity_never_updates_latest_record(consumer, bad):
    project_id, record_id = uuid4(), uuid4()
    row = SimpleNamespace(id=record_id, project_id=project_id, iteration=1, raw_telemetry={})
    data = {"simulation_record_id": str(record_id), "simulation_iteration": 1}
    if bad == "missing":
        data.pop("simulation_record_id")
    elif bad == "invalid":
        data["simulation_record_id"] = "bad-id"
    elif bad == "other_project":
        row.project_id = uuid4()
    elif bad == "other_iteration":
        data["simulation_iteration"] = 2
    with (
        patch("services.pipeline_runtime.SimulationRepository") as factory,
        patch("services.pipeline_runtime.MonitorReportRepository") as reports,
    ):
        repo = factory.return_value
        repo.get = AsyncMock(return_value=None if bad == "deleted" else row)
        repo.update_from_schema = AsyncMock()
        await consumer(None, project_id, data)
        repo.get_latest_for_project.assert_not_called()
        repo.create_from_schema.assert_not_called()
        repo.update_from_schema.assert_not_awaited()
        reports.return_value.create_from_schema.assert_not_called()


@pytest.mark.asyncio
async def test_late_evaluation_and_report_target_older_attempt_by_id():
    project_id = uuid4()
    old = SimpleNamespace(
        id=uuid4(), project_id=project_id, iteration=1, raw_telemetry={"points": [{"alt_m": 2}]}
    )
    data = {
        "simulation_record_id": str(old.id),
        "simulation_iteration": 1,
        "simulation_success": False,
        "simulation_acceptance": {"passed": False},
        "monitor_report": {"overall_score": None},
    }
    with (
        patch("services.pipeline_runtime.SimulationRepository") as factory,
        patch("services.pipeline_runtime.MonitorReportRepository") as reports,
    ):
        repo = factory.return_value
        repo.get = AsyncMock(return_value=old)
        repo.get_latest_for_project = AsyncMock(side_effect=AssertionError("Must not use latest"))
        repo.update_from_schema = AsyncMock()
        reports.return_value.create_from_schema = AsyncMock()
        await _persist_simulation_evaluation(None, project_id, data)
        await _persist_monitor_report(None, project_id, data)
        assert repo.update_from_schema.await_args.args[0] is old
        assert reports.return_value.create_from_schema.await_args.args[0].simulation_id == old.id
        repo.get_latest_for_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_supplement_does_not_duplicate_or_reclassify_row():
    project_id = uuid4()
    row = SimpleNamespace(
        id=uuid4(),
        project_id=project_id,
        iteration=2,
        raw_telemetry={"acceptance": {"passed": False}, "points": []},
    )
    with patch("services.pipeline_runtime.SimulationRepository") as factory:
        repo = factory.return_value
        repo.get = AsyncMock(return_value=row)

        async def apply_update(record, update):
            record.raw_telemetry = update.raw_telemetry

        repo.update_from_schema = AsyncMock(side_effect=apply_update)
        data = {"simulation_record_id": str(row.id), "simulation_iteration": 2}
        await _persist_simulation(None, project_id, data)
        await _persist_simulation(None, project_id, data)
        repo.create_from_schema.assert_not_called()
        repo.update_from_schema.assert_awaited_once()
        update = repo.update_from_schema.await_args.args[1]
        assert "status" not in update.model_fields_set
        assert update.raw_telemetry["acceptance"] == {"passed": False}


@pytest.mark.asyncio
async def test_node_uses_one_based_attempt_and_streams_record_id():
    record_id = str(uuid4())
    for previous in (0, 1):
        with (
            patch(
                "agents.test_agent.SimulationTestAgent.run_single",
                new=AsyncMock(return_value={"telemetry": [], "mission_completed": False}),
            ) as run,
            patch(
                "graph.nodes._persist_simulation_record", new=AsyncMock(return_value=record_id)
            ) as persist,
        ):
            result = await run_simulation(
                {
                    "project_id": "test",
                    "generated_code": "code",
                    "approved_code_sha256": _code_sha256("code"),
                    "simulation_iteration": previous,
                }
            )
        assert persist.await_args.kwargs["iteration"] == previous + 1
        assert persist.await_args.kwargs["executed_code"] == "code"
        assert run.await_args.kwargs["iteration"] == previous + 1
        assert result["simulation_record_id"] == record_id
        assert result["simulation_iteration"] == previous + 1
        events = _node_messages_from_event(
            "test", {"event": "node_complete", "node": "run_simulation", "data": result}
        )
        assert next(v for k, v in events if k == "simulation_complete")["sim_id"] == record_id


@pytest.mark.asyncio
async def test_evaluation_delta_carries_exact_identity_even_on_rejection():
    record_id = str(uuid4())
    result = await evaluate_simulation(
        good_state(simulation_record_id=record_id, simulation_iteration=4, crash_detected=True)
    )
    assert result["simulation_record_id"] == record_id
    assert result["simulation_iteration"] == 4


@pytest.mark.parametrize(
    "mismatched_code,commit_fails", [(False, False), (True, False), (False, True)]
)
@pytest.mark.asyncio
async def test_record_id_returned_only_after_commit_of_matching_code(mismatched_code, commit_fails):
    db = AsyncMock()
    db.commit = AsyncMock(side_effect=RuntimeError("DB down") if commit_fails else None)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=False)
    row = SimpleNamespace(id=uuid4())
    code = SimpleNamespace(id=uuid4(), code_content="different" if mismatched_code else "code")
    with (
        patch("db.session.AsyncSessionLocal", return_value=session),
        patch("repositories.generated_code.GeneratedCodeRepository") as codes,
        patch("repositories.simulation.SimulationRepository") as sims,
    ):
        codes.return_value.get_latest_for_project = AsyncMock(return_value=code)
        sims.return_value.create_from_schema = AsyncMock(return_value=row)
        sims.return_value.update_from_schema = AsyncMock()
        result = await _persist_simulation_record(
            project_id=str(uuid4()),
            executed_code="code",
            iteration=2,
            telemetry=[],
            observations=[],
            mission_completed=True,
            crash_detected=False,
            telemetry_source="real_wsl_sitl",
            duration_s=1,
        )
        if mismatched_code:
            sims.return_value.create_from_schema.assert_not_awaited()
        if mismatched_code or commit_fails:
            assert result is None
        else:
            assert result == str(row.id)
            saved = sims.return_value.update_from_schema.await_args.args[1]
            assert saved.status == SimulationStatus.pending
            assert saved.success_rate is None
            db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_persistence_does_not_reuse_previous_id():
    with (
        patch(
            "agents.test_agent.SimulationTestAgent.run_single",
            new=AsyncMock(return_value={"telemetry": [], "mission_completed": False}),
        ),
        patch("graph.nodes._persist_simulation_record", new=AsyncMock(return_value=None)),
    ):
        result = await run_simulation(
            {
                "generated_code": "code",
                "approved_code_sha256": _code_sha256("code"),
                "simulation_record_id": str(uuid4()),
            }
        )
    assert result["simulation_record_id"] is None
    assert any("record was not saved" in warning for warning in result["simulation_warnings"])


@pytest.mark.asyncio
async def test_blocked_resumed_design_drops_old_flight_identity():
    result = await evaluate_simulation(
        good_state(simulation_record_id=str(uuid4()), urdf_path="new_design.urdf")
    )
    assert result["simulation_status"] == "blocked"
    assert result["simulation_record_id"] is None
