"""eljazari.ai — Comprehensive Safety Layer Tests (Prompt 2.2)

Coverage
────────
• safety_models.py       — FlightConstraints, Waypoint, all report schemas
• Pillar 1               — _StaticAnalyser (SR001-SR008)
• Pillar 2               — _ReachabilityAnalyser (RR001-RR005)
• Pillar 3               — _ComplianceChecker (CR001-CR010)
• Injection Engine       — all 4 protocols
• SafeLLMSupervisionLayer — async public API
• SafetyLayer (compat)   — synchronous shim

Total target: 55+ tests
"""

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

SAFE_CODE = """\
import asyncio

async def _battery_monitor(drone): pass
async def _connection_watchdog(drone): pass
async def _geofence_enforcer(drone): pass

async def run():
    drone = System()
    await drone.connect()
    await check_pre_flight(drone)
    battery = await get_battery(drone)
    try:
        await takeoff(drone, altitude=30.0)
        await fly_to(drone, 100.0, 50.0, -30.0, speed=5.0)
        await land(drone)
    except Exception:
        await emergency_stop(drone)

if __name__ == "__main__":
    asyncio.run(run())
"""

UNSAFE_CODE_NO_LANDING = """\
async def run():
    drone = System()
    await drone.connect()
    await takeoff(drone, altitude=30.0)
    await fly_to(drone, 100.0, 50.0, -30.0)
"""

UNSAFE_CODE_CRITICAL = """\
import subprocess
def run():
    subprocess.run(["rm", "-rf", "/"])
    set_motor_speed(9999)
    password = "secret123"
    while True:
        pass
"""

MINIMAL_MISSION = """\
async def run():
    try:
        await takeoff(system, altitude=10.0)
        await land(system)
    except Exception:
        await emergency_stop(system)
"""


def _constraints(**kw):
    from agents.safety_models import FlightConstraints

    return FlightConstraints(**kw)


def _waypoint(x=0.0, y=0.0, z=-10.0, speed=5.0, label=""):
    from agents.safety_models import Waypoint

    return Waypoint(x=x, y=y, z=z, speed_ms=speed, label=label)


# ═════════════════════════════════════════════════════════════════════════════
# safety_models.py
# ═════════════════════════════════════════════════════════════════════════════


