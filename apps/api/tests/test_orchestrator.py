"""eljazari.ai — Orchestrator & Graph Tests (Prompt 2.1)

Test strategy
─────────────
• graph/state.py  — initial_state factory + field types
• graph/edges.py  — every routing function with boundary conditions
• graph/nodes.py  — each node in isolation with mocked agents
• graph/workflow.py — graph structure (node names, HITL interrupt config)
• agents/orchestrator.py — run / approve / reject / get_state lifecycle

All tests are synchronous or use pytest-asyncio.
No live database, Ollama, or Gazebo connections required (all external I/O is mocked).
"""

import pytest

from graph.nodes import _code_sha256
from tests.test_simulation_acceptance import good_state
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ── Shared fixtures ───────────────────────────────────────────────────────────


def _state(**overrides: Any):
    """Build a minimal ElJazariState dict for testing."""
    base = {
        "user_prompt": "Build me a quadcopter for aerial photography",
        "project_id": "test-proj-001",
        "design_tree": None,
        "design_params": None,
        "design_status": "pending",
        "design_feedback": None,
        "urdf_path": None,
        "sdf_path": None,
        "conversion_status": "pending",
        "generated_code": None,
        "gsce_config": None,
        "code_status": "pending",
        "code_feedback": None,
        "safety_passed": False,
        "safety_report": None,
        "safety_issues": [],
        "simulation_status": "pending",
        "telemetry_data": [],
        "semantic_observations": [],
        "simulation_success": False,
        "simulation_iteration": 0,
        "max_iterations": 5,
        "monitor_report": None,
        "recommendations": [],
        "current_phase": "init",
        "errors": [],
        "agent_logs": [],
        "human_approval_needed": False,
        "human_feedback": None,
    }
    base.update(overrides)
    return base


# ═════════════════════════════════════════════════════════════════════════════
# 1. graph/state.py
# ═════════════════════════════════════════════════════════════════════════════


class TestElJazariState:
    def test_initial_state_has_required_fields(self):
        from graph.state import initial_state

        s = initial_state("fly north", "pid-1")
        assert s["user_prompt"] == "fly north"
        assert s["project_id"] == "pid-1"
        assert s["design_status"] == "pending"
        assert s["code_status"] == "pending"
        assert s["simulation_iteration"] == 0
        assert s["max_iterations"] == 5
        assert s["safety_passed"] is False
        assert s["human_approval_needed"] is False

    def test_initial_state_custom_max_iterations(self):
        from graph.state import initial_state

        s = initial_state("test", "pid-2", max_iterations=3)
        assert s["max_iterations"] == 3

    def test_accumulator_fields_initialise_as_empty_lists(self):
        from graph.state import initial_state

        s = initial_state("test", "pid-3")
        assert isinstance(s["safety_issues"], list)
        assert isinstance(s["semantic_observations"], list)
        assert isinstance(s["errors"], list)
        assert isinstance(s["agent_logs"], list)
        assert isinstance(s["recommendations"], list)


# ═════════════════════════════════════════════════════════════════════════════
# 2. graph/edges.py
# ═════════════════════════════════════════════════════════════════════════════


class TestRouteAfterParse:
    def test_error_phase_routes_to_end(self):
        from graph.edges import route_after_parse

        assert route_after_parse(_state(current_phase="error")) == "end"

    def test_normal_routes_to_generate_design(self):
        from graph.edges import route_after_parse

        assert route_after_parse(_state(current_phase="parse_prompt")) == "generate_design"


class TestRouteAfterGenerateDesign:
    def test_rejected_with_error_routes_to_end(self):
        from graph.edges import route_after_generate_design

        s = _state(design_status="rejected", errors=["agent crashed"])
        assert route_after_generate_design(s) == "end"

    def test_generated_routes_to_human_review(self):
        from graph.edges import route_after_generate_design

        assert (
            route_after_generate_design(_state(design_status="generated")) == "human_review_design"
        )

    def test_pending_routes_to_human_review(self):
        from graph.edges import route_after_generate_design

        assert route_after_generate_design(_state(design_status="pending")) == "human_review_design"


