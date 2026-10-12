"""eljazari.ai monitor agent.

Full ReActEval-style performance evaluation pipeline:
1. _react_loop() collects deterministic metric observations.
2. _build_metrics() validates them into MissionMetrics.
3. _compute_score() returns the final 0-100 score.
4. _synthesize_text() produces qualitative feedback, with a rule-based fallback.
"""

import asyncio
import ast
import json
import math
import statistics
import re
from typing import Any

import structlog
from langchain_ollama import OllamaLLM

from config.settings import settings
from .monitor_models import (
    IssueRecord,
    MissionMetrics,
    ReActEvalReport,
    ReActStep,
    score_to_grade,
)

logger = structlog.get_logger(__name__)
LLM_TIMEOUT_S = 20

N_REACT_STEPS = 6  # one per tool in _TOOL_DISPATCH (Phase 11 added the 6th)
MAX_VIOLATIONS = 10
MAX_SPEED_WARN = 15.0
MIN_COVERAGE_SPAN_M = 20.0
COVERAGE_MISSION_KEYWORDS = (
    "coverage",
    "survey",
    "mapping",
    "square",
    "grid",
    "lawnmower",
)


class _EvalTools:
    """Deterministic evaluation helpers used by the ReAct loop."""

    @staticmethod
    def compute_path_metrics(results: list[dict]) -> dict:
        efficiencies, distances, wp_errors = [], [], []
        coverage_rates, closed_loop_rates, rth_errors = [], [], []
        north_spans, east_spans = [], []
        for result in results:
            telemetry = result.get("telemetry", [])
            if not telemetry:
                continue

            points = [
                (
                    sample.get("north_m", 0),
                    sample.get("east_m", 0),
                    sample.get("alt_m", 0),
                )
                for sample in telemetry
            ]
            north_values = [point[0] for point in points]
            east_values = [point[1] for point in points]
            north_span = max(north_values) - min(north_values)
            east_span = max(east_values) - min(east_values)
            horizontal_span = math.hypot(north_span, east_span)
            return_to_home_error = (
                math.hypot(points[-1][0] - points[0][0], points[-1][1] - points[0][1])
                if len(points) >= 2
                else 0.0
            )

            distance = sum(
                (
                    (points[idx][0] - points[idx - 1][0]) ** 2
                    + (points[idx][1] - points[idx - 1][1]) ** 2
                    + (points[idx][2] - points[idx - 1][2]) ** 2
                )
                ** 0.5
                for idx in range(1, len(points))
            )
            distances.append(distance)
            north_spans.append(north_span)
            east_spans.append(east_span)
            rth_errors.append(return_to_home_error)

            mission_text = " ".join(
                str(result.get(key) or "") for key in ("mission_description", "mission", "prompt")
            ).lower()
            requested_coverage = any(
                keyword in mission_text for keyword in COVERAGE_MISSION_KEYWORDS
            )
            closed_loop = (
                len(points) >= 4
                and horizontal_span >= MIN_COVERAGE_SPAN_M
                and return_to_home_error <= max(5.0, horizontal_span * 0.12)
            )
            coverage_pattern = requested_coverage or closed_loop
            coverage_rates.append(1.0 if coverage_pattern else 0.0)
            closed_loop_rates.append(1.0 if closed_loop else 0.0)

            if len(points) >= 2:
                if coverage_pattern:
                    efficiencies.append(
                        _EvalTools._coverage_path_quality(
                            north_span=north_span,
                            east_span=east_span,
                            horizontal_span=horizontal_span,
                            return_to_home_error=return_to_home_error,
                        )
                    )
                else:
                    optimal = (
                        (points[-1][0] - points[0][0]) ** 2
                        + (points[-1][1] - points[0][1]) ** 2
                        + (points[-1][2] - points[0][2]) ** 2
                    ) ** 0.5
                    efficiencies.append(optimal / max(distance, 0.01))

            for waypoint in result.get("waypoints", []):
                closest = min(
                    points,
                    key=lambda point: (
                        (
                            (point[0] - waypoint.get("north_m", 0)) ** 2
                            + (point[1] - waypoint.get("east_m", 0)) ** 2
                        )
                        ** 0.5
                    ),
                    default=None,
                )
                if closest:
                    error = (
                        (closest[0] - waypoint.get("north_m", 0)) ** 2
                        + (closest[1] - waypoint.get("east_m", 0)) ** 2
                    ) ** 0.5
                    wp_errors.append(error)

        return {
            "path_efficiency": round(statistics.mean(efficiencies), 3) if efficiencies else 0.0,
            "total_distance_m": round(statistics.mean(distances), 2) if distances else 0.0,
            "waypoint_accuracy_m": round(statistics.mean(wp_errors), 2) if wp_errors else 0.0,
            "coverage_pattern_rate": round(statistics.mean(coverage_rates), 3)
            if coverage_rates
            else 0.0,
            "closed_loop_rate": round(statistics.mean(closed_loop_rates), 3)
            if closed_loop_rates
            else 0.0,
            "return_to_home_error_m": round(statistics.mean(rth_errors), 2) if rth_errors else 0.0,
            "north_span_m": round(statistics.mean(north_spans), 2) if north_spans else 0.0,
            "east_span_m": round(statistics.mean(east_spans), 2) if east_spans else 0.0,
        }

    @staticmethod
    def _coverage_path_quality(
        *,
        north_span: float,
        east_span: float,
        horizontal_span: float,
        return_to_home_error: float,
    ) -> float:
        """Score coverage routes by area coverage and closure, not straight-line distance."""
        if horizontal_span <= 0:
            return 0.0

        largest_span = max(north_span, east_span)
        smallest_span = min(north_span, east_span)
        span_score = min(1.0, horizontal_span / MIN_COVERAGE_SPAN_M)
        balance_score = smallest_span / max(largest_span, 0.01)
        return_score = 1.0 - min(
            1.0,
            return_to_home_error / max(10.0, horizontal_span * 0.12),
        )
        return round(
            max(0.0, min(1.0, span_score * 0.45 + balance_score * 0.30 + return_score * 0.25)),
            3,
        )

    @staticmethod
    def compute_battery_metrics(results: list[dict]) -> dict:
        remainings, efficiencies = [], []
        for result in results:
            telemetry = result.get("telemetry", [])
            if not telemetry:
                continue
            start_pct = _EvalTools._sample_float(
                telemetry[0], "battery_pct", "battery_percent", default=100.0
            )
            end_pct = _EvalTools._sample_float(
                telemetry[-1], "battery_pct", "battery_percent", default=0.0
            )
            remainings.append(end_pct)
            used = start_pct - end_pct
            if start_pct > 0:
                efficiencies.append(1.0 - max(0, used - (100 - end_pct)) / max(start_pct, 1))
            else:
                efficiencies.append(1.0)

        return {
            "battery_remaining_pct": round(statistics.mean(remainings), 1) if remainings else 0.0,
            "battery_efficiency": round(min(1.0, statistics.mean(efficiencies)), 3)
            if efficiencies
            else 0.0,
        }

    @staticmethod
    def _sample_float(sample: dict, *keys: str, default: float = 0.0) -> float:
        for key in keys:
            value = sample.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return default

    @staticmethod
    def compute_safety_metrics(results: list[dict]) -> dict:
        if not results:
            return {
                "safety_violations_count": 0,
                "safety_score": 0.0,
                "constraint_compliance": 0.0,
            }

        violations_per_run, compliances = [], []
        for result in results:
            violations = result.get("safety_violations", [])
            violations_per_run.append(len(violations))
            constraints_total = result.get("constraints_total", 10)
            passed = constraints_total - len(violations)
            compliances.append(passed / max(constraints_total, 1))

        total_violations = sum(violations_per_run)
        safety_score = max(
            0.0,
            1.0 - total_violations / (MAX_VIOLATIONS * max(len(results), 1)),
        )
        return {
            "safety_violations_count": total_violations,
            "safety_score": round(safety_score, 3),
            "constraint_compliance": round(statistics.mean(compliances), 3) if compliances else 1.0,
        }

    @staticmethod
    def compute_success_metrics(results: list[dict]) -> dict:
        passed_results = [result for result in results if result.get("passed", False)]
        completed_results = [
            result
            for result in results
            if result.get("mission_completed", result.get("passed", False))
        ]
        real_telemetry_results = [
            result
            for result in results
            if "real" in str(result.get("telemetry_source") or "").lower()
        ]
        durations = [result.get("duration_s", 0) for result in results if result.get("duration_s")]
        telemetry_counts = [len(result.get("telemetry", [])) for result in results]
        mission_started = any(
            bool(
                result.get(
                    "mission_started", result.get("mission_completed", result.get("passed", False))
                )
            )
            for result in results
        )
        mission_error_count = sum(
            1 for result in results if str(result.get("mission_error") or "").strip()
        )
        warning_count = sum(len(result.get("warnings") or []) for result in results)
        speeds: list[float] = []
        altitudes: list[float] = []

        for result in results:
            for sample in result.get("telemetry", []):
                speed = sample.get("speed_ms", 0)
                altitude = sample.get("alt_m", 0)
                if speed > 0:
                    speeds.append(speed)
                if altitude > 0:
                    altitudes.append(altitude)

        return {
            "mission_success_rate": len(passed_results) / max(len(results), 1),
            "mission_completed_rate": len(completed_results) / max(len(results), 1),
            "telemetry_real_rate": len(real_telemetry_results) / max(len(results), 1),
            "avg_telemetry_points": round(statistics.mean(telemetry_counts), 1)
            if telemetry_counts
            else 0.0,
            "mission_started": mission_started,
            "telemetry_points": sum(telemetry_counts),
            "mission_error_count": mission_error_count,
            "warning_count": warning_count,
            "simulation_iterations": len(results),
            "avg_flight_duration_s": round(statistics.mean(durations), 1) if durations else 0.0,
            "avg_speed_ms": round(statistics.mean(speeds), 2) if speeds else 0.0,
            "max_altitude_m": round(max(altitudes), 1) if altitudes else 0.0,
        }

    @staticmethod
    def compute_code_quality(results: list[dict]) -> dict:
        iterations = [result.get("code_iterations", 1) for result in results]
        semantic_scores = [
            result.get("evaluation", {}).get("score", 0)
            for result in results
            if result.get("evaluation", {}).get("score") is not None
        ]
        return {
            "code_iterations_needed": round(statistics.mean(iterations)) if iterations else 1,
            "avg_semantic_score": round(statistics.mean(semantic_scores), 1)
            if semantic_scores
            else 0.0,
        }