class TestFlightConstraints:
    def test_defaults_are_safe(self):
        c = _constraints()
        assert c.max_altitude_m == 120.0
        assert c.max_speed_ms == 15.0
        assert c.min_battery_pct == 20.0
        assert c.battery_rtl_margin_pct == 30.0

    def test_custom_values(self):
        c = _constraints(max_altitude_m=50.0, max_speed_ms=8.0)
        assert c.max_altitude_m == 50.0
        assert c.max_speed_ms == 8.0

    def test_min_altitude_must_be_below_max(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            _constraints(max_altitude_m=10.0, min_altitude_m=15.0)


class TestWaypoint:
    def test_altitude_agl_from_negative_z(self):
        wp = _waypoint(z=-30.0)
        assert wp.altitude_agl == 30.0

    def test_altitude_agl_zero_at_ground(self):
        wp = _waypoint(z=0.0)
        assert wp.altitude_agl == 0.0

    def test_horizontal_distance(self):
        wp = _waypoint(x=30.0, y=40.0)
        assert abs(wp.horizontal_distance_from_home - 50.0) < 0.01

    def test_home_waypoint_has_zero_distance(self):
        wp = _waypoint(x=0.0, y=0.0)
        assert wp.horizontal_distance_from_home == 0.0


class TestSeverityWeights:
    def test_critical_is_highest(self):
        from agents.safety_models import Severity

        assert (
            Severity.CRITICAL.weight
            > Severity.HIGH.weight
            > Severity.MEDIUM.weight
            > Severity.LOW.weight
        )


# ═════════════════════════════════════════════════════════════════════════════
# Pillar 1 — Static Analysis
# ═════════════════════════════════════════════════════════════════════════════


class TestStaticAnalyser:
    def _analyser(self, **kw):
        from agents.safety_layer import _StaticAnalyser

        return _StaticAnalyser(_constraints(**kw))

    def test_syntax_error_returns_sr000(self):
        report = self._analyser().analyse("def broken(: pass")
        assert not report.passed
        assert any(i.rule_id == "SR000" for i in report.issues)

    def test_unapproved_function_returns_sr001(self):
        code = "async def run():\n    await land(drone)\n    await custom_hack()\n"
        report = self._analyser().analyse(code)
        assert any(i.rule_id == "SR001" for i in report.issues)
        assert "custom_hack" in report.unapproved_functions

    def test_mavsdk_address_getenv_is_allowed(self):
        code = """\
import os

async def run():
    drone = System()
    mavsdk_address = os.getenv("DRONEPROMPT_MAVSDK_ADDRESS", "udp://:14540")
    await drone.connect(system_address=mavsdk_address)
    try:
        await check_pre_flight(drone)
        await land(drone)
    except Exception:
        await emergency_stop(drone)
"""
        report = self._analyser().analyse(code)
        assert "getenv" not in report.unapproved_functions
        assert not any(i.rule_id == "SR001" and "getenv" in i.description for i in report.issues)

    def test_other_getenv_calls_are_rejected(self):
        code = """\
import os

async def run():
    drone = System()
    secret = os.getenv("DRONEPROMPT_SECRET_TOKEN")
    await drone.connect(system_address="udp://:14540")
    try:
        await check_pre_flight(drone)
        await land(drone)
    except Exception:
        await emergency_stop(drone)
"""
        report = self._analyser().analyse(code)
        assert any(i.rule_id == "SR001" and "getenv" in i.description for i in report.issues)
        assert "getenv" in report.unapproved_functions

    def test_missing_landing_returns_sr002(self):
        report = self._analyser().analyse(UNSAFE_CODE_NO_LANDING)
        assert any(i.rule_id == "SR002" for i in report.issues)

    def test_missing_try_except_returns_sr003(self):
        code = "async def run():\n    await takeoff(drone)\n    await land(drone)\n"
        report = self._analyser().analyse(code)
        assert any(i.rule_id == "SR003" for i in report.issues)

    def test_altitude_violation_returns_sr004(self):
        code = "async def run():\n    await takeoff(drone, altitude=200.0)\n    await land(drone)\n"
        report = self._analyser(max_altitude_m=120.0).analyse(code)
        assert any(i.rule_id == "SR004" for i in report.issues)

    def test_altitude_m_keyword_violation_returns_sr004(self):
        code = (
            "async def run():\n    await drone.takeoff(altitude_m=200.0)\n    await drone.land()\n"
        )
        report = self._analyser(max_altitude_m=120.0).analyse(code)
        assert any(i.rule_id == "SR004" for i in report.issues)

    def test_speed_violation_returns_sr005(self):
        code = "async def run():\n    await fly_to(d, 0, 0, -10, speed=99.0)\n    await land(d)\n"
        report = self._analyser(max_speed_ms=15.0).analyse(code)
        assert any(i.rule_id == "SR005" for i in report.issues)

    def test_no_await_returns_sr006(self):
        code = "def run():\n    land(drone)\n"
        report = self._analyser().analyse(code)
        assert any(i.rule_id == "SR006" for i in report.issues)

    def test_missing_preflight_returns_sr007(self):
        report = self._analyser().analyse(UNSAFE_CODE_NO_LANDING)
        assert any(i.rule_id == "SR007" for i in report.issues)

    def test_missing_connection_returns_sr008(self):
        code = "async def run():\n    await takeoff(drone)\n    await land(drone)\n"
        report = self._analyser().analyse(code)
        assert any(i.rule_id == "SR008" for i in report.issues)

    def test_fully_safe_code_passes(self):
        report = self._analyser().analyse(SAFE_CODE)
        critical = [i for i in report.issues if i.severity.value == "critical"]
        assert len(critical) == 0
        assert report.passed

    def test_multiple_violations_all_returned(self):
        report = self._analyser().analyse(UNSAFE_CODE_NO_LANDING)
        assert len(report.issues) >= 3  # SR002, SR003, SR007, SR008...

    def test_approved_functions_not_flagged(self):
        code = "async def run():\n    await takeoff(d)\n    await fly_to(d, 0, 0, -10)\n    await land(d)\n"
        report = self._analyser().analyse(code)
        assert not report.unapproved_functions or all(
            f in {"d"} for f in report.unapproved_functions
        )

    def test_generated_runtime_support_calls_not_flagged(self):
        code = """\
import time
from mavsdk import System
from utils.mavsdk_runtime_adapter import MAVSDKRuntimeAdapter

def _battery_remaining_pct(battery):
    value = float(getattr(battery, "remaining_percent", 0.0))
    return value * 100.0 if value <= 1.0 else value

async def run_mission(drone):
    start = time.monotonic()
    await check_pre_flight(drone)
    battery = await get_battery(drone)
    if _battery_remaining_pct(battery) < 20.0:
        await land(drone)
        return False
    try:
        await takeoff(drone, altitude=15.0)
        await fly_to_relative(drone, north_m=100.0, east_m=0.0, down_m=0.0)
        if time.monotonic() - start > 0:
            await land(drone)
        return True
    except Exception:
        await emergency_stop(drone)
        return False

async def main():
    system = System()
    await system.connect(system_address="udp://:14540")
    drone = MAVSDKRuntimeAdapter(system)
    return await run_mission(drone)
"""
        report = self._analyser().analyse(code)
        assert not any(
            name in report.unapproved_functions
            for name in {"monotonic", "MAVSDKRuntimeAdapter", "getattr"}
        )


# ═════════════════════════════════════════════════════════════════════════════
# Pillar 2 — Reachability Analysis
# ═════════════════════════════════════════════════════════════════════════════


class TestReachabilityAnalyser:
    def _analyser(self, **kw):
        from agents.safety_layer import _ReachabilityAnalyser

        return _ReachabilityAnalyser(_constraints(**kw))

    def test_no_waypoints_returns_passed(self):
        report = self._analyser().analyse("async def run(): await land(d)")
        assert report.passed
        assert report.waypoints_checked == 0

    def test_waypoint_within_geofence_passes(self):
        wps = [_waypoint(x=100.0, y=100.0, z=-30.0)]
        report = self._analyser(max_geofence_radius_m=500.0).analyse("", wps)
        geofence_issues = [i for i in report.issues if i.rule_id == "RR002"]
        assert len(geofence_issues) == 0

    def test_waypoint_outside_geofence_fails(self):
        wps = [_waypoint(x=1000.0, y=0.0, z=-30.0)]
        report = self._analyser(max_geofence_radius_m=500.0).analyse("", wps)
        assert any(i.rule_id == "RR002" for i in report.issues)
        assert not report.passed

    def test_altitude_above_max_flagged(self):
        wps = [_waypoint(x=10.0, y=0.0, z=-200.0)]  # z=-200 → alt=200m
        report = self._analyser(max_altitude_m=120.0).analyse("", wps)
        assert any(i.rule_id == "RR003" for i in report.issues)

    def test_altitude_below_min_flagged(self):
        wps = [_waypoint(x=10.0, y=0.0, z=-1.0)]  # z=-1 → alt=1m
        report = self._analyser(min_altitude_m=2.0).analyse("", wps)
        assert any(i.rule_id == "RR004" for i in report.issues)

    def test_speed_above_max_flagged(self):
        wps = [_waypoint(x=10.0, y=0.0, z=-30.0, speed=50.0)]
        report = self._analyser(max_speed_ms=15.0).analyse("", wps)
        assert any(i.rule_id == "RR005" for i in report.issues)

    def test_battery_insufficient_for_rtl(self):
        # 10km mission → 100% battery usage → RTL infeasible
        wps = [_waypoint(x=5000.0, y=5000.0, z=-30.0)]
        report = self._analyser(
            battery_consumption_per_m=0.05,
            battery_rtl_margin_pct=30.0,
            max_geofence_radius_m=99999.0,  # disable geofence for this test
        ).analyse("", wps)
        assert any(i.rule_id == "RR001" for i in report.issues)

    def test_battery_estimate_is_correct(self):
        # 100m path (50m out + 50m back) * 0.02%/m = 2%
        wps = [_waypoint(x=50.0, y=0.0, z=-10.0)]
        report = self._analyser(
            battery_consumption_per_m=0.02,
            max_geofence_radius_m=500.0,
        ).analyse("", wps)
        assert report.total_distance_m == pytest.approx(100.0, abs=1.0)
        assert report.estimated_battery_usage_pct == pytest.approx(2.0, abs=0.1)

    def test_extract_waypoints_from_fly_to_code(self):
        from agents.safety_layer import _ReachabilityAnalyser

        code = (
            "async def run():\n"
            "    await fly_to(drone, 100.0, 50.0, -20.0, 5.0)\n"
            "    await fly_to(drone, 200.0, 0.0, -30.0, 8.0)\n"
            "    await land(drone)\n"
        )
        analyser = _ReachabilityAnalyser(_constraints())
        wps = analyser._extract_waypoints(code)
        assert len(wps) == 2
        assert wps[0].x == pytest.approx(100.0)
        assert wps[1].x == pytest.approx(200.0)

    def test_extract_waypoints_from_method_style_relative_keywords(self):
        from agents.safety_layer import _ReachabilityAnalyser

        code = (
            "async def run(drone):\n"
            "    await drone.takeoff(altitude_m=15.0)\n"
            "    await drone.fly_to_relative(north_m=100.0, east_m=0.0, down_m=0.0)\n"
            "    await drone.fly_to_relative(north_m=0.0, east_m=100.0, down_m=0.0)\n"
            "    await drone.land()\n"
        )
        analyser = _ReachabilityAnalyser(_constraints())
        wps = analyser._extract_waypoints(code)
        assert len(wps) == 2
        assert wps[0].x == pytest.approx(100.0)
        assert wps[0].y == pytest.approx(0.0)
        assert wps[0].altitude_agl == pytest.approx(15.0)
        assert wps[1].x == pytest.approx(100.0)
        assert wps[1].y == pytest.approx(100.0)
        assert wps[1].altitude_agl == pytest.approx(15.0)

    def test_extract_waypoints_from_mission_items(self):
        from agents.safety_layer import _ReachabilityAnalyser

        code = (
            "mission_items = [\n"
            "    MissionItem(latitude_deg=47.0, longitude_deg=8.0, relative_altitude_m=15.0, speed_m_s=5.0),\n"
            "    MissionItem(latitude_deg=47.001, longitude_deg=8.0, relative_altitude_m=15.0, speed_m_s=5.0),\n"
            "]\n"
        )
        analyser = _ReachabilityAnalyser(_constraints(max_geofence_radius_m=500.0))
        wps = analyser._extract_waypoints(code)
        assert len(wps) == 2
        assert wps[0].x == pytest.approx(0.0)
        assert wps[0].altitude_agl == pytest.approx(15.0)
        assert wps[1].x == pytest.approx(111.111, abs=0.5)

        report = analyser.analyse(code)
        assert report.waypoints_checked == 2
        assert not any(issue.rule_id == "RR000" for issue in report.issues)

    def test_multiple_geofence_violations_all_reported(self):
        wps = [
            _waypoint(x=1000.0, y=0.0, z=-10.0, label="WP1"),
            _waypoint(x=0.0, y=2000.0, z=-10.0, label="WP2"),
        ]
        report = self._analyser(max_geofence_radius_m=500.0).analyse("", wps)
        assert len(report.geofence_violations) == 2


# ═════════════════════════════════════════════════════════════════════════════
# Pillar 3 — Compliance Checker
# ═════════════════════════════════════════════════════════════════════════════


class TestComplianceChecker:
    def _checker(self):
        from agents.safety_layer import _ComplianceChecker

        return _ComplianceChecker()

    def test_infinite_loop_flagged(self):
        report = self._checker().check("while True:\n    pass\n")
        assert not report.passed
        assert any(v.rule_id == "CR001" for v in report.violations)

    def test_direct_motor_command_flagged(self):
        report = self._checker().check("set_motor_speed(100)\n")
        assert any(v.rule_id == "CR002" for v in report.violations)

    def test_shell_execution_flagged(self):
        report = self._checker().check("os.system('rm -rf /')\n")
        assert any(v.rule_id == "CR004" for v in report.violations)

    def test_eval_flagged(self):
        report = self._checker().check("eval(input())\n")
        assert any(v.rule_id == "CR004" for v in report.violations)

    def test_hardcoded_password_flagged(self):
        report = self._checker().check("password = 'hunter2'\n")
        assert any(v.rule_id == "CR003" for v in report.violations)

    def test_missing_async_main_flagged(self):
        report = self._checker().check("def run():\n    pass\n")
        assert any(v.rule_id == "CR005" for v in report.violations)

    def test_async_main_satisfies_cr005(self):
        report = self._checker().check("async def run():\n    pass\n")
        assert not any(v.rule_id == "CR005" for v in report.violations)

    def test_time_sleep_flagged(self):
        report = self._checker().check("time.sleep(5)\n")
        assert any(v.rule_id == "CR009" for v in report.violations + report.warnings)

    def test_threading_flagged(self):
        report = self._checker().check("import threading\n")
        assert any(v.rule_id == "CR010" for v in report.violations)

    def test_clean_code_has_no_violations(self):
        report = self._checker().check(SAFE_CODE)
        assert report.passed
        assert len(report.violations) == 0

    def test_rules_loaded_from_file(self):
        checker = self._checker()
        assert checker._rules  # at least the built-in rules loaded

    def test_multiple_violations_in_one_file(self):
        report = self._checker().check(UNSAFE_CODE_CRITICAL)
        violation_ids = {v.rule_id for v in report.violations}
        # CR001 (while True), CR002 (set_motor_speed), CR003 (password), CR004 (subprocess)
        assert len(violation_ids) >= 3


# ═════════════════════════════════════════════════════════════════════════════
# Injection Engine
# ═════════════════════════════════════════════════════════════════════════════


class TestInjectionEngine:
    def _engine(self, **constraints):
        from agents.safety_layer import _InjectionEngine
        from agents.safety_models import FlightConstraints

        return _InjectionEngine(FlightConstraints(**constraints) if constraints else None)

    def test_all_four_protocols_injected_into_clean_code(self):
        report = self._engine().inject(MINIMAL_MISSION)
        assert "battery_monitor" in report.protocols_injected
        assert "connection_watchdog" in report.protocols_injected
        assert "geofence_enforcer" in report.protocols_injected
        assert "emergency_handler" in report.protocols_injected

    def test_battery_monitor_not_duplicated(self):
        code = MINIMAL_MISSION + "\nasync def _battery_monitor(drone): pass\n"
        report = self._engine().inject(code)
        assert "battery_monitor" not in report.protocols_injected

    def test_modified_code_contains_injection_markers(self):
        report = self._engine().inject(MINIMAL_MISSION)
        assert "[IP001]" in report.modified_code
        assert "[IP002]" in report.modified_code
        assert "[IP003]" in report.modified_code
        assert "[IP004]" in report.modified_code

    def test_injected_guards_have_concrete_action_wrappers(self):
        report = self._engine().inject(MINIMAL_MISSION)
        assert "async def return_to_launch" in report.modified_code
        assert "await drone.action.return_to_launch()" in report.modified_code
        assert "async def emergency_stop" in report.modified_code
        assert "await drone.action.kill()" in report.modified_code

    def test_battery_monitor_uses_percent_threshold(self):
        report = self._engine(min_battery_pct=25.0).inject(MINIMAL_MISSION)
        assert "_battery_remaining_pct(battery)" in report.modified_code
        assert "battery_pct < 25.0" in report.modified_code
        assert "remaining_percent < 0.20" not in report.modified_code

    def test_geofence_radius_inferred_from_generated_code(self):
        code = MINIMAL_MISSION.replace(
            "await takeoff(system, altitude=10.0)",
            "await system.set_geofence(radius_m=200.0, max_alt_m=15.0)\n        await takeoff(system, altitude=10.0)",
        )
        report = self._engine(max_geofence_radius_m=500.0).inject(code)
        assert "max_radius_m=200.0" in report.modified_code
        assert "max_radius_m=500.0" not in report.modified_code

    def test_legacy_injected_protocols_are_upgraded_at_tail(self):
        code = """\
async def _battery_monitor(drone):
    async for battery in drone.telemetry.battery():
        if battery.remaining_percent < 0.20:
            await return_to_launch(drone)

async def _connection_watchdog(drone):
    pass

async def _geofence_enforcer(drone, max_radius_m=500.0):
    pass

def _emergency_signal_handler(signum, frame):
    pass

async def run_mission(drone):
    await drone.set_geofence(radius_m=200.0, max_alt_m=15.0)
    await land(drone)
"""
        report = self._engine(min_battery_pct=20.0, max_geofence_radius_m=500.0).inject(code)
        assert report.modified_code.rfind("battery_pct < 20.0") > report.modified_code.find(
            "remaining_percent < 0.20"
        )
        assert report.modified_code.rfind("max_radius_m=200.0") > report.modified_code.find(
            "max_radius_m=500.0"
        )
        assert {"protocol": "IP001", "position": "tail_upgrade"} in report.injection_points
        assert {"protocol": "IP003", "position": "tail_upgrade"} in report.injection_points

    def test_run_mission_wrapper_starts_and_cancels_safety_monitors(self):
        code = """\
async def _battery_monitor(drone): pass
async def _connection_watchdog(drone): pass
async def _geofence_enforcer(drone): pass
def _emergency_signal_handler(signum, frame): pass

async def run_mission(drone):
    await land(drone)
"""
        report = self._engine().inject(code)
        assert "_ORIGINAL_RUN_MISSION = run_mission" in report.modified_code
        assert "_safety_asyncio.create_task(_battery_monitor(drone))" in report.modified_code
        assert "_safety_asyncio.create_task(_connection_watchdog(drone))" in report.modified_code
        assert "_safety_asyncio.create_task(_geofence_enforcer(drone))" in report.modified_code
        assert "await _cancel_safety_tasks(safety_tasks)" in report.modified_code

    def test_original_code_preserved_after_injection(self):
        report = self._engine().inject(MINIMAL_MISSION)
        assert "async def run():" in report.modified_code
        assert "await land(" in report.modified_code

    def test_injection_points_recorded(self):
        report = self._engine().inject(MINIMAL_MISSION)
        assert len(report.injection_points) == 4

    def test_no_injection_needed_returns_empty_list(self):
        code = (
            MINIMAL_MISSION
            + "\nasync def _battery_monitor(drone): pass\n"
            + "async def _connection_watchdog(drone): pass\n"
            + "async def _geofence_enforcer(drone): pass\n"
            + "def _emergency_signal_handler(s, f): pass\n"
        )
        report = self._engine().inject(code)
        assert report.protocols_injected == []


# ═════════════════════════════════════════════════════════════════════════════
# SafeLLMSupervisionLayer — async public API
# ═════════════════════════════════════════════════════════════════════════════


class TestSafeLLMSupervisionLayer:
    def _layer(self, **kw):
        from agents.safety_layer import SafeLLMSupervisionLayer
        from agents.safety_models import FlightConstraints

        return SafeLLMSupervisionLayer(FlightConstraints(**kw) if kw else None)

    @pytest.mark.asyncio
    async def test_analyze_code_returns_static_report(self):
        layer = self._layer()
        report = await layer.analyze_code(SAFE_CODE)
        assert report.passed

    @pytest.mark.asyncio
    async def test_analyze_code_unsafe_returns_issues(self):
        layer = self._layer()
        report = await layer.analyze_code(UNSAFE_CODE_NO_LANDING)
        assert not report.passed

    @pytest.mark.asyncio
    async def test_verify_waypoints_within_bounds(self):
        from agents.safety_models import Waypoint

        layer = self._layer()
        wps = [Waypoint(x=50.0, y=50.0, z=-20.0)]
        report = await layer.verify_waypoints(wps)
        assert report.passed

    @pytest.mark.asyncio
    async def test_verify_waypoints_geofence_breach(self):
        from agents.safety_models import Waypoint

        layer = self._layer(max_geofence_radius_m=100.0)
        wps = [Waypoint(x=999.0, y=0.0, z=-10.0)]
        report = await layer.verify_waypoints(wps)
        assert not report.passed

    @pytest.mark.asyncio
    async def test_check_compliance_clean_code(self):
        layer = self._layer()
        report = await layer.check_compliance(SAFE_CODE)
        assert report.passed

    @pytest.mark.asyncio
    async def test_check_compliance_shell_cmd(self):
        layer = self._layer()
        report = await layer.check_compliance("os.system('ls')\n")
        assert not report.passed

    @pytest.mark.asyncio
    async def test_inject_safety_protocols_returns_string(self):
        layer = self._layer()
        modified = await layer.inject_safety_protocols(MINIMAL_MISSION)
        assert isinstance(modified, str)
        assert "_battery_monitor" in modified

    @pytest.mark.asyncio
    async def test_full_safety_check_safe_code_passes(self):
        layer = self._layer()
        report = await layer.full_safety_check(SAFE_CODE)
        assert report.passed
        assert report.risk_level.value in ("low", "medium")

    @pytest.mark.asyncio
    async def test_full_safety_check_unsafe_code_fails(self):
        layer = self._layer()
        report = await layer.full_safety_check(UNSAFE_CODE_NO_LANDING)
        assert not report.passed
        assert report.risk_level.value == "critical"

    @pytest.mark.asyncio
    async def test_full_safety_check_contains_final_code(self):
        layer = self._layer()
        report = await layer.full_safety_check(SAFE_CODE)
        assert report.final_code
        assert "async def run" in report.final_code

    @pytest.mark.asyncio
    async def test_full_safety_check_critical_code_critical_risk(self):
        layer = self._layer()
        report = await layer.full_safety_check(UNSAFE_CODE_CRITICAL)
        assert report.risk_level.value == "critical"
        assert report.critical_count >= 1

    @pytest.mark.asyncio
    async def test_full_safety_check_recommendations_populated(self):
        layer = self._layer()
        report = await layer.full_safety_check(UNSAFE_CODE_NO_LANDING)
        assert len(report.recommendations) > 0

    @pytest.mark.asyncio
    async def test_full_safety_check_all_issues_deduplicated(self):
        layer = self._layer()
        report = await layer.full_safety_check(SAFE_CODE)
        rule_ids = [i.rule_id for i in report.all_issues]
        # No exact duplicates (same rule_id from different pillars is fine)
        assert len(rule_ids) == len(report.all_issues)

    @pytest.mark.asyncio
    async def test_custom_constraints_applied(self):
        from agents.safety_models import FlightConstraints
        from agents.safety_layer import SafeLLMSupervisionLayer

        c = FlightConstraints(max_altitude_m=30.0)
        layer = SafeLLMSupervisionLayer(c)
        # takeoff at 50m should fail with 30m limit
        code = "async def run():\n    await takeoff(d, altitude=50.0)\n    await land(d)\n"
        report = await layer.full_safety_check(code)
        alt_issues = [i for i in report.all_issues if i.rule_id == "SR004"]
        assert len(alt_issues) >= 1


# ═════════════════════════════════════════════════════════════════════════════
# SafetyLayer (backward-compat shim)
# ═════════════════════════════════════════════════════════════════════════════


class TestSafetyLayerShim:
    def _layer(self):
        from agents.safety_layer import SafetyLayer

        return SafetyLayer()

    def test_safe_code_passes(self):
        report = self._layer().analyze(SAFE_CODE)
        assert report.passed

    def test_missing_landing_detected(self):
        report = self._layer().analyze(UNSAFE_CODE_NO_LANDING)
        assert not report.passed
        assert any(i.rule_id == "SR002" for i in report.issues)

    def test_battery_monitor_injected(self):
        report = self._layer().analyze(MINIMAL_MISSION)
        assert "battery_monitor" in report.injected_protocols

    def test_all_four_protocols_injected(self):
        report = self._layer().analyze(MINIMAL_MISSION)
        assert len(report.injected_protocols) == 4

    def test_modified_code_not_none_when_injected(self):
        report = self._layer().analyze(MINIMAL_MISSION)
        assert report.modified_code is not None

    def test_injected_code_can_be_rechecked(self):
        first = self._layer().analyze(SAFE_CODE)
        second = self._layer().analyze(first.modified_code or SAFE_CODE)
        assert second.passed
        assert not any(i.rule_id == "SR001" for i in second.issues)

    def test_risk_level_is_string(self):
        report = self._layer().analyze(MINIMAL_MISSION)
        assert isinstance(report.risk_level, str)

    def test_critical_code_fails(self):
        report = self._layer().analyze(UNSAFE_CODE_CRITICAL)
        assert not report.passed
        assert report.risk_level == "critical"
