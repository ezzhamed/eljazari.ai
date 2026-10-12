"""Tests for Monitor Agent — ReActEval pipeline.

Run: pytest apps/api/tests/test_monitor_agent.py -v
"""

import json
import pytest
from unittest.mock import AsyncMock, patch

from agents.monitor_models import (
    IssueRecord,
    MissionMetrics,
    ReActEvalReport,
    GRADE_COLORS,
    GRADE_LABELS_AR,
    score_to_grade,
)
from agents.monitor_agent import MonitorAgent, _EvalTools, _TOOL_ORDER


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


def _make_telemetry(n=10, start_bat=100, end_bat=70, max_speed=8.0, max_alt=30.0):
    pts = []
    for i in range(n):
        frac = i / max(n - 1, 1)
        pts.append(
            {
                "north_m": frac * 100,
                "east_m": frac * 50,
                "alt_m": max_alt * min(frac * 2, 1.0),
                "speed_ms": max_speed * min(frac * 2, 1.0),
                "battery_pct": start_bat - (start_bat - end_bat) * frac,
            }
        )
    return pts


def _make_square_telemetry(side=100.0, laps=1, samples_per_leg=6, alt=15.0):
    corners = [(0.0, 0.0), (side, 0.0), (side, side), (0.0, side), (0.0, 0.0)]
    pts = []
    total_segments = laps * (len(corners) - 1)
    sample_idx = 0
    total_samples = max(total_segments * samples_per_leg, 1)
    for _ in range(laps):
        for start, end in zip(corners, corners[1:]):
            for i in range(samples_per_leg):
                frac = i / samples_per_leg
                north = start[0] + (end[0] - start[0]) * frac
                east = start[1] + (end[1] - start[1]) * frac
                battery_frac = sample_idx / total_samples
                pts.append(
                    {
                        "north_m": north,
                        "east_m": east,
                        "alt_m": alt,
                        "speed_ms": 8.0,
                        "battery_pct": 100.0 - 10.0 * battery_frac,
                    }
                )
                sample_idx += 1
    pts.append(
        {
            "north_m": 0.0,
            "east_m": 0.0,
            "alt_m": 0.0,
            "speed_ms": 0.0,
            "battery_pct": 90.0,
        }
    )
    return pts


def _make_result(
    passed=True,
    code_iterations=1,
    violations=0,
    duration_s=120.0,
    score=80.0,
    waypoints=None,
):
    return {
        "passed": passed,
        "code_iterations": code_iterations,
        "safety_violations": [{"type": "geofence"}] * violations,
        "constraints_total": 10,
        "duration_s": duration_s,
        "evaluation": {"score": score},
        "telemetry": _make_telemetry(),
        "waypoints": waypoints or [{"north_m": 50, "east_m": 25}],
    }


GOOD_RESULTS = [_make_result() for _ in range(3)]
MIXED_RESULTS = [
    _make_result(passed=True),
    _make_result(passed=False, violations=3, score=40.0),
    _make_result(passed=True, code_iterations=4),
]


# ─────────────────────────────────────────────────────────────
# monitor_models tests
# ─────────────────────────────────────────────────────────────


class TestScoreToGrade:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (95.0, "A"),
            (90.0, "A"),
            (89.9, "B"),
            (75.0, "B"),
            (74.9, "C"),
            (60.0, "C"),
            (59.9, "D"),
            (45.0, "D"),
            (44.9, "F"),
            (0.0, "F"),
            (None, "INDETERMINATE"),
        ],
    )
    def test_grade_boundaries(self, score, expected):
        assert score_to_grade(score) == expected


class TestGradeDicts:
    def test_all_grades_have_colors(self):
        for grade in ("A", "B", "C", "D", "F", "INDETERMINATE"):
            assert grade in GRADE_COLORS
            assert GRADE_COLORS[grade].startswith("#")

    def test_all_grades_have_arabic_labels(self):
        for grade in ("A", "B", "C", "D", "F", "INDETERMINATE"):
            assert grade in GRADE_LABELS_AR
            assert len(GRADE_LABELS_AR[grade]) > 0


class TestMissionMetrics:
    def test_composite_efficiency_formula(self):
        m = MissionMetrics(
            path_efficiency=0.8,
            battery_efficiency=0.9,
            safety_score=1.0,
        )
        expected = round(0.8 * 0.4 + 0.9 * 0.3 + 1.0 * 0.3, 3)
        assert m.composite_efficiency == expected

    def test_defaults_are_zero(self):
        m = MissionMetrics()
        assert m.mission_success_rate == 0.0
        assert m.safety_violations_count == 0
        assert m.mission_started is True
        assert m.telemetry_points == 0

    def test_field_bounds(self):
        with pytest.raises(Exception):
            MissionMetrics(mission_success_rate=1.5)  # > 1.0
        with pytest.raises(Exception):
            MissionMetrics(safety_score=-0.1)  # < 0.0


