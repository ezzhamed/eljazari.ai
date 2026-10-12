"""Generated models must never silently fly as the reference x500."""

from unittest.mock import AsyncMock, patch

import pytest

from graph.edges import route_after_evaluate_simulation, route_after_run_simulation
from graph.nodes import evaluate_simulation, generate_monitor_report, run_simulation
from graph.state import initial_state
from pipelines.simulation_readiness import generated_model_block_reason
from services.pipeline_runtime import _node_messages_from_event, _persist_simulation


@pytest.mark.parametrize(
    "config",
    [
        {"urdf_path": "missing.urdf"},
        {"sdf_path": "model.sdf"},
        {"model_manifest_path": "model_manifest.json", "flight_ready": True},
        {"resolved_drone_sha256": "stale"},
        {"bom_status": "selected"},
        {"selected_components": {"frame": "custom"}},
    ],
)
def test_generated_artifacts_cannot_fall_back(config):
    assert generated_model_block_reason(config)


def test_reference_runs_remain_available():
    assert generated_model_block_reason({"model": "x500"}) is None
    assert generated_model_block_reason(initial_state("demo", "test")) is None


@pytest.mark.asyncio
async def test_block_before_agent_and_skip_code_retries():
    state = initial_state("demo", "test")
    state.update(
        urdf_path="model.urdf",
        generated_code="pass",
        simulation_success=True,
        simulation_score=99,
        telemetry_data=[{"alt": 5}],
        duration_s=60,
    )
    with patch("agents.test_agent.SimulationTestAgent") as agent:
        result = await run_simulation(state)
    agent.assert_not_called()
    assert result["simulation_status"] == "blocked"
    assert result["simulation_success"] is False
    assert result["simulation_score"] is None
    assert result["telemetry_data"] == []
    assert result["duration_s"] is None
    assert result["mission_started"] is False
    assert route_after_run_simulation(result) == "generate_monitor_report"
    assert "simulation_block_reason" in initial_state("demo", "test")


@pytest.mark.asyncio
async def test_resumed_reference_success_cannot_validate_generated_design():
    with patch("agents.test_agent.SimulationTestAgent") as agent:
        result = await evaluate_simulation(
            {
                "resolved_drone_sha256": "generated",
                "mission_completed": True,
                "semantic_observations": ["Landed successfully"],
                "simulation_success": True,
            }
        )
    agent.assert_not_called()
    assert result["simulation_success"] is False
    assert result["simulation_score"] is None
    assert route_after_evaluate_simulation(result) == "generate_monitor_report"


@pytest.mark.asyncio
async def test_direct_service_rejects_before_side_effects():
    from services.gazebo_service import GazeboService

    service = object.__new__(GazeboService)
    with pytest.raises(ValueError, match="no verified flight bundle"):
        await service.launch_world({"urdf_path": "custom.urdf"})


@pytest.mark.asyncio
async def test_blocked_run_is_not_completed_event_or_database_flight():
    data = {"simulation_status": "blocked", "simulation_block_reason": "Not ready"}
    events = _node_messages_from_event(
        "test",
        {
            "event": "node_complete",
            "node": "run_simulation",
            "data": data,
        },
    )
    assert [event for event, _ in events] == ["sim_status"]
    assert events[0][1]["blocked"] is True
    await _persist_simulation(None, "test", data)


@pytest.mark.asyncio
async def test_report_exposes_block_without_success_score(monkeypatch):
    monkeypatch.setattr(
        "agents.monitor_agent.MonitorAgent.run",
        AsyncMock(
            return_value={
                "overall_score": 90,
                "grade": "A",
                "metrics": {"flight_quality_score": 95},
            }
        ),
    )
    monkeypatch.setattr("graph.nodes._write_engineering_deliverables", lambda *args: ["bom"])
    result = await generate_monitor_report(
        {
            "simulation_status": "blocked",
            "simulation_block_reason": "Not ready",
            "model_validation_scope": "structural_only",
            "resolved_drone_sha256": "abc",
        }
    )
    report = result["monitor_report"]
    assert report["overall_score"] is None
    assert report["grade"] is None
    assert report["metrics"]["flight_quality_score"] is None
    assert report["flight_validation_status"] == "blocked"
    assert report["flight_validation_reason"] == "Not ready"
    assert report["resolved_drone_sha256"] == "abc"
