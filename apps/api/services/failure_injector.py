"""eljazari.ai — Phase 13: Failure injection harness.

The Safety Layer (Phase 2.2) injects four protocols into every generated
mission: battery monitor, connection watchdog, geofence enforcer, and
emergency stop handler. But until now we have never *tested* that these
protocols actually work — we just trusted that the AST-injected snippets
do the right thing under failure.

This module is the harness that fires synthetic faults at the generated
mission code (or at a running SITL telemetry stream) and records whether
the injected protocols reacted within the published Mean-Time-To-Detect
budget (MTTD) and reached a safe state within the Mean-Time-To-Safe-State
budget (MTTSS).

The harness is intentionally *transport-agnostic*: each scenario produces
a stream of synthetic telemetry events that the protocol-under-test
consumes. The validator then asserts the expected reaction signature.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight budgets (seconds)
# ─────────────────────────────────────────────────────────────────────────────

MTTD_BUDGET_S: float = 2.0  # detect failure within 2 s of injection
MTTSS_BUDGET_S: float = 5.0  # reach a safe state within 5 s


ExpectedReaction = Literal[
    "rtl",  # IP001 battery monitor → return-to-launch
    "land",  # generic safe landing
    "kill",  # IP004 emergency handler → kill
    "altctl",  # PX4 altitude-control mode fallback after GPS loss
    "switch_to_backup_imu",
    "limit_throttle",
]


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────


class FailureScenario(BaseModel):
    """A single synthetic-fault test case."""

    model_config = ConfigDict(frozen=True)

    scenario_id: str
    title: str
    inject_at_t_s: float = Field(0.0, ge=0.0)
    duration_s: float = Field(10.0, gt=0.0)
    expected_reaction: ExpectedReaction
    expected_mttd_s: float = Field(MTTD_BUDGET_S, gt=0.0)
    expected_mttss_s: float = Field(MTTSS_BUDGET_S, gt=0.0)
    description: str = ""


class FailureInjectionResult(BaseModel):
    """Outcome of running a single FailureScenario."""

    scenario_id: str
    passed: bool
    mttd_s: float | None = None
    mttss_s: float | None = None
    detected_event: str | None = None
    safe_state_event: str | None = None
    rejected_reason: str | None = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Telemetry stream model — synthetic events for the protocol-under-test
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TelemetryEvent:
    timestamp_s: float
    kind: str  # e.g. "battery", "gps", "imu", "command"
    payload: dict[str, Any] = field(default_factory=dict)


# A "protocol probe" is an async callable that consumes telemetry events
# and emits action events. The harness records the actions and matches them
# against ``expected_reaction``.
ProtocolProbe = Callable[[list[TelemetryEvent]], "asyncio.Future[list[TelemetryEvent]]"]


# ─────────────────────────────────────────────────────────────────────────────
# Scenario catalog
# ─────────────────────────────────────────────────────────────────────────────

SCENARIOS: dict[str, FailureScenario] = {
    "gps_jam_at_15s": FailureScenario(
        scenario_id="gps_jam_at_15s",
        title="GPS jamming after 15 s — expect ALTCTL fallback",
        inject_at_t_s=15.0,
        duration_s=10.0,
        expected_reaction="altctl",
        description=(
            "Drone loses GPS lock at t=15 s. PX4 should drop to altitude "
            "control mode within 2 s and maintain attitude."
        ),
    ),
    "battery_cell_open_at_10s": FailureScenario(
        scenario_id="battery_cell_open_at_10s",
        title="Battery cell open at 10 s — expect emergency land",
        inject_at_t_s=10.0,
        duration_s=15.0,
        expected_reaction="land",
        description=(
            "One LiPo cell goes open-circuit, dropping pack voltage by ~3.7 V. "
            "IP001 battery monitor should trigger immediate landing."
        ),
    ),
    "low_battery_at_20s": FailureScenario(
        scenario_id="low_battery_at_20s",
        title="Battery below 20 % at 20 s — expect RTL",
        inject_at_t_s=20.0,
        duration_s=20.0,
        expected_reaction="rtl",
        description=(
            "Slow discharge to 18 % SoC. IP001 must trigger return-to-launch "
            "before pack reaches the 15 % critical threshold."
        ),
    ),
    "radio_signal_loss_at_25s": FailureScenario(
        scenario_id="radio_signal_loss_at_25s",
        title="RC signal loss at 25 s — expect RTL",
        inject_at_t_s=25.0,
        duration_s=10.0,
        expected_reaction="rtl",
        description=(
            "MAVLink heartbeat stops for > 2 s. IP002 connection watchdog should trigger RTL."
        ),
    ),
    "motor_3_failure_at_20s": FailureScenario(
        scenario_id="motor_3_failure_at_20s",
        title="Motor #3 failure at 20 s — expect safe land (quad) or thrust redistribution (hexa)",
        inject_at_t_s=20.0,
        duration_s=10.0,
        expected_reaction="land",
        description=(
            "Motor #3 stops producing thrust. On a quadcopter this is "
            "irrecoverable — IP004 emergency handler must initiate land."
        ),
    ),
    "compass_offset_at_5s": FailureScenario(
        scenario_id="compass_offset_at_5s",
        title="50 µT compass offset at 5 s — expect yaw-from-GPS",
        inject_at_t_s=5.0,
        duration_s=15.0,
        expected_reaction="switch_to_backup_imu",
        description=(
            "Magnetic interference adds 50 µT compass offset. PX4 should "
            "switch to GPS-derived yaw within MTTD budget."
        ),
    ),
    "esc_overheat_at_40s": FailureScenario(
        scenario_id="esc_overheat_at_40s",
        title="ESC overheat at 40 s — expect throttle limit",
        inject_at_t_s=40.0,
        duration_s=10.0,
        expected_reaction="limit_throttle",
        description=(
            "ESC temperature exceeds 90 °C. Firmware should limit throttle to 70 % within 1 s."
        ),
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic telemetry generators
# ─────────────────────────────────────────────────────────────────────────────


def make_battery_drain_stream(
    inject_at_s: float,
    cell_open: bool = False,
    final_pct: float = 18.0,
    duration_s: float = 30.0,
    sample_hz: float = 5.0,
    trigger_threshold_pct: float = 20.0,
) -> list[TelemetryEvent]:
    """Generate a synthetic battery telemetry timeline.

    ``cell_open=True`` introduces a 3.7 V voltage drop at ``inject_at_s``.
    Otherwise the pack drains linearly so the SoC crosses
    ``trigger_threshold_pct`` exactly at ``inject_at_s`` — this is the
    "battery hits the protocol threshold at t=inject_at_s" semantic, which
    is what the harness measures MTTD against.
    """
    events: list[TelemetryEvent] = []
    n = int(duration_s * sample_hz)
    # When inject_at_s > 0, design the linear curve so SoC crosses the
    # trigger threshold at inject_at_s — that lets the harness measure
    # MTTD as the delta between the protocol's reaction and the threshold
    # crossing. When inject_at_s == 0 (or > duration), fall back to a
    # plain 100 → final_pct linear drain over the full window.
    if inject_at_s > 0 and inject_at_s <= duration_s:
        slope = (100.0 - trigger_threshold_pct) / inject_at_s
    else:
        slope = (100.0 - final_pct) / max(duration_s, 1e-6)
    for i in range(n):
        t = i / sample_hz
        if cell_open and t >= inject_at_s:
            voltage = 11.1  # 4S nominal − 3.7 V
            pct = 60.0
        else:
            pct = max(final_pct, 100.0 - slope * t)
            voltage = (4.20 - (4.20 - 3.50) * (1.0 - pct / 100.0)) * 4
        events.append(
            TelemetryEvent(
                timestamp_s=t,
                kind="battery",
                payload={"remaining_percent": pct, "voltage_v": voltage},
            )
        )
    return events


def make_gps_loss_stream(
    inject_at_s: float,
    duration_s: float = 30.0,
    sample_hz: float = 5.0,
) -> list[TelemetryEvent]:
    """Synthetic GPS timeline: lock until ``inject_at_s``, then zero satellites."""
    events: list[TelemetryEvent] = []
    n = int(duration_s * sample_hz)
    for i in range(n):
        t = i / sample_hz
        if t >= inject_at_s:
            payload = {"num_satellites": 0, "fix_type": 0}
        else:
            payload = {"num_satellites": 12, "fix_type": 3}
        events.append(TelemetryEvent(timestamp_s=t, kind="gps", payload=payload))
    return events


def make_connection_loss_stream(
    inject_at_s: float,
    duration_s: float = 30.0,
    sample_hz: float = 5.0,
) -> list[TelemetryEvent]:
    """Synthetic MAVLink heartbeat timeline."""
    events: list[TelemetryEvent] = []
    n = int(duration_s * sample_hz)
    for i in range(n):
        t = i / sample_hz
        connected = t < inject_at_s
        events.append(
            TelemetryEvent(
                timestamp_s=t,
                kind="connection",
                payload={"is_connected": connected},
            )
        )
    return events


# ─────────────────────────────────────────────────────────────────────────────
# Built-in protocol probes (these mimic the IP001-IP004 safety injections)
# ─────────────────────────────────────────────────────────────────────────────


async def battery_monitor_probe(stream: list[TelemetryEvent]) -> list[TelemetryEvent]:
    """Mirror of IP001: emit ``rtl`` when battery_pct drops below 20 %.

    Real injected code lives in agents/safety_layer.py; this probe uses the
    same threshold so the harness validates the *behaviour* without
    coupling to the AST-injected snippet's exact source.
    """
    actions: list[TelemetryEvent] = []
    for ev in stream:
        if ev.kind != "battery":
            continue
        pct = ev.payload.get("remaining_percent", 100.0)
        # Detect critically-low cell first (voltage drop too sudden).
        v = ev.payload.get("voltage_v", 16.0)
        if v < 13.0:
            actions.append(
                TelemetryEvent(
                    timestamp_s=ev.timestamp_s,
                    kind="action",
                    payload={"action": "land", "reason": "cell_open"},
                )
            )
            return actions
        if pct < 20.0:
            actions.append(
                TelemetryEvent(
                    timestamp_s=ev.timestamp_s,
                    kind="action",
                    payload={"action": "rtl", "reason": "low_battery"},
                )
            )
            return actions
    return actions


async def connection_watchdog_probe(stream: list[TelemetryEvent]) -> list[TelemetryEvent]:
    """Mirror of IP002: emit ``rtl`` after 2 s of consecutive disconnects."""
    actions: list[TelemetryEvent] = []
    disconnect_started_at: float | None = None
    for ev in stream:
        if ev.kind != "connection":
            continue
        if not ev.payload.get("is_connected", True):
            if disconnect_started_at is None:
                disconnect_started_at = ev.timestamp_s
            elif ev.timestamp_s - disconnect_started_at >= 2.0:
                actions.append(
                    TelemetryEvent(
                        timestamp_s=ev.timestamp_s,
                        kind="action",
                        payload={"action": "rtl", "reason": "signal_loss"},
                    )
                )
                return actions
        else:
            disconnect_started_at = None
    return actions


async def gps_loss_probe(stream: list[TelemetryEvent]) -> list[TelemetryEvent]:
    """PX4-style: drop to altitude control (ALTCTL) on GPS loss."""
    actions: list[TelemetryEvent] = []
    for ev in stream:
        if ev.kind != "gps":
            continue
        if int(ev.payload.get("num_satellites", 0)) < 4:
            actions.append(
                TelemetryEvent(
                    timestamp_s=ev.timestamp_s,
                    kind="action",
                    payload={"action": "altctl", "reason": "gps_loss"},
                )
            )
            return actions
    return actions


# ─────────────────────────────────────────────────────────────────────────────
# Harness runner
# ─────────────────────────────────────────────────────────────────────────────


async def run_scenario(
    scenario: FailureScenario,
    stream: list[TelemetryEvent],
    probe: ProtocolProbe,
) -> FailureInjectionResult:
    """Run one scenario against one probe and compute MTTD / MTTSS."""
    timeline: list[dict[str, Any]] = []
    actions = await probe(stream)

    detected: TelemetryEvent | None = None
    safe_state: TelemetryEvent | None = None
    for a in actions:
        if a.kind == "action" and a.payload.get("action") == scenario.expected_reaction:
            detected = detected or a
            safe_state = a
            timeline.append({"t": a.timestamp_s, "event": a.payload})
            break
    if detected is None:
        return FailureInjectionResult(
            scenario_id=scenario.scenario_id,
            passed=False,
            rejected_reason=(
                f"Protocol did not emit expected reaction "
                f"'{scenario.expected_reaction}'. "
                f"Actions seen: {[a.payload for a in actions]}"
            ),
            timeline=timeline,
        )

    mttd = detected.timestamp_s - scenario.inject_at_t_s
    mttss = (safe_state.timestamp_s - scenario.inject_at_t_s) if safe_state else None

    passed = (
        mttd >= 0
        and mttd <= scenario.expected_mttd_s
        and (mttss is None or mttss <= scenario.expected_mttss_s)
    )
    rejected_reason: str | None = None
    if mttd < 0:
        rejected_reason = (
            f"MTTD negative — probe reacted before the injection at "
            f"t={scenario.inject_at_t_s:.1f}s."
        )
    elif mttd > scenario.expected_mttd_s:
        rejected_reason = f"MTTD {mttd:.2f}s > budget {scenario.expected_mttd_s:.2f}s."
    elif mttss is not None and mttss > scenario.expected_mttss_s:
        rejected_reason = f"MTTSS {mttss:.2f}s > budget {scenario.expected_mttss_s:.2f}s."

    return FailureInjectionResult(
        scenario_id=scenario.scenario_id,
        passed=passed,
        mttd_s=round(mttd, 3),
        mttss_s=round(mttss, 3) if mttss is not None else None,
        detected_event=detected.payload.get("action"),
        safe_state_event=safe_state.payload.get("action") if safe_state else None,
        rejected_reason=rejected_reason,
        timeline=timeline,
    )


async def run_default_battery_scenario(scenario_id: str) -> FailureInjectionResult:
    """Convenience: run a built-in scenario against the built-in battery probe."""
    scenario = SCENARIOS[scenario_id]
    if scenario_id == "battery_cell_open_at_10s":
        stream = make_battery_drain_stream(inject_at_s=scenario.inject_at_t_s, cell_open=True)
    elif scenario_id == "low_battery_at_20s":
        stream = make_battery_drain_stream(inject_at_s=scenario.inject_at_t_s, final_pct=18.0)
    else:
        raise ValueError(f"Unsupported scenario for battery probe: {scenario_id}")
    return await run_scenario(scenario, stream, battery_monitor_probe)
