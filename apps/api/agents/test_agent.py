"""eljazari.ai — Simulation & Test Agent (Closed-Loop)
Full closed-loop pipeline:
    Code → Gazebo SITL launch → Telemetry collection
    → Semantic observations → LLM evaluation
    → Feedback generation → Code refinement → Repeat (max 5×)

Reference: Closed-Loop UAV (arxiv:2507.01930) — 85% success, 98.5% completeness
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Coroutine

from langchain_ollama import OllamaLLM
from pydantic import BaseModel, Field

from config.settings import settings
from pipelines.semantic_converter import (
    SemanticConverter,
    SemanticObservation,
    TelemetryData,
)
from services.gazebo_service import GazeboService
from services.px4_service import PX4Service

logger = logging.getLogger(__name__)
LLM_TIMEOUT_S = 20
MISSION_TIMEOUT_MIN_S = 60.0
MISSION_TIMEOUT_HEADROOM_S = 60.0
MISSION_TIMEOUT_MAX_S = 600.0


def encode_observation_text(text: str) -> str:
    """Keep workflow state ASCII-safe on Windows consoles."""
    try:
        return text.encode("unicode_escape").decode("ascii")
    except Exception:
        return text


# ─────────────────────────────────────────────────────────────────────────────
# Output Pydantic models
# ─────────────────────────────────────────────────────────────────────────────


class EvaluationResult(BaseModel):
    """LLM-based evaluation of one simulation run."""

    passed: bool
    score: float = Field(default=0.0, ge=0.0, le=100.0)
    completeness: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    errors: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    raw_llm_response: str = ""


class CodeFeedback(BaseModel):
    """Structured feedback for the code generator on next iteration."""

    iteration: int
    error_type: str = "unknown"  # path | speed | altitude | battery | mission_logic
    description: str = ""
    specific_fixes: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class SimulationResult(BaseModel):
    """Result of one simulation iteration."""

    sim_id: str
    iteration: int
    passed: bool
    telemetry: TelemetryData
    observations: list[SemanticObservation]
    evaluation: EvaluationResult
    duration_s: float
    acceptance: dict[str, Any] | None = None
    feedback: CodeFeedback | None = None
    # Generated aircraft only: what the simulator attested it actually loaded.
    model_attestation: dict[str, Any] | None = None
    # The mission code that flew; the gate compares the flight with its mission.
    code: str | None = None


class ClosedLoopResult(BaseModel):
    """Aggregate result of the full closed-loop pipeline."""

    total_iterations: int
    final_passed: bool
    final_score: float
    best_iteration: int
    iteration_results: list[SimulationResult]
    final_feedback: CodeFeedback | None = None
    convergence_reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Evaluator LLM prompt
# ─────────────────────────────────────────────────────────────────────────────

_EVAL_SYSTEM = """\
You are a drone-simulation evaluation system. Your task is to analyze flight observations and assess how successful the mission was.

## How to evaluate:
- Compare the semantic observations with the original mission description step by step
- Determine whether each mission phase was executed (takeoff, transit, return, landing)
- Detect any deviation from the required path or altitude
- Note safety violations: altitude overshoot, battery depletion, geofence breach