class TestReActEvalReport:
    def _report(self, score: float) -> ReActEvalReport:
        grade = score_to_grade(score)
        return ReActEvalReport(
            overall_score=score,
            grade=grade,
            metrics=MissionMetrics(mission_success_rate=score / 100),
        )

    def test_grade_color_computed(self):
        r = self._report(95.0)
        assert r.grade_color == GRADE_COLORS["A"]

    def test_grade_label_ar_computed(self):
        r = self._report(80.0)
        assert r.grade_label_ar == GRADE_LABELS_AR["B"]

    def test_grade_label_ar_override(self):
        r = ReActEvalReport(
            overall_score=70.0,
            grade="C",
            metrics=MissionMetrics(),
            grade_label_override_ar="Preliminary estimate (fallback simulation)",
        )
        assert r.grade_label_ar == "Preliminary estimate (fallback simulation)"

    def test_passed_threshold(self):
        assert self._report(60.0).passed is True
        assert self._report(59.9).passed is False
        r = ReActEvalReport(
            overall_score=None,
            grade="INDETERMINATE",
            metrics=MissionMetrics(mission_started=False, telemetry_points=0),
        )
        assert r.passed is False

    def test_critical_issues_filter(self):
        r = self._report(50.0)
        r.issues = [
            IssueRecord(category="safety", severity="critical", description="x"),
            IssueRecord(category="battery", severity="medium", description="y"),
        ]
        assert len(r.critical_issues) == 1
        assert r.critical_issues[0].severity == "critical"


# ─────────────────────────────────────────────────────────────
# _EvalTools tests
# ─────────────────────────────────────────────────────────────


class TestEvalToolsPath:
    def test_returns_expected_keys(self):
        result = _EvalTools.compute_path_metrics(GOOD_RESULTS)
        assert "path_efficiency" in result
        assert "total_distance_m" in result
        assert "waypoint_accuracy_m" in result
        assert "coverage_pattern_rate" in result

    def test_efficiency_between_0_and_1(self):
        result = _EvalTools.compute_path_metrics(GOOD_RESULTS)
        assert 0.0 <= result["path_efficiency"] <= 1.0

    def test_empty_results(self):
        result = _EvalTools.compute_path_metrics([])
        assert result["path_efficiency"] == 0.0

    def test_single_point_telemetry(self):
        r = [{"telemetry": [{"north_m": 0, "east_m": 0, "alt_m": 0}], "waypoints": []}]
        result = _EvalTools.compute_path_metrics(r)
        assert result["total_distance_m"] == 0.0

    def test_square_coverage_loop_is_not_penalized_as_direct_path(self):
        result = _EvalTools.compute_path_metrics(
            [
                {
                    "telemetry": _make_square_telemetry(laps=2),
                    "mission_description": "aerial photo square survey 100x100m",
                    "waypoints": [],
                }
            ]
        )
        assert result["coverage_pattern_rate"] == 1.0
        assert result["closed_loop_rate"] == 1.0
        assert result["return_to_home_error_m"] == 0.0
        assert result["path_efficiency"] >= 0.8


class TestEvalToolsBattery:
    def test_returns_expected_keys(self):
        result = _EvalTools.compute_battery_metrics(GOOD_RESULTS)
        assert "battery_remaining_pct" in result
        assert "battery_efficiency" in result

    def test_efficiency_range(self):
        result = _EvalTools.compute_battery_metrics(GOOD_RESULTS)
        assert 0.0 <= result["battery_efficiency"] <= 1.0

    def test_empty_results(self):
        result = _EvalTools.compute_battery_metrics([])
        assert result["battery_remaining_pct"] == 0.0

    def test_accepts_real_sitl_battery_percent_key(self):
        result = _EvalTools.compute_battery_metrics(
            [
                {
                    "telemetry": [
                        {"battery_percent": 100.0},
                        {"battery_percent": 50.0},
                    ]
                }
            ]
        )
        assert result["battery_remaining_pct"] == 50.0


