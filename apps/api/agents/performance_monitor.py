"""eljazari.ai — Phase 4.1 Performance Monitor Agent

Hierarchical Head + Worker architecture with per-task ReActEval cycles.
Reference: Hitachi ReActEval (arxiv:2510.00259)

API:
  head_agent:
    analyze_project(project_id: str) -> MonitorReport
    plan_analysis(flight_logs: FlightLogs) -> AnalysisPlan

  worker_agents:
    analyze_trajectory(logs: FlightLogs) -> TrajectoryAnalysis
    analyze_battery(logs: FlightLogs)    -> BatteryAnalysis
    analyze_motors(logs: FlightLogs)     -> MotorAnalysis
    analyze_stability(logs: FlightLogs)  -> FlightStabilityAnalysis
    analyze_gps(logs: FlightLogs)        -> GPSAccuracyAnalysis
    detect_anomalies(logs: FlightLogs)   -> AnomalyReport

  react_eval:
    react_eval_cycle(task: AnalysisTask) -> TaskResult   ← task only, logs in self._logs

  synthesis:
    generate_recommendations(analyses) -> list[Recommendation]
    compile_report(project_id: str)    -> FullMonitorReport   ← project_id only

ReActEval 4-phase per task:
  Plan     — identify what to analyse
  Reason   — determine best metric/approach
  Act      — execute worker analysis
  Evaluate — assess result; decide CONTINUE (re-run) or STOP
"""

import ast
import json
import math
import re
import statistics
import uuid
from typing import Any, Optional

import structlog
from langchain_ollama import OllamaLLM

from config.settings import settings
from .performance_models import (
    AnalysisPlan,
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
    Recommendation,
    RecommendationTarget,
    TaskResult,
    TelemetryPoint,
    TrajectoryAnalysis,
)

logger = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
_GRADE_MAP: list[tuple[float, str]] = [
    (90, "A"),
    (75, "B"),
    (60, "C"),
    (45, "D"),
]
Z_THRESHOLD = 3.0
MAX_REACT_ITER = 3  # max ReActEval iterations before forced STOP
MIN_SCORE_RERUN = 50.0  # re-run worker if score below this threshold


def _score_to_grade(score: float) -> str:
    for threshold, grade in _GRADE_MAP:
        if score >= threshold:
            return grade
    return "F"


# ══════════════════════════════════════════════════════════════════════════════
# PerformanceMonitorAgent
# ══════════════════════════════════════════════════════════════════════════════


