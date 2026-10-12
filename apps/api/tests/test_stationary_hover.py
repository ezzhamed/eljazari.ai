import ast

import pytest

from agents.code_agent import CodeGenerationAgent
from agents.safety_layer import SafeLLMSupervisionLayer
from agents.safety_models import FlightConstraints
from utils.mission_parser import parse_mission_spec

PROMPT = "In simulation, take off to 10 meters, hover for 20 seconds and land."


def generated():
    agent = CodeGenerationAgent.__new__(CodeGenerationAgent)
    plan = agent._normalize_plan(
        PROMPT,
        dict(max_altitude_m=10, max_speed_ms=3, max_duration_s=90, geofence_radius_m=30),
        {"hold_time_s": 90, "waypoints": [{"north_m": 100, "east_m": 0, "name": "invented"}]},
    )
    return plan, agent._build_code(plan)


def test_hover_is_stationary_and_duration_is_not_the_ceiling():
    plan, code = generated()
    assert plan["estimated_distance_m"] == 0
    assert plan["hold_time_s"] == 20
    assert plan["estimated_duration_s"] == 45
    calls = [
        n
        for n in ast.walk(ast.parse(code))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "MissionItem"
    ]
    assert len(calls) == 1
    values = {
        k.arg: ast.literal_eval(k.value)
        for k in calls[0].keywords
        if isinstance(k.value, ast.Constant)
    }
    assert values["loiter_time_s"] == 20
    assert values["is_fly_through"] is False
    assert values["speed_m_s"] <= 3


def test_hover_minutes_are_converted():
    assert parse_mission_spec("hover for 0.5 minutes then land").hover_duration_s == 30


def test_impossible_hover_time_limit_is_rejected():
    agent = CodeGenerationAgent.__new__(CodeGenerationAgent)
    with pytest.raises(ValueError, match="exceeds max_duration_s"):
        agent._normalize_plan(PROMPT, {"max_duration_s": 30}, {})


@pytest.mark.asyncio
async def test_real_template_preflight_and_bounds_pass_static_check():
    _, code = generated()
    report = await SafeLLMSupervisionLayer(
        FlightConstraints(max_altitude_m=10, max_speed_ms=3, max_geofence_radius_m=30)
    ).full_safety_check(code)
    assert report.passed, report.all_issues
    assert not any(i.rule_id == "SR007" for i in report.all_issues)


@pytest.mark.asyncio
@pytest.mark.parametrize("change,rule", [("speed", "RR005"), ("preflight", "SR007")])
async def test_medium_violations_cannot_pass(change, rule):
    _, code = generated()
    if change == "speed":
        code = code.replace("speed_m_s=3.00", "speed_m_s=5.00")
    else:
        code = code.replace("await check_pre_flight(drone)", "pass # preflight removed")
    report = await SafeLLMSupervisionLayer(
        FlightConstraints(max_altitude_m=10, max_speed_ms=3, max_geofence_radius_m=30)
    ).full_safety_check(code)
    assert any(i.rule_id == rule for i in report.all_issues)
    assert not report.passed


@pytest.mark.asyncio
async def test_constraint_override_applies_to_static_altitude_check():
    # An explicit per-call bound must replace the analyser's constructor bound.
    code = "async def mission(drone):\n    await check_pre_flight(drone)\n    await drone.takeoff(altitude_m=50)\n    await drone.land()\n"
    report = await SafeLLMSupervisionLayer().full_safety_check(
        code, constraints=FlightConstraints(max_altitude_m=10)
    )
    assert any(i.rule_id == "SR004" for i in report.all_issues)
    assert not report.passed


@pytest.mark.asyncio
async def test_graph_safety_receives_mission_bounds(monkeypatch):
    from unittest.mock import Mock
    from graph.nodes import safety_check
    from agents.safety_layer import SafetyLayer

    _, code = generated()
    factory = Mock(wraps=SafetyLayer)
    monkeypatch.setattr("agents.safety_layer.SafetyLayer", factory)
    await safety_check(
        {
            "generated_code": code,
            "gsce_config": {
                "max_altitude_m": 10,
                "max_speed_ms": 3,
                "geofence_radius_m": 30,
            },
        }
    )
    assert factory.call_args.kwargs == dict(max_altitude=10, max_speed=3, max_geofence_radius=30)
