"""Tests for the three fixes from the Jun-2026 end-to-end run review:

1. Design-agent timeout is logged informatively (not an empty message) and
   records a fallback_reason diagnostic.
2. HITL approve is idempotent: the orchestrator exposes `is_awaiting_review`
   and the continue path no-ops when no gate is open (root-cause fix for the
   duplicated simulation/monitor runs under rapid repeated approvals).
3. Phase 14 deliverables now fill the mission-code path + hover endurance +
   max speed instead of rendering "—".
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# 1. Design-agent timeout observability
# ─────────────────────────────────────────────────────────────────────────────


class TestDesignTimeoutObservability:
    @pytest.mark.asyncio
    async def test_timeout_records_fallback_reason_and_uses_rule_based(self):
        from agents.design_agent import DesignAgent

        agent = DesignAgent.__new__(DesignAgent)
        agent._diagnostics = {}
        agent.calculator = MagicMock()
        from agents.design_models import DroneDesignTree, PerformanceEstimate

        tree = DroneDesignTree()
        perf = PerformanceEstimate(
            hover_time_s=1200.0,
            max_speed_ms=14.0,
            max_distance_m=5000.0,
            total_weight_g=1500.0,
            thrust_to_weight_ratio=2.4,
        )

        with (
            patch.object(DesignAgent, "_retrieve_similar", new=AsyncMock(return_value=[])),
            patch.object(
                DesignAgent,
                "_generate_design_response",
                new=AsyncMock(side_effect=asyncio.TimeoutError()),
            ),
            patch.object(DesignAgent, "_rule_based_design", return_value=tree) as rb,
            patch.object(
                DesignAgent, "_select_best_design", return_value=(tree, perf, "rule_based", 1)
            ),
        ):
            result = await agent.run("aerial photography quadcopter, 30x30m square")

        # Fell back to rule-based and recorded WHY.
        assert agent._diagnostics.get("fallback_reason") == "llm_timeout"
        rb.assert_called_once()
        assert result.generation_method == "rule_based"

    @pytest.mark.asyncio
    async def test_non_timeout_error_records_typed_reason(self):
        from agents.design_agent import DesignAgent

        agent = DesignAgent.__new__(DesignAgent)
        agent._diagnostics = {}
        agent.calculator = MagicMock()
        from agents.design_models import DroneDesignTree, PerformanceEstimate

        tree = DroneDesignTree()
        perf = PerformanceEstimate(
            hover_time_s=900.0,
            max_speed_ms=12.0,
            max_distance_m=3000.0,
            total_weight_g=1400.0,
            thrust_to_weight_ratio=2.1,
        )

        with (
            patch.object(DesignAgent, "_retrieve_similar", new=AsyncMock(return_value=[])),
            patch.object(
                DesignAgent,
                "_generate_design_response",
                new=AsyncMock(side_effect=ValueError("bad json")),
            ),
            patch.object(DesignAgent, "_rule_based_design", return_value=tree),
            patch.object(
                DesignAgent, "_select_best_design", return_value=(tree, perf, "rule_based", 0)
            ),
        ):
            await agent.run("quadcopter")

        reason = agent._diagnostics.get("fallback_reason", "")
        assert "ValueError" in reason and "bad json" in reason


# ─────────────────────────────────────────────────────────────────────────────
# 2. HITL approve idempotency
# ─────────────────────────────────────────────────────────────────────────────


class TestHITLIdempotency:
    @pytest.mark.asyncio
    async def test_is_awaiting_review_true_when_gate_open(self):
        from agents.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        snapshot = MagicMock()
        snapshot.next = ("human_review_design",)
        graph = MagicMock()
        graph.aget_state = AsyncMock(return_value=snapshot)
        with patch.object(Orchestrator, "_get_graph", new=AsyncMock(return_value=graph)):
            assert await orch.is_awaiting_review("p", "design") is True
            assert await orch.is_awaiting_review("p", "code") is False
            assert await orch.is_awaiting_review("p", None) is True

    @pytest.mark.asyncio
    async def test_is_awaiting_review_false_when_completed(self):
        from agents.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        snapshot = MagicMock()
        snapshot.next = ()  # graph finished — no next node
        graph = MagicMock()
        graph.aget_state = AsyncMock(return_value=snapshot)
        with patch.object(Orchestrator, "_get_graph", new=AsyncMock(return_value=graph)):
            assert await orch.is_awaiting_review("p", "design") is False
            assert await orch.is_awaiting_review("p", None) is False

    @pytest.mark.asyncio
    async def test_continue_pipeline_noops_when_no_gate(self):
        """The continue path must NOT call approve/resume when the gate is
        closed — this is the fix that prevents re-running the pipeline."""
        import services.pipeline_runtime as pr

        approve = AsyncMock()
        resume = AsyncMock()
        with (
            patch.object(pr._ORCHESTRATOR, "is_awaiting_review", new=AsyncMock(return_value=False)),
            patch.object(pr._ORCHESTRATOR, "_resolve_phase", new=AsyncMock(return_value="design")),
            patch.object(pr._ORCHESTRATOR, "approve", new=approve),
            patch.object(pr._ORCHESTRATOR, "resume", new=resume),
        ):
            await pr._continue_pipeline("p", phase="design", approved=True, feedback="")

        approve.assert_not_awaited()
        resume.assert_not_called()

    @pytest.mark.asyncio
    async def test_continue_pipeline_acts_when_gate_open(self):
        import services.pipeline_runtime as pr

        approve = AsyncMock()

        async def _empty_resume(_pid):
            if False:
                yield {}

        with (
            patch.object(pr._ORCHESTRATOR, "is_awaiting_review", new=AsyncMock(return_value=True)),
            patch.object(pr._ORCHESTRATOR, "_resolve_phase", new=AsyncMock(return_value="design")),
            patch.object(pr._ORCHESTRATOR, "approve", new=approve),
            patch.object(pr._ORCHESTRATOR, "resume", new=_empty_resume),
            patch.object(pr, "broadcast_to_project", new=AsyncMock()),
            patch.object(
                pr, "_build_worker_session_factory", return_value=(AsyncMock(), MagicMock())
            ),
        ):
            await pr._continue_pipeline("p", phase="design", approved=True, feedback="")

        approve.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# 3. Deliverables wiring (mission code path + perf values)
# ─────────────────────────────────────────────────────────────────────────────


class TestDeliverableWiring:
    @pytest.mark.asyncio
    async def test_report_has_mission_code_and_perf(self, tmp_path, monkeypatch):
        from graph.nodes import generate_monitor_report
        from config.settings import settings
        from tests.test_orchestrator import _state  # reuse the helper

        monkeypatch.setattr(settings, "storage_path", str(tmp_path))

        mock_report = {
            "metrics": {"flight_quality_score": 90.0, "flight_quality_grade": "A"},
            "recommendations": [],
            "overall_score": 90.0,
        }
        st = _state(
            project_id="deliv-perf-001",
            telemetry_data=[{"alt_m": 12.0}],
            simulation_success=True,
            simulation_iteration=1,
            mission_completed=True,
            generated_code="async def run_mission(d):\n    await d.takeoff(altitude_m=12.0)\n",
            design_tree={
                "frame": {"type": "X", "arm_length_mm": 250},
                "battery": {"cells": 4, "capacity_mah": 5000, "weight_g": 480},
                "topology": "quadcopter",
            },
            performance_estimate={
                "hover_time_s": 1320.0,
                "max_speed_ms": 16.0,
                "max_distance_m": 6000.0,
                "total_weight_g": 1500.0,
                "thrust_to_weight_ratio": 2.4,
            },
        )
        with patch(
            "agents.monitor_agent.MonitorAgent.run",
            new_callable=AsyncMock,
            return_value=mock_report,
        ):
            result = await generate_monitor_report(st)

        paths = result.get("deliverables_paths") or []
        assert paths, "no deliverables written"

        deliv = tmp_path / "deliv-perf-001" / "deliverables"
        report = (deliv / "report.md").read_text(encoding="utf-8")
        # Mission code path filled (not "—")
        assert "mission_code.py" in report
        # Hover endurance = 1320s / 60 = 22.0 min
        assert "22.0 min" in report
        # Max speed surfaced
        assert "16.0 m/s" in report
        # And the mission code file was actually persisted.
        assert (tmp_path / "deliv-perf-001" / "mission_code.py").exists()