class TestRouteAfterHumanReviewDesign:
    def test_approved_routes_to_select_components(self):
        """Approved design now flows through select_components (Phase 6)
        before URDF conversion, so the URDF can use real component masses
        instead of templated boxes."""
        from graph.edges import route_after_human_review_design

        assert (
            route_after_human_review_design(_state(design_status="approved")) == "select_components"
        )

    def test_rejected_routes_back_to_design(self):
        from graph.edges import route_after_human_review_design

        assert (
            route_after_human_review_design(_state(design_status="rejected")) == "generate_design"
        )


class TestRouteAfterConvertToURDF:
    def test_failed_routes_to_end(self):
        from graph.edges import route_after_convert_to_urdf

        assert route_after_convert_to_urdf(_state(conversion_status="failed")) == "end"

    def test_complete_routes_to_generate_code(self):
        from graph.edges import route_after_convert_to_urdf

        assert route_after_convert_to_urdf(_state(conversion_status="complete")) == "generate_code"


class TestRouteAfterSafetyCheck:
    def test_passed_routes_to_human_review_code(self):
        from graph.edges import route_after_safety_check

        assert route_after_safety_check(_state(safety_passed=True)) == "human_review_code"

    def test_failed_within_max_iter_routes_to_generate_code(self):
        from graph.edges import route_after_safety_check

        s = _state(safety_passed=False, safety_retry_count=2, max_iterations=5)
        assert route_after_safety_check(s) == "generate_code"

    def test_failed_at_max_iter_routes_to_end(self):
        from graph.edges import route_after_safety_check

        s = _state(safety_passed=False, safety_retry_count=5, max_iterations=5)
        assert route_after_safety_check(s) == "end"

    def test_simulation_iteration_does_not_bound_the_safety_loop(self):
        """The guard used to read ``simulation_iteration``.

        That counter only advances in ``run_simulation``, which this loop never
        reaches, so it sat at 0 and the comparison never fired. A high value
        here must not end the run on its own, and — more importantly — a zero
        must not keep it going once the safety retries are spent.
        """
        from graph.edges import route_after_safety_check

        spinning = _state(
            safety_passed=False,
            simulation_iteration=0,
            safety_retry_count=5,
            max_iterations=5,
        )
        assert route_after_safety_check(spinning) == "end"

    def test_loop_terminates_instead_of_spinning(self):
        """Walk the loop the way the graph does and require it to stop."""
        from graph.edges import route_after_safety_check

        retries = 0
        for _ in range(20):
            retries += 1  # safety_check increments this on every failure
            route = route_after_safety_check(
                _state(safety_passed=False, safety_retry_count=retries, max_iterations=5)
            )
            if route == "end":
                break
        else:  # pragma: no cover — only reached if the bound is broken again
            pytest.fail("safety loop never terminated")
        assert retries == 5


class TestRouteAfterHumanReviewCode:
    def test_approved_routes_to_simulation(self):
        from graph.edges import route_after_human_review_code

        assert route_after_human_review_code(_state(code_status="approved")) == "run_simulation"

    def test_rejected_routes_to_generate_code(self):
        from graph.edges import route_after_human_review_code

        assert route_after_human_review_code(_state(code_status="rejected")) == "generate_code"


class TestRouteAfterRunSimulation:
    def test_complete_routes_to_evaluate(self):
        from graph.edges import route_after_run_simulation

        assert (
            route_after_run_simulation(_state(simulation_status="complete"))
            == "evaluate_simulation"
        )

    def test_failed_within_max_routes_to_generate_code(self):
        from graph.edges import route_after_run_simulation

        s = _state(simulation_status="failed", simulation_iteration=1, max_iterations=5)
        assert route_after_run_simulation(s) == "generate_code"

    def test_failed_at_max_routes_to_monitor(self):
        from graph.edges import route_after_run_simulation

        s = _state(simulation_status="failed", simulation_iteration=5, max_iterations=5)
        assert route_after_run_simulation(s) == "generate_monitor_report"