class TestEvalToolsSafety:
    def test_no_violations(self):
        results = [_make_result(violations=0)]
        r = _EvalTools.compute_safety_metrics(results)
        assert r["safety_violations_count"] == 0
        assert r["safety_score"] == 1.0

    def test_with_violations(self):
        results = [_make_result(violations=5)]
        r = _EvalTools.compute_safety_metrics(results)
        assert r["safety_violations_count"] == 5
        assert r["safety_score"] < 1.0

    def test_compliance_range(self):
        r = _EvalTools.compute_safety_metrics(GOOD_RESULTS)
        assert 0.0 <= r["constraint_compliance"] <= 1.0


class TestEvalToolsSuccess:
    def test_all_passed(self):
        r = _EvalTools.compute_success_metrics(GOOD_RESULTS)
        assert r["mission_success_rate"] == 1.0

    def test_partial_pass(self):
        r = _EvalTools.compute_success_metrics(MIXED_RESULTS)
        assert 0.0 < r["mission_success_rate"] < 1.0

    def test_simulation_count(self):
        r = _EvalTools.compute_success_metrics(MIXED_RESULTS)
        assert r["simulation_iterations"] == len(MIXED_RESULTS)

    def test_real_telemetry_and_completion_are_reported(self):
        results = [
            {
                **_make_result(),
                "mission_completed": True,
                "telemetry_source": "real_wsl_sitl",
            }
        ]
        r = _EvalTools.compute_success_metrics(results)
        assert r["mission_completed_rate"] == 1.0
        assert r["telemetry_real_rate"] == 1.0
        assert r["avg_telemetry_points"] == len(results[0]["telemetry"])


class TestEvalToolsCodeQuality:
    def test_returns_expected_keys(self):
        r = _EvalTools.compute_code_quality(GOOD_RESULTS)
        assert "code_iterations_needed" in r
        assert "avg_semantic_score" in r

    def test_high_semantic_score(self):
        results = [_make_result(score=90.0), _make_result(score=85.0)]
        r = _EvalTools.compute_code_quality(results)
        assert r["avg_semantic_score"] >= 85.0


# ─────────────────────────────────────────────────────────────
# MonitorAgent unit tests
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def agent():
    with patch("agents.monitor_agent.OllamaLLM"):
        a = MonitorAgent()
        a.llm = AsyncMock()
        return a


class TestParseJson:
    def test_plain_json(self, agent):
        data = agent._parse_json('{"a": 1}')
        assert data == {"a": 1}

    def test_with_markdown_fence(self, agent):
        raw = '```json\n{"key": "value"}\n```'
        data = agent._parse_json(raw)
        assert data["key"] == "value"

    def test_json_embedded_in_text(self, agent):
        raw = 'Here is the answer: {"score": 42} done.'
        data = agent._parse_json(raw)
        assert data["score"] == 42

    def test_invalid_returns_empty(self, agent):
        assert agent._parse_json("not json at all") == {}

    def test_single_quoted_dict(self, agent):
        data = agent._parse_json("{'action': 'compute_success_metrics'}")
        assert data["action"] == "compute_success_metrics"


class TestComputeScore:
    @pytest.mark.parametrize(
        "kwargs,expected_range",
        [
            (
                {
                    "mission_success_rate": 1.0,
                    "path_efficiency": 1.0,
                    "battery_efficiency": 1.0,
                    "safety_score": 1.0,
                    "avg_semantic_score": 100.0,
                },
                (99, 100),
            ),
            (
                {
                    "mission_success_rate": 0.0,
                    "path_efficiency": 0.0,
                    "battery_efficiency": 0.0,
                    "safety_score": 0.0,
                    "avg_semantic_score": 0.0,
                },
                (0, 1),
            ),
        ],
    )
    def test_score_extremes(self, kwargs, expected_range):
        m = MissionMetrics(**kwargs)
        s = MonitorAgent._compute_score(m)
        lo, hi = expected_range
        assert lo <= s <= hi

    def test_weights_sum_to_100(self):
        # weights: 30+20+15+25+10 = 100
        m = MissionMetrics(
            mission_success_rate=1.0,
            path_efficiency=1.0,
            battery_efficiency=1.0,
            safety_score=1.0,
            avg_semantic_score=100.0,
        )
        assert MonitorAgent._compute_score(m) == pytest.approx(100.0)


