"""Tests for Design Agent (Phase 2.3)

Coverage:
- Pydantic model validation (design_models.py)
- PerformanceCalculator physics estimates
- AircraftVerseRAG synthetic data loading
- DesignAgent._parse_design (JSON extraction)
- DesignAgent._rule_based_design (keyword routing)
- DesignAgent._build_prompt (RAG context injection)
- DesignAgent.run() full pipeline (mocked LLM + RAG)
- Edge cases: bad JSON, missing keys, feedback, English prompts
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.design_models import (
    BatterySpec,
    CenterBody,
    DesignResult,
    DroneDesignTree,
    ElectronicsSpec,
    FrameSpec,
    MotorSpec,
    PayloadSpec,
    PerformanceEstimate,
)
from agents.design_agent import (
    MOTOR_POSITIONS,
    TOPOLOGY_MOTORS,
    AircraftVerseRAG,
    DesignAgent,
    PerformanceCalculator,
)


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def quad_design() -> DroneDesignTree:
    return DroneDesignTree(
        topology="quadcopter",
        frame=FrameSpec(
            type="X", arm_length_mm=250, center_body=CenterBody(length=180, width=120, height=45)
        ),
        propulsion=[
            MotorSpec(motor="2206-2300KV", propeller="5045", position=p)
            for p in ["front_left", "front_right", "rear_left", "rear_right"]
        ],
        battery=BatterySpec(type="LiPo", cells=4, capacity_mah=5000, weight_g=480),
        payload=PayloadSpec(max_weight_g=500),
        electronics=ElectronicsSpec(flight_controller="PX4"),
    )


@pytest.fixture
def hex_design() -> DroneDesignTree:
    return DroneDesignTree(
        topology="hexacopter",
        frame=FrameSpec(type="X", arm_length_mm=380, center_body=CenterBody()),
        propulsion=[
            MotorSpec(motor="4114-320KV", propeller="1555", position=p)
            for p in MOTOR_POSITIONS["hexacopter"]
        ],
        battery=BatterySpec(type="LiPo", cells=6, capacity_mah=10000, weight_g=950),
        payload=PayloadSpec(max_weight_g=2000),
        electronics=ElectronicsSpec(flight_controller="PX4"),
    )


@pytest.fixture
def calculator() -> PerformanceCalculator:
    return PerformanceCalculator()


# ─────────────────────────────────────────────────────────────
# 1. Pydantic model tests
# ─────────────────────────────────────────────────────────────


class TestDesignModels:
    def test_center_body_defaults(self):
        cb = CenterBody()
        assert cb.length == 180.0
        assert cb.width == 120.0
        assert cb.height == 45.0

    def test_frame_spec_defaults(self):
        fs = FrameSpec()
        assert fs.type == "X"
        assert fs.arm_length_mm == 250.0
        assert isinstance(fs.center_body, CenterBody)

    def test_motor_spec_fields(self):
        m = MotorSpec(motor="2206-2300KV", propeller="5045", position="front_left")
        assert m.motor == "2206-2300KV"
        assert m.propeller == "5045"
        assert m.position == "front_left"

    def test_battery_spec_cells_validation(self):
        b = BatterySpec(cells=6, capacity_mah=10000, weight_g=900)
        assert b.cells == 6

    def test_battery_spec_cells_ge_1(self):
        with pytest.raises(Exception):
            BatterySpec(cells=0, capacity_mah=1000, weight_g=100)

    def test_battery_spec_cells_le_12(self):
        with pytest.raises(Exception):
            BatterySpec(cells=13, capacity_mah=1000, weight_g=100)

    def test_battery_capacity_positive(self):
        with pytest.raises(Exception):
            BatterySpec(cells=4, capacity_mah=0, weight_g=100)

    def test_drone_design_tree_defaults(self):
        d = DroneDesignTree()
        assert d.topology == "quadcopter"
        assert len(d.propulsion) == 0

    def test_drone_design_tree_full(self, quad_design):
        assert quad_design.topology == "quadcopter"
        assert len(quad_design.propulsion) == 4

    def test_performance_estimate_fields(self):
        pe = PerformanceEstimate(
            hover_time_s=1200,
            max_speed_ms=15,
            max_distance_m=5000,
            total_weight_g=1850,
            thrust_to_weight_ratio=2.1,
        )
        assert pe.hover_time_s == 1200
        assert pe.thrust_to_weight_ratio == 2.1

    def test_design_result_defaults(self, quad_design):
        perf = PerformanceEstimate(
            hover_time_s=600,
            max_speed_ms=12,
            max_distance_m=3000,
            total_weight_g=1500,
            thrust_to_weight_ratio=2.0,
        )
        dr = DesignResult(design_tree=quad_design, performance_estimate=perf)
        assert dr.similar_designs_count == 0
        assert dr.generation_method == "rag_llm"
        assert dr.raw_context == []


# ─────────────────────────────────────────────────────────────
# 2. PerformanceCalculator tests
# ─────────────────────────────────────────────────────────────


class TestPerformanceCalculator:
    def test_quad_hover_time_accounts_for_full_payload(self, calculator, quad_design):
        perf = calculator.estimate(quad_design)
        half_load = quad_design.model_copy(deep=True)
        half_load.payload.installed_weight_g = quad_design.payload.max_weight_g / 2
        half_perf = calculator.estimate(half_load)
        assert 0 < perf.hover_time_s < half_perf.hover_time_s
        assert perf.total_weight_g - half_perf.total_weight_g == pytest.approx(250)

    def test_quad_twr_above_1(self, calculator, quad_design):
        perf = calculator.estimate(quad_design)
        assert perf.thrust_to_weight_ratio > 1.0

    def test_quad_speed_positive(self, calculator, quad_design):
        perf = calculator.estimate(quad_design)
        assert perf.max_speed_ms > 0

    def test_quad_distance_positive(self, calculator, quad_design):
        perf = calculator.estimate(quad_design)
        assert perf.max_distance_m > 0

    def test_hex_heavier_than_quad(self, calculator, quad_design, hex_design):
        quad_perf = calculator.estimate(quad_design)
        hex_perf = calculator.estimate(hex_design)
        assert hex_perf.total_weight_g > quad_perf.total_weight_g

    def test_parse_kv_standard(self, calculator):
        assert calculator._parse_kv("2206-2300KV") == 2300

    def test_parse_kv_lowercase(self, calculator):
        assert calculator._parse_kv("4114-320kv") == 320

    def test_parse_kv_fallback(self, calculator):
        assert calculator._parse_kv("unknown_motor") == 2300

    def test_parse_prop_size_5inch(self, calculator):
        assert calculator._parse_prop_size("5045") == 5.0

    def test_parse_prop_size_15inch(self, calculator):
        assert calculator._parse_prop_size("1555") == 15.0

    def test_max_speed_capped_45(self, calculator):
        # Very large prop should not exceed 45 m/s cap
        design = DroneDesignTree(
            topology="quadcopter",
            propulsion=[MotorSpec(motor="100-200KV", propeller="99xx", position="front_left")],
        )
        perf = calculator.estimate(design)
        assert perf.max_speed_ms <= 45.0

    def test_empty_propulsion_uses_topology_default(self, calculator):
        design = DroneDesignTree(topology="hexacopter", propulsion=[])
        perf = calculator.estimate(design)
        assert perf.total_weight_g > 0

    def test_hover_time_scales_with_capacity(self, calculator):
        small_bat = DroneDesignTree(
            topology="quadcopter",
            propulsion=[MotorSpec(motor="2206-2300KV", propeller="5045", position="front_left")]
            * 4,
            battery=BatterySpec(cells=4, capacity_mah=1500, weight_g=200),
        )
        large_bat = DroneDesignTree(
            topology="quadcopter",
            propulsion=[MotorSpec(motor="2206-2300KV", propeller="5045", position="front_left")]
            * 4,
            battery=BatterySpec(cells=4, capacity_mah=8000, weight_g=600),
        )
        small_perf = calculator.estimate(small_bat)
        large_perf = calculator.estimate(large_bat)
        assert large_perf.hover_time_s > small_perf.hover_time_s


# ─────────────────────────────────────────────────────────────
# 3. AircraftVerseRAG tests
# ─────────────────────────────────────────────────────────────


class TestAircraftVerseRAG:
    def test_synthetic_designs_returns_documents(self):
        from langchain_core.documents import Document

        docs = AircraftVerseRAG._synthetic_designs(Document)
        assert len(docs) == 5
        assert all(hasattr(d, "page_content") for d in docs)

    def test_synthetic_designs_have_metadata(self):
        from langchain_core.documents import Document

        docs = AircraftVerseRAG._synthetic_designs(Document)
        for d in docs:
            assert "topology" in d.metadata

    def test_design_to_text_quadcopter(self):
        d = {
            "topology": "quadcopter",
            "use_case": "photography",
            "frame": {"arm_length_mm": 250, "type": "X"},
            "battery": {"cells": 4, "capacity_mah": 5000},
            "propulsion": [{}, {}, {}, {}],
            "payload": {"max_weight_g": 500},
        }
        text = AircraftVerseRAG._design_to_text(d)
        assert "quadcopter" in text
        assert "250" in text

    def test_design_to_text_empty_dict(self):
        text = AircraftVerseRAG._design_to_text({})
        assert isinstance(text, str)

    @pytest.mark.asyncio
    async def test_retrieve_returns_empty_without_vectorstore(self):
        rag = AircraftVerseRAG(MagicMock(), mode="memory")
        rag._initialized = True
        rag._vectorstore = None
        result = await rag.retrieve("quadcopter photography")
        assert result == []

    @pytest.mark.asyncio
    async def test_retrieve_handles_vectorstore_exception(self):
        rag = AircraftVerseRAG(MagicMock(), mode="memory")
        rag._initialized = True
        mock_vs = MagicMock()
        mock_vs.similarity_search.side_effect = RuntimeError("db error")
        rag._vectorstore = mock_vs
        result = await rag.retrieve("test query")
        assert result == []

    @pytest.mark.asyncio
    async def test_initialize_skips_if_already_done(self):
        rag = AircraftVerseRAG(MagicMock(), mode="memory")
        rag._initialized = True
        await rag.initialize()  # should not raise or reset
        assert rag._initialized is True


# ─────────────────────────────────────────────────────────────
# 4. DesignAgent._parse_design tests
# ─────────────────────────────────────────────────────────────


class TestDesignAgentParsing:
    """Tests for JSON extraction from LLM responses."""

    @pytest.fixture
    def agent(self):
        with patch("agents.design_agent.OllamaLLM"), patch("agents.design_agent.OllamaEmbeddings"):
            return DesignAgent()

    def _quad_json(self) -> str:
        return json.dumps(
            {
                "topology": "quadcopter",
                "frame": {
                    "type": "X",
                    "arm_length_mm": 250,
                    "center_body": {"length": 180, "width": 120, "height": 45},
                },
                "propulsion": [
                    {"motor": "2206-2300KV", "propeller": "5045", "position": pos}
                    for pos in ["front_left", "front_right", "rear_left", "rear_right"]
                ],
                "battery": {"type": "LiPo", "cells": 4, "capacity_mah": 5000, "weight_g": 480},
                "payload": {"max_weight_g": 500, "mount_type": "bottom"},
                "electronics": {"flight_controller": "PX4", "gps": True, "compass": True},
            }
        )

    def test_parse_clean_json(self, agent):
        design = agent._parse_design(self._quad_json())
        assert design.topology == "quadcopter"
        assert len(design.propulsion) == 4

    def test_parse_json_in_markdown_fence(self, agent):
        wrapped = f"```json\n{self._quad_json()}\n```"
        design = agent._parse_design(wrapped)
        assert design.topology == "quadcopter"

    def test_parse_json_with_preamble(self, agent):
        text = f"Here is the design:\n{self._quad_json()}\nDone."
        design = agent._parse_design(text)
        assert design.topology == "quadcopter"

    def test_parse_motors_alias(self, agent):
        """LLM sometimes uses 'motors' instead of 'propulsion'."""
        d = json.loads(self._quad_json())
        d["motors"] = d.pop("propulsion")
        design = agent._parse_design(json.dumps(d))
        assert len(design.propulsion) == 4

    def test_parse_invalid_json_raises(self, agent):
        with pytest.raises(Exception):
            agent._parse_design("not json at all")

    def test_parse_hexacopter(self, agent):
        d = {
            "topology": "hexacopter",
            "frame": {"type": "X", "arm_length_mm": 380, "center_body": {}},
            "propulsion": [
                {"motor": "4114-320KV", "propeller": "1555", "position": p}
                for p in MOTOR_POSITIONS["hexacopter"]
            ],
            "battery": {"cells": 6, "capacity_mah": 10000, "weight_g": 950},
            "payload": {"max_weight_g": 2000},
            "electronics": {"flight_controller": "PX4"},
        }
        design = agent._parse_design(json.dumps(d))
        assert design.topology == "hexacopter"
        assert len(design.propulsion) == 6

    def test_parse_collapsed_position_lists_expands_to_topology_count(self, agent):
        d = {
            "topology": "quadcopter",
            "frame": {"type": "X", "arm_length_mm": 320, "center_body": {}},
            "propulsion": [
                {
                    "motor": "2814-700KV",
                    "propeller": "1045",
                    "position": "['front_left', 'back_right']",
                },
                {
                    "motor": "2814-700KV",
                    "propeller": "1045",
                    "position": "['front_right', 'back_left']",
                },
            ],
            "battery": {"cells": 4, "capacity_mah": 8000, "weight_g": 720},
            "payload": {"max_weight_g": 700, "mount_type": "gimbal"},
            "electronics": {"flight_controller": "PX4", "gps": True, "compass": True},
        }
        design = agent._parse_design(json.dumps(d))
        assert len(design.propulsion) == 4
        assert {motor.position for motor in design.propulsion} == {
            "front_left",
            "front_right",
            "rear_left",
            "rear_right",
        }


# ─────────────────────────────────────────────────────────────
# 5. DesignAgent._rule_based_design tests
# ─────────────────────────────────────────────────────────────


class TestRuleBasedFallback:
    @pytest.fixture
    def agent(self):
        with patch("agents.design_agent.OllamaLLM"), patch("agents.design_agent.OllamaEmbeddings"):
            return DesignAgent()

    def test_cargo_becomes_hexacopter(self, agent):
        d = agent._rule_based_design("drone for heavy cargo delivery")
        assert d.topology == "hexacopter"
        assert len(d.propulsion) == 6

    def test_racing_becomes_quadcopter(self, agent):
        d = agent._rule_based_design("fast racing fpv drone")
        assert d.topology == "quadcopter"
        assert d.battery.capacity_mah == 2200

    def test_industrial_becomes_octocopter(self, agent):
        d = agent._rule_based_design("industrial inspection octocopter")
        assert d.topology == "octocopter"
        assert len(d.propulsion) == 8

    def test_default_is_quadcopter(self, agent):
        d = agent._rule_based_design("take photos from the sky")
        assert d.topology == "quadcopter"
        assert d.electronics.flight_controller == "PX4"

    def test_long_range_keyword(self, agent):
        d = agent._rule_based_design("long range survey mapping drone")
        # should pick long-range quad
        assert d.topology == "quadcopter"
        assert d.battery.capacity_mah >= 8000

    def test_cargo_prompt(self, agent):
        d = agent._rule_based_design("drone for heavy cargo transport")
        assert d.topology == "hexacopter"

    def test_propulsion_count_matches_topology(self, agent):
        for kw, expected_topo in [
            ("cargo delivery", "hexacopter"),
            ("fpv racing", "quadcopter"),
            ("industrial inspection octocopter", "octocopter"),
        ]:
            d = agent._rule_based_design(kw)
            assert len(d.propulsion) == TOPOLOGY_MOTORS[d.topology], (
                f"Mismatch for '{kw}': {len(d.propulsion)} motors but topology is {d.topology}"
            )


# ─────────────────────────────────────────────────────────────
# 6. DesignAgent._build_prompt tests
# ─────────────────────────────────────────────────────────────


class TestBuildPrompt:
    @pytest.fixture
    def agent(self):
        with patch("agents.design_agent.OllamaLLM"), patch("agents.design_agent.OllamaEmbeddings"):
            return DesignAgent()

    def test_prompt_contains_user_request(self, agent):
        p = agent._build_prompt("aerial photography drone", [], "")
        assert "aerial photography drone" in p

    def test_prompt_contains_rag_context(self, agent):
        similar = [{"topology": "quadcopter", "use_case": "photography"}]
        p = agent._build_prompt("photography drone", similar, "")
        assert "AircraftVerse" in p
        assert "quadcopter" in p

    def test_prompt_contains_feedback(self, agent):
        p = agent._build_prompt("delivery drone", [], "arm_length should be 400mm")
        assert "arm_length should be 400mm" in p

    def test_prompt_no_rag_no_context_block(self, agent):
        p = agent._build_prompt("test", [], "")
        assert "Reference" not in p

    def test_prompt_contains_json_template(self, agent):
        p = agent._build_prompt("test", [], "")
        assert '"topology"' in p


# ─────────────────────────────────────────────────────────────
# 7. DesignAgent.run() integration tests (mocked LLM + RAG)
# ─────────────────────────────────────────────────────────────


class TestDesignAgentRun:
    _QUAD_JSON = json.dumps(
        {
            "topology": "quadcopter",
            "frame": {
                "type": "X",
                "arm_length_mm": 250,
                "center_body": {"length": 180, "width": 120, "height": 45},
            },
            "propulsion": [
                {"motor": "2206-2300KV", "propeller": "5045", "position": pos}
                for pos in ["front_left", "front_right", "rear_left", "rear_right"]
            ],
            "battery": {"type": "LiPo", "cells": 4, "capacity_mah": 5000, "weight_g": 480},
            "payload": {"max_weight_g": 500, "mount_type": "bottom"},
            "electronics": {"flight_controller": "PX4", "gps": True, "compass": True},
        }
    )

    @pytest.fixture
    def agent(self):
        with (
            patch("agents.design_agent.OllamaLLM") as MockLLM,
            patch("agents.design_agent.OllamaEmbeddings"),
        ):
            instance = MockLLM.return_value
            instance.ainvoke = AsyncMock(return_value=self._QUAD_JSON)
            a = DesignAgent()
            # _retrieve_similar() now calls retrieve_filtered(); mock both for safety
            a.rag.retrieve_filtered = AsyncMock(return_value=[])
            a.rag.retrieve = AsyncMock(return_value=[])
            return a

    @pytest.mark.asyncio
    async def test_run_returns_design_result(self, agent):
        result = await agent.run("take aerial photos")
        assert isinstance(result, DesignResult)

    @pytest.mark.asyncio
    async def test_run_design_tree_topology(self, agent):
        result = await agent.run("take aerial photos")
        assert result.design_tree.topology == "quadcopter"

    @pytest.mark.asyncio
    async def test_run_performance_estimate_exists(self, agent):
        result = await agent.run("take aerial photos")
        assert result.performance_estimate.hover_time_s > 0

    @pytest.mark.asyncio
    async def test_run_with_feedback(self, agent):
        result = await agent.run("delivery drone", feedback="increase arm length to 380mm")
        assert isinstance(result, DesignResult)

    @pytest.mark.asyncio
    async def test_run_llm_failure_uses_fallback(self):
        with (
            patch("agents.design_agent.OllamaLLM") as MockLLM,
            patch("agents.design_agent.OllamaEmbeddings"),
        ):
            instance = MockLLM.return_value
            instance.ainvoke = AsyncMock(side_effect=RuntimeError("LLM offline"))
            agent = DesignAgent()
            agent.rag.retrieve = AsyncMock(return_value=[])
            # The run retrieves through retrieve_filtered; unmocked, it embedded
            # the whole corpus for real (two minutes) in a test about the LLM.
            agent.rag.retrieve_filtered = AsyncMock(return_value=[])
            result = await agent.run("photography drone")
            assert result.generation_method == "rule_based"
            assert isinstance(result.design_tree, DroneDesignTree)

    @pytest.mark.asyncio
    async def test_run_rag_similar_count(self):
        with (
            patch("agents.design_agent.OllamaLLM") as MockLLM,
            patch("agents.design_agent.OllamaEmbeddings"),
        ):
            instance = MockLLM.return_value
            instance.ainvoke = AsyncMock(return_value=self._QUAD_JSON)
            agent = DesignAgent()
            # _retrieve_similar() now calls retrieve_filtered(), not retrieve()
            agent.rag.retrieve_filtered = AsyncMock(
                return_value=[{"topology": "quadcopter"}, {"topology": "hexacopter"}]
            )
            result = await agent.run("cargo drone")
            assert result.similar_designs_count == 2

    @pytest.mark.asyncio
    async def test_run_rag_method_is_rag_llm_when_similar_found(self):
        with (
            patch("agents.design_agent.OllamaLLM") as MockLLM,
            patch("agents.design_agent.OllamaEmbeddings"),
        ):
            instance = MockLLM.return_value
            instance.ainvoke = AsyncMock(return_value=self._QUAD_JSON)
            agent = DesignAgent()
            agent.rag.retrieve_filtered = AsyncMock(return_value=[{"topology": "quadcopter"}])
            result = await agent.run("drone")
            assert result.generation_method == "rag_llm"

    @pytest.mark.asyncio
    async def test_run_method_is_llm_only_when_rag_empty(self, agent):
        result = await agent.run("drone")
        assert result.generation_method == "llm_only"

    @pytest.mark.asyncio
    async def test_run_rag_error_falls_back_gracefully(self):
        with (
            patch("agents.design_agent.OllamaLLM") as MockLLM,
            patch("agents.design_agent.OllamaEmbeddings"),
        ):
            instance = MockLLM.return_value
            instance.ainvoke = AsyncMock(return_value=self._QUAD_JSON)
            agent = DesignAgent()
            # Both retrieve_filtered and retrieve should fail to test full fallback
            agent.rag.retrieve_filtered = AsyncMock(side_effect=RuntimeError("rag crash"))
            agent.rag.retrieve = AsyncMock(side_effect=RuntimeError("rag crash"))
            result = await agent.run("drone")
            assert isinstance(result, DesignResult)

    @pytest.mark.asyncio
    async def test_run_photography_prompt(self, agent):
        result = await agent.run("drone for professional aerial photography")
        assert isinstance(result, DesignResult)

    @pytest.mark.asyncio
    async def test_run_raw_context_limited_to_3(self):
        with (
            patch("agents.design_agent.OllamaLLM") as MockLLM,
            patch("agents.design_agent.OllamaEmbeddings"),
        ):
            instance = MockLLM.return_value
            instance.ainvoke = AsyncMock(return_value=self._QUAD_JSON)
            agent = DesignAgent()
            # _retrieve_similar() calls retrieve_filtered(); simulate 5 designs
            agent.rag.retrieve_filtered = AsyncMock(
                return_value=[{"topology": f"t{i}"} for i in range(5)]
            )
            result = await agent.run("test")
            assert len(result.raw_context) <= 3


# ─────────────────────────────────────────────────────────────
# RAG Use-Case Filtering Tests
# ─────────────────────────────────────────────────────────────


class TestExtractPromptFilters:
    """Unit tests for DesignAgent._extract_prompt_filters()."""

    def _extract(self, prompt: str):
        return DesignAgent._extract_prompt_filters(prompt)

    # ── use_case detection ────────────────────────────────────

    def test_racing_keywords(self):
        use_case, topology = self._extract("Design a fast FPV racing drone")
        assert use_case == "racing FPV high speed"
        assert topology == "quadcopter"  # auto-inferred for racing

    def test_fpv_keyword(self):
        use_case, _ = self._extract("I want an FPV freestyle quad")
        assert use_case == "racing FPV high speed"

    def test_photography_is_unmapped(self):
        """The corpus has no photography designs, so no filter is better than one.

        Filtering on a label absent from the metadata matches zero documents;
        returning None leaves the query unfiltered and still retrieves
        something useful.
        """
        use_case, topology = self._extract("Aerial photography drone with gimbal")
        assert use_case is None
        assert topology is None

    def test_industrial_inspection(self):
        use_case, _ = self._extract("Drone for industrial inspection of pipelines")
        assert use_case == "industrial inspection"

    def test_cargo_delivery(self):
        use_case, _ = self._extract("Package delivery cargo drone")
        assert use_case == "cargo delivery"

    def test_long_range(self):
        use_case, _ = self._extract("Long range surveillance drone with 60min endurance")
        assert use_case == "fixed-wing VTOL long range"

    def test_search_rescue_is_unmapped(self):
        """No search-and-rescue designs exist in the corpus either."""
        use_case, _ = self._extract("Search and rescue SAR drone")
        assert use_case is None

    # ── topology detection ────────────────────────────────────

    def test_hexacopter_explicit(self):
        _, topology = self._extract("Design a hexacopter for payload")
        assert topology == "hexacopter"

    def test_quadcopter_explicit(self):
        _, topology = self._extract("Build a quadcopter for racing")
        assert topology == "quadcopter"

    def test_octocopter_explicit(self):
        _, topology = self._extract("Heavy lift octocopter")
        assert topology == "octocopter"

    def test_fixed_wing(self):
        _, topology = self._extract("Fixed wing airplane for surveillance")
        assert topology == "fixed_wing"

    # ── combined filters ──────────────────────────────────────

    def test_racing_quad_combined(self):
        use_case, topology = self._extract("lightweight racing quadcopter for FPV competition")
        assert use_case == "racing FPV high speed"
        assert topology == "quadcopter"

    def test_photography_hexa_combined(self):
        """Topology still resolves even when the use case has no corpus match."""
        use_case, topology = self._extract("hexacopter for professional cinema photography")
        assert use_case is None
        assert topology == "hexacopter"

    # ── no keywords → both None ───────────────────────────────

    def test_no_keywords(self):
        use_case, topology = self._extract("Build a drone")
        assert use_case is None
        assert topology is None

    def test_case_insensitive(self):
        use_case, topology = self._extract("RACING FPV QUADCOPTER")
        assert use_case == "racing FPV high speed"
        assert topology == "quadcopter"


class TestAircraftVerseRAGFiltered:
    """Unit tests for AircraftVerseRAG.retrieve_filtered()."""

    @pytest.mark.asyncio
    async def test_retrieve_filtered_uses_where_clause(self):
        """retrieve_filtered() should call similarity_search with the correct filter."""
        rag = AircraftVerseRAG(embeddings=MagicMock(), mode="memory")
        rag._initialized = True

        mock_vs = MagicMock()
        mock_vs.similarity_search.return_value = [
            MagicMock(metadata={"topology": "quadcopter", "use_case": "racing"})
        ]
        rag._vectorstore = mock_vs

        results = await rag.retrieve_filtered(
            "fast racing quad", use_case="racing", topology="quadcopter", k=3
        )

        # Verify filter was applied
        call_kwargs = mock_vs.similarity_search.call_args.kwargs
        assert "filter" in call_kwargs
        assert call_kwargs["filter"] == {
            "$and": [
                {"use_case": {"$eq": "racing"}},
                {"topology": {"$eq": "quadcopter"}},
            ]
        }
        assert len(results) == 1
        assert results[0]["use_case"] == "racing"

    @pytest.mark.asyncio
    async def test_retrieve_filtered_single_filter(self):
        """Single filter should use plain dict, not $and."""
        rag = AircraftVerseRAG(embeddings=MagicMock(), mode="memory")
        rag._initialized = True
        mock_vs = MagicMock()
        mock_vs.similarity_search.return_value = [
            MagicMock(metadata={"topology": "quadcopter", "use_case": "racing"})
        ]
        rag._vectorstore = mock_vs

        # fallback_unfiltered=False so only one call is made and call_args is reliable
        await rag.retrieve_filtered("query", use_case="racing", k=3, fallback_unfiltered=False)

        call_kwargs = mock_vs.similarity_search.call_args.kwargs
        assert "filter" in call_kwargs
        assert call_kwargs["filter"] == {"use_case": {"$eq": "racing"}}

    @pytest.mark.asyncio
    async def test_retrieve_filtered_fallback_on_empty(self):
        """When filtered search returns 0 results, it retries unfiltered."""
        rag = AircraftVerseRAG(embeddings=MagicMock(), mode="memory")
        rag._initialized = True
        mock_vs = MagicMock()
        # First call (filtered) returns empty; second call (unfiltered) returns result
        mock_vs.similarity_search.side_effect = [
            [],  # filtered → empty
            [MagicMock(metadata={"topology": "hexacopter"})],  # unfiltered fallback
        ]
        rag._vectorstore = mock_vs

        results = await rag.retrieve_filtered("query", use_case="racing", fallback_unfiltered=True)

        assert mock_vs.similarity_search.call_count == 2
        assert len(results) == 1
        assert results[0]["topology"] == "hexacopter"

    @pytest.mark.asyncio
    async def test_retrieve_filtered_no_fallback(self):
        """When fallback_unfiltered=False, empty filtered results stay empty."""
        rag = AircraftVerseRAG(embeddings=MagicMock(), mode="memory")
        rag._initialized = True
        mock_vs = MagicMock()
        mock_vs.similarity_search.return_value = []
        rag._vectorstore = mock_vs

        results = await rag.retrieve_filtered("query", use_case="racing", fallback_unfiltered=False)

        assert mock_vs.similarity_search.call_count == 1
        assert results == []

    @pytest.mark.asyncio
    async def test_retrieve_filtered_no_filter(self):
        """No filter arguments → no where clause passed to similarity_search."""
        rag = AircraftVerseRAG(embeddings=MagicMock(), mode="memory")
        rag._initialized = True
        mock_vs = MagicMock()
        mock_vs.similarity_search.return_value = []
        rag._vectorstore = mock_vs

        await rag.retrieve_filtered("query", k=5)

        # Should not have a filter kwarg
        call_kwargs = mock_vs.similarity_search.call_args.kwargs
        assert "filter" not in call_kwargs


class TestBuildableBaselineFilter:
    """The real AircraftVerse corpus mixes fixed-wing hybrids, unsupported motor
    counts, and designs whose physics run produced zeroed metrics. Retrieval
    filters those out by default — see data/aircraftverse/README.md."""

    @staticmethod
    def _rag(with_provenance: bool = True):
        rag = AircraftVerseRAG(embeddings=MagicMock(), mode="memory")
        rag._initialized = True
        rag._has_provenance_fields = with_provenance
        mock_vs = MagicMock()
        mock_vs.similarity_search.return_value = [MagicMock(metadata={"topology": "quadcopter"})]
        rag._vectorstore = mock_vs
        return rag, mock_vs

    @pytest.mark.asyncio
    async def test_retrieve_applies_buildable_baseline(self):
        rag, mock_vs = self._rag()

        await rag.retrieve("inspection quad")

        assert mock_vs.similarity_search.call_args.kwargs["filter"] == {
            "$and": [
                {"airframe_class": {"$eq": "multirotor"}},
                {"evaluated": {"$eq": True}},
            ]
        }

    @pytest.mark.asyncio
    async def test_buildable_only_false_disables_baseline(self):
        rag, mock_vs = self._rag()

        await rag.retrieve("inspection quad", buildable_only=False)

        assert "filter" not in mock_vs.similarity_search.call_args.kwargs

    @pytest.mark.asyncio
    async def test_baseline_skipped_for_corpora_without_provenance(self):
        """Older corpora carry no airframe_class — filtering would match nothing."""
        rag, mock_vs = self._rag(with_provenance=False)

        await rag.retrieve("inspection quad")

        assert "filter" not in mock_vs.similarity_search.call_args.kwargs

    @pytest.mark.asyncio
    async def test_baseline_combines_with_semantic_filters(self):
        rag, mock_vs = self._rag()

        await rag.retrieve_filtered(
            "racing quad", use_case="racing", topology="quadcopter", fallback_unfiltered=False
        )

        assert mock_vs.similarity_search.call_args.kwargs["filter"] == {
            "$and": [
                {"airframe_class": {"$eq": "multirotor"}},
                {"evaluated": {"$eq": True}},
                {"use_case": {"$eq": "racing"}},
                {"topology": {"$eq": "quadcopter"}},
            ]
        }

    @pytest.mark.asyncio
    async def test_baseline_survives_the_unfiltered_fallback(self):
        """Relaxing use_case/topology must not readmit unbuildable airframes."""
        rag, mock_vs = self._rag()
        mock_vs.similarity_search.side_effect = [
            [],  # filtered → empty
            [MagicMock(metadata={"topology": "hexacopter"})],  # relaxed retry
        ]

        results = await rag.retrieve_filtered("racing quad", use_case="racing")

        assert mock_vs.similarity_search.call_count == 2
        assert mock_vs.similarity_search.call_args_list[1].kwargs["filter"] == {
            "$and": [
                {"airframe_class": {"$eq": "multirotor"}},
                {"evaluated": {"$eq": True}},
            ]
        }
        assert len(results) == 1


class TestSharedIndex:
    """2026-09-29: each design re-embedded the corpus into one shared in-memory
    collection; sixteen runs in one process held sixteen copies and the next
    query hung for 17 hours."""

    @staticmethod
    def _rag(monkeypatch, docs):
        from langchain_core.embeddings import FakeEmbeddings

        from agents import design_agent

        monkeypatch.setattr(design_agent, "_INDEXES", {})
        monkeypatch.setattr(design_agent, "_RAG_DISABLED", None)
        rag = AircraftVerseRAG(FakeEmbeddings(size=8), mode="memory")
        monkeypatch.setattr(rag, "_load_designs", lambda: docs)
        return rag

    @pytest.mark.asyncio
    async def test_one_index_per_process_whatever_the_number_of_agents(self, monkeypatch):
        from langchain_core.documents import Document

        docs = [Document(page_content=f"design {i}", metadata={"i": i}) for i in range(40)]
        first = self._rag(monkeypatch, docs)
        await first.initialize()
        second = AircraftVerseRAG(first.embeddings, mode="memory")
        monkeypatch.setattr(second, "_load_designs", lambda: docs)
        await second.initialize()
        assert second._vectorstore is first._vectorstore
        assert first._vectorstore._collection.count() == 40

    @pytest.mark.asyncio
    async def test_a_polluted_collection_is_rebuilt_clean(self, monkeypatch):
        from services.vector_index import IndexSettings as ChromaSettings
        from services.vector_index import LocalVectorIndex as Chroma
        from langchain_core.documents import Document

        docs = [Document(page_content=f"design {i}", metadata={"i": i}) for i in range(30)]
        rag = self._rag(monkeypatch, docs)
        # What the old code left behind: the same corpus appended twice.
        settings_ = ChromaSettings(anonymized_telemetry=False, is_persistent=False)
        for _ in range(2):
            Chroma.from_documents(
                docs, rag.embeddings, collection_name="aircraftverse_mem", client_settings=settings_
            )
        await rag.initialize()
        assert rag._vectorstore._collection.count() == 30

    @pytest.mark.asyncio
    async def test_a_stuck_query_gives_up_instead_of_stalling_the_design(self, monkeypatch):
        import time as _time

        from agents import design_agent

        monkeypatch.setattr(design_agent, "RAG_QUERY_TIMEOUT_S", 0.2)
        monkeypatch.setattr(design_agent, "_RAG_DISABLED", None)
        rag = AircraftVerseRAG(MagicMock(), mode="memory")
        rag._initialized = True
        rag._vectorstore = MagicMock()
        rag._vectorstore.similarity_search.side_effect = lambda *a, **k: _time.sleep(2)
        started = _time.monotonic()
        assert await rag.retrieve_filtered("q", topology="quadcopter") == []
        assert await rag.retrieve("q") == []
        assert _time.monotonic() - started < 1.5
        # Later designs in the process skip the stuck index instead of queueing on it.
        assert design_agent._RAG_DISABLED and "exceeded" in design_agent._RAG_DISABLED
        later = AircraftVerseRAG(MagicMock(), mode="memory")
        await later.initialize()
        assert later._vectorstore is None
