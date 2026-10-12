"""Requests are bound to what flies: extraction, feasibility and conformance.

Regression tests for the four findings of the first published benchmark
(docs/BENCHMARK.md). The prompts are the published suite's own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graph.edges import route_after_parse
from graph.nodes import parse_prompt
from pipelines.request_requirements import request_limit_violations
from pipelines.simulation_acceptance import assess_simulation
from tests.test_simulation_acceptance import good_state
from utils.mission_parser import parse_mission_spec

BENCHMARKS = Path(__file__).resolve().parents[3] / "benchmarks"
SUITE = BENCHMARKS / "prompt_suite_v2.json"
PROMPTS = {case["id"]: case["prompt"] for case in json.loads(SUITE.read_text("utf-8"))["cases"]}

# (topology, payload_g, total_g, altitude_m, endurance_min, hover_s, square_m)
EXPECTED = {
    "en-hover-camera": ("quadcopter", 150, None, 10, None, 20, None),
    "en-square-inspection": ("quadcopter", 300, None, 15, None, None, 20),
    "en-out-and-back": ("quadcopter", 100, None, 12, None, None, None),
    "ar-hover-camera": ("quadcopter", 200, None, 10, None, 20, None),
    "ar-square-farm": ("quadcopter", 250, None, 15, None, None, 25),
    "ar-out-and-back": ("quadcopter", 120, None, 12, None, None, None),
    "en-mass-paradox": ("quadcopter", 20000, 250, None, 120, None, None),
    "en-endurance": ("quadcopter", 2000, None, None, 300, None, None),
    "ar-heavy-lift": ("quadcopter", 50000, None, None, 240, None, None),
    "ar-altitude-limit": ("quadcopter", 200, None, 3000, None, None, None),
    "en-hexacopter-spray": ("hexacopter", 3000, None, 5, None, None, 20),
    "ar-octocopter-cinema": ("octocopter", 2000, None, 10, None, None, None),
    "en-fixed-wing-survey": ("fixed_wing", 200, None, 30, None, None, 100),
    "ar-tricopter-hover": ("tricopter", 150, None, 8, None, 15, None),
    "en-make-a-drone": (None, None, None, None, None, None, None),
    "ar-good-drone": (None, None, None, None, None, None, None),
}

LIMITS = {"max_altitude_m": 120.0, "min_altitude_m": 2.0, "max_geofence_radius_m": 500.0}


def test_suite_v2_keeps_every_v1_prompt_so_runs_stay_comparable():
    v1 = json.loads((BENCHMARKS / "prompt_suite_v1.json").read_text("utf-8"))
    v2 = json.loads(SUITE.read_text("utf-8"))
    v2_cases = {case["id"]: case for case in v2["cases"]}
    for case in v1["cases"]:
        assert v2_cases[case["id"]]["prompt"] == case["prompt"]
        assert v2_cases[case["id"]]["language"] == case["language"]
        # A changed category says why, in the suite itself.
        if v2_cases[case["id"]]["category"] != case["category"]:
            assert v2_cases[case["id"]]["change_from_v1"].startswith(f"v1: {case['category']}")
    assert set(PROMPTS) == set(EXPECTED)


@pytest.mark.parametrize("case_id", sorted(EXPECTED))
def test_suite_requirements_are_extracted_in_both_languages(case_id):
    spec = parse_mission_spec(PROMPTS[case_id])
    got = (
        spec.topology,
        spec.payload_g,
        spec.max_total_mass_g,
        spec.altitude_m,
        spec.duration_min,
        spec.hover_duration_s,
        spec.square_size_m,
    )
    assert got == EXPECTED[case_id]
    assert spec.unresolved == []


@pytest.mark.parametrize(
    "text,field,expected",
    [
        ("طائرة تحمل ٢٠٠ غرام", "payload_g", 200),  # Arabic-Indic digits
        ("طائرة تحمل 1٫5 كغ", "payload_g", 1500),  # Arabic decimal separator
        ("تحمل كاميرا وزنها 300 جرام", "payload_g", 300),
        ("مدى 2 كيلومتر", "payload_g", None),  # a distance, not a mass
        ("a drone with an electric motor", "topology", None),  # not «tri»
        ("orbit the building", "topology", None),  # not «bi»
        ("a quadratic path", "topology", None),
        ("fly to a point 30 m north and back", "altitude_m", None),
        ("fly at 40 m", "altitude_m", 40),
        ("climb to 0.5 km", "altitude_m", 500),
        ("لمدة ساعتين", "duration_min", 120),
        ("hover for 2 min", "hover_duration_s", 120),
        ("a 250 g drone carrying a camera", "payload_g", None),
        ("a 250 g drone carrying a camera", "max_total_mass_g", 250),
    ],
)
def test_extraction_edge_cases(text, field, expected):
    assert getattr(parse_mission_spec(text), field) == expected


def test_conflicting_payloads_are_reported_not_guessed():
    spec = parse_mission_spec("carries a 100 g camera and a 200 g sensor")
    assert spec.payload_g is None
    assert spec.unresolved == ["conflicting payload masses: 100 g, 200 g"]
    assert request_limit_violations(spec, **LIMITS) == [
        "the request states conflicting payload masses: 100 g, 200 g; state one value"
    ]


@pytest.mark.parametrize(
    "text,fragment",
    [
        (PROMPTS["ar-altitude-limit"], "3000 m exceeds the 120 m limit"),
        ("hover at 1 m for 10 seconds", "below the 2 m minimum"),
        ("fly a 400 m square at 20 m altitude", "beyond the 500 m geofence"),
    ],
)
def test_requests_beyond_limits_are_infeasible(text, fragment):
    violations = request_limit_violations(parse_mission_spec(text), **LIMITS)
    assert len(violations) == 1 and fragment in violations[0]


@pytest.mark.parametrize("case_id", [c for c in EXPECTED if c != "ar-altitude-limit"])
def test_other_suite_prompts_are_within_limits(case_id):
    assert request_limit_violations(parse_mission_spec(PROMPTS[case_id]), **LIMITS) == []


@pytest.mark.asyncio
async def test_parse_prompt_stops_an_infeasible_request_before_any_agent():
    update = await parse_prompt({"user_prompt": PROMPTS["ar-altitude-limit"], "project_id": "p"})
    assert update["current_phase"] == "error"
    assert "exceeds the 120 m limit" in update["errors"][0]
    assert route_after_parse(update) == "end"
    assert update["design_params"]["requirements"]["altitude_m"] == 3000


@pytest.mark.asyncio
async def test_parse_prompt_passes_requested_endurance_to_selection():
    update = await parse_prompt({"user_prompt": PROMPTS["ar-heavy-lift"], "project_id": "p"})
    assert update["current_phase"] == "parse_prompt"
    assert update["design_params"]["min_endurance_min"] == 240
    assert update["design_params"]["requirements"]["payload_g"] == 50000


def _flight(altitude_m: float) -> dict:
    """A hover whose code commands, and which flies, ``altitude_m``."""
    from tests.flight_fixtures import flight

    return flight(altitude_m=altitude_m)


def _generated(prompt: str, *, payload_g: float, topology: str = "quadcopter", **overrides):
    attestation = {
        "status": "matched",
        "resolved_spec_sha256": "s" * 64,
        "bundle_sha256": "b" * 64,
        "rotor_link_count": 4,
        "px4_params_checked": 30,
        "simulated_total_mass_kg": 0.9,
    }
    return good_state(
        user_prompt=prompt,
        resolved_drone_sha256="s" * 64,
        flight_model_bundle_sha256="b" * 64,
        simulation_model_attestation=attestation,
        resolved_drone_spec={"topology": topology, "payload_g": payload_g},
        **overrides,
    )


def test_matching_flight_passes_and_records_requested_versus_flown():
    state = _generated(PROMPTS["en-out-and-back"], payload_g=100, **_flight(12.4))
    verdict = assess_simulation(state)
    assert verdict["passed"], verdict["reasons"]
    assert verdict["requirements"]["requested"]["altitude_m"] == 12
    assert verdict["requirements"]["flown"] == {
        "topology": "quadcopter",
        "payload_g": 100,
        "total_mass_g": 900.0,
        "max_altitude_m": 12.4,
    }


def test_wrong_altitude_fails_and_code_may_retry():
    # The benchmark's English out-and-back runs flew 15 m for «12 m».
    state = _generated(PROMPTS["en-out-and-back"], payload_g=100, **_flight(15.3))
    verdict = assess_simulation(state)
    assert not verdict["passed"]
    assert any("the request asked for 12 m" in r for r in verdict["reasons"])
    assert verdict["retryable_with_code_change"] is True


@pytest.mark.parametrize(
    "prompt,kwargs,fragment",
    [
        (PROMPTS["ar-heavy-lift"], {"payload_g": 500}, "asked for 50000 g"),
        (PROMPTS["ar-octocopter-cinema"], {"payload_g": 2000}, "asked for a octocopter"),
        (PROMPTS["en-mass-paradox"], {"payload_g": 20000}, "limits it to 250 g"),
    ],
)
def test_a_different_aircraft_fails_and_no_code_change_can_fix_it(prompt, kwargs, fragment):
    verdict = assess_simulation(_generated(prompt, **_flight(10.0), **kwargs))
    assert not verdict["passed"]
    assert any(fragment in r for r in verdict["reasons"]), verdict["reasons"]
    assert verdict["retryable_with_code_change"] is False


def test_reference_aircraft_is_still_held_to_the_requested_altitude():
    state = good_state(user_prompt="hover at 10 m for 20 seconds", **_flight(15.0))
    verdict = assess_simulation(state)
    assert any("asked for 10 m" in r for r in verdict["reasons"])
    assert verdict["requirements"]["flown"]["payload_g"] is None


def test_code_agent_binds_the_requested_altitude_over_the_model_choice():
    from agents.code_agent import CodeGenerationAgent

    agent = CodeGenerationAgent.__new__(CodeGenerationAgent)
    plan = agent._normalize_plan(
        PROMPTS["ar-out-and-back"],
        {"max_altitude_m": 120.0, "max_speed_ms": 15.0, "geofence_radius_m": 500.0},
        {"takeoff_altitude_m": 27, "waypoints": [{"name": "out", "north_m": 30, "east_m": 0}]},
    )
    assert plan["altitude_m"] == 12


def test_arabic_hover_is_a_stationary_hover_at_the_requested_altitude():
    from agents.code_agent import CodeGenerationAgent

    agent = CodeGenerationAgent.__new__(CodeGenerationAgent)
    plan = agent._normalize_plan(PROMPTS["ar-hover-camera"], {"max_altitude_m": 120.0}, {})
    assert plan["mission_type"] == "stationary_hover"
    assert (plan["altitude_m"], plan["hold_time_s"]) == (10, 20)


def test_baseline_keeps_the_requested_topology():
    from agents.design_agent import DesignAgent

    agent = DesignAgent.__new__(DesignAgent)
    assert agent._rule_based_design(PROMPTS["en-square-inspection"]).topology == "quadcopter"
    assert agent._rule_based_design("inspection drone").topology == "octocopter"
    assert agent._rule_based_design(PROMPTS["ar-octocopter-cinema"]).topology == "octocopter"


def test_selection_never_pairs_a_curve_with_another_voltage():
    from agents.component_selector_agent import ComponentSelectorAgent
    from agents.design_agent import DesignAgent
    from pipelines.propulsion_evidence import curve_matches_battery

    design = DesignAgent.__new__(DesignAgent)._rule_based_design(PROMPTS["ar-out-and-back"])
    result = ComponentSelectorAgent().select(design, payload_g=120.0)
    for tier in (result.chosen, *result.alternatives):
        parts = tier.components
        assert curve_matches_battery(
            parts.motor.thrust_curve, parts.battery.pack_voltage_nominal_v
        ), (parts.motor.part_number, parts.battery.part_number)