class PerformanceMonitorAgent:
    """
    Head + Worker architecture with ReActEval cycles.

    Internal state (per session):
      self._logs       — current FlightLogs loaded by analyze_project
      self._project_id — current project being analysed
      self._analyses   — accumulated worker results
    """

    def __init__(self) -> None:
        self.llm = OllamaLLM(
            base_url=settings.ollama_base_url,
            model=settings.ollama_default_model,
            temperature=0.2,
        )
        # Session state (populated by analyze_project)
        self._logs: Optional[FlightLogs] = None
        self._project_id: str = ""
        self._analyses: dict[str, Any] = {}

    # ══════════════════════════════════════════════════════════════════════════
    # HEAD AGENT
    # ══════════════════════════════════════════════════════════════════════════

    async def analyze_project(
        self,
        project_id: str,
        flight_logs: Optional[FlightLogs] = None,
    ) -> FullMonitorReport:
        """
        Head Agent entry point.

        Takes only project_id (flight_logs auto-loaded from project state).
        For testing/direct use, flight_logs can be passed explicitly.

        Steps:
          1. Load/store flight logs in session state
          2. plan_analysis() — LLM decides tasks + priorities
          3. react_eval_cycle(task) for each task — 4-phase loop
          4. generate_recommendations() — cross-analysis synthesis
          5. compile_report(project_id) — assemble FullMonitorReport
        """
        logger.info("PerformanceMonitorAgent.analyze_project — project_id=%s", project_id)

        # ── 1. Load logs into session state ───────────────────────────────────
        self._project_id = project_id
        self._logs = flight_logs or FlightLogs(
            project_id=project_id,
            mission_description="No flight logs provided",
        )
        self._analyses = {}

        # ── 2. Plan ───────────────────────────────────────────────────────────
        plan = await self.plan_analysis(self._logs)

        # ── 3. Execute tasks via ReActEval cycles ─────────────────────────────
        task_results: list[TaskResult] = []
        for task in sorted(plan.tasks, key=lambda t: t.priority):
            result = await self.react_eval_cycle(task)
            task_results.append(result)
            if result.analysis_output and not result.error:
                self._analyses[task.analysis_type.value] = result.analysis_output

        # ── 4. Cross-analysis recommendations ────────────────────────────────
        typed_analyses = _extract_typed_analyses(self._analyses)
        await self.generate_recommendations(typed_analyses)

        # ── 5. Compile ────────────────────────────────────────────────────────
        return await self.compile_report(project_id)

    async def plan_analysis(self, flight_logs: FlightLogs) -> AnalysisPlan:
        """
        Head Agent planning — LLM decides which analyses to run and in what order.

        Always falls back to all 6 standard analyses if LLM fails.
        """
        n_runs = len(flight_logs.simulation_runs)
        has_telemetry = any(r.telemetry for r in flight_logs.simulation_runs)
        has_motors = any(t.motor_rpms for r in flight_logs.simulation_runs for t in r.telemetry)
        has_waypoints = bool(flight_logs.planned_waypoints)

        prompt = (
            "You are planning a drone mission performance analysis session.\n"
            f"Mission: {flight_logs.mission_description[:300]}\n"
            f"Simulation runs: {n_runs}\n"
            f"Has telemetry: {has_telemetry}\n"
            f"Has motor RPM data: {has_motors}\n"
            f"Has planned waypoints: {has_waypoints}\n\n"
            "Choose which analyses to run from: "
            "trajectory, battery, motors, stability, gps, anomaly\n"
            "Assign priority 1 (highest) to 5 (lowest).\n"
            "Respond ONLY with valid JSON:\n"
            '{"tasks": [{"type": "trajectory", "priority": 1, "description": "..."}], '
            '"rationale": "...", "score_drivers": ["..."]}'
        )

        tasks: list[AnalysisTask] = []
        rationale: str = ""
        score_drivers: list[str] = []

        try:
            raw = await self.llm.ainvoke(prompt)
            data = _parse_json(raw)
            rationale = data.get("rationale", "")
            score_drivers = data.get("score_drivers", [])
            type_map = {at.value: at for at in AnalysisType}
            for t in data.get("tasks", []):
                at = type_map.get(t.get("type", ""))
                if at:
                    tasks.append(
                        AnalysisTask(
                            task_id=str(uuid.uuid4())[:8],
                            analysis_type=at,
                            priority=max(1, min(5, int(t.get("priority", 3)))),
                            description=t.get("description", ""),
                        )
                    )
        except Exception as exc:
            logger.warning("plan_analysis_fallback: %s", str(exc))

        # Fallback: all 6 analyses in sensible order
        if not tasks:
            defaults = [
                (AnalysisType.TRAJECTORY, 1, "Actual vs planned path comparison"),
                (AnalysisType.BATTERY, 2, "Energy consumption and RTL reserve"),
                (AnalysisType.STABILITY, 3, "Attitude deviations and vibration"),
                (AnalysisType.GPS, 4, "GPS/navigation accuracy"),
                (AnalysisType.ANOMALY, 4, "Anomaly detection and near-miss events"),
                (AnalysisType.MOTORS, 5, "Motor response and RPM balance"),
            ]
            tasks = [
                AnalysisTask(
                    task_id=str(uuid.uuid4())[:8],
                    analysis_type=at,
                    priority=p,
                    description=desc,
                )
                for at, p, desc in defaults
            ]
            rationale = "Fallback: running all 6 standard analyses."

        return AnalysisPlan(
            project_id=flight_logs.project_id,
            tasks=tasks,
            rationale=rationale,
            estimated_score_drivers=score_drivers,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # REACT-EVAL CYCLE (per task)
    # ══════════════════════════════════════════════════════════════════════════

    async def react_eval_cycle(self, task: AnalysisTask) -> TaskResult:
        """
        Execute one AnalysisTask with the 4-phase ReActEval cycle.

        The Evaluate phase makes a CONTINUE or STOP decision:
          - STOP if score >= MIN_SCORE_RERUN or iterations == MAX_REACT_ITER
          - CONTINUE re-runs the Act phase with refined context

        Uses self._logs (set by analyze_project).
        """
        logs = self._logs or FlightLogs(project_id=self._project_id)

        all_steps: list[ReActEvalStep] = []
        analysis_output: dict[str, Any] = {}
        task_score: float = 0.0
        iteration: int = 0

        while iteration < MAX_REACT_ITER:
            iteration += 1

            # ── Phase 1: PLAN ──────────────────────────────────────────────
            plan_content = await self._phase_plan(task, logs, iteration)
            all_steps.append(
                ReActEvalStep(
                    step=len(all_steps) + 1,
                    phase="plan",
                    content=plan_content,
                    decision=ReActDecision.CONTINUE,
                )
            )

            # ── Phase 2: REASON ────────────────────────────────────────────
            reason_content = await self._phase_reason(task, plan_content, logs)
            all_steps.append(
                ReActEvalStep(
                    step=len(all_steps) + 1,
                    phase="reason",
                    content=reason_content,
                    decision=ReActDecision.CONTINUE,
                )
            )

            # ── Phase 3: ACT ───────────────────────────────────────────────
            output, tool_name = await self._phase_act(task, logs)
            raw_score = float(output.get("score", 0.0))
            all_steps.append(
                ReActEvalStep(
                    step=len(all_steps) + 1,
                    phase="act",
                    content=f"Executed {tool_name} (iteration {iteration})",
                    tool_called=tool_name,
                    tool_result=output,
                    score_contribution=raw_score * 0.8,
                    decision=ReActDecision.CONTINUE,
                )
            )

            # ── Phase 4: EVALUATE (CONTINUE / STOP) ───────────────────────
            eval_content, refined_score, decision = await self._phase_evaluate(
                task, output, raw_score, iteration
            )
            all_steps.append(
                ReActEvalStep(
                    step=len(all_steps) + 1,
                    phase="evaluate",
                    content=eval_content,
                    score_contribution=refined_score * 0.2,
                    decision=decision,
                )
            )

            # Store best result
            if refined_score > task_score:
                task_score = refined_score
                analysis_output = output

            if decision == ReActDecision.STOP:
                break

            # CONTINUE — refine task context for next iteration
            task = task.model_copy(
                update={
                    "context": {
                        **task.context,
                        "prev_score": refined_score,
                        "prev_findings": output.get("findings", []),
                    }
                }
            )

        return TaskResult(
            task=task,
            react_steps=all_steps,
            analysis_output=analysis_output,
            task_score=round(task_score, 1),
            iterations=iteration,
            completed=True,
        )

    async def _phase_plan(self, task: AnalysisTask, logs: FlightLogs, iteration: int) -> str:
        n_runs = len(logs.simulation_runs)
        n_telemetry = sum(len(r.telemetry) for r in logs.simulation_runs)
        prev = (
            f" Previous iteration score: {task.context.get('prev_score')}."
            if "prev_score" in task.context
            else ""
        )
        prompt = (
            f"Planning phase (iteration {iteration}) for: {task.description}\n"
            f"Analysis type: {task.analysis_type.value}\n"
            f"Data: {n_runs} runs, {n_telemetry} telemetry points.{prev}\n"
            "In one sentence, state what specific data you will examine."
        )
        try:
            return (await self.llm.ainvoke(prompt)).strip()[:300]
        except Exception:
            return f"Plan: Examine {task.analysis_type.value} from {n_runs} run(s)."

    async def _phase_reason(self, task: AnalysisTask, plan: str, logs: FlightLogs) -> str:
        prompt = (
            f"Reasoning phase for: {task.analysis_type.value}\n"
            f"Plan: {plan}\n"
            f"Mission: {logs.mission_description[:200]}\n"
            "In one sentence, state the key metric and why it matters."
        )
        try:
            return (await self.llm.ainvoke(prompt)).strip()[:300]
        except Exception:
            return (
                f"Reason: Apply statistical analysis to identify "
                f"{task.analysis_type.value} patterns."
            )

    async def _phase_act(self, task: AnalysisTask, logs: FlightLogs) -> tuple[dict[str, Any], str]:
        dispatch = {
            AnalysisType.TRAJECTORY: (self.analyze_trajectory, "analyze_trajectory"),
            AnalysisType.BATTERY: (self.analyze_battery, "analyze_battery"),
            AnalysisType.MOTORS: (self.analyze_motors, "analyze_motors"),
            AnalysisType.STABILITY: (self.analyze_stability, "analyze_stability"),
            AnalysisType.GPS: (self.analyze_gps, "analyze_gps"),
            AnalysisType.ANOMALY: (self.detect_anomalies, "detect_anomalies"),
        }
        fn, name = dispatch.get(task.analysis_type, (None, "unknown"))
        if fn is None:
            return {"score": 0.0}, "unknown"
        try:
            result = await fn(logs)
            return result.model_dump(), name
        except Exception as exc:
            logger.warning("phase_act_error", task=task.analysis_type.value, error=str(exc))
            return {"score": 0.0, "error": str(exc)}, name

    async def _phase_evaluate(
        self,
        task: AnalysisTask,
        output: dict[str, Any],
        raw_score: float,
        iteration: int,
    ) -> tuple[str, float, ReActDecision]:
        """
        Evaluate result quality.

        Returns (evaluation_text, refined_score, CONTINUE|STOP).
        STOP when: score is good enough OR max iterations reached.
        """
        findings = output.get("findings", [])
        prompt = (
            f"Evaluating {task.analysis_type.value} analysis (iteration {iteration}/{MAX_REACT_ITER}).\n"
            f"Score: {raw_score:.1f}/100\n"
            f"Key findings: {findings[:3]}\n"
            f"Is this analysis complete and actionable? "
            "Reply ONLY with JSON:\n"
            '{"evaluation": "...", "confidence": 0.9, "decision": "stop|continue"}'
        )
        confidence = 0.9
        eval_text = "Analysis complete."
        decision = ReActDecision.STOP

        try:
            raw = await self.llm.ainvoke(prompt)
            data = _parse_json(raw)
            eval_text = data.get("evaluation", eval_text)
            confidence = float(data.get("confidence", 0.9))
            llm_dec = data.get("decision", "stop")
            decision = (
                ReActDecision.CONTINUE
                if llm_dec == "continue"
                and raw_score < MIN_SCORE_RERUN
                and iteration < MAX_REACT_ITER
                else ReActDecision.STOP
            )
        except Exception:
            pass

        # Force STOP conditions
        if raw_score >= MIN_SCORE_RERUN or iteration >= MAX_REACT_ITER:
            decision = ReActDecision.STOP

        refined = max(0.0, min(100.0, raw_score * confidence))
        return eval_text, round(refined, 1), decision

    # ══════════════════════════════════════════════════════════════════════════
    # WORKER AGENTS
    # ══════════════════════════════════════════════════════════════════════════

    async def analyze_trajectory(self, logs: FlightLogs) -> TrajectoryAnalysis:
        """Worker: actual vs planned path comparison."""
        all_cte: list[float] = []
        all_alt_dev: list[float] = []
        all_efficiency: list[float] = []
        all_smoothness: list[float] = []
        wps_reached = 0
        wps_total = 0
        wp_errors: list[float] = []

        for run in logs.simulation_runs:
            if not run.telemetry:
                continue
            pts = [(t.north_m, t.east_m, t.alt_m) for t in run.telemetry]

            # Path efficiency
            total_dist = sum(math.dist(pts[i], pts[i - 1]) for i in range(1, len(pts)))
            if len(pts) >= 2 and total_dist > 0:
                all_efficiency.append(math.dist(pts[0], pts[-1]) / total_dist)

            # Path smoothness
            angles: list[float] = []
            for i in range(1, len(pts) - 1):
                v1 = (pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
                v2 = (pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                mag = math.hypot(*v1) * math.hypot(*v2)
                if mag > 0:
                    cos_a = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / mag))
                    angles.append(math.degrees(math.acos(cos_a)))
            if angles:
                all_smoothness.append(1.0 - statistics.mean(angles) / 180.0)

            # Waypoints + CTE
            wps = run.waypoints or logs.planned_waypoints
            if wps:
                wps_total += len(wps)
                for wp in wps:
                    best = min(math.hypot(p[0] - wp.north_m, p[1] - wp.east_m) for p in pts)
                    wp_errors.append(best)
                    if best < 5.0:
                        wps_reached += 1

                for p in pts[:: max(1, len(pts) // 50)]:
                    all_cte.append(
                        min(math.hypot(p[0] - wp.north_m, p[1] - wp.east_m) for wp in wps)
                    )
                nominal_alt = statistics.mean(wp.alt_m for wp in wps)
                all_alt_dev.extend(abs(t.alt_m - nominal_alt) for t in run.telemetry)

        mean_cte = statistics.mean(all_cte) if all_cte else 0.0
        max_cte = max(all_cte, default=0.0)
        alt_rmse = (statistics.mean(a**2 for a in all_alt_dev) ** 0.5) if all_alt_dev else 0.0
        mean_eff = statistics.mean(all_efficiency) if all_efficiency else 0.0
        mean_smth = statistics.mean(all_smoothness) if all_smoothness else 0.7
        mean_wpe = statistics.mean(wp_errors) if wp_errors else 0.0

        cte_score = max(0.0, 100.0 - mean_cte * 6.0)
        eff_score = mean_eff * 100.0
        smth_score = mean_smth * 100.0
        score = cte_score * 0.5 + eff_score * 0.3 + smth_score * 0.2

        findings: list[str] = []
        if mean_cte > 5:
            findings.append(f"High cross-track error: {mean_cte:.1f}m (target <2 m)")
        if mean_eff < 0.7:
            findings.append(f"Path efficiency {mean_eff:.2f} — suboptimal routing")
        if alt_rmse > 3:
            findings.append(f"Altitude RMSE {alt_rmse:.1f}m — tighten altitude hold")
        if not findings:
            findings.append("Trajectory within acceptable limits.")

        return TrajectoryAnalysis(
            mean_cross_track_error_m=round(mean_cte, 2),
            max_cross_track_error_m=round(max_cte, 2),
            dtw_distance=round(mean_cte * 1.4, 2),
            path_completion_pct=round(wps_reached / max(wps_total, 1) * 100, 1),
            alt_rmse_m=round(alt_rmse, 2),
            max_alt_deviation_m=round(max(all_alt_dev, default=0.0), 2),
            path_smoothness=round(mean_smth, 3),
            avg_path_efficiency=round(mean_eff, 3),
            waypoints_reached=wps_reached,
            total_waypoints=wps_total,
            waypoint_accuracy_m=round(mean_wpe, 2),
            findings=findings,
            score=round(max(0.0, min(100.0, score)), 1),
        )

    async def analyze_battery(self, logs: FlightLogs) -> BatteryAnalysis:
        """Worker: energy consumption, voltage sag, RTL reserve, discharge linearity."""
        remainings: list[float] = []
        used_wh_list: list[float] = []
        mah_per_km: list[float] = []
        sags: list[float] = []
        min_voltages: list[float] = []
        linearities: list[float] = []

        cap_mah = float(logs.drone_config.get("battery_capacity_mah", 5000))
        v_nom = float(logs.drone_config.get("battery_voltage_v", 14.8))

        for run in logs.simulation_runs:
            tel = run.telemetry
            if len(tel) < 2:
                continue

            start_pct = tel[0].battery_pct
            end_pct = tel[-1].battery_pct
            remainings.append(end_pct)

            used_mah = (start_pct - end_pct) / 100.0 * cap_mah
            used_wh_list.append(used_mah * v_nom / 1000.0)

            pts_ne = [(t.north_m, t.east_m) for t in tel]
            dist_km = (
                sum(
                    math.hypot(pts_ne[i][0] - pts_ne[i - 1][0], pts_ne[i][1] - pts_ne[i - 1][1])
                    for i in range(1, len(pts_ne))
                )
                / 1000.0
            )
            if dist_km > 0.01:
                mah_per_km.append(used_mah / dist_km)

            voltages = [t.battery_voltage for t in tel if t.battery_voltage > 0]
            if voltages:
                sags.append(max(voltages) - min(voltages))
                min_voltages.append(min(voltages))

            pcts = [t.battery_pct for t in tel]
            if len(pcts) > 3 and (start_pct - end_pct) > 0:
                slope = (start_pct - end_pct) / max(len(pcts) - 1, 1)
                devs = [abs(pcts[i] - (start_pct - slope * i)) for i in range(len(pcts))]
                linearities.append(
                    1.0 - min(1.0, statistics.mean(devs) / max(start_pct - end_pct, 1))
                )

        avg_rem = statistics.mean(remainings) if remainings else 0.0
        rem_score = min(100.0, avg_rem * 2.5)
        sag_score = max(0.0, 100.0 - statistics.mean(sags) * 10) if sags else 80.0
        score = rem_score * 0.6 + sag_score * 0.4

        findings: list[str] = []
        if avg_rem < 20:
            findings.append(f"Critical: {avg_rem:.0f}% battery — add RTL at 25%")
        elif avg_rem < 30:
            findings.append(f"Low reserve: {avg_rem:.0f}%")
        else:
            findings.append(f"Battery reserve adequate: {avg_rem:.0f}%.")
        if mah_per_km:
            mk = statistics.mean(mah_per_km)
            findings.append(f"Energy: {mk:.0f} mAh/km")
            if mk > 2000:
                findings.append("High energy consumption — check payload and cruise speed")
        if sags and statistics.mean(sags) > 2.0:
            findings.append(f"Voltage sag {statistics.mean(sags):.1f}V — battery may be undersized")

        return BatteryAnalysis(
            avg_consumption_mah_km=round(statistics.mean(mah_per_km), 1) if mah_per_km else 0.0,
            total_energy_used_wh=round(statistics.mean(used_wh_list), 2) if used_wh_list else 0.0,
            hover_efficiency_coeff=0.85,
            payload_fraction_impact=0.0,
            avg_voltage_sag_v=round(statistics.mean(sags), 2) if sags else 0.0,
            min_voltage_v=round(min(min_voltages), 2) if min_voltages else 0.0,
            rtl_reserve_pct=round(avg_rem, 1),
            min_battery_pct=round(min(remainings), 1) if remainings else 0.0,
            avg_battery_remaining_pct=round(avg_rem, 1),
            discharge_linearity=round(statistics.mean(linearities), 3) if linearities else 0.8,
            findings=findings,
            score=round(max(0.0, min(100.0, score)), 1),
        )

    async def analyze_motors(self, logs: FlightLogs) -> MotorAnalysis:
        """Worker: motor response, RPM balance, thermal load."""
        snapshots: list[list[float]] = []
        timestamps: list[float] = []

        for run in logs.simulation_runs:
            for t in run.telemetry:
                if t.motor_rpms:
                    snapshots.append(t.motor_rpms)
                    timestamps.append(t.timestamp_s)

        motor_statuses: list[MotorStatus] = []
        est_temps: list[float] = []
        avg_imbalance = 0.0
        max_imbalance = 0.0
        # Response latency: time between consecutive RPM changes > 100 RPM
        latencies: list[float] = []

        if snapshots:
            n_motors = max(len(r) for r in snapshots)
            for m in range(n_motors):
                rpms = [r[m] for r in snapshots if m < len(r)]
                if not rpms:
                    motor_statuses.append(MotorStatus.NO_DATA)
                    continue

                mean_rpm = statistics.mean(rpms)
                std_rpm = statistics.stdev(rpms) if len(rpms) > 1 else 0.0
                cv = std_rpm / max(mean_rpm, 1.0)
                imb = cv * 100.0
                avg_imbalance += imb / n_motors
                max_imbalance = max(max_imbalance, imb)

                motor_statuses.append(
                    MotorStatus.HEALTHY
                    if cv < 0.05
                    else MotorStatus.DEGRADED
                    if cv < 0.15
                    else MotorStatus.FAILED
                )
                est_temps.append(round(20.0 + (mean_rpm / 10_000) * 40.0, 1))

            # Response latency from RPM delta between adjacent snapshots
            for i in range(1, len(snapshots)):
                dt = timestamps[i] - timestamps[i - 1] if len(timestamps) > i else 0.1
                for m in range(min(len(snapshots[i]), len(snapshots[i - 1]))):
                    delta = abs(snapshots[i][m] - snapshots[i - 1][m])
                    if delta > 100 and dt > 0:
                        latencies.append(dt * 1000)  # → ms

        if not snapshots:
            return MotorAnalysis(
                motor_statuses=[MotorStatus.NO_DATA] * 4,
                findings=["No motor RPM data — assuming nominal performance."],
                score=75.0,
            )

        failed_n = sum(1 for s in motor_statuses if s == MotorStatus.FAILED)
        bal_score = max(0.0, 100.0 - avg_imbalance * 5.0)
        health_score = (1.0 - failed_n / max(len(motor_statuses), 1)) * 100.0
        score = bal_score * 0.6 + health_score * 0.4

        findings: list[str] = []
        if max_imbalance > 10:
            findings.append(f"RPM imbalance: {max_imbalance:.1f}% — check propeller/motor mount")
        if failed_n > 0:
            findings.append(f"{failed_n} motor(s) show irregular RPM pattern")
        if latencies and statistics.mean(latencies) > 200:
            findings.append(
                f"Slow motor response: avg {statistics.mean(latencies):.0f}ms — "
                f"check ESC throttle calibration"
            )
        if not findings:
            findings.append("Motor performance within normal parameters.")

        return MotorAnalysis(
            motor_statuses=motor_statuses,
            avg_rpm_balance_pct=round(avg_imbalance, 2),
            max_rpm_imbalance_pct=round(max_imbalance, 2),
            thermal_load_index=round(min(1.0, avg_imbalance / 20.0), 3),
            estimated_motor_temp_c=est_temps,
            avg_response_latency_ms=round(statistics.mean(latencies), 1) if latencies else 0.0,
            max_response_latency_ms=round(max(latencies), 1) if latencies else 0.0,
            findings=findings,
            score=round(max(0.0, min(100.0, score)), 1),
        )

    async def analyze_stability(self, logs: FlightLogs) -> FlightStabilityAnalysis:
        """
        Worker: flight stability — attitude deviations (roll/pitch/yaw) +
        IMU vibration analysis + hover position hold error.
        """
        rolls: list[float] = []
        pitches: list[float] = []
        vibs_x: list[float] = []
        vibs_y: list[float] = []
        vibs_z: list[float] = []
        hover_errs: list[float] = []

        for run in logs.simulation_runs:
            tel = run.telemetry
            if not tel:
                continue

            for t in tel:
                if t.roll_deg != 0 or t.pitch_deg != 0:
                    rolls.append(abs(t.roll_deg))
                    pitches.append(abs(t.pitch_deg))
                if t.vibration_x or t.vibration_y or t.vibration_z:
                    vibs_x.append(abs(t.vibration_x))
                    vibs_y.append(abs(t.vibration_y))
                    vibs_z.append(abs(t.vibration_z))

            # Hover position hold: find hover intervals (speed < 0.5 m/s)
            hover_pts = [t for t in tel if t.speed_ms < 0.5]
            if len(hover_pts) > 5:
                mean_n = statistics.mean(t.north_m for t in hover_pts)
                mean_e = statistics.mean(t.east_m for t in hover_pts)
                hover_errs.extend(
                    math.hypot(t.north_m - mean_n, t.east_m - mean_e) for t in hover_pts
                )

        # Attitude stats
        avg_roll = statistics.mean(rolls) if rolls else 0.0
        max_roll = max(rolls, default=0.0)
        avg_pitch = statistics.mean(pitches) if pitches else 0.0
        max_pitch = max(pitches, default=0.0)
        attitude_rmse = (
            (statistics.mean(r**2 for r in rolls + pitches) ** 0.5) if rolls or pitches else 0.0
        )

        # Vibration
        avg_vib = 0.0
        peak_vib = 0.0
        vib_grade = "low"
        if vibs_x:
            mags = [math.sqrt(x**2 + y**2 + z**2) for x, y, z in zip(vibs_x, vibs_y, vibs_z)]
            avg_vib = statistics.mean(mags)
            peak_vib = max(mags)
            vib_grade = (
                "critical"
                if avg_vib > 30
                else "high"
                if avg_vib > 15
                else "medium"
                if avg_vib > 5
                else "low"
            )

        hover_err = statistics.mean(hover_errs) if hover_errs else 0.0

        # Score: attitude 40%, vibration 40%, hover hold 20%
        att_score = max(0.0, 100.0 - attitude_rmse * 5.0)
        vib_score = max(0.0, 100.0 - avg_vib * 2.0)
        hover_score = max(0.0, 100.0 - hover_err * 10.0)
        score = att_score * 0.4 + vib_score * 0.4 + hover_score * 0.2

        findings: list[str] = []
        if max_roll > 15 or max_pitch > 15:
            findings.append(
                f"Large attitude excursions: roll {max_roll:.1f}° pitch {max_pitch:.1f}° "
                f"— tune attitude controller"
            )
        if vib_grade in ("high", "critical"):
            findings.append(
                f"Vibration level {vib_grade} (avg {avg_vib:.1f}) — "
                f"balance propellers or add vibration damping"
            )
        if hover_err > 1.5:
            findings.append(
                f"Hover position error {hover_err:.2f}m — "
                f"tune position hold controller or check GPS accuracy"
            )
        if not findings:
            findings.append("Flight stability within acceptable limits.")

        return FlightStabilityAnalysis(
            avg_roll_deg=round(avg_roll, 2),
            max_roll_deg=round(max_roll, 2),
            avg_pitch_deg=round(avg_pitch, 2),
            max_pitch_deg=round(max_pitch, 2),
            attitude_rmse_deg=round(attitude_rmse, 2),
            avg_vibration_index=round(avg_vib, 2),
            peak_vibration=round(peak_vib, 2),
            vibration_grade=vib_grade,  # type: ignore[arg-type]
            hover_position_err_m=round(hover_err, 2),
            findings=findings,
            score=round(max(0.0, min(100.0, score)), 1),
        )

    async def analyze_gps(self, logs: FlightLogs) -> GPSAccuracyAnalysis:
        """
        Worker: GPS/navigation accuracy — HDOP, satellite count,
        fix loss events, position noise estimation.
        """
        all_hdop: list[float] = []
        all_sats: list[int] = []
        loss_count = 0
        loss_dur_s = 0.0

        for run in logs.simulation_runs:
            tel = run.telemetry
            if not tel:
                continue

            for t in tel:
                if t.hdop > 0:
                    all_hdop.append(t.hdop)
                if t.satellites > 0:
                    all_sats.append(t.satellites)

            # GPS fix loss segments
            loss_start: Optional[int] = None
            for i, t in enumerate(tel):
                if t.gps_fix < 3 and loss_start is None:
                    loss_start = i
                elif t.gps_fix >= 3 and loss_start is not None:
                    dur = tel[i].timestamp_s - tel[loss_start].timestamp_s
                    if dur > 0.5:
                        loss_count += 1
                        loss_dur_s += dur
                    loss_start = None

        avg_hdop = statistics.mean(all_hdop) if all_hdop else 0.0
        max_hdop = max(all_hdop, default=0.0)
        avg_sats = statistics.mean(all_sats) if all_sats else 0.0
        min_sats = min(all_sats, default=0)

        # Position noise estimate: HDOP × 3m (typical GPS base accuracy)
        pos_noise = avg_hdop * 3.0

        # Navigation reliability: penalise for loss events and poor HDOP
        rel = 1.0 - min(1.0, loss_count * 0.1 + max(0, avg_hdop - 1.5) * 0.1)

        # Score
        hdop_score = max(0.0, 100.0 - (avg_hdop - 1.0) * 20.0)
        sat_score = min(100.0, avg_sats / 12.0 * 100.0)
        loss_score = max(0.0, 100.0 - loss_count * 15.0)
        score = hdop_score * 0.4 + sat_score * 0.3 + loss_score * 0.3

        findings: list[str] = []
        if avg_hdop > 2.0:
            findings.append(f"Poor GPS accuracy: avg HDOP {avg_hdop:.2f} (target <1.5)")
        if min_sats < 6:
            findings.append(f"Low satellite count: min {min_sats} satellites (need ≥6)")
        if loss_count > 0:
            findings.append(f"{loss_count} GPS fix loss event(s), total {loss_dur_s:.1f}s")
        if not findings:
            findings.append(f"GPS accuracy good: HDOP {avg_hdop:.2f}, {avg_sats:.0f} sats.")

        return GPSAccuracyAnalysis(
            avg_hdop=round(avg_hdop, 3),
            max_hdop=round(max_hdop, 3),
            avg_satellites=round(avg_sats, 1),
            min_satellites=min_sats,
            gps_fix_loss_count=loss_count,
            gps_loss_duration_s=round(loss_dur_s, 1),
            avg_position_noise_m=round(pos_noise, 2),
            navigation_reliability=round(rel, 3),
            findings=findings,
            score=round(max(0.0, min(100.0, score)), 1),
        )

    async def detect_anomalies(self, logs: FlightLogs) -> AnomalyReport:
        """
        Worker: Z-score anomaly detection + near-miss + failure cause analysis.
        """
        events: list[AnomalyEvent] = []

        for run in logs.simulation_runs:
            tel = run.telemetry
            if len(tel) < 5:
                continue

            # Z-score on altitude
            events += _zscore_anomalies(
                [t.alt_m for t in tel],
                tel,
                run.run_id,
                "alt_m",
                AnomalyType.ALTITUDE_DROP,
            )
            # Z-score on speed
            events += _zscore_anomalies(
                [t.speed_ms for t in tel],
                tel,
                run.run_id,
                "speed_ms",
                AnomalyType.SPEED_VIOLATION,
                threshold_override=15.0,
            )
            # Sudden battery drop
            for i in range(1, len(tel)):
                drop = tel[i - 1].battery_pct - tel[i].battery_pct
                if drop > 5.0:
                    events.append(
                        AnomalyEvent(
                            event_type=AnomalyType.BATTERY_SPIKE,
                            severity="high" if drop <= 10 else "critical",
                            timestamp_s=tel[i].timestamp_s,
                            run_id=run.run_id,
                            description=f"Sudden battery drop: {drop:.1f}% in one step",
                            value=drop,
                            threshold=5.0,
                            probable_cause="ESC over-current or battery cell failure",
                        )
                    )
            # GPS loss
            loss_start: Optional[int] = None
            for i, t in enumerate(tel):
                if t.gps_fix < 3 and loss_start is None:
                    loss_start = i
                elif t.gps_fix >= 3 and loss_start is not None:
                    dur = tel[i].timestamp_s - tel[loss_start].timestamp_s
                    if dur > 1.0:
                        events.append(
                            AnomalyEvent(
                                event_type=AnomalyType.GPS_LOSS,
                                severity="critical" if dur > 5 else "high",
                                timestamp_s=tel[loss_start].timestamp_s,
                                run_id=run.run_id,
                                description=f"GPS fix lost for {dur:.1f}s",
                                value=dur,
                                threshold=1.0,
                                probable_cause="RF interference or antenna obstruction",
                            )
                        )
                    loss_start = None
            # Near-miss
            for i in range(1, len(tel)):
                drop = tel[i - 1].alt_m - tel[i].alt_m
                if drop > 5.0 and tel[i].alt_m < 10.0:
                    events.append(
                        AnomalyEvent(
                            event_type=AnomalyType.NEAR_MISS,
                            severity="critical",
                            timestamp_s=tel[i].timestamp_s,
                            run_id=run.run_id,
                            description=(
                                f"Near-miss: altitude dropped {drop:.1f}m to {tel[i].alt_m:.1f}m"
                            ),
                            value=tel[i].alt_m,
                            threshold=10.0,
                            probable_cause="Altitude controller failure or downwash",
                        )
                    )
            # Attitude upset
            for t in tel:
                if abs(t.roll_deg) > 45 or abs(t.pitch_deg) > 45:
                    events.append(
                        AnomalyEvent(
                            event_type=AnomalyType.ATTITUDE_UPSET,
                            severity="critical" if abs(t.roll_deg) > 60 else "high",
                            timestamp_s=t.timestamp_s,
                            run_id=run.run_id,
                            description=(
                                f"Attitude upset: roll {t.roll_deg:.1f}° pitch {t.pitch_deg:.1f}°"
                            ),
                            value=max(abs(t.roll_deg), abs(t.pitch_deg)),
                            threshold=45.0,
                            probable_cause="Motor failure or wind gust",
                        )
                    )

        critical = [e for e in events if e.severity == "critical"]
        high = [e for e in events if e.severity == "high"]
        near_miss = [e for e in events if e.event_type == AnomalyType.NEAR_MISS]
        gps_evs = [e for e in events if e.event_type == AnomalyType.GPS_LOSS]
        gps_dur = sum(e.value for e in gps_evs)

        # Failure cause synthesis
        causes = list({e.probable_cause for e in events if e.probable_cause})

        penalty = len(critical) * 15 + len(high) * 5 + len(events)
        score = max(0.0, 100.0 - penalty)

        findings: list[str] = []
        if critical:
            findings.append(f"{len(critical)} critical event(s) detected")
        if near_miss:
            findings.append(f"{len(near_miss)} near-miss event(s) — review altitude hold")
        if gps_dur > 0:
            findings.append(f"Total GPS loss: {gps_dur:.1f}s")
        if causes:
            findings.append(f"Probable causes: {'; '.join(causes[:3])}")
        if not events:
            findings.append("No anomalies detected.")
        elif not findings:
            findings.append(f"{len(events)} minor anomaly event(s).")

        return AnomalyReport(
            total_anomalies=len(events),
            critical_count=len(critical),
            high_count=len(high),
            events=events[:50],
            z_score_threshold=Z_THRESHOLD,
            near_miss_count=len(near_miss),
            gps_loss_duration_s=round(gps_dur, 1),
            identified_failure_causes=causes[:5],
            findings=findings,
            score=round(score, 1),
        )

    # ══════════════════════════════════════════════════════════════════════════
    # RECOMMENDATIONS
    # ══════════════════════════════════════════════════════════════════════════

    async def generate_recommendations(self, analyses: list[Any]) -> list[Recommendation]:
        """Cross-analysis synthesis → prioritised Recommendation list."""
        all_findings: list[str] = []
        scores: dict[str, float] = {}
        for a in analyses:
            if hasattr(a, "findings"):
                all_findings.extend(a.findings)
            if hasattr(a, "score"):
                scores[type(a).__name__] = a.score

        prompt = (
            "You are a drone engineering advisor.\n"
            "Analysis findings:\n"
            + "\n".join(f"  - {f}" for f in all_findings[:15])
            + f"\n\nSubsystem scores: {json.dumps(scores, indent=2)}\n\n"
            "Generate 4-6 actionable recommendations targeting: "
            "code, design, mission, constraints, or hardware.\n"
            "Respond ONLY with valid JSON:\n"
            '{"recommendations": [{'
            '"target": "code|design|mission|constraints|hardware",'
            '"priority": "critical|high|medium|low",'
            '"title": "...", "description": "...",'
            '"expected_improvement": "..."}]}'
        )

        recommendations: list[Recommendation] = []
        try:
            raw = await self.llm.ainvoke(prompt)
            data = _parse_json(raw)
            for r in data.get("recommendations", [])[:8]:
                try:
                    recommendations.append(
                        Recommendation(
                            target=RecommendationTarget(r.get("target", "code")),
                            priority=r.get("priority", "medium"),
                            title=r.get("title", ""),
                            description=r.get("description", ""),
                            expected_improvement=r.get("expected_improvement", ""),
                        )
                    )
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("recommendations_llm_failed: %s", str(exc))

        if not recommendations:
            recommendations = _rule_based_recommendations(analyses)

        return recommendations

    # ══════════════════════════════════════════════════════════════════════════
    # REPORT COMPILATION
    # ══════════════════════════════════════════════════════════════════════════

    async def compile_report(self, project_id: str) -> FullMonitorReport:
        """
        Compile FullMonitorReport from session state (self._analyses).

        Takes only project_id — all data is in self._analyses and self._logs.
        """
        logs = self._logs or FlightLogs(project_id=project_id)

        # Extract typed analyses from session dict
        def _get(key: str, cls: type):
            data = self._analyses.get(key)
            if not data:
                return None
            try:
                return cls(**data)
            except Exception:
                return None

        trajectory = _get("trajectory", TrajectoryAnalysis)
        battery = _get("battery", BatteryAnalysis)
        motors = _get("motors", MotorAnalysis)
        stability = _get("stability", FlightStabilityAnalysis)
        gps = _get("gps", GPSAccuracyAnalysis)
        anomalies = _get("anomaly", AnomalyReport)

        # Weighted composite score
        # trajectory×0.25, battery×0.20, stability×0.20,
        # anomaly×0.20, gps×0.10, motors×0.05
        t_s = trajectory.score if trajectory else 75.0
        b_s = battery.score if battery else 75.0
        m_s = motors.score if motors else 75.0
        st_s = stability.score if stability else 75.0
        g_s = gps.score if gps else 75.0
        a_s = anomalies.score if anomalies else 75.0

        overall = t_s * 0.25 + b_s * 0.20 + st_s * 0.20 + a_s * 0.20 + g_s * 0.10 + m_s * 0.05

        scores = PerformanceScore(
            trajectory=round(t_s, 1),
            battery=round(b_s, 1),
            motors=round(m_s, 1),
            stability=round(st_s, 1),
            gps=round(g_s, 1),
            anomaly=round(a_s, 1),
            overall=round(overall, 1),
            grade=_score_to_grade(overall),  # type: ignore[arg-type]
        )

        # Cross-analysis recommendations (stored from generate_recommendations)
        typed_analyses = _extract_typed_analyses(self._analyses)
        recommendations = await self.generate_recommendations(typed_analyses)

        executive_summary = await self._executive_summary(logs, scores, recommendations)

        from utils.visualization_helpers import generate_all_viz_data

        viz_data = generate_all_viz_data(
            trajectory=trajectory,
            battery=battery,
            motors=motors,
            stability=stability,
            gps=gps,
            anomalies=anomalies,
            scores=scores,
        )

        return FullMonitorReport(
            project_id=project_id,
            mission_description=logs.mission_description,
            trajectory=trajectory,
            battery=battery,
            motors=motors,
            stability=stability,
            gps=gps,
            anomalies=anomalies,
            analysis_plan=None,
            scores=scores,
            recommendations=recommendations,
            executive_summary=executive_summary,
            viz_data=viz_data,
        )

    async def _executive_summary(
        self,
        logs: FlightLogs,
        scores: PerformanceScore,
        recommendations: list[Recommendation],
    ) -> str:
        crit_n = sum(1 for r in recommendations if r.priority == "critical")
        prompt = (
            "2-3 sentence executive summary for this drone mission performance report.\n"
            f"Mission: {logs.mission_description[:200]}\n"
            f"Grade: {scores.grade} ({scores.overall:.0f}/100)\n"
            f"Trajectory:{scores.trajectory:.0f}  Battery:{scores.battery:.0f}  "
            f"Stability:{scores.stability:.0f}  GPS:{scores.gps:.0f}  "
            f"Anomaly:{scores.anomaly:.0f}  Motors:{scores.motors:.0f}\n"
            f"Critical issues: {crit_n}\n"
            "Be concise and professional."
        )
        try:
            return (await self.llm.ainvoke(prompt)).strip()[:500]
        except Exception:
            gw = {
                "A": "excellent",
                "B": "good",
                "C": "satisfactory",
                "D": "marginal",
                "F": "failing",
            }.get(scores.grade, "")
            return (
                f"Mission performance is {gw} — Grade {scores.grade} "
                f"({scores.overall:.0f}/100). "
                f"Trajectory {scores.trajectory:.0f}, battery {scores.battery:.0f}, "
                f"stability {scores.stability:.0f}, GPS {scores.gps:.0f}, "
                f"anomaly {scores.anomaly:.0f}. "
                + (
                    f"{crit_n} critical issue(s) require attention."
                    if crit_n
                    else "No critical issues."
                )
            )


# ══════════════════════════════════════════════════════════════════════════════
# Module-level helpers
# ══════════════════════════════════════════════════════════════════════════════


def _parse_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    try:
        return ast.literal_eval(text)
    except Exception:
        return {}


def _zscore_anomalies(
    values: list[float],
    tel: list[TelemetryPoint],
    run_id: str,
    field: str,
    anomaly_type: AnomalyType,
    threshold: float = Z_THRESHOLD,
    threshold_override: Optional[float] = None,
) -> list[AnomalyEvent]:
    if len(values) < 3:
        return []
    mean = statistics.mean(values)
    try:
        std = statistics.stdev(values)
    except Exception:
        return []
    if std < 0.01:
        return []

    events: list[AnomalyEvent] = []
    for i, v in enumerate(values):
        z = abs(v - mean) / std
        if z <= threshold:
            continue
        if threshold_override is not None and v <= threshold_override:
            continue
        events.append(
            AnomalyEvent(
                event_type=anomaly_type,
                severity="critical" if z > threshold * 1.5 else "high",
                timestamp_s=tel[i].timestamp_s if i < len(tel) else float(i),
                run_id=run_id,
                description=f"Anomalous {field}: {v:.2f} (z={z:.1f}σ, mean={mean:.2f})",
                value=v,
                threshold=mean + threshold * std,
            )
        )
    return events


def _extract_typed_analyses(analyses_dict: dict[str, Any]) -> list[Any]:
    """Convert session analyses dict to typed objects for recommendations."""
    from agents.performance_models import (
        TrajectoryAnalysis,
        BatteryAnalysis,
        MotorAnalysis,
        FlightStabilityAnalysis,
        GPSAccuracyAnalysis,
        AnomalyReport,
    )

    mapping = {
        "trajectory": TrajectoryAnalysis,
        "battery": BatteryAnalysis,
        "motors": MotorAnalysis,
        "stability": FlightStabilityAnalysis,
        "gps": GPSAccuracyAnalysis,
        "anomaly": AnomalyReport,
    }
    result = []
    for key, cls in mapping.items():
        data = analyses_dict.get(key)
        if data:
            try:
                result.append(cls(**data))
            except Exception:
                pass
    return result


def _rule_based_recommendations(analyses: list[Any]) -> list[Recommendation]:
    """Deterministic fallback recommendations from analysis results."""
    recs: list[Recommendation] = []

    for a in analyses:
        from agents.performance_models import (
            TrajectoryAnalysis,
            BatteryAnalysis,
            FlightStabilityAnalysis,
            GPSAccuracyAnalysis,
            AnomalyReport,
        )

        if isinstance(a, TrajectoryAnalysis):
            if a.mean_cross_track_error_m > 5:
                recs.append(
                    Recommendation(
                        target="code",
                        priority="high",
                        title="Improve Path-Following Controller",
                        description=(
                            f"Cross-track error {a.mean_cross_track_error_m:.1f}m > 5m. "
                            "Tune lateral PID or implement pure-pursuit/NLGL controller."
                        ),
                        expected_improvement="Reduce CTE to <2 m",
                    )
                )
            if a.avg_path_efficiency < 0.7:
                recs.append(
                    Recommendation(
                        target="mission",
                        priority="medium",
                        title="Optimise Waypoint Routing",
                        description=("Path efficiency below 70%. Apply RRT*/A* path planning."),
                        expected_improvement="Efficiency >85%",
                    )
                )

        elif isinstance(a, BatteryAnalysis):
            if a.avg_battery_remaining_pct < 25:
                recs.append(
                    Recommendation(
                        target="constraints",
                        priority="critical",
                        title="Add RTL Battery Trigger",
                        description=(
                            f"Only {a.avg_battery_remaining_pct:.0f}% remaining. "
                            "Add RTL trigger at 25%."
                        ),
                        expected_improvement="Prevent battery crashes",
                        code_snippet=(
                            "if remaining_pct < 0.25:\n    await drone.action().return_to_launch()"
                        ),
                    )
                )

        elif isinstance(a, FlightStabilityAnalysis):
            if a.vibration_grade in ("high", "critical"):
                recs.append(
                    Recommendation(
                        target="hardware",
                        priority="high",
                        title="Reduce Airframe Vibration",
                        description=(
                            f"Vibration grade {a.vibration_grade} "
                            f"(avg {a.avg_vibration_index:.1f}). "
                            "Balance propellers or add damping mounts."
                        ),
                        expected_improvement="Reduce to 'low' grade",
                    )
                )
            if a.max_roll_deg > 15 or a.max_pitch_deg > 15:
                recs.append(
                    Recommendation(
                        target="code",
                        priority="high",
                        title="Tune Attitude Controller",
                        description=(
                            f"Excessive attitude excursions: roll {a.max_roll_deg:.1f}°, "
                            f"pitch {a.max_pitch_deg:.1f}°. Reduce attitude PID gains."
                        ),
                        expected_improvement="Keep roll/pitch < 10° in cruise",
                    )
                )

        elif isinstance(a, GPSAccuracyAnalysis):
            if a.avg_hdop > 2.0:
                recs.append(
                    Recommendation(
                        target="hardware",
                        priority="medium",
                        title="Improve GPS Antenna Placement",
                        description=(
                            f"Average HDOP {a.avg_hdop:.2f} > 2.0. "
                            "Relocate GPS antenna away from EMI sources."
                        ),
                        expected_improvement="HDOP < 1.5",
                    )
                )
            if a.gps_fix_loss_count > 0:
                recs.append(
                    Recommendation(
                        target="hardware",
                        priority="high",
                        title="Add Optical Flow Fallback",
                        description=(
                            f"{a.gps_fix_loss_count} GPS loss event(s) detected. "
                            "Add optical-flow sensor for GPS-denied navigation."
                        ),
                        expected_improvement="Zero position loss events",
                    )
                )

        elif isinstance(a, AnomalyReport):
            if a.near_miss_count > 0:
                recs.append(
                    Recommendation(
                        target="code",
                        priority="critical",
                        title="Fix Altitude Hold Logic",
                        description=(
                            f"{a.near_miss_count} near-miss event(s). "
                            "Review altitude controller and enforce min safe altitude."
                        ),
                        expected_improvement="Eliminate near-miss events",
                    )
                )
            if a.identified_failure_causes:
                recs.append(
                    Recommendation(
                        target="design",
                        priority="medium",
                        title="Address Identified Failure Causes",
                        description=(
                            "Probable causes: " + "; ".join(a.identified_failure_causes[:3])
                        ),
                        expected_improvement="Reduce anomaly count to zero",
                    )
                )

    if not recs:
        recs.append(
            Recommendation(
                target="code",
                priority="low",
                title="Performance Satisfactory",
                description="No critical issues. Continue with current configuration.",
                expected_improvement="Maintain current performance",
            )
        )
    return recs