## Response format (JSON only — do not add any text outside the JSON):
```json
{
  "passed": true,
  "score": 85,
  "completeness": 0.85,
  "reason": "Mission completed successfully with a slight path deviation",
  "errors": ["Altitude was 12m instead of 10m"],
  "suggestions": ["Adjust altitude_m=10.0 in takeoff()"]
}
```"""


# ─────────────────────────────────────────────────────────────────────────────
# SimulationTestAgent
# ─────────────────────────────────────────────────────────────────────────────


class SimulationTestAgent:
    """
    Closed-loop simulation and test agent.

    Pipeline per iteration:
        code_str → GazeboService.launch_world()
               → PX4Service.execute_mission()    [subprocess + MAVSDK]
               → SemanticConverter.convert()     [numbers → English text]
               → LLM evaluate_mission()          [Evaluator LLM]
               → generate_feedback()             [classify + suggest fixes]
               → code_generator_fn(feedback)     [refine code]
               → repeat

    Terminates when:
        - evaluation.passed == True
        - max_iterations reached
        - code_generator_fn is None (single-shot mode)
        - code generator raises an exception
    """

    def __init__(self) -> None:
        self.eval_llm = OllamaLLM(
            base_url=settings.ollama_base_url,
            model=settings.ollama_eval_model,
            temperature=0.0,  # deterministic evaluation
            client_kwargs={"timeout": LLM_TIMEOUT_S},
        )
        self.gazebo = GazeboService()
        self.px4 = PX4Service()
        self.converter = SemanticConverter(sample_every=5)

    # ── Step 1: Run one simulation ────────────────────────────────────────────

    async def run_simulation(
        self,
        code_str: str,
        drone_config: dict[str, Any],
        iteration: int = 1,
        mission_description: str = "",
        on_point: Callable[[dict], None] | None = None,
        expected_code_sha256: str | None = None,
    ) -> SimulationResult:
        """
        Launch Gazebo + SITL, execute code, collect telemetry, convert semantics.
        Returns SimulationResult (evaluation field is a placeholder — call
        evaluate_mission() separately for the LLM assessment).
        """
        start = time.monotonic()
        sim_id = await self.gazebo.launch_world(drone_config)
        attestation = None

        try:
            attestation = self.gazebo.get_model_attestation(sim_id)
            if not isinstance(attestation, dict):
                attestation = None
            port = self.gazebo.get_mavlink_port(sim_id)
            mavsdk_address = self.gazebo.get_mavsdk_address(sim_id)
            telemetry = await self.px4.execute_mission(
                code=code_str,
                sim_id=sim_id,
                mavlink_port=port,
                mavsdk_address=mavsdk_address,
                timeout_s=self._mission_timeout_s(code_str, mission_description),
                on_point=on_point,
                expected_code_sha256=expected_code_sha256,
            )
        finally:
            await self.gazebo.stop_world(sim_id)

        observations = self.converter.convert(telemetry)
        duration = time.monotonic() - start

        # Placeholder evaluation — replaced by LLM call in run_closed_loop()
        placeholder_eval = EvaluationResult(
            passed=False,
            score=0.0,
            completeness=0.5,
            reason="Pending LLM evaluation",
        )

        return SimulationResult(
            sim_id=sim_id,
            iteration=iteration,
            passed=False,
            telemetry=telemetry,
            observations=observations,
            evaluation=placeholder_eval,
            duration_s=round(duration, 2),
            model_attestation=attestation,
            code=code_str,
        )

    # ── Step 1b: Simplified single-run for graph node ─────────────────────────

    async def run_single(
        self,
        code: str,
        urdf_path: str = "",
        mission_description: str = "",
        iteration: int = 1,
        on_point: Callable[[dict], None] | None = None,
        drone_config: dict[str, Any] | None = None,
        expected_code_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Run one simulation iteration and return a plain dict for the graph node."""
        drone_config = dict(drone_config or {})
        if urdf_path:
            drone_config["urdf_path"] = urdf_path
        sim = await self.run_simulation(
            code,
            drone_config,
            iteration,
            mission_description,
            on_point=on_point,
            expected_code_sha256=expected_code_sha256,
        )
        telemetry_points = []
        for point in getattr(sim.telemetry, "points", []) or []:
            raw_point = point.model_dump() if hasattr(point, "model_dump") else vars(point)
            if "battery_percent" in raw_point and "battery_pct" not in raw_point:
                raw_point["battery_pct"] = raw_point["battery_percent"]
            telemetry_points.append(raw_point)
        return {
            "telemetry": telemetry_points,
            "semantic_observations": [
                encode_observation_text(getattr(o, "description", str(o)))
                for o in (sim.observations or [])
            ],
            "duration_s": sim.duration_s,
            "passed": sim.passed,
            "mission_started": bool(getattr(sim.telemetry, "mission_started", False)),
            "mission_completed": bool(getattr(sim.telemetry, "mission_completed", False)),
            "crash_detected": bool(getattr(sim.telemetry, "crash_detected", False)),
            "geofence_violation": bool(getattr(sim.telemetry, "geofence_violation", False)),
            "runtime_mode": str(getattr(sim.telemetry, "runtime_mode", "unknown") or "unknown"),
            "telemetry_source": str(
                getattr(sim.telemetry, "telemetry_source", "unknown") or "unknown"
            ),
            "mission_error": str(getattr(sim.telemetry, "mission_error", "") or ""),
            "warnings": list(getattr(sim.telemetry, "warnings", []) or []),
            "artifacts": dict(getattr(sim.telemetry, "artifacts", {}) or {}),
            "model_attestation": sim.model_attestation,
            "mission_isolation": str(getattr(sim.telemetry, "mission_isolation", "unknown")),
        }

    # ── Step 1c: Simplified evaluate for graph node ───────────────────────────

    async def evaluate(
        self,
        observations: list[Any],
        mission_description: str = "",
    ) -> dict[str, Any]:
        """Evaluate observations against mission description and return a plain dict."""
        try:
            obs_text = "\n".join(f"- {o}" for o in observations[-30:])
            prompt = (
                f"{_EVAL_SYSTEM}\n\n"
                f"Mission:\n{mission_description}\n\n"
                f"Observations ({len(observations)}):\n{obs_text}\n\n"
                "Evaluation:"
            )
            raw = await asyncio.wait_for(self.eval_llm.ainvoke(prompt), timeout=LLM_TIMEOUT_S)
            result = self._parse_evaluation(raw)
            if result.score <= 0.0 and not result.reason.strip():
                result = self._rule_based_evaluation([str(o) for o in observations])
        except Exception:
            result = self._rule_based_evaluation([str(o) for o in observations])
        return {
            "passed": result.passed,
            "score": result.score,
            "feedback": result.reason,
        }

    async def collect_telemetry(self, sim_id: str) -> TelemetryData:
        """Collect telemetry for an already-running simulation."""
        port = self.gazebo.get_mavlink_port(sim_id)
        mavsdk_address = self.gazebo.get_mavsdk_address(sim_id)
        return await self.px4._collect_telemetry_stream(
            sim_id,
            port,
            60.0,
            mavsdk_address=mavsdk_address,
        )

    # ── Step 3: Semantic conversion ───────────────────────────────────────────

    async def generate_semantic_observations(
        self, telemetry: TelemetryData
    ) -> list[SemanticObservation]:
        """Convert numeric TelemetryData to English SemanticObservations."""
        return self.converter.convert(telemetry)

    # ── Step 4: LLM evaluation ────────────────────────────────────────────────

    async def evaluate_mission(
        self,
        observations: list[SemanticObservation],
        original_prompt: str,
    ) -> EvaluationResult:
        """
        Use Evaluator LLM to judge whether the drone completed the mission.
        Parses structured JSON from the response.
        """
        obs_text = "\n".join(f"- {o.description}" for o in observations[-30:])
        prompt = (
            f"{_EVAL_SYSTEM}\n\n"
            f"Mission:\n{original_prompt}\n\n"
            f"Observations ({len(observations)}):\n{obs_text}\n\n"
            "Evaluation:"
        )

        try:
            raw = await asyncio.wait_for(self.eval_llm.ainvoke(prompt), timeout=LLM_TIMEOUT_S)
            result = self._parse_evaluation(raw)
            if result.score <= 0.0 and not result.reason.strip():
                return self._rule_based_evaluation([o.description for o in observations])
            return result
        except Exception as exc:
            logger.error("LLM evaluation failed", extra={"error": str(exc)})
            fallback = self._rule_based_evaluation([o.description for o in observations])
            if fallback.passed or fallback.score > 0.0:
                return fallback
            return EvaluationResult(
                passed=False,
                score=0.0,
                reason=f"Evaluation failed: {exc}",
                errors=["Evaluator unavailable"],
                raw_llm_response=str(exc),
            )

    async def generate_feedback(
        self,
        evaluation: EvaluationResult,
        iteration: int,
    ) -> CodeFeedback:
        """
        Convert EvaluationResult → CodeFeedback with specific code-level fixes.
        """
        error_type = self._classify_error_type(evaluation.errors)
        fixes = self._build_fix_suggestions(evaluation.errors, evaluation.suggestions)
        confidence = round(min(0.95, 0.4 + (evaluation.score / 100.0) * 0.55), 3)

        description_parts = [evaluation.reason]
        if evaluation.errors:
            description_parts.append("Errors: " + "; ".join(evaluation.errors[:3]))

        return CodeFeedback(
            iteration=iteration,
            error_type=error_type,
            description="\n".join(description_parts),
            specific_fixes=fixes,
            confidence=confidence,
        )

    # ── Full closed-loop ──────────────────────────────────────────────────────

    async def evaluate_simulation_result(
        self, result: SimulationResult, mission_description: str
    ) -> EvaluationResult:
        """Gate a collected run before performing semantic-only evaluation."""
        from pipelines.simulation_acceptance import assess_simulation

        data = result.telemetry.model_dump()
        data["telemetry_data"] = data.pop("points")
        data["simulation_status"] = "complete"
        data["simulation_model_attestation"] = result.model_attestation
        data["generated_code"] = result.code
        result.acceptance = assess_simulation(data)
        if not result.acceptance["passed"]:
            return EvaluationResult(
                passed=False,
                score=0,
                completeness=0,
                reason="Rejected by basic simulation checks",
                errors=result.acceptance["reasons"],
            )
        return await self.evaluate_mission(result.observations, mission_description)

    async def run_closed_loop(
        self,
        initial_code_str: str,
        drone_config: dict[str, Any],
        mission_description: str,
        code_generator_fn: Callable[..., Coroutine[Any, Any, str]] | None = None,
        max_iterations: int = 5,
    ) -> ClosedLoopResult:
        """
        Full closed-loop pipeline.

        Args:
            initial_code_str:    First code version to test.
            drone_config:        Drone hardware configuration dict.
            mission_description: Original user mission text (used for evaluation).
            code_generator_fn:   async (mission: str, feedback: str) -> str
                                 If None, stops after the first failed iteration.
            max_iterations:      Hard cap (also capped by settings).

        Returns:
            ClosedLoopResult summarising all iterations.
        """
        max_iter = min(max_iterations, settings.max_simulation_iterations)
        current_code = initial_code_str
        results: list[SimulationResult] = []
        best_score = -1.0
        best_iter = 1
        feedback: CodeFeedback | None = None
        convergence = f"max iterations limit ({max_iter})"

        for iteration in range(1, max_iter + 1):
            logger.info(
                f"Closed-loop iteration {iteration}/{max_iter}",
                extra={"mission": mission_description[:60]},
            )

            # ── Step A: Simulate ──────────────────────────────────────────
            sim_result = await self.run_simulation(current_code, drone_config, iteration)

            # ── Step B: Evaluate ──────────────────────────────────────────
            evaluation = await self.evaluate_simulation_result(sim_result, mission_description)
            sim_result.evaluation = evaluation
            sim_result.passed = evaluation.passed

            if evaluation.score > best_score:
                best_score = evaluation.score
                best_iter = iteration

            results.append(sim_result)
            logger.info(
                "Iteration complete",
                extra={
                    "iteration": iteration,
                    "passed": evaluation.passed,
                    "score": evaluation.score,
                },
            )

            # ── Step C: Early exit on success ─────────────────────────────
            if evaluation.passed:
                convergence = f"mission success at iteration {iteration}"
                break

            if (
                sim_result.acceptance
                and not sim_result.acceptance["passed"]
                and not sim_result.acceptance["retryable_with_code_change"]
            ):
                convergence = "Simulation infrastructure or telemetry failed basic checks"
                break

            # ── Step D: Generate feedback ─────────────────────────────────
            feedback = await self.generate_feedback(evaluation, iteration)
            sim_result.feedback = feedback

            if code_generator_fn is None:
                convergence = "single-iteration mode — no code generator"
                break

            # ── Step E: Regenerate code ───────────────────────────────────
            if iteration < max_iter:
                feedback_text = self._feedback_to_prompt(feedback)
                try:
                    current_code = await code_generator_fn(mission_description, feedback_text)
                    logger.info(f"Code regenerated for iteration {iteration + 1}")
                except Exception as exc:
                    logger.error("Code generation failed", extra={"error": str(exc)})
                    convergence = f"code generation failed at iteration {iteration}: {exc}"
                    break

        final = results[-1]
        return ClosedLoopResult(
            total_iterations=len(results),
            final_passed=final.evaluation.passed,
            final_score=final.evaluation.score,
            best_iteration=best_iter,
            iteration_results=results,
            final_feedback=feedback,
            convergence_reason=convergence,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _parse_evaluation(self, raw: str) -> EvaluationResult:
        """Extract and parse the JSON block from an LLM evaluation response."""
        # Try ```json … ``` block first
        m = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        if m:
            json_str = m.group(1)
        else:
            # Fallback: find first { … } in response
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            json_str = m.group(0) if m else ""

        if not json_str:
            logger.warning("No JSON found in evaluator response")
            passed = any(w in raw for w in ["passed", "completed", "true"])
            return EvaluationResult(
                passed=passed,
                score=70.0 if passed else 30.0,
                reason=raw[:200],
                raw_llm_response=raw,
            )

        try:
            data = json.loads(json_str)
            return EvaluationResult(
                passed=bool(data.get("passed", False)),
                score=float(data.get("score", 0)),
                completeness=float(data.get("completeness", 0)),
                reason=str(data.get("reason", "")),
                errors=list(data.get("errors", [])),
                suggestions=list(data.get("suggestions", [])),
                raw_llm_response=raw,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Evaluation JSON parse error", extra={"error": str(exc)})
            passed = any(w in raw for w in ["passed", "completed", "true"])
            return EvaluationResult(
                passed=passed,
                score=65.0 if passed else 25.0,
                reason=raw[:200],
                raw_llm_response=raw,
            )

    @staticmethod
    def _classify_error_type(errors: list[str]) -> str:
        """Map evaluation error strings to a canonical error_type."""
        text = " ".join(errors).lower()
        if any(w in text for w in ["altitude", "alt"]):
            return "altitude"
        if any(w in text for w in ["speed", "velocity"]):
            return "speed"
        if any(w in text for w in ["path", "waypoint", "position"]):
            return "path"
        if any(w in text for w in ["battery"]):
            return "battery"
        if any(w in text for w in ["geofence"]):
            return "geofence"
        if any(w in text for w in ["landing", "takeoff"]):
            return "mission_logic"
        return "unknown"

    @staticmethod
    def _build_fix_suggestions(errors: list[str], suggestions: list[str]) -> list[str]:
        """Combine LLM suggestions with rule-based code fixes."""
        fixes: list[str] = list(suggestions[:3])

        for error in errors:
            err = error.lower()
            if "altitude" in err:
                fixes.append(
                    "Check the altitude_m value in takeoff() and ensure it matches the mission"
                )
            elif "path" in err or "waypoint" in err or "position" in err:
                fixes.append(
                    "Review the fly_to_relative() coordinates — check the north_m/east_m direction"
                )
            elif "speed" in err:
                fixes.append("Lower the speed_ms value below the maximum allowed limit")
            elif "battery" in err:
                fixes.append(
                    "Add a battery check before each waypoint and add return logic at < 30%"
                )
            elif "landing" in err:
                fixes.append("Ensure drone.land() is present at the end and in every except block")

        # Deduplicate while preserving order, cap at 5
        seen: set[str] = set()
        unique: list[str] = []
        for f in fixes:
            if f not in seen:
                seen.add(f)
                unique.append(f)
            if len(unique) == 5:
                break
        return unique

    @staticmethod
    def _feedback_to_prompt(feedback: CodeFeedback) -> str:
        """Format CodeFeedback as the feedback string for the code generator."""
        fixes_text = "\n".join(f"  - {f}" for f in feedback.specific_fixes)
        return (
            f"## Simulation results (iteration {feedback.iteration}):\n"
            f"Error type: {feedback.error_type}\n"
            f"{feedback.description}\n\n"
            f"## Required code changes:\n{fixes_text}"
        )

    # ─────────────────────────────────────────────────────────────────────────────
    # Backwards-compatible wrapper (consumed by LangGraph nodes)
    # ─────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _rule_based_evaluation(observations: list[str]) -> EvaluationResult:
        text = "\n".join(observations).lower()
        success_markers = [
            "mission completed",
            "completed successfully",
            "landed",
        ]
        failure_markers = [
            "mission not completed",
            "warning:",
            "danger:",
            "crash",
            "violation",
            "failed",
        ]
        passed = any(marker in text for marker in success_markers) and not any(
            marker in text for marker in failure_markers
        )
        if passed:
            return EvaluationResult(
                passed=True,
                score=85.0,
                completeness=0.85,
                reason="Rule-based evaluation: mission completed successfully.",
                suggestions=[],
            )
        return EvaluationResult(
            passed=False,
            score=35.0 if observations else 0.0,
            completeness=0.35 if observations else 0.0,
            reason="Rule-based evaluation: mission completion could not be confirmed.",
            errors=["Mission completion signal not detected"],
            suggestions=["Review waypoint flow and mission completion conditions."],
        )

    @staticmethod
    def _mission_timeout_s(code: str, mission_description: str = "") -> float:
        """Derive a realistic timeout from generated mission metadata or prompt text."""
        candidates: list[float] = []

        target_match = re.search(
            r"MISSION_TARGET_DURATION_S\s*=\s*([0-9]+(?:\.[0-9]+)?)",
            code or "",
        )
        if target_match:
            candidates.append(float(target_match.group(1)))

        for match in re.finditer(
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:minute|minutes|min)\b",
            mission_description or "",
            flags=re.IGNORECASE,
        ):
            candidates.append(float(match.group(1)) * 60.0)

        for match in re.finditer(
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:second|seconds|sec)\b",
            mission_description or "",
            flags=re.IGNORECASE,
        ):
            candidates.append(float(match.group(1)))

        requested = max(candidates, default=float(settings.max_altitude_m) * 2.0)
        with_headroom = requested + MISSION_TIMEOUT_HEADROOM_S
        return min(max(with_headroom, MISSION_TIMEOUT_MIN_S), MISSION_TIMEOUT_MAX_S)


class TestAgent:
    """Thin wrapper around SimulationTestAgent for LangGraph node compatibility."""

    __test__ = False

    def __init__(self) -> None:
        self._agent = SimulationTestAgent()

    async def run(
        self,
        code: str,
        mission_description: str,
        drone_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute code in simulation, evaluate semantically, return dict."""
        logger.info("TestAgent.run — starting simulation")
        sim_result = await self._agent.run_simulation(code, drone_config)
        evaluation = await self._agent.evaluate_simulation_result(sim_result, mission_description)
        return {
            "telemetry": sim_result.telemetry.model_dump(),
            "observations": [o.model_dump() for o in sim_result.observations],
            "evaluation": evaluation.model_dump(),
            "acceptance": sim_result.acceptance,
            "passed": evaluation.passed,
            "score": evaluation.score,
        }
