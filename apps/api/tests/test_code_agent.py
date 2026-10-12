"""Unit tests for CodeGenerationAgent (GSCE framework).

Run with:
    cd eljazari/apps/api
    pytest tests/test_code_agent.py -v
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.code_agent import (
    CodeGenerationAgent,
    GeneratedCode,
    ValidationResult,
    Waypoint,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & helpers
# ─────────────────────────────────────────────────────────────────────────────

VALID_CODE = """\
import asyncio
import logging

logger = logging.getLogger(__name__)

async def run_mission(drone):
    try:
        pre_flight = await drone.check_pre_flight()
        if not pre_flight.passed:
            logger.error("Pre-flight failed")
            return False

        battery = await drone.get_battery()
        if battery.remaining_percent < 50:
            logger.error("Low battery")
            return False

        await drone.takeoff(altitude_m=10.0)
        await asyncio.sleep(2)
        await drone.fly_to_relative(north_m=50.0, east_m=0.0, down_m=0.0)
        logger.info("Waypoint reached")
        await drone.land()
        return True

    except Exception as exc:
        logger.error(f"Error: {exc}")
        await drone.land()
        return False
"""

VALID_METADATA = json.dumps(
    {
        "mission_summary": "Fly 50m north and return",
        "estimated_duration_s": 45.0,
        "estimated_distance_m": 100.0,
        "waypoints": [
            {"name": "WP1", "north_m": 50.0, "east_m": 0.0, "alt_m": 10.0, "action": "fly"}
        ],
        "safety_checks": ["battery > 50%", "GPS fix ≥ 8 sats"],
        "constraints_applied": ["geofence 200m", "max_alt 30m"],
        "confidence_score": 0.92,
    }
)


def _make_agent_with_mock_llm(code_response: str, meta_response: str = "") -> CodeGenerationAgent:
    """Return a CodeGenerationAgent whose LLM is mocked."""
    agent = CodeGenerationAgent.__new__(CodeGenerationAgent)

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=f"```python\n{code_response}\n```\n```json\n{meta_response}\n```"
    )

    from pipelines.code_generator import GCSECodeGenerator

    agent.llm = mock_llm
    agent.generator = GCSECodeGenerator(mock_llm)
    return agent


# ─────────────────────────────────────────────────────────────────────────────
# ValidationResult tests
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateCode:
    def setup_method(self):
        self.agent = CodeGenerationAgent.__new__(CodeGenerationAgent)

    def test_valid_code_passes(self):
        result: ValidationResult = self.agent.validate_code(VALID_CODE)
        assert result.passed is True
        assert result.errors == []

    def test_syntax_error_detected(self):
        bad_code = "def foo(\n    pass"
        result = self.agent.validate_code(bad_code)
        assert result.passed is False
        assert any("SyntaxError" in e for e in result.errors)

    def test_missing_landing_is_error(self):
        code = "async def run(drone):\n    await drone.takeoff(altitude_m=10)\n"
        result = self.agent.validate_code(code)
        assert not result.passed
        assert any("landing" in e.lower() for e in result.errors)

    def test_missing_try_except_is_error(self):
        code = (
            "async def run(drone):\n"
            "    await drone.takeoff(altitude_m=10)\n"
            "    await drone.land()\n"
        )
        result = self.agent.validate_code(code)
        assert not result.passed
        assert any("try/except" in e for e in result.errors)

    def test_no_async_is_error(self):
        code = "def run(drone):\n    drone.takeoff(10)\n    drone.land()\n"
        result = self.agent.validate_code(code)
        assert not result.passed
        assert any("async" in e.lower() for e in result.errors)

    def test_no_battery_check_is_warning(self):
        code = (
            "async def run(drone):\n"
            "    try:\n"
            "        await drone.takeoff(altitude_m=10)\n"
            "        await drone.land()\n"
            "    except Exception:\n"
            "        await drone.land()\n"
        )
        result = self.agent.validate_code(code)
        assert any("battery" in w.lower() for w in result.warnings)

    def test_no_preflight_is_warning(self):
        code = (
            "async def run(drone):\n"
            "    battery = await drone.get_battery()\n"
            "    try:\n"
            "        await drone.takeoff(altitude_m=10)\n"
            "        await drone.land()\n"
            "    except Exception:\n"
            "        await drone.land()\n"
        )
        result = self.agent.validate_code(code)
        assert any(
            "pre_flight" in w.lower() or "preflight" in w.lower() or "check_pre_flight" in w.lower()
            for w in result.warnings
        )

    def test_unapproved_function_is_warning(self):
        code = (
            "async def run(drone):\n"
            "    try:\n"
            "        await drone.check_pre_flight()\n"
            "        battery = await drone.get_battery()\n"
            "        await drone.takeoff(altitude_m=10)\n"
            "        await some_custom_api()\n"  # unapproved
            "        await drone.land()\n"
            "    except Exception:\n"
            "        await drone.land()\n"
        )
        result = self.agent.validate_code(code)
        assert any("Non-standard" in w for w in result.warnings)


# ─────────────────────────────────────────────────────────────────────────────
# GeneratedCode Pydantic model tests
# ─────────────────────────────────────────────────────────────────────────────


class TestGeneratedCodeModel:
    def test_valid_construction(self):
        gc = GeneratedCode(
            code=VALID_CODE,
            mission_summary="Test mission",
            estimated_duration_s=30.0,
            estimated_distance_m=100.0,
            waypoints=[Waypoint(name="WP1", north_m=50.0, alt_m=10.0)],
            safety_checks=["battery check"],
            constraints_applied=["geofence 200m"],
            confidence_score=0.9,
        )
        assert gc.confidence_score == 0.9
        assert len(gc.waypoints) == 1

    def test_empty_code_raises(self):
        with pytest.raises(Exception):
            GeneratedCode(code="   ", mission_summary="x")

    def test_confidence_score_bounds(self):
        with pytest.raises(Exception):
            GeneratedCode(code=VALID_CODE, mission_summary="x", confidence_score=1.5)
        with pytest.raises(Exception):
            GeneratedCode(code=VALID_CODE, mission_summary="x", confidence_score=-0.1)

    def test_defaults_applied(self):
        gc = GeneratedCode(code=VALID_CODE, mission_summary="Test")
        assert gc.estimated_duration_s == 0.0
        assert gc.waypoints == []
        assert gc.confidence_score == 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Metadata parsing tests
# ─────────────────────────────────────────────────────────────────────────────


class TestMetadataParsing:
    def setup_method(self):
        self.agent = CodeGenerationAgent.__new__(CodeGenerationAgent)

    def test_valid_metadata_parsed(self):
        meta = self.agent._parse_metadata(VALID_METADATA, "default summary")
        assert meta["mission_summary"] == "Fly 50m north and return"
        assert meta["estimated_duration_s"] == 45.0
        assert len(meta["waypoints"]) == 1
        assert isinstance(meta["waypoints"][0], Waypoint)
        assert meta["confidence_score"] == 0.92

    def test_empty_metadata_returns_defaults(self):
        meta = self.agent._parse_metadata("", "fallback mission")
        assert meta["mission_summary"] == "fallback mission"
        assert meta["confidence_score"] == 0.5
        assert meta["waypoints"] == []

    def test_invalid_json_returns_defaults(self):
        meta = self.agent._parse_metadata("{not valid json}", "fallback")
        assert meta["mission_summary"] == "fallback"

    def test_partial_metadata_fills_defaults(self):
        partial = json.dumps({"mission_summary": "Partial"})
        meta = self.agent._parse_metadata(partial, "original")
        assert meta["mission_summary"] == "Partial"
        assert meta["estimated_duration_s"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Integration-style tests (mocked LLM)
# ─────────────────────────────────────────────────────────────────────────────


class TestGenerateCode:
    @pytest.mark.asyncio
    async def test_successful_generation(self):
        agent = _make_agent_with_mock_llm(VALID_CODE, VALID_METADATA)
        result = await agent.generate_code(
            mission_description="Fly 50m north and return",
            constraints_params={"max_altitude_m": 30, "geofence_radius_m": 200},
        )
        assert isinstance(result, GeneratedCode)
        # Phase 8.5 rewriter converts the legacy VALID_CODE (skill-API)
        # into real MAVSDK. We assert the rewritten output is valid PX4
        # code rather than exact-string equality with the legacy fixture.
        assert "from mavsdk import System" in result.code
        assert "drone.mission.upload_mission" in result.code
        assert "drone.action.arm()" in result.code
        # Legacy patterns must not survive the rewrite.
        assert "drone.fly_to_relative(" not in result.code
        assert "drone.set_geofence(" not in result.code
        assert result.mission_summary == "Fly 50m north and return"
        assert result.confidence_score == 0.92

    @pytest.mark.asyncio
    async def test_empty_llm_response_retries_and_raises(self):
        agent = CodeGenerationAgent.__new__(CodeGenerationAgent)
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value="```python\n\n```")

        from pipelines.code_generator import GCSECodeGenerator

        agent.llm = mock_llm
        agent.generator = GCSECodeGenerator(mock_llm)

        with pytest.raises(ValueError, match="empty code"):
            await agent.generate_code("Test mission")

    @pytest.mark.asyncio
    async def test_invalid_code_retries_and_raises(self):
        # Code with no landing — will fail validation on all 3 attempts
        bad_code = (
            "async def run(drone):\n"
            "    try:\n"
            "        await drone.takeoff(altitude_m=10)\n"
            "    except Exception:\n"
            "        pass\n"
        )
        agent = _make_agent_with_mock_llm(bad_code)

        with pytest.raises(ValueError, match="validation failed"):
            await agent.generate_code("Mission that will fail")

    @pytest.mark.asyncio
    async def test_constraints_forwarded_to_generator(self):
        agent = _make_agent_with_mock_llm(VALID_CODE, VALID_METADATA)
        constraints = {
            "max_altitude_m": 50,
            "geofence_radius_m": 100,
            "max_speed_ms": 8,
            "max_duration_s": 300,
        }
        result = await agent.generate_code(
            mission_description="Constrained mission",
            constraints_params=constraints,
        )
        assert isinstance(result, GeneratedCode)
        # Verify LLM was called
        agent.generator.llm.ainvoke.assert_called_once()
        prompt_used = agent.generator.llm.ainvoke.call_args[0][0]
        assert "50" in prompt_used  # max_altitude_m
        assert "100" in prompt_used  # geofence_radius_m

    @pytest.mark.asyncio
    async def test_feedback_included_in_prompt(self):
        agent = _make_agent_with_mock_llm(VALID_CODE, VALID_METADATA)
        await agent.generate_code(
            mission_description="Mission with feedback",
            feedback="Previous attempt missed battery check",
        )
        prompt = agent.generator.llm.ainvoke.call_args[0][0]
        assert "Previous attempt missed battery check" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# GCSECodeGenerator prompt-building tests
# ─────────────────────────────────────────────────────────────────────────────


class TestGCSEPromptBuilding:
    def setup_method(self):
        from pipelines.code_generator import GCSECodeGenerator

        self.gen = GCSECodeGenerator(llm=MagicMock())

    def test_all_four_layers_in_prompt(self):
        prompt = self.gen._build_prompt("Test mission", {}, "")
        # Layer 1 — Guidelines
        assert "Your strict rules" in prompt
        # Layer 2 — Skill APIs (Phase 8: real MAVSDK only)
        assert "drone.action.arm" in prompt
        assert "drone.mission.upload_mission" in prompt
        assert "drone.telemetry.health" in prompt
        # Phase 8 forbids the legacy skill-API names in the prompt.
        assert "✗ drone.fly_to_relative" in prompt
        # Layer 3 — Constraints
        assert "GEOFENCE_RADIUS" in prompt
        assert "MAX_ALTITUDE" in prompt
        # Layer 4 — Examples (Phase 8 examples are English, MAVSDK-based)
        assert "Example 1" in prompt
        assert "Example 2" in prompt
        assert "MissionItem(" in prompt

    def test_constraints_template_filled(self):
        constraints = {
            "geofence_radius_m": 300,
            "max_altitude_m": 60,
            "max_speed_ms": 10,
            "max_duration_s": 120,
        }
        prompt = self.gen._build_prompt("Mission", constraints, "")
        assert "300" in prompt
        assert "60" in prompt
        assert "120" in prompt

    def test_feedback_section_present_when_provided(self):
        prompt = self.gen._build_prompt("Mission", {}, "Fix missing battery check")
        assert "Fix missing battery check" in prompt

    def test_no_feedback_section_when_empty(self):
        prompt = self.gen._build_prompt("Mission", {}, "")
        assert "Notes on the previous attempt" not in prompt

    def test_output_format_appended(self):
        prompt = self.gen._build_prompt("Mission", {}, "")
        assert "```json" in prompt

    def test_code_extraction_python_block(self):
        response = "Some text\n```python\nprint('hello')\n```\nMore text"
        assert self.gen._extract_code(response) == "print('hello')"

    def test_code_extraction_generic_block(self):
        response = "```\nsome code\n```"
        assert self.gen._extract_code(response) == "some code"

    def test_code_extraction_plain_text(self):
        response = "just plain text"
        assert self.gen._extract_code(response) == "just plain text"

    def test_metadata_extraction(self):
        response = 'code here\n```json\n{"key": "val"}\n```'
        assert self.gen._extract_metadata(response) == '{"key": "val"}'

    def test_metadata_extraction_missing(self):
        response = "no json block here"
        assert self.gen._extract_metadata(response) == ""


# ─────────────────────────────────────────────────────────────────────────────
# Waypoint model tests
# ─────────────────────────────────────────────────────────────────────────────


class TestWaypoint:
    def test_defaults(self):
        wp = Waypoint(name="Start")
        assert wp.north_m == 0.0
        assert wp.east_m == 0.0
        assert wp.alt_m == 10.0
        assert wp.action == "fly"

    def test_custom_values(self):
        wp = Waypoint(name="Photo", north_m=100.0, east_m=50.0, alt_m=20.0, action="photo")
        assert wp.north_m == 100.0
        assert wp.action == "photo"


class TestMissionAwareFallback:
    def setup_method(self):
        self.agent = CodeGenerationAgent.__new__(CodeGenerationAgent)

    def test_square_prompt_generates_square_path_and_duration(self):
        prompt = (
            "quadcopter photography drone, altitude 15 m, square path 100x100 m, "
            "flight time 20 minutes, aerial camera"
        )
        result = self.agent._fallback_result(prompt, {}, "")

        assert result["estimated_duration_s"] >= 1200.0
        assert result["estimated_distance_m"] >= 6000.0
        assert result["code"].count("fly_to_relative") >= 20
        assert "north_m=100.0" in result["code"]
        assert "east_m=100.0" in result["code"]
        assert 'os.getenv("DRONEPROMPT_MAVSDK_ADDRESS", "udp://:14540")' in result["code"]
        assert "drone = MAVSDKRuntimeAdapter(system)" in result["code"]
        assert "_battery_remaining_pct(battery)" in result["code"]
        assert "battery.remaining_percent < 50" not in result["code"]
        assert self.agent.validate_code(result["code"]).passed is True
        assert result["waypoints"][-1]["north_m"] == pytest.approx(0.0)
        assert result["waypoints"][-1]["east_m"] == pytest.approx(0.0)
