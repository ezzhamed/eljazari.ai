"""Phase 13 tests — failure injection harness for IP001-IP004 protocols."""

from __future__ import annotations

import pytest

from services.failure_injector import (
    MTTD_BUDGET_S,
    SCENARIOS,
    FailureScenario,
    TelemetryEvent,
    battery_monitor_probe,
    connection_watchdog_probe,
    gps_loss_probe,
    make_battery_drain_stream,
    make_connection_loss_stream,
    make_gps_loss_stream,
    run_default_battery_scenario,
    run_scenario,
)


# ─────────────────────────────────────────────────────────────────────────────
# Scenario catalog
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioCatalog:
    def test_catalog_has_required_scenarios(self):
        required = {
            "gps_jam_at_15s",
            "battery_cell_open_at_10s",
            "low_battery_at_20s",
            "radio_signal_loss_at_25s",
            "motor_3_failure_at_20s",
            "compass_offset_at_5s",
            "esc_overheat_at_40s",
        }
        assert required.issubset(set(SCENARIOS.keys()))

    def test_default_budgets_present_on_all_scenarios(self):
        for sid, scn in SCENARIOS.items():
            assert scn.expected_mttd_s > 0
            assert scn.expected_mttss_s > 0
            assert scn.expected_mttd_s <= scn.expected_mttss_s, sid


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic streams
# ─────────────────────────────────────────────────────────────────────────────


class TestSyntheticStreams:
    def test_battery_stream_drains_to_target(self):
        stream = make_battery_drain_stream(
            inject_at_s=0.0, final_pct=20.0, duration_s=10.0, sample_hz=5.0
        )
        first_pct = stream[0].payload["remaining_percent"]
        last_pct = stream[-1].payload["remaining_percent"]
        assert first_pct == pytest.approx(100.0, abs=1.0)
        assert last_pct == pytest.approx(20.0, abs=2.0)

    def test_cell_open_drops_voltage(self):
        stream = make_battery_drain_stream(
            inject_at_s=5.0, cell_open=True, duration_s=10.0, sample_hz=5.0
        )
        pre = [e for e in stream if e.timestamp_s < 5.0]
        post = [e for e in stream if e.timestamp_s >= 5.0]
        assert pre[-1].payload["voltage_v"] > 14.0
        assert post[0].payload["voltage_v"] < 12.0

    def test_gps_loss_stream(self):
        stream = make_gps_loss_stream(inject_at_s=10.0, duration_s=20.0)
        pre_sat = next(e.payload["num_satellites"] for e in stream if e.timestamp_s < 10.0)
        post_sat = next(e.payload["num_satellites"] for e in stream if e.timestamp_s >= 10.0)
        assert pre_sat >= 8
        assert post_sat == 0

    def test_connection_loss_stream(self):
        stream = make_connection_loss_stream(inject_at_s=5.0, duration_s=20.0)
        before = [e for e in stream if e.timestamp_s < 5.0]
        after = [e for e in stream if e.timestamp_s >= 5.0]
        assert all(e.payload["is_connected"] for e in before)
        assert all(not e.payload["is_connected"] for e in after)


# ─────────────────────────────────────────────────────────────────────────────
# Probes
# ─────────────────────────────────────────────────────────────────────────────


