import ast
from unittest.mock import AsyncMock

import pytest

from agents.code_agent import CodeGenerationAgent


@pytest.mark.asyncio
async def test_structured_renderer_keeps_speed_and_hover_geometry():
    agent = CodeGenerationAgent.__new__(CodeGenerationAgent)
    agent.llm = object()
    mission = "In simulation, take off to 10 meters, hover for 20 seconds and land."
    constraints = dict(max_altitude_m=10, max_speed_ms=3, max_duration_s=90, geofence_radius_m=30)
    plan = agent._normalize_plan(
        mission,
        constraints,
        {
            "takeoff_altitude_m": 10,
            "cruise_speed_ms": 3,
            "hold_time_s": 20,
            "return_to_home": True,
            "waypoints": [],
        },
    )
    agent._generate_structured_plan = AsyncMock(return_value=plan)
    before = agent._render_plan_result(mission, constraints, plan, "", fallback=False)["code"]
    result = await agent.generate_code(mission, constraints, {})

    def items(code):
        return [
            n
            for n in ast.walk(ast.parse(code))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "MissionItem"
        ]

    from pipelines.code_post_processor import extract_plan_from_legacy_code

    assert len(items(result.code)) == (
        len(items(before)) or len(extract_plan_from_legacy_code(before).waypoints)
    )
    for item in items(result.code):
        speed = next(k.value for k in item.keywords if k.arg == "speed_m_s")
        assert isinstance(speed, ast.Constant) and speed.value <= 3
    assert "generation=ollama_plan" in result.constraints_applied