class TestRuleBasedFeedback:
    def test_low_success_generates_critical_issue(self, agent):
        m = MissionMetrics(mission_success_rate=0.3)
        recs, strengths, issues, summary = agent._rule_based_feedback(m, 30.0)
        critical = [i for i in issues if i.severity == "critical"]
        assert len(critical) >= 1

    def test_good_metrics_generate_strengths(self, agent):
        m = MissionMetrics(
            mission_success_rate=1.0,
            path_efficiency=0.9,
            battery_efficiency=0.9,
            safety_score=1.0,
            battery_remaining_pct=50.0,
            avg_semantic_score=80.0,
        )
        _, strengths, _, _ = agent._rule_based_feedback(m, 90.0)
        assert len(strengths) >= 2

    def test_summary_contains_score(self, agent):
        m = MissionMetrics(mission_success_rate=0.8)
        _, _, _, summary = agent._rule_based_feedback(m, 75.0)
        assert "75" in summary

    def test_battery_warning_below_20(self, agent):
        m = MissionMetrics(battery_remaining_pct=15.0)
        recs, _, issues, _ = agent._rule_based_feedback(m, 50.0)
        bat_issues = [i for i in issues if i.category == "battery"]
        assert len(bat_issues) >= 1

    def test_coverage_route_avoids_shortest_path_recommendation(self, agent):
        m = MissionMetrics(
            mission_success_rate=1.0,
            mission_completed_rate=1.0,
            telemetry_real_rate=1.0,
            avg_telemetry_points=1956,
            path_efficiency=0.9,
            coverage_pattern_rate=1.0,
            closed_loop_rate=1.0,
            return_to_home_error_m=1.0,
            north_span_m=100.0,
            east_span_m=100.0,
            battery_efficiency=0.9,
            battery_remaining_pct=80.0,
            safety_score=1.0,
            avg_semantic_score=85.0,
        )
        recs, strengths, issues, summary = agent._rule_based_feedback(m, 95.0)
        joined_recs = " ".join(recs).lower()
        assert "shortest-path" not in joined_recs
        assert "square" in joined_recs or "corner" in joined_recs
        assert any("real sitl telemetry" in item.lower() for item in strengths)
        assert issues == []
        assert "coverage/square" in summary


class TestBuildMetrics:
    def test_all_keys_mapped(self, agent):
        obs = {
            "mission_success_rate": 0.8,
            "simulation_iterations": 5,
            "code_iterations_needed": 2,
            "path_efficiency": 0.75,
            "total_distance_m": 500.0,
            "waypoint_accuracy_m": 1.2,
            "coverage_pattern_rate": 1.0,
            "closed_loop_rate": 1.0,
            "return_to_home_error_m": 0.8,
            "north_span_m": 100.0,
            "east_span_m": 98.0,
            "battery_efficiency": 0.85,
            "battery_remaining_pct": 40.0,
            "safety_score": 0.9,
            "constraint_compliance": 0.95,
            "safety_violations_count": 1,
            "avg_flight_duration_s": 180.0,
            "avg_speed_ms": 8.5,
            "max_altitude_m": 50.0,
            "mission_completed_rate": 1.0,
            "telemetry_real_rate": 1.0,
            "avg_telemetry_points": 1234.0,
            "mission_started": True,
            "telemetry_points": 2468,
            "mission_error_count": 0,
            "warning_count": 2,
            "avg_semantic_score": 78.0,
        }
        m = agent._build_metrics(obs)
        assert isinstance(m, MissionMetrics)
        assert m.mission_success_rate == 0.8
        assert m.avg_speed_ms == 8.5
        assert m.coverage_pattern_rate == 1.0
        assert m.telemetry_real_rate == 1.0
        assert m.mission_started is True
        assert m.telemetry_points == 2468
        assert m.warning_count == 2

    def test_missing_keys_use_defaults(self, agent):
        m = agent._build_metrics({})
        assert m.mission_success_rate == 0.0
        assert m.simulation_iterations == 1


# ─────────────────────────────────────────────────────────────
# Integration: MonitorAgent.run()
# ─────────────────────────────────────────────────────────────