def _compute_flight_quality_tool(results: list[dict]) -> dict:
    """ReActEval tool wrapper around the Phase 11 flight-quality solver.

    Aggregates across iterations by taking the best (lowest) tracking RMSE
    and worst (highest) oscillation/saturation — engineers care about the
    most pessimistic stability signal we ever saw, not the average.
    """
    from pipelines.flight_quality_solver import compute_flight_quality

    if not results:
        return {}

    sols = []
    for r in results:
        telemetry = r.get("telemetry") or []
        waypoints_raw = r.get("waypoints") or []
        waypoints_ned: list = []
        try:
            from utils.coordinates import NED  # local import to avoid cycles

            for wp in waypoints_raw:
                if isinstance(wp, dict):
                    waypoints_ned.append(
                        NED(
                            north_m=float(wp.get("north_m", 0.0)),
                            east_m=float(wp.get("east_m", 0.0)),
                            down_m=float(wp.get("down_m", 0.0)),
                        )
                    )
        except Exception:
            waypoints_ned = []
        target_alt = r.get("target_altitude_m")
        try:
            target_alt = float(target_alt) if target_alt is not None else None
        except (TypeError, ValueError):
            target_alt = None
        sols.append(
            compute_flight_quality(
                telemetry=telemetry,
                commanded_waypoints_ned=waypoints_ned or None,
                target_altitude_m=target_alt,
            )
        )

    if not sols:
        return {}

    # Aggregate: tracking from best run, stability from worst.
    best = min(sols, key=lambda m: m.position_rmse_m or 1e9)
    worst_stab = max(
        sols,
        key=lambda m: max(m.roll_oscillation_power, m.pitch_oscillation_power),
    )
    worst_sat = max(sols, key=lambda m: m.motor_saturation_percent_peak)
    worst_rth = max(sols, key=lambda m: m.return_to_home_error_m)

    return {
        "position_rmse_m": best.position_rmse_m,
        "altitude_rmse_m": best.altitude_rmse_m,
        "tracking_evaluated": best.tracking_attempted,
        "altitude_tracking_evaluated": best.altitude_tracking_attempted,
        "cross_track_error_max_m": best.cross_track_error_max_m,
        "cross_track_error_mean_m": best.cross_track_error_mean_m,
        "heading_rmse_deg": best.heading_rmse_deg,
        "roll_oscillation_freq_hz": worst_stab.roll_oscillation_freq_hz,
        "roll_oscillation_power": worst_stab.roll_oscillation_power,
        "pitch_oscillation_freq_hz": worst_stab.pitch_oscillation_freq_hz,
        "pitch_oscillation_power": worst_stab.pitch_oscillation_power,
        "attitude_rms_deg": worst_stab.attitude_rms_deg,
        "motor_saturation_percent_peak": worst_sat.motor_saturation_percent_peak,
        "motor_saturation_percent_mean": worst_sat.motor_saturation_percent_mean,
        "hover_drift_m_per_min": best.hover_drift_m_per_min,
        "flight_quality_rth_error_m": worst_rth.return_to_home_error_m,
        "setpoint_tracking_grade": best.setpoint_tracking_grade,
        "stability_grade": worst_stab.stability_grade,
        "flight_quality_score": best.overall_flight_quality_score,
        "flight_quality_grade": best.grade,
        "flight_quality_warnings": [w for s in sols for w in s.warnings],
        "flight_quality_strengths": [s for sol in sols for s in sol.strengths],
    }


