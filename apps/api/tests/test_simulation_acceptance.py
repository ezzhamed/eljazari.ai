"""Synthetic fixtures test deterministic verdicts, not real flight quality."""

from unittest.mock import AsyncMock, patch

import pytest

from graph.edges import route_after_evaluate_simulation
from graph.nodes import evaluate_simulation, run_simulation
from graph.state import ElJazariState, initial_state
from pipelines.simulation_acceptance import assess_simulation
from services.pipeline_runtime import _node_messages_from_event
from tests.flight_fixtures import flight


def good_state(**overrides):
    result = {
        "simulation_status": "complete",
        "runtime_mode": "wsl",
        "telemetry_source": "real_wsl_sitl",
        "mission_isolation": "linux_namespaces",
        "mission_started": True,
        "mission_completed": True,
        "crash_detected": False,
        "geofence_violation": False,
        "mission_error": "",
        "simulation_iteration": 1,
        # A 5 m hover flown exactly as its code commands it.
        **flight(),
        "semantic_observations": ["Synthetic test observation"],
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize(
    "changes",
    [
        {"telemetry_source": "simulated_fallback"},
        {"telemetry_source": "unknown"},
        {"telemetry_source": "real_wsl_sitl_partial"},
        {"runtime_mode": "docker"},
        {"simulation_status": "failed"},
        {"mission_started": False},
        {"mission_completed": False},
        {"mission_completed": "true"},
        {"mission_error": "Process exited with code 1"},
        {"crash_detected": True},
        {"geofence_violation": True},
        {"crash_detected": None},
        {"telemetry_data": []},
        {"telemetry_data": [{"alt_m": 1}]},
    ],
)
@pytest.mark.asyncio
async def test_rejected_data_cannot_be_overridden_by_llm(changes):
    with patch("agents.test_agent.SimulationTestAgent") as agent:
        result = await evaluate_simulation(good_state(**changes))
    agent.assert_not_called()
    assert result["simulation_success"] is False
    assert result["simulation_score"] is None
    assert result["simulation_acceptance"]["passed"] is False
    assert result["evaluation_status"] == "rejected_by_checks"


@pytest.mark.parametrize(
    "key,value",
    [
        ("timestamp_s", 0.0),
        ("timestamp_s", -1),
        ("alt_m", float("nan")),
        ("north_m", float("inf")),
        ("east_m", "1.0"),
        ("alt_m", True),
    ],
)
def test_bad_samples_fail_closed(key, value):
    state = good_state()
    state["telemetry_data"][1][key] = value
    result = assess_simulation(state)
    assert not result["passed"]
    assert not result["retryable_with_code_change"]


@pytest.mark.asyncio
async def test_semantic_pass_requires_basic_gate():
    with patch(
        "agents.test_agent.SimulationTestAgent.evaluate",
        new=AsyncMock(return_value={"passed": True, "score": 90, "feedback": "Test only"}),
    ) as evaluate:
        result = await evaluate_simulation(good_state())
    evaluate.assert_awaited_once()
    assert result["simulation_success"] is True
    assert result["simulation_acceptance"]["passed"] is True
    assert result["simulation_acceptance"]["generated_aircraft_validated"] is False


@pytest.mark.parametrize(
    "verdict,score", [("true", 90), (True, float("nan")), (True, 101), (True, -1), (True, True)]
)
@pytest.mark.asyncio
async def test_malformed_llm_verdict_is_not_success(verdict, score):
    with patch(
        "agents.test_agent.SimulationTestAgent.evaluate",
        new=AsyncMock(return_value={"passed": verdict, "score": score}),
    ):
        result = await evaluate_simulation(good_state())
    assert result["simulation_success"] is False
    assert result["simulation_score"] is None
    assert result["evaluation_status"] == "failed"


def test_infrastructure_failures_skip_code_regeneration_but_crashes_can_retry():
    state = good_state(telemetry_source="simulated_fallback")
    state["simulation_acceptance"] = assess_simulation(state)
    assert route_after_evaluate_simulation(state) == "generate_monitor_report"
    state = good_state(crash_detected=True)
    state["simulation_acceptance"] = assess_simulation(state)
    assert route_after_evaluate_simulation(state) == "generate_code"


@pytest.mark.asyncio
async def test_failed_attempt_clears_previous_success_and_observations():
    state = good_state(generated_code="pass", simulation_success=True, simulation_score=95)
    with patch(
        "agents.test_agent.SimulationTestAgent.run_single",
        new=AsyncMock(side_effect=RuntimeError("Runtime stopped")),
    ):
        result = await run_simulation(state)
    assert result["simulation_success"] is False
    assert result["simulation_score"] is None
    assert result["semantic_observations"] == []
    assert result["telemetry_data"] == []
    assert result["simulation_acceptance"] is None


def test_graph_observations_are_current_attempt_only():
    from langgraph.graph import StateGraph, END

    graph = StateGraph(ElJazariState)
    graph.add_node("replace", lambda state: {"semantic_observations": ["new attempt"]})
    graph.set_entry_point("replace")
    graph.add_edge("replace", END)
    state = initial_state("test", "test")
    state["semantic_observations"] = ["previous successful attempt"]
    result = graph.compile().invoke(state)
    assert result["semantic_observations"] == ["new attempt"]


def test_collection_complete_event_is_not_a_pass_verdict():
    events = _node_messages_from_event(
        "test",
        {
            "event": "node_complete",
            "node": "run_simulation",
            "data": {"simulation_status": "complete"},
        },
    )
    payload = next(payload for name, payload in events if name == "simulation_complete")
    assert payload["passed"] is False


@pytest.mark.asyncio
async def test_rejected_gate_is_in_report_without_success_score(monkeypatch):
    from graph.nodes import generate_monitor_report

    state = good_state(crash_detected=True)
    state["simulation_acceptance"] = assess_simulation(state)
    monkeypatch.setattr(
        "agents.monitor_agent.MonitorAgent.run",
        AsyncMock(return_value={"overall_score": 95, "metrics": {"flight_quality_score": 95}}),
    )
    monkeypatch.setattr("graph.nodes._write_engineering_deliverables", lambda *args: ["bom"])
    result = await generate_monitor_report(state)
    report = result["monitor_report"]
    assert report["overall_score"] is None
    assert report["simulation_acceptance"]["passed"] is False
    assert report["flight_validation_status"] == "rejected_by_checks"


def test_nonfinite_motor_sample_is_rejected():
    state = good_state()
    state["telemetry_data"][1]["motors"] = [0.5, float("nan")]
    assert not assess_simulation(state)["passed"]


@pytest.mark.asyncio
async def test_acceptance_is_persisted_with_existing_telemetry():
    from types import SimpleNamespace
    from services.pipeline_runtime import _persist_simulation_evaluation

    from uuid import uuid4

    project_id = uuid4()
    row = SimpleNamespace(
        id=uuid4(),
        project_id=project_id,
        iteration=1,
        raw_telemetry={"points": [{"alt_m": 3}], "source": "test"},
    )
    gate = assess_simulation(good_state(crash_detected=True))
    with patch("services.pipeline_runtime.SimulationRepository") as repository:
        repository.return_value.get = AsyncMock(return_value=row)
        update = AsyncMock()
        repository.return_value.update_from_schema = update
        await _persist_simulation_evaluation(
            None,
            project_id,
            {
                "simulation_record_id": str(row.id),
                "simulation_iteration": 1,
                "simulation_success": False,
                "simulation_acceptance": gate,
            },
        )
    payload = update.await_args.args[1]
    assert payload.raw_telemetry["points"] == [{"alt_m": 3}]
    assert payload.raw_telemetry["acceptance"] == gate


@pytest.mark.asyncio
async def test_direct_loop_rejects_synthetic_source_without_llm_or_retries():
    from tests.test_simulation_agent import _agent_with_mocks, _make_telemetry

    agent = _agent_with_mocks()
    agent.px4.execute_mission = AsyncMock(
        return_value=_make_telemetry().model_copy(
            update={"runtime_mode": "mock", "telemetry_source": "simulated_fallback"}
        )
    )
    regenerate = AsyncMock(return_value="new code")
    result = await agent.run_closed_loop("code", {}, "test", code_generator_fn=regenerate)
    assert not result.final_passed
    assert result.total_iterations == 1
    assert result.iteration_results[0].acceptance["passed"] is False
    agent.eval_llm.ainvoke.assert_not_awaited()
    regenerate.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_wrapper_rejects_crash_despite_successful_observations():
    from agents.test_agent import TestAgent
    from tests.test_simulation_agent import _agent_with_mocks, _make_telemetry

    wrapper = TestAgent.__new__(TestAgent)
    wrapper._agent = _agent_with_mocks()
    wrapper._agent.px4.execute_mission = AsyncMock(
        return_value=_make_telemetry(crash=True).model_copy(
            update={
                "runtime_mode": "wsl",
                "telemetry_source": "real_wsl_sitl",
                "mission_isolation": "linux_namespaces",
            }
        )
    )
    result = await wrapper.run("code", "test", {})
    assert not result["passed"]
    assert not result["acceptance"]["passed"]
    wrapper._agent.eval_llm.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_rechecks_cached_success_against_current_data(monkeypatch):
    from graph.nodes import generate_monitor_report

    state = good_state(
        crash_detected=True, simulation_success=True, simulation_acceptance={"passed": True}
    )
    monitor = AsyncMock(return_value={"overall_score": 99})
    monkeypatch.setattr("agents.monitor_agent.MonitorAgent.run", monitor)
    monkeypatch.setattr("graph.nodes._write_engineering_deliverables", lambda *args: ["bom"])
    result = await generate_monitor_report(state)
    assert not result["simulation_success"]
    assert not result["simulation_acceptance"]["passed"]
    assert result["monitor_report"]["overall_score"] is None
    assert not monitor.await_args.kwargs["simulation_results"][0]["passed"]


def test_validation_summary_names_the_aircraft_and_the_verdict():
    from pipelines.simulation_acceptance import summarize_validation
    from services.pipeline_runtime import _node_messages_from_event

    assert summarize_validation({"overall_score": 80}) is None
    blocked = summarize_validation(
        {
            "flight_validation_status": "blocked",
            "flight_validation_reason": "No verified flight bundle",
            "aircraft_under_test": {"kind": "not_flown"},
        }
    )
    assert blocked["status"] == "blocked" and blocked["aircraft"]["kind"] == "not_flown"
    flown = summarize_validation(
        {
            "simulation_acceptance": assess_simulation(good_state()),
            "aircraft_under_test": {"kind": "reference_x500"},
        }
    )
    assert flown["status"] == "passed"
    assert flown["mission_isolation"] == "linux_namespaces"
    assert flown["aircraft"] == {
        "kind": "reference_x500",
        "model_instance": None,
        "validation_scope": None,
        "simulated_total_mass_kg": None,
        "identity_attested": False,
    }
    rejected = summarize_validation(
        {"simulation_acceptance": assess_simulation(good_state(mission_isolation="none"))}
    )
    assert rejected["status"] == "rejected_by_checks"
    assert "Mission code did not run in the isolated sandbox" in rejected["acceptance_reasons"]
    events = _node_messages_from_event(
        "p",
        {
            "event": "node_complete",
            "node": "generate_monitor_report",
            "data": {"monitor_report": {"flight_validation_status": "blocked"}},
        },
    )
    update = next(payload for name, payload in events if name == "monitor_update")
    assert update["data"]["validation"]["status"] == "blocked"
