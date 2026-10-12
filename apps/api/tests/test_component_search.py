import pytest

from agents.component_selector_agent import ComponentSelectorAgent
from agents.design_agent import DesignAgent
from services.component_catalog import ComponentCatalog


def design():
    return DesignAgent.__new__(DesignAgent)._rule_based_design(
        "quadcopter photography with a 100 gram camera payload"
    )


def test_search_finds_feasible_combinations_beyond_three_guesses():
    result = ComponentSelectorAgent().select(design(), payload_g=100, min_endurance_min=5)
    assert result.evaluated_candidates > 3
    assert result.feasible_candidates > 0
    tiers = [result.chosen, *result.alternatives]
    identities = [ComponentSelectorAgent._identity(t.components) for t in tiers]
    assert len(set(identities)) == len(identities)
    for tier in tiers:
        assert tier.performance.cruise_endurance_min >= 5
        assert tier.performance.total_mass_g == pytest.approx(
            tier.components.total_bom_weight_g + 100
        )
        assert not any(w.startswith("[critical]") for w in tier.performance.warnings)


def test_search_is_independent_of_catalog_order():
    catalog = ComponentCatalog()
    first = ComponentSelectorAgent(catalog).select(design(), payload_g=100)
    for kind in ("motors", "propellers", "batteries", "escs", "flight_controllers", "frames"):
        getattr(catalog, kind).reverse()
    second = ComponentSelectorAgent(catalog).select(design(), payload_g=100)
    assert first.to_dict() == second.to_dict()


def test_search_limit_is_not_reported_as_infeasibility_or_partial_success():
    with pytest.raises(RuntimeError, match="search incomplete"):
        ComponentSelectorAgent(max_candidates=1).select(design(), payload_g=100)
