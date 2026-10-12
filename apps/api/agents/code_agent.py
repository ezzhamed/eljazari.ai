"""eljazari.ai code generation agent.

This agent keeps the legacy GSCE prompt path for mocked tests, but uses a
direct Ollama JSON plan in real runs and renders deterministic MAVSDK code
from that plan.
"""

import asyncio
import ast
import json
import logging
import math
import os
import re
from typing import Any

from langchain_ollama import OllamaLLM
from pydantic import BaseModel, Field, field_validator
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import settings
from pipelines.code_generator import GCSECodeGenerator
from services.llm_service import LLMService
from utils.mission_parser import (
    MissionWaypointSegment,
    build_square_relative_waypoints,
    estimate_relative_path_distance,
    parse_mission_spec,
)

logger = logging.getLogger(__name__)

LEGACY_LLM_TIMEOUT_S = 45
DIRECT_LLM_TIMEOUT_S = 180
DEFAULT_CRUISE_SPEED_MS = 5.0
DEFAULT_ALTITUDE_M = 15.0
DEFAULT_GEOFENCE_M = 250.0
MAX_RENDERED_SEGMENTS = 120


try:
    from langsmith import traceable as _traceable  # type: ignore

    def traceable(func):  # noqa: D401
        """Wrap with LangSmith tracing when enabled."""

        if settings.langchain_tracing_v2:
            os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
            os.environ.setdefault("LANGCHAIN_API_KEY", settings.langchain_api_key)
            os.environ.setdefault("LANGCHAIN_PROJECT", settings.langchain_project)
            return _traceable(func)
        return func

except ImportError:

    def traceable(func):  # type: ignore[misc]
        return func


class Waypoint(BaseModel):
    name: str
    north_m: float = 0.0
    east_m: float = 0.0
    alt_m: float = 10.0
    action: str = "fly"


class GeneratedCode(BaseModel):
    """Full structured result from the code generation agent."""

    code: str = Field(..., description="Complete Python/MAVSDK mission code")
    mission_summary: str = Field(..., description="One-paragraph mission description")
    estimated_duration_s: float = Field(default=0.0, ge=0)
    estimated_distance_m: float = Field(default=0.0, ge=0)
    waypoints: list[Waypoint] = Field(default_factory=list)
    safety_checks: list[str] = Field(default_factory=list)
    constraints_applied: list[str] = Field(default_factory=list)
    confidence_score: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("code")
    @classmethod
    def code_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Generated code must not be empty")
        return v