class TestMonitorAgentRun:
    @pytest.mark.asyncio
    async def test_react_loop_uses_deterministic_fallback_after_first_failure(self, agent):
        agent.llm.ainvoke = AsyncMock(side_effect=TimeoutError())
        chain, raw = await agent._react_loop(GOOD_RESULTS, "Fallback mission")
        assert [step.action for step in chain] == list(_TOOL_ORDER)
        assert agent.llm.ainvoke.await_count == 1
        assert raw["mission_success_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_returns_react_eval_report(self, agent):
        react_response = json.dumps(
            {
                "thought": "I should check success metrics first.",
                "action": "compute_success_metrics",
                "action_input": {},
            }
        )
        synth_response = json.dumps(
            {
                "recommendations": ["Tune PID gains", "Add obstacle avoidance"],
                "strengths": ["High success rate"],
                "issues": [],
                "mission_summary": "The mission performed well overall.",
            }
        )
        from agents.monitor_agent import N_REACT_STEPS

        agent.llm.ainvoke = AsyncMock(
            side_effect=[react_response] * N_REACT_STEPS + [synth_response]
        )
        report = await agent.run(GOOD_RESULTS, "Survey agricultural field")

        assert isinstance(report, ReActEvalReport)
        assert 0.0 <= report.overall_score <= 100.0
        assert report.grade in ("A", "B", "C", "D", "F")
        assert len(report.react_chain) == N_REACT_STEPS

    @pytest.mark.asyncio
    async def test_fallback_telemetry_caps_score(self, agent):
        react_response = json.dumps(
            {
                "thought": "I should check success metrics first.",
                "action": "compute_success_metrics",
                "action_input": {},
            }
        )
        synth_response = json.dumps(
            {
                "recommendations": ["Looks good"],
                "strengths": ["Mission completed"],
                "issues": [],
                "mission_summary": "Fallback telemetry run.",
            }
        )
        from agents.monitor_agent import N_REACT_STEPS

        agent.llm.ainvoke = AsyncMock(
            side_effect=[react_response] * N_REACT_STEPS + [synth_response]
        )

        report = await agent.run(GOOD_RESULTS, "Survey agricultural field")

        assert report.overall_score <= 70.0
        assert report.grade_label_ar == "Preliminary estimate (fallback simulation)"
        assert any(issue.category == "compliance" for issue in report.issues)

    @pytest.mark.asyncio
    async def test_llm_failure_uses_fallback(self, agent):
        agent.llm.ainvoke = AsyncMock(side_effect=Exception("LLM unavailable"))
        report = await agent.run(GOOD_RESULTS, "Test mission")
        assert isinstance(report, ReActEvalReport)
        assert report.overall_score >= 0.0

    @pytest.mark.asyncio
    async def test_empty_results_does_not_crash(self, agent):
        agent.llm.ainvoke = AsyncMock(return_value="{}")
        report = await agent.run([], "Empty test")
        assert isinstance(report, ReActEvalReport)
        assert report.overall_score is None
        assert report.grade == "INDETERMINATE"

    @pytest.mark.asyncio
    async def test_no_start_no_telemetry_is_indeterminate(self, agent):
        agent.llm.ainvoke = AsyncMock(side_effect=Exception("LLM unavailable"))
        results = [
            {
                "passed": False,
                "mission_started": False,
                "mission_completed": False,
                "mission_error": "Failed to copy mission file into WSL.",
                "telemetry_source": "real_wsl_sitl_empty",
                "telemetry": [],
                "semantic_observations": [],
                "evaluation": {"score": None},
            }
        ]

        report = await agent.run(results, "Mission did not start")

        assert report.overall_score is None
        assert report.grade == "INDETERMINATE"
        assert report.metrics.mission_started is False
        assert report.metrics.telemetry_points == 0

    @pytest.mark.asyncio
    async def test_mixed_results_score_between_0_100(self, agent):
        agent.llm.ainvoke = AsyncMock(
            return_value=json.dumps(
                {"thought": "Checking path.", "action": "compute_path_metrics", "action_input": {}}
            )
        )
        report = await agent.run(MIXED_RESULTS, "Mixed mission")
        assert 0.0 <= report.overall_score <= 100.0

    @pytest.mark.asyncio
    async def test_react_chain_step_indices(self, agent):
        agent.llm.ainvoke = AsyncMock(
            return_value=json.dumps(
                {"thought": "test", "action": "compute_safety_metrics", "action_input": {}}
            )
        )
        report = await agent.run(GOOD_RESULTS, "Index test")
        for i, step in enumerate(report.react_chain, start=1):
            assert step.step == i

    @pytest.mark.asyncio
    async def test_issues_are_issue_records(self, agent):
        synth = json.dumps(
            {
                "recommendations": ["Fix path"],
                "strengths": [],
                "issues": [
                    {
                        "category": "safety",
                        "severity": "high",
                        "description": "Geofence breach",
                        "suggestion": "Reduce speed",
                    }
                ],
                "mission_summary": "Issues found.",
            }
        )
        agent.llm.ainvoke = AsyncMock(
            side_effect=[
                json.dumps(
                    {"thought": "t", "action": "compute_success_metrics", "action_input": {}}
                )
            ]
            * 5
            + [synth]
        )
        report = await agent.run(GOOD_RESULTS, "Safety test")
        for issue in report.issues:
            assert isinstance(issue, IssueRecord)