class TestProbes:
    @pytest.mark.asyncio
    async def test_battery_monitor_triggers_rtl_below_20pct(self):
        stream = make_battery_drain_stream(
            inject_at_s=0.0, final_pct=15.0, duration_s=30.0, sample_hz=2.0
        )
        actions = await battery_monitor_probe(stream)
        assert len(actions) >= 1
        assert actions[0].payload["action"] == "rtl"

    @pytest.mark.asyncio
    async def test_battery_monitor_triggers_land_on_cell_open(self):
        stream = make_battery_drain_stream(
            inject_at_s=5.0, cell_open=True, duration_s=20.0, sample_hz=2.0
        )
        actions = await battery_monitor_probe(stream)
        assert actions[0].payload["action"] == "land"
        assert actions[0].payload["reason"] == "cell_open"

    @pytest.mark.asyncio
    async def test_connection_watchdog_triggers_rtl_after_2s(self):
        stream = make_connection_loss_stream(inject_at_s=5.0, duration_s=20.0, sample_hz=5.0)
        actions = await connection_watchdog_probe(stream)
        assert len(actions) >= 1
        assert actions[0].payload["action"] == "rtl"
        # Reaction must occur ~2s after the loss starts.
        assert actions[0].timestamp_s == pytest.approx(7.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_connection_watchdog_ignores_brief_loss(self):
        # 1-second blip — below the 2s threshold.
        stream = [
            TelemetryEvent(
                timestamp_s=i / 5.0,
                kind="connection",
                payload={"is_connected": not (1.0 <= i / 5.0 < 1.8)},
            )
            for i in range(50)
        ]
        actions = await connection_watchdog_probe(stream)
        assert actions == []

    @pytest.mark.asyncio
    async def test_gps_loss_probe_triggers_altctl(self):
        stream = make_gps_loss_stream(inject_at_s=10.0, duration_s=20.0)
        actions = await gps_loss_probe(stream)
        assert actions[0].payload["action"] == "altctl"


# ─────────────────────────────────────────────────────────────────────────────
# Full scenario runs (MTTD / MTTSS budgets)
# ─────────────────────────────────────────────────────────────────────────────


class TestFullScenarios:
    @pytest.mark.asyncio
    async def test_low_battery_scenario_passes(self):
        result = await run_default_battery_scenario("low_battery_at_20s")
        assert result.passed, result.rejected_reason
        assert result.mttd_s is not None and result.mttd_s <= MTTD_BUDGET_S

    @pytest.mark.asyncio
    async def test_cell_open_scenario_passes(self):
        result = await run_default_battery_scenario("battery_cell_open_at_10s")
        assert result.passed, result.rejected_reason
        assert result.detected_event == "land"

    @pytest.mark.asyncio
    async def test_radio_signal_loss_scenario(self):
        scenario = SCENARIOS["radio_signal_loss_at_25s"]
        stream = make_connection_loss_stream(
            inject_at_s=scenario.inject_at_t_s,
            duration_s=40.0,
            sample_hz=10.0,
        )
        result = await run_scenario(scenario, stream, connection_watchdog_probe)
        assert result.passed, result.rejected_reason
        assert result.detected_event == "rtl"
        assert result.mttd_s is not None
        assert result.mttd_s <= scenario.expected_mttd_s + 0.5

    @pytest.mark.asyncio
    async def test_gps_jam_scenario(self):
        scenario = SCENARIOS["gps_jam_at_15s"]
        stream = make_gps_loss_stream(
            inject_at_s=scenario.inject_at_t_s,
            duration_s=30.0,
            sample_hz=5.0,
        )
        result = await run_scenario(scenario, stream, gps_loss_probe)
        assert result.passed, result.rejected_reason
        assert result.detected_event == "altctl"

    @pytest.mark.asyncio
    async def test_scenario_fails_when_probe_emits_wrong_action(self):
        """If the protocol-under-test reacts incorrectly, the harness must
        catch it and refuse to pass."""

        async def noop_probe(_stream):
            return []

        scenario = SCENARIOS["low_battery_at_20s"]
        stream = make_battery_drain_stream(inject_at_s=scenario.inject_at_t_s)
        result = await run_scenario(scenario, stream, noop_probe)
        assert not result.passed
        assert "did not emit" in (result.rejected_reason or "")

    @pytest.mark.asyncio
    async def test_scenario_fails_on_late_reaction(self):
        """Reaction outside the MTTD budget must be rejected."""
        scenario = FailureScenario(
            scenario_id="late_reaction_test",
            title="Late reaction test",
            inject_at_t_s=0.0,
            duration_s=10.0,
            expected_reaction="rtl",
            expected_mttd_s=0.5,
            expected_mttss_s=1.0,
        )

        async def slow_probe(_stream):
            return [
                TelemetryEvent(
                    timestamp_s=5.0,
                    kind="action",
                    payload={"action": "rtl", "reason": "test"},
                )
            ]

        result = await run_scenario(scenario, [], slow_probe)
        assert not result.passed
        assert "budget" in (result.rejected_reason or "")


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────


class TestReporting:
    @pytest.mark.asyncio
    async def test_result_includes_mttd_and_mttss(self):
        result = await run_default_battery_scenario("low_battery_at_20s")
        assert result.mttd_s is not None
        assert result.mttss_s is not None

    @pytest.mark.asyncio
    async def test_result_is_serialisable(self):
        result = await run_default_battery_scenario("low_battery_at_20s")
        # Pydantic model must JSON-serialise.
        payload = result.model_dump_json()
        assert "scenario_id" in payload
        assert "mttd_s" in payload
