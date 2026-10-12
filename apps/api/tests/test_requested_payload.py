import pytest

from agents.design_agent import DesignAgent, PerformanceCalculator
from utils.mission_parser import parse_mission_spec


@pytest.mark.parametrize(
    "text,expected",
    [
        ("100 gram camera payload", 100),
        ("100 grams payload", 100),
        ("0.1 kg camera", 100),
        ("payload: 0.1 kilograms", 100),
        ("camera weight is 100 g", 100),
        ("30g fpv", 30),
        ("0g payload", 0),
        ("total weight under 800g", None),
        ("100g payload and 200g camera", None),
        ("-100g payload", None),
    ],
)
def test_explicit_payload_units(text, expected):
    assert parse_mission_spec(text).payload_g == expected


def test_repair_preserves_requested_payload():
    agent = DesignAgent.__new__(DesignAgent)
    agent.calculator = PerformanceCalculator()
    prompt = "quadcopter photography with a 100 gram camera payload, 5 minute flight"
    candidate = agent._rule_based_design(prompt)
    candidate.payload.installed_weight_g = 500
    candidate.propulsion = []  # Force the baseline repair path.
    repaired, _, _, _ = agent._select_best_design(prompt, candidate, "rag_llm")
    assert repaired.payload.installed_weight_g == 100
    assert candidate.payload.installed_weight_g == 500  # No mutation of input.


def test_unspecified_installed_mass_uses_full_load():
    agent = DesignAgent.__new__(DesignAgent)
    design = agent._rule_based_design("quadcopter")
    design.payload.max_weight_g = 100
    design.payload.installed_weight_g = None
    calculator = PerformanceCalculator()
    full = calculator.estimate(design)
    design.payload.installed_weight_g = 0
    empty = calculator.estimate(design)
    assert full.total_weight_g - empty.total_weight_g == pytest.approx(100)
