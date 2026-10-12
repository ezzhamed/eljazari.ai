"""Tests for Phase 4.1 — PerformanceMonitorAgent.

Key fixes verified:
  - analyze_project(project_id) → loads logs from self._logs (internal state)
  - react_eval_cycle(task) → uses self._logs (no extra param)
  - compile_report(project_id) → compiles from self._analyses
  - ReActEval has CONTINUE/STOP decision in Evaluate phase
  - 6 worker agents: trajectory, battery, motors, stability, gps, anomaly
  - GPS/navigation accuracy analysis
  - Flight stability analysis (attitude + vibration)
  - Failure cause analysis in anomaly report
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.performance_models import (
    AnalysisTask,
    AnalysisType,
    AnomalyEvent,
    AnomalyReport,
    AnomalyType,
    BatteryAnalysis,
    FlightLogs,
    FlightStabilityAnalysis,
    FullMonitorReport,
    GPSAccuracyAnalysis,
    MotorAnalysis,
    MotorStatus,
    PerformanceScore,
    ReActDecision,
    ReActEvalStep,
    SimulationRun,
    TaskResult,
    TelemetryPoint,
    TrajectoryAnalysis,
    WaypointPlan,
)
from agents.performance_monitor import (
    PerformanceMonitorAgent,
    _parse_json,
    _rule_based_recommendations,
    _zscore_anomalies,
)
from utils.visualization_helpers import (
    generate_all_viz_data,
    generate_anomaly_timeline,
    generate_battery_chart,
    generate_gps_chart,
    generate_motor_radar,
    generate_score_breakdown,
    generate_score_gauge,
    generate_stability_chart,
    generate_trajectory_plot,
)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════


def _make_telemetry(n: int = 20) -> list[TelemetryPoint]:
    pts = []
    for i in range(n):
        pts.append(
            TelemetryPoint(
                timestamp_s=float(i) * 0.5,
                north_m=float(i) * 2.0,
                east_m=0.0,
                alt_m=15.0,
                speed_ms=5.0 if i > 1 else 0.0,
                battery_pct=max(20.0, 100.0 - i * 2.0),
                battery_voltage=14.8 - i * 0.01,
                roll_deg=2.0,
                pitch_deg=1.5,
                gps_fix=3,
                hdop=1.2,
                satellites=12,
                motor_rpms=[3000.0, 2990.0, 3010.0, 3005.0],
                vibration_x=1.0,
                vibration_y=0.5,
                vibration_z=0.3,
            )
        )
    return pts


def _make_waypoints() -> list[WaypointPlan]:
    return [
        WaypointPlan(index=0, north_m=0, east_m=0, alt_m=15),
        WaypointPlan(index=1, north_m=20, east_m=0, alt_m=15),
        WaypointPlan(index=2, north_m=38, east_m=0, alt_m=15),
    ]


def _make_run(passed: bool = True, n: int = 20) -> SimulationRun:
    return SimulationRun(
        run_id=str(uuid.uuid4())[:8],
        passed=passed,
        duration_s=float(n) * 0.5,
        telemetry=_make_telemetry(n),
        waypoints=_make_waypoints(),
        evaluation={"score": 85.0},
    )


def _make_logs(n_runs: int = 2) -> FlightLogs:
    return FlightLogs(
        project_id="test-project",
        mission_description="Deliver package to waypoint 1 km north",
        planned_waypoints=_make_waypoints(),
        simulation_runs=[_make_run(passed=(i % 2 == 0)) for i in range(n_runs)],
        drone_config={"battery_capacity_mah": 5000, "battery_voltage_v": 14.8},
    )


def _make_agent(logs: FlightLogs = None) -> PerformanceMonitorAgent:
    agent = PerformanceMonitorAgent.__new__(PerformanceMonitorAgent)
    agent.llm = MagicMock()
    agent.llm.ainvoke = AsyncMock(return_value='{"confidence": 0.9, "decision": "stop"}')
    agent._logs = logs or _make_logs()
    agent._project_id = "test-project"
    agent._analyses = {}
    return agent


# ══════════════════════════════════════════════════════════════════════════════
# 1. Pydantic models
# ══════════════════════════════════════════════════════════════════════════════


class TestPydanticModels:
    def test_flight_logs_defaults(self):
        logs = FlightLogs(project_id="p1")
        assert logs.simulation_runs == []

    def test_telemetry_point_all_fields(self):
        pt = TelemetryPoint(gps_fix=3, hdop=1.1, satellites=11)
        assert pt.satellites == 11

    def test_flight_stability_analysis(self):
        s = FlightStabilityAnalysis(
            avg_roll_deg=3.0, max_roll_deg=8.0, vibration_grade="low", score=90.0
        )
        assert s.vibration_grade == "low"

    def test_gps_accuracy_analysis(self):
        g = GPSAccuracyAnalysis(avg_hdop=1.2, avg_satellites=11.5, score=88.0)
        assert g.avg_hdop == 1.2

    def test_react_eval_step_has_decision(self):
        s = ReActEvalStep(
            step=1,
            phase="evaluate",
            content="Good result",
            decision=ReActDecision.CONTINUE,
        )
        assert s.decision == ReActDecision.CONTINUE

    def test_task_result_has_iterations(self):
        task = AnalysisTask(task_id="t1", analysis_type=AnalysisType.BATTERY)
        result = TaskResult(task=task, iterations=2, completed=True)
        assert result.iterations == 2

    def test_anomaly_event_probable_cause(self):
        ev = AnomalyEvent(
            event_type="near_miss",
            severity="critical",
            timestamp_s=5.0,
            run_id="r1",
            description="Near miss",
            probable_cause="Altitude controller failure",
        )
        assert ev.probable_cause == "Altitude controller failure"

    def test_anomaly_report_failure_causes(self):
        ar = AnomalyReport(identified_failure_causes=["Motor failure", "GPS loss"], score=70.0)
        assert len(ar.identified_failure_causes) == 2

    def test_full_report_has_stability_and_gps(self):
        report = FullMonitorReport(
            project_id="p1",
            stability=FlightStabilityAnalysis(score=85.0),
            gps=GPSAccuracyAnalysis(score=90.0),
        )
        assert report.stability is not None
        assert report.gps is not None

    def test_performance_score_has_stability_gps(self):
        ps = PerformanceScore(stability=80.0, gps=75.0, overall=78.0, grade="B")
        assert ps.stability == 80.0
        assert ps.gps == 75.0

    def test_full_report_passed_threshold(self):
        ps = PerformanceScore(overall=65.0, grade="C")
        report = FullMonitorReport(project_id="p1", scores=ps)
        assert report.passed is True

    def test_full_report_not_passed(self):
        ps = PerformanceScore(overall=55.0, grade="D")
        report = FullMonitorReport(project_id="p1", scores=ps)
        assert report.passed is False


# ══════════════════════════════════════════════════════════════════════════════
# 2. Worker agents
# ══════════════════════════════════════════════════════════════════════════════


class TestWorkerAgents:
    @pytest.mark.asyncio
    async def test_analyze_trajectory_returns_analysis(self):
        agent = _make_agent()
        result = await agent.analyze_trajectory(agent._logs)
        assert isinstance(result, TrajectoryAnalysis)
        assert 0.0 <= result.score <= 100.0

    @pytest.mark.asyncio
    async def test_analyze_trajectory_empty(self):
        agent = _make_agent(FlightLogs(project_id="p1"))
        result = await agent.analyze_trajectory(agent._logs)
        assert result.mean_cross_track_error_m == 0.0

    @pytest.mark.asyncio
    async def test_analyze_battery_score_range(self):
        agent = _make_agent()
        result = await agent.analyze_battery(agent._logs)
        assert 0.0 <= result.score <= 100.0

    @pytest.mark.asyncio
    async def test_analyze_battery_remaining_pct(self):
        agent = _make_agent()
        result = await agent.analyze_battery(agent._logs)
        assert 0.0 <= result.avg_battery_remaining_pct <= 100.0

    @pytest.mark.asyncio
    async def test_analyze_motors_returns_analysis(self):
        agent = _make_agent()
        result = await agent.analyze_motors(agent._logs)
        assert isinstance(result, MotorAnalysis)

    @pytest.mark.asyncio
    async def test_analyze_motors_detects_healthy(self):
        agent = _make_agent()
        result = await agent.analyze_motors(agent._logs)
        # All RPMs are ~3000 with tiny variance → healthy
        assert all(s == MotorStatus.HEALTHY for s in result.motor_statuses)

    @pytest.mark.asyncio
    async def test_analyze_motors_no_rpm_data(self):
        logs = _make_logs(1)
        for run in logs.simulation_runs:
            for t in run.telemetry:
                t.motor_rpms = []
        agent = _make_agent(logs)
        result = await agent.analyze_motors(logs)
        assert result.score == 75.0  # no data → assume healthy

    @pytest.mark.asyncio
    async def test_analyze_stability_returns_analysis(self):
        agent = _make_agent()
        result = await agent.analyze_stability(agent._logs)
        assert isinstance(result, FlightStabilityAnalysis)
        assert 0.0 <= result.score <= 100.0

    @pytest.mark.asyncio
    async def test_analyze_stability_detects_vibration(self):
        logs = _make_logs(1)
        for run in logs.simulation_runs:
            for t in run.telemetry:
                t.vibration_x = 50.0  # high vibration
        agent = _make_agent(logs)
        result = await agent.analyze_stability(logs)
        assert result.vibration_grade in ("high", "critical")

    @pytest.mark.asyncio
    async def test_analyze_gps_returns_analysis(self):
        agent = _make_agent()
        result = await agent.analyze_gps(agent._logs)
        assert isinstance(result, GPSAccuracyAnalysis)
        assert 0.0 <= result.score <= 100.0

    @pytest.mark.asyncio
    async def test_analyze_gps_detects_loss(self):
        logs = _make_logs(1)
        for run in logs.simulation_runs:
            for t in run.telemetry[5:10]:
                t.gps_fix = 0  # GPS loss for 5 steps
        agent = _make_agent(logs)
        result = await agent.analyze_gps(logs)
        assert result.gps_fix_loss_count >= 1

    @pytest.mark.asyncio
    async def test_detect_anomalies_clean(self):
        agent = _make_agent()
        result = await agent.detect_anomalies(agent._logs)
        assert isinstance(result, AnomalyReport)

    @pytest.mark.asyncio
    async def test_detect_anomalies_near_miss(self):
        logs = _make_logs(1)
        tel = logs.simulation_runs[0].telemetry
        tel[10].alt_m = 15.0
        tel[11].alt_m = 2.0  # 13 m drop to low altitude
        agent = _make_agent(logs)
        result = await agent.detect_anomalies(logs)
        assert result.near_miss_count >= 1

    @pytest.mark.asyncio
    async def test_detect_anomalies_attitude_upset(self):
        logs = _make_logs(1)
        tel = logs.simulation_runs[0].telemetry
        tel[5].roll_deg = 60.0  # severe upset
        tel[5].pitch_deg = 50.0
        agent = _make_agent(logs)
        result = await agent.detect_anomalies(logs)
        upset = [e for e in result.events if e.event_type == AnomalyType.ATTITUDE_UPSET]
        assert len(upset) >= 1

    @pytest.mark.asyncio
    async def test_detect_anomalies_failure_causes(self):
        logs = _make_logs(1)
        tel = logs.simulation_runs[0].telemetry
        tel[11].alt_m = 2.0  # near-miss triggers failure cause
        tel[10].alt_m = 15.0
        agent = _make_agent(logs)
        result = await agent.detect_anomalies(logs)
        assert len(result.identified_failure_causes) > 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. ReActEval cycle — CORRECTED SIGNATURE
# ══════════════════════════════════════════════════════════════════════════════


class TestReActEvalCycle:
    @pytest.mark.asyncio
    async def test_react_eval_cycle_takes_only_task(self):
        """react_eval_cycle(task) — no flight_logs param (uses self._logs)."""
        agent = _make_agent()
        task = AnalysisTask(task_id="t1", analysis_type=AnalysisType.BATTERY, priority=1)
        # Must work with just task argument
        result = await agent.react_eval_cycle(task)
        assert isinstance(result, TaskResult)

    @pytest.mark.asyncio
    async def test_react_eval_cycle_has_4_phases(self):
        agent = _make_agent()
        task = AnalysisTask(task_id="t2", analysis_type=AnalysisType.TRAJECTORY, priority=1)
        result = await agent.react_eval_cycle(task)
        phases = [s.phase for s in result.react_steps]
        assert "plan" in phases
        assert "reason" in phases
        assert "act" in phases
        assert "evaluate" in phases

    @pytest.mark.asyncio
    async def test_react_eval_cycle_evaluate_has_decision(self):
        agent = _make_agent()
        task = AnalysisTask(task_id="t3", analysis_type=AnalysisType.GPS, priority=1)
        result = await agent.react_eval_cycle(task)
        eval_steps = [s for s in result.react_steps if s.phase == "evaluate"]
        assert len(eval_steps) >= 1
        assert eval_steps[-1].decision in (ReActDecision.STOP, ReActDecision.CONTINUE)

    @pytest.mark.asyncio
    async def test_react_eval_cycle_continues_when_low_score(self):
        """If LLM says 'continue' and score is low, should run another iteration."""
        agent = _make_agent()
        # Mock LLM to always say continue (and return low score)
        call_count = 0

        async def _llm(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            return '{"confidence": 0.5, "decision": "continue"}'

        agent.llm.ainvoke = _llm

        # Override act to always return low score
        async def _low_score_act(logs):
            from agents.performance_models import TrajectoryAnalysis

            return TrajectoryAnalysis(score=30.0, findings=["Low score"])

        agent.analyze_trajectory = _low_score_act

        task = AnalysisTask(task_id="t4", analysis_type=AnalysisType.TRAJECTORY, priority=1)
        result = await agent.react_eval_cycle(task)
        # Should have run more than 1 iteration (up to MAX_REACT_ITER=3)
        assert result.iterations >= 1
        assert result.completed is True

    @pytest.mark.asyncio
    async def test_react_eval_cycle_stops_at_max_iterations(self):
        agent = _make_agent()
        # Always say continue
        agent.llm.ainvoke = AsyncMock(return_value='{"confidence": 0.5, "decision": "continue"}')
        task = AnalysisTask(task_id="t5", analysis_type=AnalysisType.ANOMALY, priority=1)
        result = await agent.react_eval_cycle(task)
        from agents.performance_monitor import MAX_REACT_ITER

        assert result.iterations <= MAX_REACT_ITER

    @pytest.mark.asyncio
    async def test_react_eval_cycle_act_uses_correct_worker(self):
        agent = _make_agent()
        task = AnalysisTask(task_id="t6", analysis_type=AnalysisType.STABILITY, priority=1)
        result = await agent.react_eval_cycle(task)
        act_steps = [s for s in result.react_steps if s.phase == "act"]
        assert any("analyze_stability" in s.tool_called for s in act_steps)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Head agent API — CORRECTED SIGNATURES
# ══════════════════════════════════════════════════════════════════════════════


class TestHeadAgent:
    @pytest.mark.asyncio
    async def test_plan_analysis_returns_all_6_on_fallback(self):
        agent = _make_agent()
        agent.llm.ainvoke = AsyncMock(side_effect=Exception("LLM unavailable"))
        plan = await agent.plan_analysis(agent._logs)
        assert len(plan.tasks) == 6
        types = {t.analysis_type for t in plan.tasks}
        assert AnalysisType.STABILITY in types
        assert AnalysisType.GPS in types

    @pytest.mark.asyncio
    async def test_plan_analysis_priority_range(self):
        agent = _make_agent()
        agent.llm.ainvoke = AsyncMock(side_effect=Exception())
        plan = await agent.plan_analysis(agent._logs)
        for task in plan.tasks:
            assert 1 <= task.priority <= 5

    @pytest.mark.asyncio
    async def test_compile_report_takes_only_project_id(self):
        """compile_report(project_id) — no extra params (uses self._analyses)."""
        agent = _make_agent()
        # Seed analyses dict with minimal data
        agent._analyses = {
            "trajectory": TrajectoryAnalysis(score=80.0).model_dump(),
            "battery": BatteryAnalysis(score=75.0).model_dump(),
        }
        with patch("utils.visualization_helpers.generate_all_viz_data", return_value={}):
            report = await agent.compile_report("test-project")
        assert isinstance(report, FullMonitorReport)
        assert report.project_id == "test-project"

    @pytest.mark.asyncio
    async def test_compile_report_score_formula(self):
        """Verify weighted formula: traj×0.25 + bat×0.20 + stab×0.20 + anom×0.20 + gps×0.10 + mot×0.05"""
        agent = _make_agent()
        agent._analyses = {
            "trajectory": TrajectoryAnalysis(score=100.0).model_dump(),
            "battery": BatteryAnalysis(score=100.0).model_dump(),
            "motors": MotorAnalysis(score=100.0).model_dump(),
            "stability": FlightStabilityAnalysis(score=100.0).model_dump(),
            "gps": GPSAccuracyAnalysis(score=100.0).model_dump(),
            "anomaly": AnomalyReport(score=100.0).model_dump(),
        }
        agent.generate_recommendations = AsyncMock(return_value=[])
        with patch("utils.visualization_helpers.generate_all_viz_data", return_value={}):
            report = await agent.compile_report("p1")
        assert report.scores.overall == 100.0
        assert report.scores.grade == "A"

    @pytest.mark.asyncio
    async def test_analyze_project_loads_logs_internally(self):
        """analyze_project stores logs in self._logs, does not require them as param."""
        agent = _make_agent()
        logs = _make_logs(n_runs=1)

        call_count = 0

        async def _llm(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            return '{"confidence": 0.9, "decision": "stop"}'

        agent.llm.ainvoke = _llm

        with patch("utils.visualization_helpers.generate_all_viz_data", return_value={}):
            report = await agent.analyze_project("p1", flight_logs=logs)

        # Verify logs were stored in session state
        assert agent._logs is logs
        assert agent._project_id == "p1"
        assert isinstance(report, FullMonitorReport)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Recommendations
# ══════════════════════════════════════════════════════════════════════════════


class TestRecommendations:
    def test_rule_based_battery_critical(self):
        ba = BatteryAnalysis(avg_battery_remaining_pct=15.0, score=30.0)
        recs = _rule_based_recommendations([ba])
        assert any(r.priority == "critical" for r in recs)

    def test_rule_based_stability_vibration(self):
        sa = FlightStabilityAnalysis(vibration_grade="high", avg_vibration_index=20.0, score=50.0)
        recs = _rule_based_recommendations([sa])
        assert any("Vibration" in r.title or "vibration" in r.description.lower() for r in recs)

    def test_rule_based_gps_hdop(self):
        ga = GPSAccuracyAnalysis(avg_hdop=3.0, score=45.0)
        recs = _rule_based_recommendations([ga])
        assert any("GPS" in r.title or "HDOP" in r.description for r in recs)

    def test_rule_based_gps_loss(self):
        ga = GPSAccuracyAnalysis(gps_fix_loss_count=2, score=60.0)
        recs = _rule_based_recommendations([ga])
        assert any("Optical Flow" in r.title or "GPS" in r.title for r in recs)

    def test_rule_based_near_miss_critical(self):
        ar = AnomalyReport(near_miss_count=1, score=60.0)
        recs = _rule_based_recommendations([ar])
        assert any(r.priority == "critical" for r in recs)

    def test_rule_based_attitude_issue(self):
        sa = FlightStabilityAnalysis(max_roll_deg=30.0, max_pitch_deg=25.0, score=55.0)
        recs = _rule_based_recommendations([sa])
        assert any("Attitude" in r.title for r in recs)

    def test_rule_based_failure_causes_recommendation(self):
        ar = AnomalyReport(identified_failure_causes=["Motor failure", "GPS loss"], score=65.0)
        recs = _rule_based_recommendations([ar])
        assert any("Failure Causes" in r.title or "causes" in r.description.lower() for r in recs)

    def test_rule_based_no_issues_returns_satisfactory(self):
        ta = TrajectoryAnalysis(avg_path_efficiency=0.9, mean_cross_track_error_m=1.0, score=92.0)
        ba = BatteryAnalysis(avg_battery_remaining_pct=40.0, score=90.0)
        recs = _rule_based_recommendations([ta, ba])
        assert recs[0].title == "Performance Satisfactory"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Helpers
# ══════════════════════════════════════════════════════════════════════════════


class TestHelpers:
    def test_parse_json_plain(self):
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_parse_json_fence(self):
        assert _parse_json('```json\n{"x": 2}\n```') == {"x": 2}

    def test_parse_json_invalid(self):
        assert _parse_json("not json") == {}

    def test_zscore_detects_outlier(self):
        values = [10.0] * 10 + [100.0]
        tel = [TelemetryPoint(timestamp_s=float(i)) for i in range(11)]
        events = _zscore_anomalies(values, tel, "r1", "alt_m", AnomalyType.ALTITUDE_DROP)
        assert len(events) == 1

    def test_zscore_no_anomaly(self):
        values = [10.0] * 20
        tel = [TelemetryPoint(timestamp_s=float(i)) for i in range(20)]
        events = _zscore_anomalies(values, tel, "r1", "alt_m", AnomalyType.ALTITUDE_DROP)
        assert events == []

    def test_zscore_threshold_override_skips(self):
        values = [5.0] * 15 + [20.0]  # 20 is outlier but below override=25
        tel = [TelemetryPoint(timestamp_s=float(i)) for i in range(16)]
        events = _zscore_anomalies(
            values,
            tel,
            "r1",
            "speed_ms",
            AnomalyType.SPEED_VIOLATION,
            threshold_override=25.0,
        )
        assert len(events) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 7. Visualization helpers (corrected: 6 dimensions + 8 functions)
# ══════════════════════════════════════════════════════════════════════════════


class TestVisualizationHelpers:
    def _scores(self) -> PerformanceScore:
        return PerformanceScore(
            trajectory=85.0,
            battery=75.0,
            motors=90.0,
            stability=80.0,
            gps=88.0,
            anomaly=82.0,
            overall=83.0,
            grade="B",
        )

    def test_score_breakdown_6_radar_items(self):
        data = generate_score_breakdown(self._scores())
        assert len(data["radar"]) == 6

    def test_score_breakdown_6_bar_items(self):
        data = generate_score_breakdown(self._scores())
        assert len(data["bar"]) == 6

    def test_trajectory_plot_none(self):
        assert generate_trajectory_plot(None) == {"available": False}

    def test_trajectory_plot_6_metrics(self):
        ta = TrajectoryAnalysis(score=85.0)
        data = generate_trajectory_plot(ta)
        assert data["available"] is True
        assert len(data["metrics"]) == 6

    def test_battery_chart_none(self):
        assert generate_battery_chart(None) == {"available": False}

    def test_battery_chart_discharge_curve(self):
        ba = BatteryAnalysis(avg_battery_remaining_pct=30.0, score=75.0)
        data = generate_battery_chart(ba)
        assert len(data["discharge_curve"]) == 10

    def test_motor_radar_none(self):
        assert generate_motor_radar(None) == {"available": False}

    def test_motor_radar_has_response_metric(self):
        ma = MotorAnalysis(motor_statuses=[MotorStatus.HEALTHY] * 4, score=90.0)
        data = generate_motor_radar(ma)
        subjects = [d["subject"] for d in data["radar"]]
        assert "Motor Response" in subjects

    def test_stability_chart_none(self):
        assert generate_stability_chart(None) == {"available": False}

    def test_stability_chart_has_attitude_and_vibration(self):
        sa = FlightStabilityAnalysis(avg_roll_deg=3.0, vibration_grade="low", score=88.0)
        data = generate_stability_chart(sa)
        assert data["available"] is True
        assert "attitude" in data
        assert "vibration" in data

    def test_gps_chart_none(self):
        assert generate_gps_chart(None) == {"available": False}

    def test_gps_chart_6_metrics(self):
        ga = GPSAccuracyAnalysis(avg_hdop=1.2, score=90.0)
        data = generate_gps_chart(ga)
        assert data["available"] is True
        assert len(data["metrics"]) == 6

    def test_anomaly_timeline_includes_failure_causes(self):
        ar = AnomalyReport(
            total_anomalies=1,
            identified_failure_causes=["Motor failure"],
            score=75.0,
        )
        data = generate_anomaly_timeline(ar)
        assert "failure_causes" in data
        assert data["failure_causes"] == ["Motor failure"]

    def test_score_gauge_grade_a(self):
        data = generate_score_gauge(92.0, "A")
        assert data["grade_color"] == "#22c55e"
        assert data["passed"] is True

    def test_generate_all_viz_data_keys(self):
        data = generate_all_viz_data(scores=self._scores())
        expected = {
            "score_breakdown",
            "score_gauge",
            "trajectory_plot",
            "battery_chart",
            "motor_radar",
            "stability_chart",
            "gps_chart",
            "anomaly_timeline",
        }
        assert set(data.keys()) == expected


# ══════════════════════════════════════════════════════════════════════════════
# 8. Integration
# ══════════════════════════════════════════════════════════════════════════════


class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_pipeline_returns_report(self):
        """Full analyze_project pipeline with mocked LLM."""
        agent = _make_agent()
        logs = _make_logs(n_runs=2)

        plan_resp = (
            '{"tasks": ['
            '{"type": "trajectory", "priority": 1, "description": "path"},'
            '{"type": "battery",    "priority": 2, "description": "energy"},'
            '{"type": "stability",  "priority": 3, "description": "stability"},'
            '{"type": "gps",        "priority": 4, "description": "gps"},'
            '{"type": "anomaly",    "priority": 5, "description": "anomaly"},'
            '{"type": "motors",     "priority": 6, "description": "motors"}'
            '], "rationale": "full", "score_drivers": ["CTE"]}'
        )
        call_n = 0

        async def _llm(prompt: str) -> str:
            nonlocal call_n
            call_n += 1
            if call_n == 1:
                return plan_resp
            return '{"confidence": 0.9, "decision": "stop"}'

        agent.llm.ainvoke = _llm

        with patch("utils.visualization_helpers.generate_all_viz_data", return_value={}):
            report = await agent.analyze_project("p1", flight_logs=logs)

        assert isinstance(report, FullMonitorReport)
        assert report.project_id == "p1"
        assert 0.0 <= report.scores.overall <= 100.0
        # All 6 analyses should be present
        assert report.trajectory is not None
        assert report.battery is not None
        assert report.motors is not None
        assert report.stability is not None
        assert report.gps is not None
        assert report.anomalies is not None

    @pytest.mark.asyncio
    async def test_analyze_project_no_logs_uses_empty(self):
        """analyze_project(project_id) alone — no flight_logs."""
        agent = PerformanceMonitorAgent.__new__(PerformanceMonitorAgent)
        agent.llm = MagicMock()
        agent.llm.ainvoke = AsyncMock(side_effect=Exception("LLM offline"))
        agent._logs = None
        agent._project_id = ""
        agent._analyses = {}

        with patch("utils.visualization_helpers.generate_all_viz_data", return_value={}):
            report = await agent.analyze_project("p2")

        assert report.project_id == "p2"
        # Fallback rule-based analysis with empty logs produces ~74.0
        assert report.scores.overall == 74.0
