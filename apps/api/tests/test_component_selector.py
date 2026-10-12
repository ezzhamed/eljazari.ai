"""Phase 6 tests — component catalog, performance solver, and selector agent."""

from __future__ import annotations

import pytest

from agents.component_models import (
    SelectedComponents,
)
from agents.component_selector_agent import ComponentSelectorAgent
from agents.design_models import (
    BatterySpec,
    DroneDesignTree,
    ElectronicsSpec,
    FrameSpec,
    PayloadSpec,
)
from pipelines.performance_solver import PerformanceSolver
from services.component_catalog import ComponentCatalog, get_catalog


# ─────────────────────────────────────────────────────────────────────────────
# Catalog
# ─────────────────────────────────────────────────────────────────────────────


class TestComponentCatalog:
    def test_catalog_loads_real_components(self):
        cat = get_catalog()
        assert len(cat.motors) >= 5
        assert len(cat.propellers) >= 5
        assert len(cat.batteries) >= 4
        assert len(cat.escs) >= 4
        assert len(cat.flight_controllers) >= 3
        assert len(cat.frames) >= 4

    def test_lookup_by_part_number(self):
        cat = get_catalog()
        motor = cat.motor("tmotor_f80_pro_1900kv")
        assert motor.manufacturer == "T-Motor"
        assert motor.kv == 1900
        assert len(motor.thrust_curve) >= 4

    def test_unknown_part_raises(self):
        cat = get_catalog()
        with pytest.raises(KeyError):
            cat.motor("does_not_exist")

    def test_motor_thrust_curve_is_monotonic(self):
        cat = get_catalog()
        for motor in cat.motors:
            sorted_curve = sorted(motor.thrust_curve, key=lambda p: p.throttle_pct)
            for lo, hi in zip(sorted_curve, sorted_curve[1:]):
                assert hi.thrust_g >= lo.thrust_g, f"Non-monotonic thrust on {motor.part_number}"
                assert hi.current_a >= lo.current_a, f"Non-monotonic current on {motor.part_number}"

    def test_battery_pack_voltage_computed(self):
        cat = get_catalog()
        b = cat.battery("tattu_rline_1300_4s_95c")
        assert b.pack_voltage_nominal_v == pytest.approx(14.8, abs=0.01)
        assert b.pack_voltage_full_v == pytest.approx(16.8, abs=0.01)

    def test_battery_usable_energy_is_20pct_reserve(self):
        cat = get_catalog()
        b = cat.battery("tattu_1500_4s_75c")
        # 14.8V * 1.5Ah * 0.8 = 17.76 Wh
        assert b.usable_energy_wh == pytest.approx(17.76, rel=0.01)

    def test_propellers_filtered_by_motor_shaft(self):
        cat = get_catalog()
        motor = cat.motor("tmotor_f80_pro_1900kv")  # 5mm shaft
        props = cat.propellers_for_motor(motor)
        for p in props:
            assert abs(p.hub_id_mm - motor.shaft_diameter_mm) < 1.0

    def test_escs_filtered_for_motor_current(self):
        cat = get_catalog()
        motor = cat.motor("tmotor_mn3508_380kv")  # 14A continuous (180 s)
        escs = cat.escs_for_motor(motor, motor_count=4)
        for esc in escs:
            assert esc.max_continuous_current_per_channel_a >= motor.max_continuous_current_a * 1.2


# ─────────────────────────────────────────────────────────────────────────────
# Performance solver
# ─────────────────────────────────────────────────────────────────────────────


def _build_selected(cat: ComponentCatalog) -> SelectedComponents:
    return SelectedComponents(
        motor=cat.motor("tmotor_f80_pro_1900kv"),
        propeller=cat.propeller("hqprop_5x4_5x3_v1s"),
        battery=cat.battery("tattu_rline_1300_4s_95c"),
        esc=cat.esc("holybro_tekko32_f3_4in1"),
        flight_controller=cat.flight_controller("holybro_durandal_h7"),
        frame=cat.frame("iflight_nazgul_evoque_f5"),
        motor_count=4,
    )