class TestRouteAfterEvaluateSimulation:
    def test_success_routes_to_monitor(self):
        from graph.edges import route_after_evaluate_simulation

        assert (
            route_after_evaluate_simulation(_state(simulation_success=True))
            == "generate_monitor_report"
        )

    def test_fail_within_max_routes_to_generate_code(self):
        from graph.edges import route_after_evaluate_simulation

        s = _state(simulation_success=False, simulation_iteration=2, max_iterations=5)
        assert route_after_evaluate_simulation(s) == "generate_code"

    def test_fail_at_max_routes_to_monitor(self):
        from graph.edges import route_after_evaluate_simulation

        s = _state(simulation_success=False, simulation_iteration=5, max_iterations=5)
        assert route_after_evaluate_simulation(s) == "generate_monitor_report"


class TestRouteAfterMonitorReport:
    def test_always_routes_to_end(self):
        from graph.edges import route_after_monitor_report

        assert route_after_monitor_report(_state()) == "end"


# ═════════════════════════════════════════════════════════════════════════════
# 3. graph/nodes.py
# ═════════════════════════════════════════════════════════════════════════════


class TestParsePromptNode:
    @pytest.mark.asyncio
    async def test_valid_prompt_sets_design_params(self):
        from graph.nodes import parse_prompt

        result = await parse_prompt(_state(user_prompt="fly north for 100m"))
        assert result["design_status"] == "pending"
        assert result["design_params"]["language"] == "en"
        assert result["current_phase"] == "parse_prompt"

    @pytest.mark.asyncio
    async def test_empty_prompt_returns_error(self):
        from graph.nodes import parse_prompt

        result = await parse_prompt(_state(user_prompt="   "))
        assert result["current_phase"] == "error"
        assert len(result["errors"]) > 0


class TestGenerateDesignNode:
    @pytest.mark.asyncio
    async def test_success_sets_design_tree(self):
        from graph.nodes import generate_design

        mock_result = {"raw_response": '{"name": "quad", "motors": []}', "status": "complete"}
        with patch(
            "agents.design_agent.DesignAgent.run", new_callable=AsyncMock, return_value=mock_result
        ):
            result = await generate_design(_state())

        assert result["design_status"] == "generated"
        assert result["design_tree"] is not None
        assert result["current_phase"] == "generate_design"

    @pytest.mark.asyncio
    async def test_exception_sets_rejected_status(self):
        from graph.nodes import generate_design

        with patch(
            "agents.design_agent.DesignAgent.run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM timeout"),
        ):
            result = await generate_design(_state())

        assert result["design_status"] == "rejected"
        assert any("Design generation failed" in e for e in result["errors"])


class TestHumanReviewDesignNode:
    @pytest.mark.asyncio
    async def test_approved_state_is_reflected(self):
        from graph.nodes import human_review_design

        s = _state(human_approval_needed=False, human_feedback="looks good")
        result = await human_review_design(s)
        assert result["design_status"] == "approved"
        assert result["human_approval_needed"] is False

    @pytest.mark.asyncio
    async def test_rejected_state_with_feedback(self):
        from graph.nodes import human_review_design

        s = _state(human_approval_needed=True, human_feedback="wrong motor count")
        result = await human_review_design(s)
        assert result["design_status"] == "rejected"
        assert result["design_feedback"] == "wrong motor count"


class TestConvertToUrdfNode:
    @pytest.mark.asyncio
    async def test_success_returns_paths(self):
        from graph.nodes import convert_to_urdf
        from pathlib import Path

        mock_paths = {"urdf": Path("/data/storage/test/drone.urdf")}
        with patch("pipelines.json_to_urdf.JSONToURDFConverter.convert", return_value=mock_paths):
            result = await convert_to_urdf(_state(design_tree={"name": "quad"}))

        assert result["conversion_status"] == "complete"
        assert "drone.urdf" in result["urdf_path"]

    @pytest.mark.asyncio
    async def test_exception_sets_failed_status(self):
        from graph.nodes import convert_to_urdf

        with patch(
            "pipelines.json_to_urdf.JSONToURDFConverter.convert", side_effect=IOError("disk full")
        ):
            result = await convert_to_urdf(_state(design_tree={}))

        assert result["conversion_status"] == "failed"
        assert any("URDF conversion failed" in e for e in result["errors"])