class ValidationResult(BaseModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


_APPROVED_METHODS = {
    "takeoff",
    "land",
    "emergency_stop",
    "fly_to",
    "fly_to_relative",
    "set_yaw",
    "orbit",
    "get_position",
    "get_battery",
    "get_attitude",
    "get_gps_info",
    "set_geofence",
    "set_return_to_launch_alt",
    "check_pre_flight",
}


class CodeGenerationAgent:
    """Generate validated PX4 mission code from a natural-language request."""

    def __init__(self) -> None:
        self.llm = OllamaLLM(
            base_url=settings.ollama_base_url,
            model=settings.ollama_code_model,
            temperature=0.1,
            client_kwargs={"timeout": LEGACY_LLM_TIMEOUT_S},
        )
        self.generator = GCSECodeGenerator(self.llm)
        self.llm_service = LLMService(timeout_s=DIRECT_LLM_TIMEOUT_S)

    @traceable
    async def generate_code(
        self,
        mission_description: str,
        constraints_params: dict[str, Any] | None = None,
        design_tree: dict[str, Any] | None = None,
        feedback: str = "",
    ) -> GeneratedCode:
        constraints_params = constraints_params or {}
        design_tree = design_tree or {}
        logger.info(
            "CodeGenerationAgent.generate_code",
            extra={"mission_len": len(mission_description), "has_feedback": bool(feedback)},
        )
        return await self._generate_with_retry(
            mission_description, constraints_params, design_tree, feedback
        )

    def validate_code(self, code: str) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return ValidationResult(
                passed=False,
                errors=[f"SyntaxError at line {exc.lineno}: {exc.msg}"],
            )

        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)

        has_async = any(
            isinstance(node, (ast.AsyncFunctionDef, ast.Await)) for node in ast.walk(tree)
        )
        if not has_async:
            errors.append("No async/await found - all flight functions must be async")

        if not (calls & {"land", "emergency_stop"}):
            errors.append("Missing landing sequence - add drone.land() or drone.emergency_stop()")

        has_try = any(isinstance(node, ast.Try) for node in ast.walk(tree))
        if not has_try:
            errors.append("No try/except block - wrap flight ops with fallback landing")

        unapproved = (
            calls
            - _APPROVED_METHODS
            - {
                "asyncio",
                "sleep",
                "print",
                "logger",
                "info",
                "warning",
                "error",
                "debug",
                "float",
                "format",
                "getattr",
                "range",
                "len",
                "min",
                "enumerate",
                "System",
                "MAVSDKRuntimeAdapter",
                "connect",
                "run",
                "main",
                "getLogger",
                "basicConfig",
                "monotonic",
                "RuntimeError",
                "Exception",
                "True",
                "False",
                "None",
            }
        )
        unapproved = {name for name in unapproved if not name.startswith("_")}
        if unapproved:
            warnings.append(
                f"Non-standard function calls detected: {sorted(unapproved)}. "
                "Only SKILL_API functions are approved."
            )

        battery_patterns = [r"get_battery", r"remaining_percent", r"battery"]
        has_battery = any(re.search(pattern, code) for pattern in battery_patterns)
        if not has_battery:
            warnings.append("No battery check found - add drone.get_battery() before flight ops")

        if "check_pre_flight" not in calls:
            warnings.append("No check_pre_flight() call found - pre-flight checks are mandatory")

        return ValidationResult(passed=not errors, errors=errors, warnings=warnings)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((RuntimeError, ValueError)),
        reraise=True,
    )
    async def _generate_with_retry(
        self,
        mission: str,
        constraints: dict[str, Any],
        design_tree: dict[str, Any],
        feedback: str,
    ) -> GeneratedCode:
        if self._is_mock_llm():
            return await self._generate_with_legacy_llm(mission, constraints, design_tree, feedback)

        try:
            plan = await self._generate_structured_plan(mission, constraints, design_tree, feedback)
            rendered = self._render_plan_result(
                mission, constraints, plan, feedback, fallback=False
            )
        except Exception as exc:
            logger.warning("Code generation fallback engaged: %s", exc)
            rendered = self._fallback_result(mission, constraints, feedback)

        # Phase 8.5: rewrite any legacy skill-API calls the LLM might still
        # emit despite the GSCE prompt update. The rewriter is a no-op when
        # the LLM already produced real MAVSDK.
        rendered["code"] = self._rewrite_to_real_mavsdk(
            rendered["code"],
            mission,
            enforce_geometry=False,
            max_speed_m_s=float(constraints.get("max_speed_ms", 5.0)),
        )

        validation = self.validate_code(rendered["code"])
        if not validation.passed:
            error_summary = "; ".join(validation.errors)
            raise ValueError(f"Code validation failed: {error_summary}")

        return GeneratedCode(**rendered)

    def _rewrite_to_real_mavsdk(
        self, code: str, mission: str, *, enforce_geometry: bool = True, max_speed_m_s: float = 5.0
    ) -> str:
        """Apply Phase 8.5 post-processor: legacy skill-API → real MAVSDK.

        This step closes the gap surfaced by the May-2026 live e2e run: the
        LLM kept emitting ``drone.fly_to_relative(north_m=40.0, ...)`` for a
        "6 m north" prompt despite the Phase 8 GSCE update telling it not
        to. The rewriter parses what the LLM *intended* and re-emits the
        mission using :func:`pipelines.mavsdk_templates.render_real_mavsdk_mission`.
        """
        try:
            from pipelines.code_post_processor import (
                enforce_mission_geometry,
                rewrite_to_real_mavsdk,
            )

            rewritten, plan = rewrite_to_real_mavsdk(
                code, mission_prompt=mission, max_speed_m_s=max_speed_m_s
            )
            if plan.has_legacy_calls and rewritten != code:
                logger.info(
                    "Rewrote LLM output to real MAVSDK",
                    extra={
                        "legacy_calls": plan.legacy_call_names,
                        "waypoint_count": len(plan.waypoints),
                        "takeoff_alt_m": plan.takeoff_altitude_m,
                    },
                )

            # The rewriter above only fires on legacy skill-API output. When the
            # LLM emits real MAVSDK it authors its own coordinates, which is how
            # a "30 m square" became a ~1 km one that the geofence rejected on
            # every one of eleven identical regenerations. Recompute them.
            # Structured plans already use normalized, deterministic geometry
            # and constrained speed. The legacy repair replaces these with defaults.
            if not enforce_geometry:
                return rewritten
            rewritten, replaced = enforce_mission_geometry(rewritten, mission)
            if replaced:
                logger.info("Replaced LLM mission coordinates with computed geometry")
            return rewritten
        except Exception as exc:  # pragma: no cover — best-effort rewrite
            logger.warning("Real-MAVSDK rewrite failed; keeping LLM output: %s", exc)
            return code

    async def _generate_with_legacy_llm(
        self,
        mission: str,
        constraints: dict[str, Any],
        design_tree: dict[str, Any],
        feedback: str,
    ) -> GeneratedCode:
        code, metadata_json = await asyncio.wait_for(
            self.generator.generate(
                mission_description=mission,
                design_tree=design_tree,
                constraints=constraints,
                feedback=feedback,
            ),
            timeout=LEGACY_LLM_TIMEOUT_S,
        )

        if not code.strip():
            raise ValueError("LLM returned empty code - retrying")

        # Phase 8.5: apply the legacy-skill-API rewriter to the legacy LLM
        # path too, so this code generator is also Pixhawk-portable.
        code = self._rewrite_to_real_mavsdk(code, mission)

        validation = self.validate_code(code)
        if not validation.passed:
            error_summary = "; ".join(validation.errors)
            logger.warning("Validation failed - retrying", extra={"errors": error_summary})
            raise ValueError(f"Code validation failed: {error_summary}")

        meta = self._parse_metadata(metadata_json, mission)
        meta["code"] = code
        return GeneratedCode(**meta)

    async def _generate_structured_plan(
        self,
        mission: str,
        constraints: dict[str, Any],
        design_tree: dict[str, Any],
        feedback: str,
    ) -> dict[str, Any]:
        prompt = self._build_direct_prompt(mission, constraints, design_tree, feedback)
        raw = await self.llm_service.generate(
            prompt,
            model=settings.ollama_code_model,
            temperature=0.1,
            response_format="json",
            options={"num_predict": 400},
            request_timeout=DIRECT_LLM_TIMEOUT_S,
        )
        parsed = self._extract_json_object(raw)
        return self._normalize_plan(mission, constraints, parsed)

    def _build_direct_prompt(
        self,
        mission: str,
        constraints: dict[str, Any],
        design_tree: dict[str, Any],
        feedback: str,
    ) -> str:
        spec = parse_mission_spec(mission)
        requested_altitude = spec.altitude_m if spec.altitude_m is not None else "auto"
        requested_duration = spec.duration_min if spec.duration_min is not None else "auto"
        requested_square = spec.square_size_m if spec.square_size_m is not None else "auto"
        constraint_duration = constraints.get("max_duration_s", "auto")

        return (
            "Return JSON only.\n"
            f"Mission: {mission}\n"
            f"Parsed hints: use_case={spec.use_case}, camera_required={str(spec.camera_required).lower()}, "
            f"topology={spec.topology or 'auto'}, altitude_m={requested_altitude}, "
            f"duration_min={requested_duration}, square_size_m={requested_square}.\n"
            f"Constraints: max_altitude_m={constraints.get('max_altitude_m', 'auto')}, "
            f"geofence_radius_m={constraints.get('geofence_radius_m', 'auto')}, "
            f"max_speed_ms={constraints.get('max_speed_ms', 'auto')}, "
            f"max_duration_s={constraint_duration}.\n"
            f"Design summary: {self._summarize_design(design_tree)}\n"
            f"Feedback: {feedback.strip() or 'None'}\n"
            "Schema: "
            '{"mission_summary":"...",'
            '"mission_type":"square_patrol|survey|photo|custom",'
            '"takeoff_altitude_m":15,'
            '"cruise_speed_ms":5,'
            '"hold_time_s":0,'
            '"return_to_home":true,'
            '"waypoints":[{"name":"leg_1","north_m":100,"east_m":0,"down_m":0,"action":"capture"}]}\n'
            "Rules: use relative NED waypoint segments only. "
            "If the request asks for a square 100x100 path, output the four relative legs "
            "[100,0], [0,100], [-100,0], [0,-100]. "
            "Use hold_time_s only for camera dwell time; the renderer will calculate the lap count. "
            "Keep altitude and speed inside constraints. Output JSON only."
        )

    def _normalize_plan(
        self,
        mission: str,
        constraints: dict[str, Any],
        raw_plan: dict[str, Any],
    ) -> dict[str, Any]:
        spec = parse_mission_spec(mission)
        requested_duration_s = (spec.duration_min or 0.0) * 60.0

        max_altitude_m = float(constraints.get("max_altitude_m", max(spec.altitude_m or 0.0, 30.0)))
        max_speed_ms = float(constraints.get("max_speed_ms", 15.0))
        if spec.altitude_m is not None:
            # An altitude the user stated is a requirement, not a hint: the model
            # flew 15 m for «12 m» and 27 m for «12 متراً» in the benchmark. Out
            # of bounds it is refused, never clamped into a different mission.
            if not 2.0 <= spec.altitude_m <= max_altitude_m:
                raise ValueError("Requested altitude is outside the supported mission bounds")
            altitude_m = round(spec.altitude_m, 1)
        else:
            requested_altitude = raw_plan.get("takeoff_altitude_m", DEFAULT_ALTITUDE_M)
            altitude_m = round(self._clamp_float(requested_altitude, 2.0, max_altitude_m), 1)

        requested_speed = raw_plan.get("cruise_speed_ms", DEFAULT_CRUISE_SPEED_MS)
        cruise_speed_ms = round(self._clamp_float(requested_speed, 2.0, max_speed_ms), 1)

        geofence_floor = spec.geofence_radius_m or DEFAULT_GEOFENCE_M
        geofence_radius_m = round(
            float(
                constraints.get(
                    "geofence_radius_m", raw_plan.get("geofence_radius_m", geofence_floor)
                )
            ),
            1,
        )

        # An explicit stationary hover is not a survey path. Preserve its dwell
        # independently of an upper duration limit or model-authored waypoints.
        stationary_hover = spec.hover_duration_s is not None and not re.search(
            r"\b(square|survey|north|south|east|west|waypoint|forward|backward|circle|orbit|route|patrol|move|moving|fly|travel)\b"
            r"|مربع|مسار|شمال|جنوب|شرق|غرب|نقط[ةه]|دائر[ةه]|تطير|يطير|تتحرك|يتحرك|تنتقل|ينتقل|دوري[ةه]",
            spec.normalized.lower(),
        )
        if stationary_hover:
            if spec.altitude_m is not None:
                if not 2.0 <= spec.altitude_m <= max_altitude_m:
                    raise ValueError("Requested hover altitude exceeds supported mission bounds")
                altitude_m = spec.altitude_m
            duration = spec.hover_duration_s + 25.0  # takeoff/landing estimate
            limit = constraints.get("max_duration_s")
            if limit is not None and duration > float(limit):
                raise ValueError(
                    "Requested hover plus takeoff/landing estimate exceeds max_duration_s"
                )
            return {
                "mission_type": "stationary_hover",
                "mission_summary": mission,
                "altitude_m": altitude_m,
                "cruise_speed_ms": cruise_speed_ms,
                "geofence_radius_m": geofence_radius_m,
                "segments": [MissionWaypointSegment("hover", 0.0, 0.0)],
                "hold_time_s": spec.hover_duration_s,
                "estimated_duration_s": duration,
                "estimated_distance_m": 0.0,
                "target_duration_s": spec.hover_duration_s,
                "camera_required": spec.camera_required,
                "topology": spec.topology,
                "use_case": spec.use_case,
            }

        base_segments = self._select_base_segments(spec, raw_plan)
        base_segments = self._ensure_return_home(base_segments)
        if not base_segments:
            base_segments = self._ensure_return_home(self._default_segments(spec))

        target_duration_s = requested_duration_s
        if target_duration_s <= 0:
            target_duration_s = float(self._read_numeric(raw_plan, ("target_duration_s",)) or 0.0)

        constraint_duration = constraints.get("max_duration_s")
        if constraint_duration is not None and target_duration_s > 0:
            target_duration_s = min(
                target_duration_s,
                float(constraint_duration),
            )

        hold_time_s = float(self._read_numeric(raw_plan, ("hold_time_s",)) or 0.0)
        lap_count = self._calculate_lap_count(
            base_segments=base_segments,
            cruise_speed_ms=cruise_speed_ms,
            hold_time_s=hold_time_s,
            target_duration_s=target_duration_s,
            fallback_lap_count=int(self._read_numeric(raw_plan, ("lap_count", "loops")) or 1),
        )

        segment_budget = max(1, MAX_RENDERED_SEGMENTS // max(1, len(base_segments)))
        lap_count = min(lap_count, segment_budget)

        expanded_segments = self._expand_segments(base_segments, lap_count)
        travel_time_s = estimate_relative_path_distance(expanded_segments) / cruise_speed_ms

        if target_duration_s > 0 and expanded_segments:
            remaining = max(0.0, target_duration_s - travel_time_s)
            hold_time_s = max(hold_time_s, remaining / len(expanded_segments))

        hold_time_s = round(hold_time_s, 1)
        estimated_duration_s = round(travel_time_s + hold_time_s * len(expanded_segments) + 25.0, 1)

        if target_duration_s > 0:
            estimated_duration_s = round(max(estimated_duration_s, target_duration_s), 1)

        mission_type = raw_plan.get("mission_type") or (
            "square_patrol" if spec.square_size_m else "custom"
        )
        mission_summary = raw_plan.get("mission_summary") or self._build_summary(
            spec, altitude_m, lap_count, estimated_duration_s
        )

        return {
            "mission_type": mission_type,
            "mission_summary": mission_summary,
            "altitude_m": altitude_m,
            "cruise_speed_ms": cruise_speed_ms,
            "geofence_radius_m": geofence_radius_m,
            "segments": expanded_segments,
            "hold_time_s": hold_time_s,
            "estimated_duration_s": estimated_duration_s,
            "estimated_distance_m": round(estimate_relative_path_distance(expanded_segments), 1),
            "target_duration_s": round(target_duration_s, 1),
            "camera_required": spec.camera_required,
            "topology": spec.topology,
            "use_case": spec.use_case,
        }

    def _calculate_lap_count(
        self,
        *,
        base_segments: list[MissionWaypointSegment],
        cruise_speed_ms: float,
        hold_time_s: float,
        target_duration_s: float,
        fallback_lap_count: int,
    ) -> int:
        if target_duration_s <= 0 or not base_segments or cruise_speed_ms <= 0:
            return max(1, fallback_lap_count)

        single_lap_distance_m = sum(
            math.hypot(segment.north_m, segment.east_m) for segment in base_segments
        )
        single_lap_time_s = single_lap_distance_m / cruise_speed_ms + len(base_segments) * max(
            0.0, hold_time_s
        )
        if single_lap_time_s <= 0:
            return max(1, fallback_lap_count)

        flight_budget_s = max(1.0, target_duration_s - 30.0)
        return max(1, math.ceil(flight_budget_s / single_lap_time_s))

    def _select_base_segments(
        self,
        spec: Any,
        raw_plan: dict[str, Any],
    ) -> list[MissionWaypointSegment]:
        if spec.square_size_m:
            action = "capture" if spec.camera_required else "fly"
            return build_square_relative_waypoints(spec.square_size_m, action=action)

        raw_waypoints = raw_plan.get("waypoints", [])
        if isinstance(raw_waypoints, list):
            segments = self._segments_from_raw(raw_waypoints)
            if segments:
                return segments

        return self._default_segments(spec)

    def _segments_from_raw(self, raw_waypoints: list[Any]) -> list[MissionWaypointSegment]:
        segments: list[MissionWaypointSegment] = []
        for index, item in enumerate(raw_waypoints, start=1):
            if not isinstance(item, dict):
                continue
            north_m = self._read_numeric(item, ("north_m", "north", "x", "dx"))
            east_m = self._read_numeric(item, ("east_m", "east", "y", "dy"))
            down_m = self._read_numeric(item, ("down_m", "down", "z", "dz")) or 0.0
            if north_m is None or east_m is None:
                continue
            segments.append(
                MissionWaypointSegment(
                    name=str(item.get("name") or f"leg_{index}"),
                    north_m=float(north_m),
                    east_m=float(east_m),
                    down_m=float(down_m),
                    action=str(item.get("action") or "fly"),
                )
            )
        return segments

    def _default_segments(self, spec: Any) -> list[MissionWaypointSegment]:
        outbound_m = 60.0 if spec.camera_required else 40.0
        action = "capture" if spec.camera_required else "fly"
        return [
            MissionWaypointSegment("outbound", north_m=outbound_m, east_m=0.0, action=action),
            MissionWaypointSegment("return_leg", north_m=-outbound_m, east_m=0.0, action="return"),
        ]

    def _ensure_return_home(
        self,
        segments: list[MissionWaypointSegment],
    ) -> list[MissionWaypointSegment]:
        if not segments:
            return []

        total_north = sum(segment.north_m for segment in segments)
        total_east = sum(segment.east_m for segment in segments)
        if abs(total_north) < 1e-6 and abs(total_east) < 1e-6:
            return list(segments)

        adjusted = list(segments)
        adjusted.append(
            MissionWaypointSegment(
                "return_home",
                north_m=-total_north,
                east_m=-total_east,
                down_m=0.0,
                action="return",
            )
        )
        return adjusted

    def _expand_segments(
        self,
        segments: list[MissionWaypointSegment],
        lap_count: int,
    ) -> list[MissionWaypointSegment]:
        expanded: list[MissionWaypointSegment] = []
        for lap_index in range(lap_count):
            for segment in segments:
                expanded.append(
                    MissionWaypointSegment(
                        name=f"lap_{lap_index + 1}_{segment.name}"
                        if lap_count > 1
                        else segment.name,
                        north_m=segment.north_m,
                        east_m=segment.east_m,
                        down_m=segment.down_m,
                        action=segment.action,
                    )
                )
        return expanded

    def _render_plan_result(
        self,
        mission: str,
        constraints: dict[str, Any],
        plan: dict[str, Any],
        feedback: str,
        *,
        fallback: bool,
    ) -> dict[str, Any]:
        code = self._build_code(plan)
        waypoints = self._build_absolute_waypoints(plan["segments"], plan["altitude_m"])

        constraints_applied = [
            f"generation={'fallback' if fallback else 'ollama_plan'}",
            f"max_altitude={plan['altitude_m']:.1f}",
            f"geofence_radius={plan['geofence_radius_m']:.1f}",
            f"cruise_speed={plan['cruise_speed_ms']:.1f}",
        ]
        if plan["target_duration_s"] > 0:
            constraints_applied.append(f"target_duration={plan['target_duration_s']:.1f}")
        if constraints.get("max_duration_s") is not None:
            constraints_applied.append(f"max_duration={float(constraints['max_duration_s']):.1f}")
        if feedback:
            constraints_applied.append(f"feedback:{feedback[:80]}")

        return {
            "code": code,
            "mission_summary": plan["mission_summary"] or mission[:200],
            "estimated_duration_s": plan["estimated_duration_s"],
            "estimated_distance_m": plan["estimated_distance_m"],
            "waypoints": waypoints,
            "safety_checks": [
                "pre_flight",
                "battery_check",
                "geofence",
                "rtl_altitude",
                "exception_emergency_stop",
            ],
            "constraints_applied": constraints_applied,
            "confidence_score": 0.9 if not fallback else 0.45,
        }

    def _build_code(self, plan: dict[str, Any]) -> str:
        if plan.get("mission_type") == "stationary_hover":
            from pipelines.mission_builder import MissionPlanSpec, Waypoint
            from pipelines.mavsdk_templates import render_real_mavsdk_mission
            from utils.coordinates import PX4_SITL_DEFAULT_HOME

            spec = MissionPlanSpec(
                home=PX4_SITL_DEFAULT_HOME,
                default_speed_m_s=plan["cruise_speed_ms"],
                takeoff_altitude_m=plan["altitude_m"],
                waypoints=[
                    Waypoint(
                        name="hover",
                        north_m=0,
                        east_m=0,
                        alt_m=plan["altitude_m"],
                        speed_m_s=plan["cruise_speed_ms"],
                        is_fly_through=False,
                        loiter_time_s=plan["hold_time_s"],
                    )
                ],
            )
            return render_real_mavsdk_mission(spec, geofence_radius_m=plan["geofence_radius_m"])
        lines: list[str] = []
        for segment in plan["segments"]:
            lines.extend(
                [
                    "        battery = await drone.get_battery()",
                    "        battery_pct = _battery_remaining_pct(battery)",
                    "        if battery_pct < 20.0:",
                    f'            logger.warning("Battery below return margin before {segment.name}")',
                    "            await drone.land()",
                    "            return False",
                    (
                        f'        logger.info("Executing {segment.name}: north={segment.north_m:.1f}m '
                        f'east={segment.east_m:.1f}m")'
                    ),
                    (
                        "        await drone.fly_to_relative("
                        f"north_m={segment.north_m:.1f}, "
                        f"east_m={segment.east_m:.1f}, "
                        f"down_m={segment.down_m:.1f})"
                    ),
                ]
            )
            if segment.action in {"capture", "photo"}:
                lines.append(f'        logger.info("Capture event at {segment.name}")')
            if plan["hold_time_s"] > 0:
                lines.append(f"        await asyncio.sleep({plan['hold_time_s']:.1f})")

        mission_body = "\n".join(lines)
        parts = [
            "import asyncio\n",
            "import logging\n",
            "import os\n",
            "import time\n",
            "from mavsdk import System\n\n",
            "try:\n",
            "    from apps.api.utils.mavsdk_runtime_adapter import MAVSDKRuntimeAdapter\n",
            "except ImportError:\n",
            "    from utils.mavsdk_runtime_adapter import MAVSDKRuntimeAdapter\n\n",
            "logger = logging.getLogger(__name__)\n",
            f"MISSION_TARGET_DURATION_S = {plan['target_duration_s']:.1f}\n",
            f"MISSION_ESTIMATED_DISTANCE_M = {plan['estimated_distance_m']:.1f}\n\n",
            "def _battery_remaining_pct(battery):\n",
            '    value = float(getattr(battery, "remaining_percent", 0.0))\n',
            "    return value * 100.0 if value <= 1.0 else value\n\n",
            "async def run_mission(drone):\n",
            "    mission_start = time.monotonic()\n",
            "    pre_flight = await drone.check_pre_flight()\n",
            "    if not pre_flight.passed:\n",
            '        logger.error("Pre-flight failed")\n',
            "        return False\n\n",
            "    battery = await drone.get_battery()\n",
            "    battery_pct = _battery_remaining_pct(battery)\n",
            "    if battery_pct < 20.0:\n",
            '        logger.error("Battery below safe threshold")\n',
            "        return False\n\n",
            "    try:\n",
            (
                f"        await drone.set_geofence(radius_m={plan['geofence_radius_m']:.1f}, "
                f"max_alt_m={plan['altitude_m']:.1f})\n"
            ),
            f"        await drone.set_return_to_launch_alt(alt_m={plan['altitude_m']:.1f})\n",
            f"        await drone.takeoff(altitude_m={plan['altitude_m']:.1f})\n",
            "        await asyncio.sleep(3)\n",
            f"{mission_body}\n",
            "        elapsed_s = time.monotonic() - mission_start\n",
            "        if MISSION_TARGET_DURATION_S > 0 and elapsed_s < MISSION_TARGET_DURATION_S:\n",
            "            remaining_hold_s = MISSION_TARGET_DURATION_S - elapsed_s\n",
            '            logger.info("Holding position to satisfy target duration: %.1fs", remaining_hold_s)\n',
            "            while remaining_hold_s > 0:\n",
            "                battery = await drone.get_battery()\n",
            "                battery_pct = _battery_remaining_pct(battery)\n",
            "                if battery_pct < 20.0:\n",
            '                    logger.warning("Battery below return margin during duration hold")\n',
            "                    await drone.land()\n",
            "                    return False\n",
            "                sleep_s = min(5.0, remaining_hold_s)\n",
            "                await asyncio.sleep(sleep_s)\n",
            "                remaining_hold_s -= sleep_s\n",
            "        await drone.land()\n",
            "        return True\n",
            "    except Exception as exc:\n",
            '        logger.error(f"Mission error: {exc}")\n',
            "        await drone.emergency_stop()\n",
            "        return False\n\n",
            "async def main():\n",
            "    logging.basicConfig(level=logging.INFO)\n",
            "    system = System()\n",
            '    mavsdk_address = os.getenv("DRONEPROMPT_MAVSDK_ADDRESS", "udp://:14540")\n',
            "    await system.connect(system_address=mavsdk_address)\n",
            "    drone = MAVSDKRuntimeAdapter(system)\n",
            "    return await run_mission(drone)\n\n",
            'if __name__ == "__main__":\n',
            "    asyncio.run(main())\n",
        ]
        return "".join(parts)

    def _build_absolute_waypoints(
        self,
        segments: list[MissionWaypointSegment],
        altitude_m: float,
    ) -> list[dict[str, Any]]:
        waypoints: list[dict[str, Any]] = []
        north = 0.0
        east = 0.0
        down = -altitude_m
        for segment in segments:
            north += segment.north_m
            east += segment.east_m
            if segment.down_m:
                down += segment.down_m
            waypoints.append(
                {
                    "name": segment.name,
                    "north_m": round(north, 1),
                    "east_m": round(east, 1),
                    "alt_m": round(max(0.0, -down), 1),
                    "action": segment.action,
                }
            )
        return waypoints

    def _fallback_result(
        self,
        mission: str,
        constraints: dict[str, Any],
        feedback: str,
    ) -> dict[str, Any]:
        fallback_plan = self._normalize_plan(mission, constraints, {})
        return self._render_plan_result(
            mission,
            constraints,
            fallback_plan,
            feedback,
            fallback=True,
        )

    def _build_summary(
        self,
        spec: Any,
        altitude_m: float,
        lap_count: int,
        estimated_duration_s: float,
    ) -> str:
        if spec.square_size_m:
            return (
                f"{lap_count} square patrol lap(s) of {spec.square_size_m:.0f}m at "
                f"{altitude_m:.0f}m altitude, estimated {estimated_duration_s:.0f}s."
            )
        if spec.camera_required:
            return (
                f"Photography mission at {altitude_m:.0f}m altitude with estimated "
                f"duration {estimated_duration_s:.0f}s."
            )
        return (
            f"{spec.use_case.title()} mission at {altitude_m:.0f}m altitude with estimated "
            f"duration {estimated_duration_s:.0f}s."
        )

    def _summarize_design(self, design_tree: dict[str, Any]) -> str:
        if not isinstance(design_tree, dict) or not design_tree:
            return "none"

        topology = design_tree.get("topology", "unknown")
        battery = design_tree.get("battery", {})
        payload = design_tree.get("payload", {})
        cells = battery.get("cells", "?") if isinstance(battery, dict) else "?"
        capacity = battery.get("capacity_mah", "?") if isinstance(battery, dict) else "?"
        payload_g = payload.get("installed_weight_g") if isinstance(payload, dict) else None
        if payload_g is None:
            payload_g = payload.get("max_weight_g", "?") if isinstance(payload, dict) else "?"
        summary = f"topology={topology}, battery={cells}S/{capacity}mAh, payload_g={payload_g}"
        context = design_tree.get("catalog_context")
        if context:
            summary += "; catalog_context=" + json.dumps(context, sort_keys=True)
        else:
            summary += "; validation_scope=unverified_design"
        return summary

    def _parse_metadata(self, metadata_json: str, mission: str) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "mission_summary": mission[:200],
            "estimated_duration_s": 0.0,
            "estimated_distance_m": 0.0,
            "waypoints": [],
            "safety_checks": [],
            "constraints_applied": [],
            "confidence_score": 0.5,
        }

        if not metadata_json:
            return defaults

        try:
            parsed = json.loads(metadata_json)
            waypoints = [
                Waypoint(**wp) if isinstance(wp, dict) else wp for wp in parsed.get("waypoints", [])
            ]
            return {
                "mission_summary": parsed.get("mission_summary", defaults["mission_summary"]),
                "estimated_duration_s": float(parsed.get("estimated_duration_s", 0)),
                "estimated_distance_m": float(parsed.get("estimated_distance_m", 0)),
                "waypoints": waypoints,
                "safety_checks": parsed.get("safety_checks", []),
                "constraints_applied": parsed.get("constraints_applied", []),
                "confidence_score": float(parsed.get("confidence_score", 0.5)),
            }
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Metadata JSON parse failed", extra={"error": str(exc)})
            return defaults

    def _extract_json_object(self, raw: str) -> dict[str, Any]:
        text = raw.strip()
        fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1)
        if not text.startswith("{"):
            brace_match = re.search(r"\{.*\}", text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)
        return json.loads(text)

    def _is_mock_llm(self) -> bool:
        return self.llm.__class__.__module__.startswith("unittest.mock")

    @staticmethod
    def _read_numeric(data: dict[str, Any], keys: tuple[str, ...]) -> float | None:
        for key in keys:
            value = data.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _clamp_float(value: Any, minimum: float, maximum: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = minimum
        return max(minimum, min(maximum, numeric))


class CodeAgent:
    """Lightweight wrapper that returns only the generated code string."""

    def __init__(self) -> None:
        self._agent = CodeGenerationAgent()

    async def run(
        self,
        mission_description: str,
        design_tree: dict[str, Any],
        constraints: dict[str, Any],
        feedback: str = "",
    ) -> str:
        logger.info("CodeAgent.run")
        result = await self._agent.generate_code(
            mission_description=mission_description,
            constraints_params=constraints,
            design_tree=design_tree,
            feedback=feedback,
        )
        return result.code