class TestPerformanceSolver:
    def test_solver_runs_without_errors(self):
        cat = get_catalog()
        report = PerformanceSolver().solve(_build_selected(cat))
        assert report.total_mass_g > 0
        assert report.thrust_to_weight_ratio > 0

    def test_flyable_5inch_freestyle_quad(self):
        """T-Motor F80 + 5" prop + 1300mAh 4S = standard freestyle freestyle."""
        cat = get_catalog()
        report = PerformanceSolver().solve(_build_selected(cat))
        # Real-world T:W for this combo is ~5-7.
        assert report.thrust_to_weight_ratio >= 4.0
        assert report.thrust_to_weight_ratio <= 12.0
        assert report.is_flyable
        # Hover endurance for 1300mAh 4S with this combo is ~4-5 minutes.
        assert 2.5 <= report.hover_endurance_min <= 8.0

    def test_overloaded_drone_triggers_critical_warning(self):
        """Add 5kg payload — should fail T:W and emit critical warning."""
        cat = get_catalog()
        report = PerformanceSolver().solve(_build_selected(cat), extra_mass_g=5000.0)
        assert not report.is_flyable
        assert any(w.startswith("[critical]") for w in report.warnings)

    def test_disc_loading_increases_with_payload(self):
        cat = get_catalog()
        solver = PerformanceSolver()
        light = solver.solve(_build_selected(cat), extra_mass_g=0.0)
        heavy = solver.solve(_build_selected(cat), extra_mass_g=500.0)
        assert heavy.disc_loading_n_m2 > light.disc_loading_n_m2

    def test_hover_throttle_is_within_realistic_band(self):
        cat = get_catalog()
        report = PerformanceSolver().solve(_build_selected(cat))
        # A freestyle quad with T:W ~6 should hover at 25–45% throttle.
        assert 15.0 <= report.hover_throttle_pct <= 55.0

    def test_voltage_sag_present_at_burst(self):
        cat = get_catalog()
        report = PerformanceSolver().solve(_build_selected(cat))
        # Some sag is expected; should be > 0 but reasonable.
        assert 0.1 <= report.voltage_sag_at_max_throttle_v <= 6.0


# ─────────────────────────────────────────────────────────────────────────────
# Component selector agent
# ─────────────────────────────────────────────────────────────────────────────


def _design(arm_mm: float = 110.0, cells: int = 4) -> DroneDesignTree:
    return DroneDesignTree(
        topology="quadcopter",
        frame=FrameSpec(type="X", arm_length_mm=arm_mm),
        propulsion=[],
        battery=BatterySpec(type="LiPo", cells=cells, capacity_mah=1500, weight_g=190.0),
        payload=PayloadSpec(max_weight_g=0.0, mount_type="bottom"),
        electronics=ElectronicsSpec(flight_controller="PX4", gps=True, compass=True),
    )