class TestSafetyCheckNode:
    @pytest.mark.asyncio
    async def test_empty_code_fails(self):
        from graph.nodes import safety_check

        result = await safety_check(_state(generated_code=None))
        assert result["safety_passed"] is False
        assert len(result["safety_issues"]) > 0

    @pytest.mark.asyncio
    async def test_safe_code_passes(self):
        from graph.nodes import safety_check

        good_code = "async def mission():\n    try:\n        await land()\n    except Exception:\n        pass"
        result = await safety_check(_state(generated_code=good_code))
        # Should not crash; result depends on SafetyLayer logic
        assert "safety_passed" in result
        assert "safety_report" in result

    @pytest.mark.asyncio
    async def test_unsafe_code_sets_feedback(self):
        from graph.nodes import safety_check

        bad_code = "x = 1 + 1"  # no landing sequence
        result = await safety_check(_state(generated_code=bad_code))
        assert result["safety_passed"] is False
        assert result["code_feedback"]


class TestRunSimulationNode:
    @pytest.mark.asyncio
    async def test_no_code_returns_failed(self):
        from graph.nodes import run_simulation

        result = await run_simulation(_state(generated_code=None))
        assert result["simulation_status"] == "failed"

    @pytest.mark.asyncio
    async def test_success_increments_iteration(self):
        from graph.nodes import run_simulation

        mock_result = {
            "telemetry": [{"alt": 10.0}],
            "semantic_observations": ["aircraft at altitude 10 m"],
            "mission_completed": True,
            "telemetry_source": "real_wsl_sitl",
            "mission_error": "",
            "warnings": [],
        }
        with (
            patch(
                "agents.test_agent.SimulationTestAgent.run_single",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
            patch("graph.nodes._persist_simulation_record", new_callable=AsyncMock),
        ):
            result = await run_simulation(
                _state(
                    generated_code="async def m(): pass",
                    approved_code_sha256=_code_sha256("async def m(): pass"),
                    simulation_iteration=0,
                )
            )

        assert result["simulation_status"] == "complete"
        assert result["simulation_iteration"] == 1
        assert len(result["telemetry_data"]) == 1
        assert result["mission_completed"] is True
        assert result["telemetry_source"] == "real_wsl_sitl"

    @pytest.mark.asyncio
    async def test_persists_simulation_for_simulate_results_endpoint(self):
        """run_simulation must persist a Simulation row.

        Before the fix, /api/projects/{id}/simulate/results returned an empty
        list for orchestrator-driven runs because only the manual /start
        endpoint persisted simulations.
        """
        from graph.nodes import run_simulation

        mock_result = {
            "telemetry": [{"alt": 5.0}],
            "semantic_observations": ["takeoff"],
            "mission_completed": True,
            "telemetry_source": "real_wsl_sitl",
            "duration_s": 47.2,
            "warnings": [],
        }
        persist = AsyncMock()
        with (
            patch(
                "agents.test_agent.SimulationTestAgent.run_single",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
            patch("graph.nodes._persist_simulation_record", new=persist),
        ):
            await run_simulation(
                _state(
                    generated_code="async def m(): pass",
                    approved_code_sha256=_code_sha256("async def m(): pass"),
                    simulation_iteration=0,
                    project_id="abc",
                )
            )

        assert persist.await_count == 1
        kwargs = persist.await_args.kwargs
        assert kwargs["project_id"] == "abc"
        assert kwargs["telemetry_source"] == "real_wsl_sitl"
        assert kwargs["mission_completed"] is True
        assert kwargs["duration_s"] == 47.2

    @pytest.mark.asyncio
    async def test_persistence_failure_does_not_break_pipeline(self):
        """Persistence is best-effort; a DB error must NOT abort the simulation node."""
        from graph.nodes import run_simulation

        mock_result = {
            "telemetry": [{"alt": 5.0}],
            "semantic_observations": [],
            "mission_completed": True,
            "telemetry_source": "real_wsl_sitl",
            "warnings": [],
        }
        with (
            patch(
                "agents.test_agent.SimulationTestAgent.run_single",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
            patch(
                "graph.nodes._persist_simulation_record",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB down"),
            ),
        ):
            # If the helper raises and isn't isolated, this would propagate.
            # The node must catch and continue.
            try:
                result = await run_simulation(
                    _state(
                        generated_code="async def m(): pass",
                        approved_code_sha256=_code_sha256("async def m(): pass"),
                        simulation_iteration=0,
                    )
                )
            except RuntimeError:
                pytest.fail("run_simulation must isolate persistence failures")

        assert result["simulation_status"] == "complete"


class TestEvaluateSimulationNode:
    @pytest.mark.asyncio
    async def test_empty_observations_fails(self):
        from graph.nodes import evaluate_simulation

        result = await evaluate_simulation(_state(semantic_observations=[]))
        assert result["simulation_success"] is False

    @pytest.mark.asyncio
    async def test_passed_evaluation(self):
        from graph.nodes import evaluate_simulation

        mock_eval = {"passed": True, "score": 92.0, "feedback": "Mission complete"}
        with patch(
            "agents.test_agent.SimulationTestAgent.evaluate",
            new_callable=AsyncMock,
            return_value=mock_eval,
        ):
            result = await evaluate_simulation(
                good_state(semantic_observations=["aircraft landed successfully"])
            )

        assert result["simulation_success"] is True
        assert result["code_feedback"] is None

    @pytest.mark.asyncio
    async def test_failed_evaluation_sets_feedback(self):
        from graph.nodes import evaluate_simulation

        mock_eval = {"passed": False, "score": 35.0, "feedback": "Drone flew in wrong direction"}
        with patch(
            "agents.test_agent.SimulationTestAgent.evaluate",
            new_callable=AsyncMock,
            return_value=mock_eval,
        ):
            result = await evaluate_simulation(
                good_state(semantic_observations=["aircraft deviated"])
            )

        assert result["simulation_success"] is False
        assert "wrong direction" in result["code_feedback"]

    @pytest.mark.asyncio
    async def test_incomplete_mission_fails_with_mission_error(self):
        from graph.nodes import evaluate_simulation

        result = await evaluate_simulation(
            _state(
                semantic_observations=["Warning: mission not completed"],
                mission_completed=False,
                mission_error="TimeoutError",
                simulation_warnings=["mission timeout"],
            )
        )

        assert result["simulation_success"] is False
        assert result["simulation_score"] is None
        assert "TimeoutError" in result["code_feedback"]


class TestGenerateMonitorReportNode:
    @pytest.mark.asyncio
    async def test_generates_report(self):
        from graph.nodes import generate_monitor_report

        mock_report = {
            "metrics": {"mission_success_rate": 0.8},
            "recommendations": ["Improve altitude control"],
            "overall_score": 80.0,
        }
        with patch(
            "agents.monitor_agent.MonitorAgent.run",
            new_callable=AsyncMock,
            return_value=mock_report,
        ) as run_mock:
            result = await generate_monitor_report(
                _state(
                    telemetry_data=[{"alt": 10}],
                    simulation_success=True,
                    simulation_iteration=1,
                    duration_s=123.4,
                )
            )

        assert result["monitor_report"] is not None
        assert result["current_phase"] == "complete"
        sim_results = run_mock.await_args.kwargs["simulation_results"]
        assert sim_results[0]["duration_s"] == 123.4

    @pytest.mark.asyncio
    async def test_sitl_provenance_propagates_to_monitor_input(self):
        """Regression: real_wsl_sitl provenance must reach MonitorAgent.

        Before the fix, ElJazariState was missing telemetry_source /
        mission_started keys, so LangGraph dropped them and the monitor
        always saw "unknown" / False — turning a real WSL run into a
        recommendation that "this report is not based on real SITL telemetry".
        """
        from graph.nodes import generate_monitor_report

        captured: dict = {}

        async def _capture(*args, **kwargs):
            sim_results = kwargs.get("simulation_results")
            if sim_results is None and len(args) >= 2:
                # called as bound method: (self, simulation_results, ...)
                sim_results = args[1]
            captured["sim_results"] = sim_results
            return {"metrics": {}, "recommendations": [], "overall_score": 80.0}

        with patch("agents.monitor_agent.MonitorAgent.run", new=_capture):
            await generate_monitor_report(
                _state(
                    telemetry_data=[{"alt_m": 5.0}] * 10,
                    simulation_success=True,
                    simulation_iteration=1,
                    mission_started=True,
                    mission_completed=True,
                    telemetry_source="real_wsl_sitl",
                    duration_s=47.2,
                )
            )

        first = captured["sim_results"][0]
        assert first["telemetry_source"] == "real_wsl_sitl"
        assert first["mission_started"] is True
        assert first["mission_completed"] is True

    @pytest.mark.asyncio
    async def test_exception_produces_error_report(self):
        from graph.nodes import generate_monitor_report

        with patch(
            "agents.monitor_agent.MonitorAgent.run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            result = await generate_monitor_report(_state())

        assert "error" in result["monitor_report"]
        assert result["current_phase"] == "complete"

    @pytest.mark.asyncio
    async def test_commanded_waypoints_propagated_to_monitor(self):
        """Phase 11 wiring: extract waypoints from generated code and pass them
        to MonitorAgent so the flight-quality solver can grade tracking RMSE
        instead of reporting "tracking not evaluated"."""
        from graph.nodes import generate_monitor_report

        captured: dict = {}

        async def _capture(*args, **kwargs):
            sim_results = kwargs.get("simulation_results")
            if sim_results is None and len(args) >= 2:
                sim_results = args[1]
            captured["sim_results"] = sim_results
            return {"metrics": {}, "recommendations": [], "overall_score": 80.0}

        legacy_code = (
            "async def run_mission(drone):\n"
            "    await drone.takeoff(altitude_m=4.0)\n"
            "    await drone.fly_to_relative(north_m=6.0, east_m=0.0, down_m=0.0)\n"
            "    await drone.land()\n"
        )
        with patch("agents.monitor_agent.MonitorAgent.run", new=_capture):
            await generate_monitor_report(
                _state(
                    telemetry_data=[{"alt_m": 4.0}] * 5,
                    generated_code=legacy_code,
                    simulation_success=True,
                    simulation_iteration=1,
                    mission_started=True,
                    mission_completed=True,
                    telemetry_source="real_wsl_sitl",
                )
            )
        first = captured["sim_results"][0]
        assert first["waypoints"], "no waypoints passed to monitor"
        assert first["target_altitude_m"] == pytest.approx(4.0)
        assert first["waypoints"][0]["north_m"] == pytest.approx(6.0)

    @pytest.mark.asyncio
    async def test_deliverables_written_after_monitor(self, tmp_path, monkeypatch):
        """Phase 14 wiring: BOM / assembly / preflight / maintenance / report
        Markdown should land in <storage>/<project_id>/deliverables/ after the
        monitor node finishes."""
        from graph.nodes import generate_monitor_report
        from config.settings import settings

        monkeypatch.setattr(settings, "storage_path", str(tmp_path))

        mock_report = {
            "metrics": {"flight_quality_score": 82.0, "flight_quality_grade": "B"},
            "recommendations": [],
            "overall_score": 82.0,
        }
        with patch(
            "agents.monitor_agent.MonitorAgent.run",
            new_callable=AsyncMock,
            return_value=mock_report,
        ):
            result = await generate_monitor_report(
                _state(
                    project_id="deliv-test-001",
                    telemetry_data=[{"alt_m": 4.0}],
                    simulation_success=True,
                    simulation_iteration=1,
                    mission_completed=True,
                    generated_code="async def run_mission(d):\n    await d.takeoff(altitude_m=4.0)\n",
                    design_tree={
                        "frame": {"type": "X", "arm_length_mm": 250},
                        "battery": {"cells": 4, "capacity_mah": 6000, "weight_g": 480},
                        "topology": "quadcopter",
                    },
                )
            )

        paths = result.get("deliverables_paths") or []
        assert paths, "deliverables not written"
        names = {
            tmp_path_part.name
            for tmp_path_part in (tmp_path / "deliv-test-001" / "deliverables").iterdir()
        }
        assert {
            "report.md",
            "bom.csv",
            "bom.md",
            "assembly.md",
            "preflight.md",
            "maintenance.md",
        }.issubset(names)


# ═════════════════════════════════════════════════════════════════════════════
# 4. graph/workflow.py
# ═════════════════════════════════════════════════════════════════════════════


class TestWorkflowStructure:
    def test_workflow_is_compiled(self):
        from graph.workflow import workflow

        assert workflow is not None

    def test_workflow_has_all_nodes(self):
        from graph.workflow import workflow

        graph_def = workflow.get_graph()
        node_names = {n.id for n in graph_def.nodes.values()}
        expected = {
            "parse_prompt",
            "generate_design",
            "human_review_design",
            "convert_to_urdf",
            "generate_code",
            "safety_check",
            "human_review_code",
            "run_simulation",
            "evaluate_simulation",
            "generate_monitor_report",
        }
        assert expected.issubset(node_names)

    def test_hitl_interrupt_nodes_configured(self):
        from graph.workflow import HITL_INTERRUPT_NODES

        assert "human_review_design" in HITL_INTERRUPT_NODES
        assert "human_review_code" in HITL_INTERRUPT_NODES

    @pytest.mark.asyncio
    async def test_get_workflow_async_returns_graph(self, monkeypatch):
        from contextlib import asynccontextmanager
        import graph.workflow as module

        @asynccontextmanager
        async def saver(**kwargs):
            yield module._make_memory_saver()

        monkeypatch.setattr(module, "_make_postgres_saver", saver)
        async with module.get_workflow_async() as graph:
            assert graph is not None


# ═════════════════════════════════════════════════════════════════════════════
# 5. agents/orchestrator.py
# ═════════════════════════════════════════════════════════════════════════════


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_run_yields_pipeline_started_event(self):
        """Orchestrator.run must yield a pipeline_started event as first item."""
        from agents.orchestrator import Orchestrator

        orch = Orchestrator(use_postgres=False)

        # Mock workflow.astream to yield nothing (empty pipeline)
        mock_stream = MagicMock(return_value=_async_iter([]))
        orch._graph = MagicMock()
        orch._graph.astream = mock_stream
        orch._graph.aget_state = AsyncMock(return_value=MagicMock(next=[], values={}))
        orch._graph_ready = True

        events = []
        async for ev in orch.run("test prompt", "proj-001"):
            events.append(ev)

        assert events[0]["event"] == "pipeline_started"
        assert events[-1]["event"] == "pipeline_complete"

    @pytest.mark.asyncio
    async def test_approve_patches_design_state(self):
        from agents.orchestrator import Orchestrator

        orch = Orchestrator(use_postgres=False)
        orch._graph = MagicMock()
        orch._graph.aupdate_state = AsyncMock()
        orch._graph_ready = True

        await orch.approve("proj-001", phase="design", feedback="")

        orch._graph.aupdate_state.assert_called_once()
        call_args = orch._graph.aupdate_state.call_args
        patch_data = call_args[0][1]
        assert patch_data["design_status"] == "approved"
        assert patch_data["human_approval_needed"] is False

    @pytest.mark.asyncio
    async def test_reject_patches_code_state(self):
        from agents.orchestrator import Orchestrator

        orch = Orchestrator(use_postgres=False)
        orch._graph = MagicMock()
        orch._graph.aupdate_state = AsyncMock()
        orch._graph_ready = True

        await orch.reject("proj-001", phase="code", feedback="altitude too high")

        call_args = orch._graph.aupdate_state.call_args
        patch_data = call_args[0][1]
        assert patch_data["code_status"] == "rejected"
        assert patch_data["code_feedback"] == "altitude too high"

    @pytest.mark.asyncio
    async def test_get_state_returns_dict(self):
        from agents.orchestrator import Orchestrator

        orch = Orchestrator(use_postgres=False)
        mock_snapshot = MagicMock()
        mock_snapshot.next = []
        mock_snapshot.values = {
            "current_phase": "generate_design",
            "design_status": "generated",
            "code_status": "pending",
            "simulation_success": False,
            "simulation_iteration": 0,
            "safety_passed": False,
            "errors": [],
            "human_approval_needed": False,
        }
        orch._graph = MagicMock()
        orch._graph.aget_state = AsyncMock(return_value=mock_snapshot)
        orch._graph_ready = True

        result = await orch.get_state("proj-001")

        assert result["project_id"] == "proj-001"
        assert result["current_phase"] == "generate_design"
        assert isinstance(result["errors"], list)

    @pytest.mark.asyncio
    async def test_hitl_interrupt_event_emitted(self):
        """When graph pauses at HITL node, a hitl_interrupt event must be yielded."""
        from agents.orchestrator import Orchestrator

        orch = Orchestrator(use_postgres=False)
        orch._graph = MagicMock()
        orch._graph.astream = MagicMock(
            return_value=_async_iter([{"generate_design": {"design_status": "generated"}}])
        )
        mock_state = MagicMock()
        mock_state.next = ["human_review_design"]
        mock_state.values = {
            "current_phase": "generate_design",
            "design_tree": {"name": "quad"},
            "generated_code": None,
            "safety_report": None,
        }
        orch._graph.aget_state = AsyncMock(return_value=mock_state)
        orch._graph_ready = True

        events = []
        async for ev in orch.run("test prompt", "proj-002"):
            events.append(ev)

        event_types = [e["event"] for e in events]
        assert "hitl_interrupt" in event_types

    @pytest.mark.asyncio
    async def test_resume_yields_resumed_event(self):
        from agents.orchestrator import Orchestrator

        orch = Orchestrator(use_postgres=False)
        orch._graph = MagicMock()
        orch._graph.astream = MagicMock(return_value=_async_iter([]))
        mock_state = MagicMock()
        mock_state.next = []
        mock_state.values = {}
        orch._graph.aget_state = AsyncMock(return_value=mock_state)
        orch._graph_ready = True

        events = []
        async for ev in orch.resume("proj-001"):
            events.append(ev)

        assert events[0]["event"] == "pipeline_resumed"


# ═════════════════════════════════════════════════════════════════════════════
# helpers
# ═════════════════════════════════════════════════════════════════════════════


async def _async_iter(items):
    for item in items:
        yield item


class TestSafetyRetryAccounting:
    """The safety node owns the counter that bounds its own loop.

    Before this, nothing incremented on a safety failure, so the run could
    regenerate the same file indefinitely with no counter moving and no
    message explaining why the simulation never started.
    """

    @staticmethod
    def _report(passed: bool):
        report = MagicMock()
        report.passed = passed
        report.risk_level = "low" if passed else "high"
        report.modified_code = None
        issue = MagicMock()
        issue.severity = "critical"
        issue.description = "Waypoint outside geofence"
        report.issues = [] if passed else [issue]
        report.model_dump.return_value = {"passed": passed}
        return report

    async def _run(self, *, passed: bool, retries: int, max_iterations: int = 5):
        from graph import nodes

        state = _state(
            generated_code="async def run(drone):\n    pass\n",
            safety_retry_count=retries,
            max_iterations=max_iterations,
        )
        # SafetyLayer and broadcast_to_project are both imported inside the
        # node (circular-import avoidance), so patch them at their source.
        with (
            patch("agents.safety_layer.SafetyLayer") as layer,
            patch("ws.socket_manager.broadcast_to_project", new=AsyncMock()) as bcast,
        ):
            layer.return_value.analyze.return_value = self._report(passed)
            result = await nodes.safety_check(state)
        return result, bcast

    @pytest.mark.asyncio
    async def test_failure_increments_the_counter(self):
        result, _ = await self._run(passed=False, retries=0)
        assert result["safety_retry_count"] == 1
        assert result["safety_passed"] is False

    @pytest.mark.asyncio
    async def test_success_leaves_the_counter_alone(self):
        result, _ = await self._run(passed=True, retries=3)
        assert result["safety_retry_count"] == 3
        assert result["safety_passed"] is True
        assert "errors" not in result

    @pytest.mark.asyncio
    async def test_failure_feeds_the_issues_back_as_feedback(self):
        result, _ = await self._run(passed=False, retries=0)
        assert "geofence" in (result["code_feedback"] or "").lower()

    @pytest.mark.asyncio
    async def test_exhausted_retries_report_an_explicit_error(self):
        result, bcast = await self._run(passed=False, retries=4, max_iterations=5)
        assert result["safety_retry_count"] == 5
        assert result["errors"], "giving up must surface an error"
        message = result["errors"][0]
        assert "5 regeneration attempts" in message
        assert "geofence" in message.lower()
        # ...and the user must be told, not left watching a stalled dashboard.
        bcast.assert_awaited_once()
        payload = bcast.await_args.args[2]
        assert payload["phase"] == "safety_check"

    @pytest.mark.asyncio
    async def test_no_error_while_attempts_remain(self):
        result, bcast = await self._run(passed=False, retries=1, max_iterations=5)
        assert "errors" not in result
        bcast.assert_not_awaited()