_TOOL_DISPATCH: dict[str, Any] = {
    "compute_path_metrics": _EvalTools.compute_path_metrics,
    "compute_battery_metrics": _EvalTools.compute_battery_metrics,
    "compute_safety_metrics": _EvalTools.compute_safety_metrics,
    "compute_success_metrics": _EvalTools.compute_success_metrics,
    "compute_code_quality": _EvalTools.compute_code_quality,
    "compute_flight_quality_metrics": _compute_flight_quality_tool,
}
_TOOL_ORDER = tuple(_TOOL_DISPATCH.keys())


class MonitorAgent:
    """ReActEval-based performance monitor for eljazari.ai simulation results."""

    def __init__(self) -> None:
        self.llm = OllamaLLM(
            base_url=settings.ollama_base_url,
            model=settings.ollama_default_model,
            temperature=0.2,
            client_kwargs={"timeout": LLM_TIMEOUT_S},
        )

    async def run(
        self,
        simulation_results: list[dict[str, Any]],
        mission_description: str,
    ) -> ReActEvalReport:
        logger.info("MonitorAgent.run", num_simulations=len(simulation_results))

        enriched_results = [
            {**result, "mission_description": mission_description} for result in simulation_results
        ]
        react_chain, raw_observations = await self._react_loop(
            enriched_results, mission_description
        )
        metrics = self._build_metrics(raw_observations)
        overall_score = self._compute_score(metrics)
        indeterminate = metrics.telemetry_points == 0 and not metrics.mission_started
        fallback_limited = not indeterminate and metrics.telemetry_real_rate < 1.0
        if indeterminate:
            overall_score = None
            grade = score_to_grade(None)
            recommendations = [
                "Fix simulation startup and collect telemetry before scoring this mission.",
                "Check WSL/Docker SITL permissions and mission-file copy paths.",
            ]
            strengths: list[str] = []
            issues = [
                IssueRecord(
                    category="compliance",
                    severity="high",
                    description="Mission did not start and no telemetry was collected.",
                    suggestion="Treat this run as not evaluated; rerun after SITL bootstrap succeeds.",
                )
            ]
            summary = "Mission evaluation is indeterminate because execution did not start and no telemetry was collected."
        else:
            if fallback_limited:
                overall_score = min(overall_score, 70.0)
            grade = score_to_grade(overall_score)
            recommendations, strengths, issues, summary = await self._synthesize_text(
                metrics, overall_score, mission_description
            )
            if fallback_limited:
                recommendations.insert(
                    0,
                    "Re-run this mission with real PX4/Gazebo SITL telemetry before treating the score as production-grade.",
                )
                issues.append(
                    IssueRecord(
                        category="compliance",
                        severity="medium",
                        description="Report is based on fallback or non-real telemetry.",
                        suggestion="Enable real_wsl_sitl telemetry or force a verified SITL runtime before final scoring.",
                    )
                )

        return ReActEvalReport(
            overall_score=round(overall_score, 1) if overall_score is not None else None,
            grade=grade,
            metrics=metrics,
            react_chain=react_chain,
            recommendations=recommendations,
            strengths=strengths,
            issues=issues,
            mission_summary=summary,
            grade_label_override_ar=(
                "Not evaluable — mission did not start"
                if indeterminate
                else "Preliminary estimate (fallback simulation)"
                if fallback_limited
                else ""
            ),
        )

    async def _react_loop(
        self,
        results: list[dict],
        mission_description: str,
    ) -> tuple[list[ReActStep], dict[str, Any]]:
        chain: list[ReActStep] = []
        accumulated: dict[str, Any] = {}
        history = ""
        used_actions: set[str] = set()
        llm_fallback_reason: str | None = None

        tools_desc = (
            "Available tools:\n"
            "  compute_path_metrics(results)    -> path_efficiency, total_distance_m, waypoint_accuracy_m, coverage_pattern_rate, closed_loop_rate\n"
            "  compute_battery_metrics(results) -> battery_remaining_pct, battery_efficiency\n"
            "  compute_safety_metrics(results)  -> safety_violations_count, safety_score, constraint_compliance\n"
            "  compute_success_metrics(results) -> mission_success_rate, mission_completed_rate, telemetry_real_rate, avg_telemetry_points, simulation_iterations, avg_flight_duration_s, avg_speed_ms, max_altitude_m\n"
            "  compute_code_quality(results)    -> code_iterations_needed, avg_semantic_score\n"
        )

        for step_idx in range(1, N_REACT_STEPS + 1):
            prompt = (
                "You are evaluating a drone mission simulation.\n"
                f"Mission: {mission_description}\n"
                f"Simulations: {len(results)} runs\n\n"
                f"{tools_desc}\n"
                f"Previous steps:\n{history or 'None yet'}\n\n"
                f"Step {step_idx}: Choose the next tool to call. "
                'Respond ONLY with valid JSON: {"thought": "...", "action": "<tool_name>", "action_input": {}}'
            )

            action_input: dict[str, Any] = {}
            if llm_fallback_reason is None:
                try:
                    raw = await asyncio.wait_for(self.llm.ainvoke(prompt), timeout=LLM_TIMEOUT_S)
                    parsed = self._parse_json(raw)
                    suggested_action = parsed.get("action")
                    if (
                        not isinstance(suggested_action, str)
                        or suggested_action not in _TOOL_DISPATCH
                    ):
                        raise ValueError("missing_or_invalid_action")

                    action = suggested_action
                    if action in used_actions:
                        action = self._fallback_action(step_idx, used_actions)
                    thought = str(parsed.get("thought") or f"Evaluating {action}")
                    raw_action_input = parsed.get("action_input", {})
                    if isinstance(raw_action_input, dict):
                        action_input = raw_action_input
                except Exception as exc:
                    llm_fallback_reason = self._describe_exception(exc)
                    logger.info(
                        "react_loop_using_deterministic_fallback",
                        step=step_idx,
                        reason=llm_fallback_reason,
                    )

            if llm_fallback_reason is not None:
                action = self._fallback_action(step_idx, used_actions)
                thought = f"Fallback: running {action}"

            observation_dict, observation_str = self._execute_tool(action, results)
            accumulated.update(observation_dict)
            used_actions.add(action)

            step = ReActStep(
                step=step_idx,
                thought=thought,
                action=action,
                action_input=action_input,
                observation=observation_str,
                partial_score=min(20.0, len(observation_dict) * 4.0),
            )
            chain.append(step)
            history += (
                f"\nStep {step_idx}: thought={thought!r} action={action} -> {observation_str[:120]}"
            )

        return chain, accumulated

    def _execute_tool(self, action: str, results: list[dict]) -> tuple[dict[str, Any], str]:
        fn = _TOOL_DISPATCH.get(action)
        if fn is None:
            return {}, f"Unknown tool: {action}"
        try:
            observation = fn(results)
            return observation, json.dumps(observation)
        except Exception as exc:
            logger.warning("tool_execution_error", action=action, error=str(exc))
            return {}, f"Error running {action}: {exc}"

    def _build_metrics(self, obs: dict[str, Any]) -> MissionMetrics:
        return MissionMetrics(  # type: ignore[call-arg]
            mission_success_rate=float(obs.get("mission_success_rate", 0.0)),
            simulation_iterations=int(obs.get("simulation_iterations", 1)),
            code_iterations_needed=int(obs.get("code_iterations_needed", 1)),
            path_efficiency=float(obs.get("path_efficiency", 0.0)),
            total_distance_m=float(obs.get("total_distance_m", 0.0)),
            waypoint_accuracy_m=float(obs.get("waypoint_accuracy_m", 0.0)),
            coverage_pattern_rate=float(obs.get("coverage_pattern_rate", 0.0)),
            closed_loop_rate=float(obs.get("closed_loop_rate", 0.0)),
            return_to_home_error_m=float(obs.get("return_to_home_error_m", 0.0)),
            north_span_m=float(obs.get("north_span_m", 0.0)),
            east_span_m=float(obs.get("east_span_m", 0.0)),
            battery_efficiency=float(obs.get("battery_efficiency", 0.0)),
            battery_remaining_pct=float(obs.get("battery_remaining_pct", 0.0)),
            safety_score=float(obs.get("safety_score", 0.0)),
            constraint_compliance=float(obs.get("constraint_compliance", 1.0)),
            safety_violations_count=int(obs.get("safety_violations_count", 0)),
            avg_flight_duration_s=float(obs.get("avg_flight_duration_s", 0.0)),
            avg_speed_ms=float(obs.get("avg_speed_ms", 0.0)),
            max_altitude_m=float(obs.get("max_altitude_m", 0.0)),
            mission_completed_rate=float(obs.get("mission_completed_rate", 0.0)),
            telemetry_real_rate=float(obs.get("telemetry_real_rate", 0.0)),
            avg_telemetry_points=float(obs.get("avg_telemetry_points", 0.0)),
            mission_started=bool(obs.get("mission_started", True)),
            telemetry_points=int(obs.get("telemetry_points", obs.get("avg_telemetry_points", 0))),
            mission_error_count=int(obs.get("mission_error_count", 0)),
            warning_count=int(obs.get("warning_count", 0)),
            avg_semantic_score=float(obs.get("avg_semantic_score", 0.0)),
            # Phase 11 — Production-grade Control Engineering metrics
            position_rmse_m=float(obs.get("position_rmse_m", 0.0)),
            altitude_rmse_m=float(obs.get("altitude_rmse_m", 0.0)),
            cross_track_error_max_m=float(obs.get("cross_track_error_max_m", 0.0)),
            cross_track_error_mean_m=float(obs.get("cross_track_error_mean_m", 0.0)),
            heading_rmse_deg=float(obs.get("heading_rmse_deg", 0.0)),
            roll_oscillation_freq_hz=float(obs.get("roll_oscillation_freq_hz", 0.0)),
            pitch_oscillation_freq_hz=float(obs.get("pitch_oscillation_freq_hz", 0.0)),
            roll_oscillation_power=float(obs.get("roll_oscillation_power", 0.0)),
            pitch_oscillation_power=float(obs.get("pitch_oscillation_power", 0.0)),
            attitude_rms_deg=float(obs.get("attitude_rms_deg", 0.0)),
            motor_saturation_percent_peak=float(obs.get("motor_saturation_percent_peak", 0.0)),
            motor_saturation_percent_mean=float(obs.get("motor_saturation_percent_mean", 0.0)),
            hover_drift_m_per_min=float(obs.get("hover_drift_m_per_min", 0.0)),
            flight_quality_rth_error_m=float(obs.get("flight_quality_rth_error_m", 0.0)),
            setpoint_tracking_grade=str(obs.get("setpoint_tracking_grade", "F")),
            stability_grade=str(obs.get("stability_grade", "F")),
            flight_quality_score=float(obs.get("flight_quality_score", 0.0)),
            flight_quality_grade=str(obs.get("flight_quality_grade", "F")),
        )

    @staticmethod
    def _compute_score(metrics: MissionMetrics) -> float:
        """Phase 11: blend semantic success + production-grade flight quality.

        Old formula: success * 30 + path_eff * 20 + battery * 15 + safety * 25
                   + semantic * 10.

        New formula keeps the safety/success weights but reallocates the
        path-quality bucket to the real flight_quality_score (RMSE +
        oscillation + saturation + RTH). When flight_quality_score is 0
        (no commanded path or no attitude telemetry available), the
        legacy path_efficiency value is used as a fallback so existing
        behaviour is preserved.
        """
        semantic_norm = metrics.avg_semantic_score / 100.0
        # Prefer the production-grade flight quality score; fall back to
        # the legacy path efficiency when the new pipeline has no data.
        if metrics.flight_quality_score > 0:
            flight_quality = metrics.flight_quality_score / 100.0
        else:
            flight_quality = metrics.path_efficiency
        score = (
            metrics.mission_success_rate * 25
            + flight_quality * 25
            + metrics.battery_efficiency * 15
            + metrics.safety_score * 25
            + semantic_norm * 10
        )
        return max(0.0, min(100.0, score))

    async def _synthesize_text(
        self,
        metrics: MissionMetrics,
        overall_score: float,
        mission_description: str,
    ) -> tuple[list[str], list[str], list[IssueRecord], str]:
        prompt = (
            "You are a drone mission analyst. Analyse the following metrics and respond ONLY with valid JSON.\n\n"
            f"Mission: {mission_description}\n"
            f"Overall score: {overall_score:.1f}/100\n"
            f"Success rate: {metrics.mission_success_rate:.1%}\n"
            f"Mission completion rate: {metrics.mission_completed_rate:.1%}\n"
            f"Real telemetry rate: {metrics.telemetry_real_rate:.1%}\n"
            f"Average telemetry points: {metrics.avg_telemetry_points:.0f}\n"
            f"Path efficiency: {metrics.path_efficiency:.2f}\n"
            f"Coverage pattern rate: {metrics.coverage_pattern_rate:.1%}\n"
            f"Closed-loop rate: {metrics.closed_loop_rate:.1%}\n"
            f"Return-to-home error: {metrics.return_to_home_error_m:.1f}m\n"
            f"Coverage span: north={metrics.north_span_m:.1f}m, east={metrics.east_span_m:.1f}m\n"
            f"Battery remaining: {metrics.battery_remaining_pct:.1f}%\n"
            f"Battery efficiency: {metrics.battery_efficiency:.2f}\n"
            f"Safety score: {metrics.safety_score:.2f}\n"
            f"Safety violations: {metrics.safety_violations_count}\n"
            f"Avg semantic score: {metrics.avg_semantic_score:.1f}\n\n"
            "For square, aerial-photo, survey, mapping, or coverage missions, do not recommend "
            "shortest-path planning unless coverage_pattern_rate is low. Prefer corner accuracy, "
            "span matching, lap count, cruise speed, and return-to-home closure recommendations.\n\n"
            "Return exactly:\n"
            "{\n"
            '  "recommendations": ["...", "..."],\n'
            '  "strengths": ["...", "..."],\n'
            '  "issues": [\n'
            '    {"category": "safety|battery|path|code|timing|compliance",\n'
            '     "severity": "critical|high|medium|low",\n'
            '     "description": "...",\n'
            '     "suggestion": "..."}\n'
            "  ],\n"
            '  "mission_summary": "2-3 sentence executive summary"\n'
            "}"
        )

        try:
            raw = await asyncio.wait_for(self.llm.ainvoke(prompt), timeout=LLM_TIMEOUT_S)
            data = self._parse_json(raw)
            recommendations = [str(item) for item in data.get("recommendations", [])][:8]
            strengths = [str(item) for item in data.get("strengths", [])][:6]
            issues: list[IssueRecord] = []
            for issue in data.get("issues", [])[:10]:
                try:
                    issues.append(IssueRecord(**issue))
                except Exception:
                    continue
            summary = str(data.get("mission_summary", ""))
            if not recommendations:
                raise ValueError("empty recommendations")
        except Exception as exc:
            logger.info(
                "synthesize_text_rule_based_fallback",
                reason=self._describe_exception(exc),
            )
            recommendations, strengths, issues, summary = self._rule_based_feedback(
                metrics, overall_score
            )

        return recommendations, strengths, issues, summary

    def _rule_based_feedback(
        self,
        metrics: MissionMetrics,
        score: float,
    ) -> tuple[list[str], list[str], list[IssueRecord], str]:
        recommendations: list[str] = []
        strengths: list[str] = []
        issues: list[IssueRecord] = []
        is_coverage_mission = metrics.coverage_pattern_rate >= 0.5
        has_real_telemetry = metrics.telemetry_real_rate >= 0.5

        if metrics.mission_success_rate < 0.5:
            recommendations.append("Increase simulation iterations to identify failure modes.")
            issues.append(
                IssueRecord(
                    category="code",
                    severity="critical",
                    description=f"Mission success rate only {metrics.mission_success_rate:.0%}",
                    suggestion="Review waypoint logic and collision avoidance.",
                )
            )
        else:
            strengths.append(f"Mission success rate is {metrics.mission_success_rate:.0%}.")

        if metrics.mission_completed_rate >= 0.5:
            strengths.append("Mission completion was confirmed by the simulation runner.")
        if has_real_telemetry:
            strengths.append(
                f"Report is based on real SITL telemetry ({metrics.avg_telemetry_points:.0f} samples on average)."
            )

        if is_coverage_mission and metrics.path_efficiency < 0.7:
            recommendations.append(
                "Validate the coverage geometry: compare actual north/east spans and corner closure against the requested square."
            )
            issues.append(
                IssueRecord(
                    category="path",
                    severity="medium",
                    description=(
                        f"Coverage path quality {metrics.path_efficiency:.2f}; "
                        f"span north={metrics.north_span_m:.1f}m, east={metrics.east_span_m:.1f}m, "
                        f"return error={metrics.return_to_home_error_m:.1f}m"
                    ),
                    suggestion="Tune square waypoints, corner acceptance radius, and lap timing.",
                )
            )
        elif metrics.path_efficiency < 0.7:
            recommendations.append("Optimise flight path - use shortest-path planning.")
            issues.append(
                IssueRecord(
                    category="path",
                    severity="high",
                    description=f"Path efficiency {metrics.path_efficiency:.2f} below threshold 0.70",
                    suggestion="Apply RRT* or A* path planning.",
                )
            )
        else:
            if is_coverage_mission:
                strengths.append(
                    "Coverage-style path was recognised, so the route is judged as a survey loop instead of a straight-line trip."
                )
                recommendations.append(
                    "Next monitor upgrade: report requested-vs-actual square size, corner error, lap count, and cruise-speed consistency from telemetry."
                )
            else:
                strengths.append("Path efficiency is acceptable.")

        if metrics.battery_remaining_pct < 20:
            recommendations.append("Add RTL trigger at 25% battery.")
            issues.append(
                IssueRecord(
                    category="battery",
                    severity="high",
                    description=f"Battery only {metrics.battery_remaining_pct:.0f}% remaining at mission end",
                    suggestion="Shorten mission range or increase battery capacity.",
                )
            )
        if metrics.battery_efficiency > 0.8:
            strengths.append("Battery utilisation is efficient.")

        if metrics.safety_violations_count > 0:
            severity = "critical" if metrics.safety_violations_count >= 5 else "medium"
            issues.append(
                IssueRecord(
                    category="safety",
                    severity=severity,
                    description=f"{metrics.safety_violations_count} safety violations detected",
                    suggestion="Enable geofencing and reduce max speed in restricted zones.",
                )
            )
        if metrics.safety_score > 0.9:
            strengths.append("Excellent safety compliance throughout the mission.")

        if metrics.code_iterations_needed > 3:
            recommendations.append("Improve prompt clarity to reduce code iteration count.")
        if metrics.avg_semantic_score > 75:
            strengths.append("High semantic alignment between prompt and generated code.")
        if metrics.mission_error_count:
            issues.append(
                IssueRecord(
                    category="timing",
                    severity="medium",
                    description=f"{metrics.mission_error_count} mission run(s) reported execution errors",
                    suggestion="Inspect SITL helper stdout/stderr and telemetry payload artifacts.",
                )
            )
        if not has_real_telemetry and metrics.simulation_iterations > 0:
            recommendations.append(
                "Surface telemetry provenance prominently because this report is not based on real SITL telemetry."
            )

        profile = "coverage/square" if is_coverage_mission else "point-to-point"
        provenance = (
            "real SITL telemetry" if has_real_telemetry else "non-real or unknown telemetry"
        )
        summary = (
            f"Mission scored {score:.0f}/100 as a {profile} profile using {provenance}. "
            f"It achieved "
            f"{metrics.mission_success_rate:.0%} success rate across "
            f"{metrics.simulation_iterations} simulation(s). "
            f"Path quality: {metrics.path_efficiency:.2f}, "
            f"span: {metrics.north_span_m:.0f}m north by {metrics.east_span_m:.0f}m east, "
            f"return error: {metrics.return_to_home_error_m:.1f}m. "
            f"Battery remaining: {metrics.battery_remaining_pct:.0f}%."
        )
        return recommendations, strengths, issues, summary

    @staticmethod
    def _parse_json(text: str) -> dict:
        cleaned = re.sub(r"```(?:json)?\s*", "", str(text or "")).replace("```", "").strip()
        decoder = json.JSONDecoder()
        start_candidates = [index for index in (cleaned.find("{"), cleaned.find("[")) if index >= 0]
        for start_idx in sorted(start_candidates):
            try:
                parsed, _ = decoder.raw_decode(cleaned[start_idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                return parsed[0]

        try:
            parsed = ast.literal_eval(cleaned)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed[0]
        return {}

    @staticmethod
    def _fallback_action(step_idx: int, used_actions: set[str]) -> str:
        for action in _TOOL_ORDER:
            if action not in used_actions:
                return action
        return _TOOL_ORDER[(step_idx - 1) % len(_TOOL_ORDER)]

    @staticmethod
    def _describe_exception(exc: Exception) -> str:
        return str(exc).strip() or exc.__class__.__name__