class TestComponentSelectorAgent:
    def test_select_returns_only_feasible_tiers(self):
        agent = ComponentSelectorAgent()
        result = agent.select(_design())
        assert result.chosen is not None
        assert 0 <= len(result.alternatives) <= 2
        for tier in [result.chosen, *result.alternatives]:
            assert tier.performance.is_flyable
            assert tier.performance.cruise_endurance_min >= 5.0

    def test_chosen_tier_is_highest_score(self):
        agent = ComponentSelectorAgent()
        result = agent.select(_design())
        for alt in result.alternatives:
            assert result.chosen.score >= alt.score

    def test_economy_tier_is_cheaper_than_performance(self):
        agent = ComponentSelectorAgent()
        result = agent.select(_design())
        all_tiers = [result.chosen, *result.alternatives]
        eco = next((t for t in all_tiers if t.name == "economy"), None)
        perf = next((t for t in all_tiers if t.name == "performance"), None)
        # Economy total BOM price must be <= performance total BOM price.
        if eco and perf:
            assert eco.components.total_bom_price_usd <= perf.components.total_bom_price_usd

    def test_select_propagates_real_thrust_curve(self):
        agent = ComponentSelectorAgent()
        result = agent.select(_design())
        chosen = result.chosen
        assert len(chosen.components.motor.thrust_curve) >= 4
        # Performance numbers must be derived, not zero.
        assert chosen.performance.hover_throttle_pct > 0
        assert chosen.performance.hover_endurance_min > 0

    def test_a_design_size_no_documented_combination_fits_widens_with_a_note(self):
        """Run 3 (aborted): «a small quadcopter» got a frame only unsourced FPV motors
        fit, and SITL refused it. The design's size is an estimate; documented data
        decides, and the result says the frame search widened."""
        agent = ComponentSelectorAgent()
        for arm_mm in (32.0, 150.0):
            result = agent.select(_design(arm_mm=arm_mm), payload_g=150.0)
            assert result.chosen.performance.propulsion_evidence.status == "matched_declaration"
            assert len(result.notes) == 1 and "wheelbase" in result.notes[0]
        assert agent.select(_design(arm_mm=250.0), payload_g=150.0).notes == ()

    def test_a_requested_total_mass_bounds_the_search_widening_included(self):
        agent = ComponentSelectorAgent()
        capped = agent.select(_design(arm_mm=150.0), payload_g=150.0, max_total_mass_g=1000.0)
        assert capped.chosen.components.total_bom_weight_g + 150.0 <= 1000.0
        assert capped.notes == ()  # the documented X500 is too heavy to widen to
        with pytest.raises(RuntimeError, match="exceed the requested 250 g"):
            agent.select(_design(arm_mm=150.0), payload_g=150.0, max_total_mass_g=250.0)

    def test_without_documented_data_on_any_frame_nothing_widens(self):
        # Suite v2 en-hexacopter-spray: 3 kg on the only hexa frame is unsourced.
        design = _design(arm_mm=340.0, cells=6).model_copy(update={"topology": "hexacopter"})
        result = ComponentSelectorAgent().select(design, payload_g=3000.0)
        assert result.sourced_candidates == 0 and result.notes == ()
        assert result.chosen.performance.propulsion_evidence.status != "matched_declaration"

    def test_select_for_heavy_lift_picks_large_motor(self):
        agent = ComponentSelectorAgent()
        # Use 6S long-flight battery + big arm
        design = _design(arm_mm=300.0, cells=6)
        result = agent.select(design, payload_g=500.0)
        # Heavy-lift frame → low-KV high-thrust motor.
        assert result.chosen.components.motor.kv < 1500

    @pytest.mark.parametrize("topology", ["tricopter", "fixed_wing", "y6"])
    def test_an_unsupported_topology_is_refused_by_name(self, topology):
        # Run 3: a tricopter found «0 compatible candidates»; a fixed wing got quad parts.
        design = _design().model_copy(update={"topology": topology})
        with pytest.raises(RuntimeError, match="is not supported: generated aircraft are flat X"):
            ComponentSelectorAgent().select(design)

    def test_select_raises_when_no_components_match(self):
        agent = ComponentSelectorAgent()
        # 12S battery is not in the catalog.
        design = _design(cells=12)
        with pytest.raises(RuntimeError, match="No viable BOM"):
            agent.select(design)

    def test_chosen_tier_thrust_to_weight_acceptable(self):
        agent = ComponentSelectorAgent()
        result = agent.select(_design())
        # Selector should not pick a non-flyable BOM.
        assert result.chosen.performance.is_flyable
        assert result.chosen.performance.thrust_to_weight_ratio >= 1.5


# ─────────────────────────────────────────────────────────────────────────────
# Graph node integration
# ─────────────────────────────────────────────────────────────────────────────


class TestSelectComponentsNode:
    @pytest.mark.asyncio
    async def test_select_components_node_returns_bom(self):
        from graph.nodes import select_components

        design_tree = _design().model_dump()
        state = {
            "project_id": "test",
            "design_tree": design_tree,
        }
        result = await select_components(state)  # type: ignore[arg-type]

        assert result["bom_status"] == "selected"
        assert result["selected_components"]["motor"]["kv"] > 0
        assert result["real_performance"]["thrust_to_weight_ratio"] > 1.5
        assert all(
            tier["performance"]["cruise_endurance_min"] >= 5.0
            for tier in result["component_alternatives"]
        )
        assert result["current_phase"] == "select_components"

    @pytest.mark.asyncio
    async def test_select_components_node_fails_gracefully(self):
        from graph.nodes import select_components

        design = _design(cells=12).model_dump()
        state = {"project_id": "bad", "design_tree": design}
        result = await select_components(state)  # type: ignore[arg-type]

        assert result["bom_status"] == "failed"
        assert any("failed" in e.lower() for e in result["errors"])


# ─────────────────────────────────────────────────────────────────────────────
# Edge routing
# ─────────────────────────────────────────────────────────────────────────────


class TestRouteAfterSelectComponents:
    def test_selected_goes_to_urdf(self):
        from graph.edges import route_after_select_components

        assert route_after_select_components({"bom_status": "selected"}) == "convert_to_urdf"

    def test_failed_ends(self):
        from graph.edges import route_after_select_components

        assert route_after_select_components({"bom_status": "failed"}) == "end"
        assert route_after_select_components({"bom_status": "pending"}) == "end"

    def test_human_review_design_approved_routes_to_select_components(self):
        from graph.edges import route_after_human_review_design

        # Regression: previously routed straight to convert_to_urdf; now must
        # pass through select_components so URDF uses real component masses.
        assert route_after_human_review_design({"design_status": "approved"}) == "select_components"
