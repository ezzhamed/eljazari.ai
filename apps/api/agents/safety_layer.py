"""eljazari.ai — Safe-LLM Supervision Layer (Prompt 2.2)

Sits invisibly between the Code Agent and the Test Agent / real drone.
Every piece of generated code passes through all three pillars before
it is allowed to proceed.

Three Pillars
─────────────
1. Static Analysis        — AST parsing: function whitelist, altitude/speed
                            bounds, landing sequence, error handling coverage
2. Reachability Analysis  — Waypoint extraction + battery math + geofence
                            checks.  Guarantees every waypoint is reachable
                            and RTL is always possible.
3. RAG Compliance Filter  — Pattern-matching against the flight_rules YAML/JSON
                            database.  Catches regulatory violations not visible
                            in the AST (e.g. forbidden orbit directions, no
                            pre-flight check).

After analysis the Injection Engine patches the code with:
  • Battery monitoring loop       (IP001)
  • Connection watchdog           (IP002)
  • Geofence enforcer             (IP003)
  • Emergency stop global handler (IP004)
"""

import ast
import json
import math
import re
import textwrap
from typing import Any

import structlog
import yaml

from config.paths import PROJECT_ROOT

from agents.safety_models import (
    ComplianceReport,
    ComplianceViolation,
    FlightConstraints,
    FullSafetyReport,
    InjectionReport,
    RiskLevel,
    SafetyIssue,
    SafetyReport,
    Severity,
    StaticAnalysisReport,
    Waypoint,
    WaypointSafetyReport,
)

log = structlog.get_logger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_RULES_DIR = PROJECT_ROOT / "data" / "safety-rules"
_YAML_RULES = _RULES_DIR / "flight_rules.yaml"
_JSON_RULES = _RULES_DIR / "flight_rules.json"

# ── Approved Skill API whitelist ──────────────────────────────────────────────
# Phase 8: this set now covers REAL MAVSDK methods. Each entry below is a
# function name as it appears in the ``mavsdk`` package or in the generated
# helper code. We keep the legacy skill-API names (takeoff/fly_to/...) so the
# Phase 5/6 tests, golden fixtures, and the closed-loop refinement helper
# pass unchanged; new generations should prefer the MAVSDK names listed in
# code_generator.py.
APPROVED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # ── Legacy skill-API (deprecated — kept for backwards compat) ────────
        "takeoff",
        "land",
        "emergency_stop",
        "fly_to",
        "fly_to_relative",
        "set_yaw",
        "orbit",
        "get_position",
        "get_battery",
        "get_attitude",
        "get_velocity",
        "set_geofence",
        "set_return_to_launch_alt",
        "check_pre_flight",
        # ── Real MAVSDK plugin methods (Phase 8) ─────────────────────────────
        # drone.action.*
        "arm",
        "disarm",
        "kill",
        "return_to_launch",
        "set_takeoff_altitude",
        "set_return_to_launch_altitude",
        "set_current_speed",
        "transition_to_fixedwing",
        "transition_to_multicopter",
        # drone.mission.*
        "upload_mission",
        "start_mission",
        "pause_mission",
        "clear_mission",
        "set_return_to_launch_after_mission",
        "mission_progress",
        "download_mission",
        # drone.telemetry.*
        "battery",
        "position_velocity_ned",
        "attitude_euler",
        "attitude_quaternion",
        "gps_info",
        "home",
        "landed_state",
        "health",
        "in_air",
        "armed",
        "flight_mode",
        "rc_status",
        # PX4's own messages (for example «Low battery», «Geofence violation»),
        # read only so a failed mission can say why.
        "status_text",
        # drone.geofence.*
        "upload_geofence",
        # drone.param.* — PX4 parameter access (used for the param-based geofence:
        # GF_ACTION / GF_MAX_HOR_DIST / GF_MAX_VER_DIST).
        "set_param_int",
        "set_param_float",
        "get_param_int",
        "get_param_float",
        # drone.core
        "connection_state",
        # MAVSDK constructors (treated as call-allowed because LLM uses them)
        "MissionItem",
        "MissionPlan",
        "Point",
        "Polygon",
        "GeofenceData",
        # ── Python built-ins / async primitives ─────────────────────────────
        "print",
        "range",
        "len",
        "abs",
        "min",
        "max",
        "round",
        "float",
        "int",
        "str",
        "bool",
        "list",
        "tuple",
        "dict",
        "set",
        "getattr",
        "isinstance",
        "asyncio",
        "sleep",
        "monotonic",
        "create_task",
        "wait_for",
        "get_event_loop",
        "time",
        # Standard-library helpers required by generated MAVSDK mission code.
        "exit",
        "append",
        "extend",
        "items",
        "keys",
        "values",
        "format",
        "strip",
        "lower",
        "upper",
        "split",
        "join",
        "signal",
        # Exception classes (constructed via Call but not real flight actions).
        "RuntimeError",
        "TimeoutError",
        "ValueError",
        "Exception",
        "OSError",
        # math.* helpers needed for NED→LLA conversion inside the template.
        "cos",
        "sin",
        "tan",
        "radians",
        "degrees",
        "atan2",
        "asin",
        "acos",
        "sqrt",
        "hypot",
        "pi",
        "isnan",
        # ── Logging helpers ──────────────────────────────────────────────────
        "getLogger",
        "basicConfig",
        "info",
        "warning",
        "error",
        "debug",
        "critical",
        # ── MAVSDK connection / system entry ────────────────────────────────
        "connect",
        "System",
        "MAVSDKRuntimeAdapter",
        # ── Standard async entry points ─────────────────────────────────────
        "run",
        "main",
    }
)

# ── Numeric literal patterns for AST extraction ──────────────────────────────
_ALTITUDE_KEYWORDS = {"altitude", "alt", "height", "z", "altitude_m", "max_alt_m"}
_SPEED_KEYWORDS = {"speed", "velocity", "vel", "speed_ms"}
_ALLOWED_ENV_VARS = frozenset({"DRONEPROMPT_MAVSDK_ADDRESS"})


# ═════════════════════════════════════════════════════════════════════════════
# Pillar 1 — Static AST Analysis
# ═════════════════════════════════════════════════════════════════════════════


class _StaticAnalyser:
    """Pure AST checker — no LLM involved."""

    def __init__(self, constraints: FlightConstraints) -> None:
        self.c = constraints

    def analyse(self, code: str) -> StaticAnalysisReport:
        issues: list[SafetyIssue] = []
        unapproved: list[str] = []
        alt_violations: list[dict] = []
        spd_violations: list[dict] = []

        # ── Parse ─────────────────────────────────────────────────────────────
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return StaticAnalysisReport(
                passed=False,
                issues=[
                    SafetyIssue(
                        rule_id="SR000",
                        severity=Severity.CRITICAL,
                        description=f"Syntax error: {exc}",
                        suggestion="Fix syntax error — code cannot be analysed further",
                    )
                ],
            )

        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                setattr(child, "_safety_parent", parent)

        all_calls = list(ast.walk(tree))
        locally_defined = {
            node.name
            for node in all_calls
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        # SR001 — function whitelist
        for node in all_calls:
            if isinstance(node, ast.Call):
                func_name = self._call_name(node)
                if (
                    func_name
                    and func_name not in APPROVED_FUNCTIONS
                    and func_name not in locally_defined
                    and not func_name.startswith("_")
                    and not self._is_allowed_support_call(node, func_name)
                ):
                    unapproved.append(func_name)
                    issues.append(
                        SafetyIssue(
                            rule_id="SR001",
                            severity=Severity.CRITICAL,
                            description=f"Unapproved function call: {func_name}()",
                            line_number=getattr(node, "lineno", None),
                            suggestion=f"Replace {func_name}() with an approved Skill API function",
                        )
                    )

        # SR002 — landing sequence
        func_names_called = {self._call_name(n) for n in all_calls if isinstance(n, ast.Call)}
        has_landing = any(
            name in func_names_called
            for name in {
                "land",
                "emergency_stop",
                "return_to_launch",
                "set_return_to_launch_after_mission",
            }
        )
        if not has_landing:
            issues.append(
                SafetyIssue(
                    rule_id="SR002",
                    severity=Severity.CRITICAL,
                    description="No landing sequence detected (explicit land, RTL, or set_return_to_launch_after_mission must be called)",
                    suggestion="Add land(system), return_to_launch(system), or set_return_to_launch_after_mission(True) to ensure safe recovery",
                    auto_fixable=True,
                )
            )

        # SR003 — try/except coverage
        if not any(isinstance(n, ast.Try) for n in all_calls):
            issues.append(
                SafetyIssue(
                    rule_id="SR003",
                    severity=Severity.HIGH,
                    description="No try/except error handling found",
                    suggestion="Wrap all flight operations in try/except with safe landing fallback",
                    auto_fixable=True,
                )
            )

        # SR004 — altitude bounds (scan numeric constants assigned to altitude-like names)
        for node in all_calls:
            if isinstance(node, ast.keyword) and node.arg in _ALTITUDE_KEYWORDS:
                val = self._const_value(node.value)
                if val is not None:
                    if val > self.c.max_altitude_m:
                        alt_violations.append(
                            {
                                "value": val,
                                "limit": self.c.max_altitude_m,
                                "line": getattr(node.value, "lineno", None),
                            }
                        )
                        issues.append(
                            SafetyIssue(
                                rule_id="SR004",
                                severity=Severity.HIGH,
                                description=f"Altitude {val}m exceeds max allowed {self.c.max_altitude_m}m",
                                line_number=getattr(node.value, "lineno", None),
                                suggestion=f"Reduce altitude to ≤ {self.c.max_altitude_m}m",
                            )
                        )
                    elif val < 0:
                        issues.append(
                            SafetyIssue(
                                rule_id="SR004",
                                severity=Severity.CRITICAL,
                                description=f"Negative altitude {val}m — drone would fly underground",
                                line_number=getattr(node.value, "lineno", None),
                                suggestion="Use positive altitude values (metres AGL)",
                            )
                        )

        # Scan positional numeric args to fly_to / takeoff
        for node in all_calls:
            if isinstance(node, ast.Call):
                fname = self._call_name(node)
                if fname in {"takeoff", "fly_to", "fly_to_relative"}:
                    for i, arg in enumerate(node.args):
                        val = self._const_value(arg)
                        if val is not None and i == 0 and fname == "takeoff":
                            if val > self.c.max_altitude_m:
                                issues.append(
                                    SafetyIssue(
                                        rule_id="SR004",
                                        severity=Severity.HIGH,
                                        description=f"takeoff altitude {val}m > max {self.c.max_altitude_m}m",
                                        line_number=getattr(arg, "lineno", None),
                                        suggestion=f"Reduce takeoff altitude to ≤ {self.c.max_altitude_m}m",
                                    )
                                )

        # SR005 — speed bounds
        for node in all_calls:
            if isinstance(node, ast.keyword) and node.arg in _SPEED_KEYWORDS:
                val = self._const_value(node.value)
                if val is not None and val > self.c.max_speed_ms:
                    spd_violations.append({"value": val, "limit": self.c.max_speed_ms})
                    issues.append(
                        SafetyIssue(
                            rule_id="SR005",
                            severity=Severity.MEDIUM,
                            description=f"Speed {val} m/s exceeds max {self.c.max_speed_ms} m/s",
                            line_number=getattr(node.value, "lineno", None),
                            suggestion=f"Reduce speed to ≤ {self.c.max_speed_ms} m/s",
                        )
                    )

        # SR006 — async/await required for MAVSDK calls
        has_await = any(isinstance(n, ast.Await) for n in all_calls)
        if not has_await and "def " in code:
            issues.append(
                SafetyIssue(
                    rule_id="SR006",
                    severity=Severity.HIGH,
                    description="No await expressions found — MAVSDK calls must be awaited",
                    suggestion="Use 'await' for all MAVSDK calls (async/await pattern required)",
                )
            )

        # SR007 — pre-flight check recommended
        if "check_pre_flight" not in func_names_called:
            issues.append(
                SafetyIssue(
                    rule_id="SR007",
                    severity=Severity.MEDIUM,
                    description="Pre-flight check (check_pre_flight) not called",
                    suggestion="Add check_pre_flight(system) before takeoff()",
                    auto_fixable=True,
                )
            )

        # SR008 — connection check before arming
        if "connect" not in func_names_called and "System" not in func_names_called:
            issues.append(
                SafetyIssue(
                    rule_id="SR008",
                    severity=Severity.HIGH,
                    description="No drone connection setup found",
                    suggestion="Add drone = System() and await drone.connect() at the start",
                )
            )

        passed = not any(_is_blocking_issue(i) for i in issues)
        return StaticAnalysisReport(
            passed=passed,
            issues=issues,
            unapproved_functions=list(set(unapproved)),
            altitude_violations=alt_violations,
            speed_violations=spd_violations,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _call_name(node: ast.Call) -> str | None:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None

    @staticmethod
    def _is_allowed_support_call(node: ast.Call, func_name: str) -> bool:
        """Allow narrowly-scoped runtime plumbing without weakening flight safety."""
        if func_name != "getenv":
            path = _StaticAnalyser._attribute_path(node.func)
            if path in {
                ("drone", "telemetry", "battery"),
                ("drone", "core", "connection_state"),
                ("drone", "telemetry", "position_velocity_ned"),
                # Read-only altitude above home, the frame PX4's takeoff target uses.
                ("drone", "telemetry", "position"),
                ("drone", "action", "kill"),
                ("_safety_asyncio", "create_task"),
                ("_safety_asyncio", "gather"),
                ("_math", "hypot"),
                ("_log", "get_logger"),
            }:
                return True
            if func_name == "cancel":
                return _StaticAnalyser._enclosing_function_name(node) == "_cancel_safety_tasks"
            if path == ("_signal", "signal"):
                if len(node.args) < 2:
                    return False
                signal_path = _StaticAnalyser._attribute_path(node.args[0])
                handler = node.args[1]
                return (
                    signal_path in {("_signal", "SIGTERM"), ("_signal", "SIGINT")}
                    and isinstance(handler, ast.Name)
                    and handler.id == "_emergency_signal_handler"
                )
            if func_name == "SystemExit":
                return _StaticAnalyser._enclosing_function_name(node) == "_emergency_signal_handler"
            return False
        if not isinstance(node.func, ast.Attribute):
            return False
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "os":
            return False
        if not node.args or not isinstance(node.args[0], ast.Constant):
            return False
        return node.args[0].value in _ALLOWED_ENV_VARS

    @staticmethod
    def _attribute_path(node: ast.AST) -> tuple[str, ...] | None:
        if isinstance(node, ast.Name):
            return (node.id,)
        if isinstance(node, ast.Attribute):
            base = _StaticAnalyser._attribute_path(node.value)
            if base:
                return (*base, node.attr)
        return None

    @staticmethod
    def _enclosing_function_name(node: ast.AST) -> str | None:
        parent = getattr(node, "_safety_parent", None)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return parent.name
            parent = getattr(parent, "_safety_parent", None)
        return None

    @staticmethod
    def _const_value(node: ast.expr) -> float | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            inner = _StaticAnalyser._const_value(node.operand)
            return -inner if inner is not None else None
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Pillar 2 — Reachability Analysis
# ═════════════════════════════════════════════════════════════════════════════


class _ReachabilityAnalyser:
    """Extracts waypoints from AST and verifies physical feasibility."""

    def __init__(self, constraints: FlightConstraints) -> None:
        self.c = constraints

    def analyse(
        self, code: str, explicit_waypoints: list[Waypoint] | None = None
    ) -> WaypointSafetyReport:
        issues: list[SafetyIssue] = []
        geofence_violations: list[dict] = []
        altitude_violations: list[dict] = []

        waypoints = explicit_waypoints or self._extract_waypoints(code)

        if not waypoints:
            # No waypoints to check — still warn if code is non-trivial
            return WaypointSafetyReport(
                passed=True,
                waypoints_checked=0,
                issues=[
                    SafetyIssue(
                        rule_id="RR000",
                        severity=Severity.LOW,
                        description="No fly_to waypoints detected — only altitude-based flight",
                        suggestion="Verify mission covers intended area",
                    )
                ],
            )

        # Build path: home → wp1 → wp2 → ... → home (RTL)
        path: list[tuple[float, float]] = [(0.0, 0.0)]  # home = (N, E)
        for wp in waypoints:
            path.append((wp.x, wp.y))
        path.append((0.0, 0.0))  # RTL

        total_dist = self._path_length(path)
        battery_used = total_dist * self.c.battery_consumption_per_m
        battery_used_pct = min(battery_used, 100.0)

        rtl_feasible = (100.0 - battery_used_pct) >= self.c.battery_rtl_margin_pct

        if not rtl_feasible:
            issues.append(
                SafetyIssue(
                    rule_id="RR001",
                    severity=Severity.CRITICAL,
                    description=(
                        f"Insufficient battery for RTL: mission uses ~{battery_used_pct:.1f}% "
                        f"but RTL margin requires {self.c.battery_rtl_margin_pct}% reserve"
                    ),
                    suggestion="Reduce mission range or add battery-check waypoints",
                )
            )

        # Per-waypoint checks
        for idx, wp in enumerate(waypoints):
            label = wp.label or f"WP{idx + 1}"

            # Geofence
            dist_from_home = wp.horizontal_distance_from_home
            if dist_from_home > self.c.max_geofence_radius_m:
                geofence_violations.append(
                    {
                        "waypoint": label,
                        "distance_m": dist_from_home,
                        "limit_m": self.c.max_geofence_radius_m,
                    }
                )
                issues.append(
                    SafetyIssue(
                        rule_id="RR002",
                        severity=Severity.CRITICAL,
                        description=(
                            f"{label} is {dist_from_home:.1f}m from home — "
                            f"exceeds geofence {self.c.max_geofence_radius_m}m"
                        ),
                        suggestion=f"Keep all waypoints within {self.c.max_geofence_radius_m}m of home",
                    )
                )

            # Altitude
            alt = wp.altitude_agl
            if alt > self.c.max_altitude_m:
                altitude_violations.append(
                    {"waypoint": label, "alt": alt, "limit": self.c.max_altitude_m}
                )
                issues.append(
                    SafetyIssue(
                        rule_id="RR003",
                        severity=Severity.HIGH,
                        description=f"{label} altitude {alt:.1f}m exceeds max {self.c.max_altitude_m}m",
                        suggestion=f"Reduce {label} altitude to ≤ {self.c.max_altitude_m}m",
                    )
                )
            elif alt < self.c.min_altitude_m and alt > 0:
                issues.append(
                    SafetyIssue(
                        rule_id="RR004",
                        severity=Severity.HIGH,
                        description=f"{label} altitude {alt:.1f}m is below minimum {self.c.min_altitude_m}m",
                        suggestion=f"Increase altitude to ≥ {self.c.min_altitude_m}m",
                    )
                )

            # Speed
            if wp.speed_ms > self.c.max_speed_ms:
                issues.append(
                    SafetyIssue(
                        rule_id="RR005",
                        severity=Severity.MEDIUM,
                        description=f"{label} speed {wp.speed_ms} m/s > max {self.c.max_speed_ms} m/s",
                        suggestion=f"Reduce speed to ≤ {self.c.max_speed_ms} m/s",
                    )
                )

        passed = not any(_is_blocking_issue(i) for i in issues)
        return WaypointSafetyReport(
            passed=passed,
            waypoints_checked=len(waypoints),
            issues=issues,
            total_distance_m=round(total_dist, 2),
            estimated_battery_usage_pct=round(battery_used_pct, 2),
            battery_return_feasible=rtl_feasible,
            geofence_violations=geofence_violations,
            altitude_violations=altitude_violations,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _extract_waypoints(self, code: str) -> list[Waypoint]:
        """Best-effort: parse absolute and relative waypoint calls from AST."""
        waypoints: list[Waypoint] = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return waypoints

        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        calls.sort(key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)))

        mission_item_waypoints = self._extract_mission_item_waypoints(calls)
        if mission_item_waypoints:
            return mission_item_waypoints

        north = 0.0
        east = 0.0
        down = -self._extract_takeoff_altitude(calls)

        for node in calls:
            fname = self._call_name(node)
            if fname not in {"fly_to", "fly_to_relative"}:
                continue

            command = self._extract_motion_command(node, fname)
            if command is None:
                continue

            if fname == "fly_to_relative":
                north += command["north_m"]
                east += command["east_m"]
                if command["down_m"] != 0.0:
                    down += command["down_m"]
            else:
                north = command["north_m"]
                east = command["east_m"]
                down = command["down_m"]

            waypoints.append(
                Waypoint(
                    x=north,
                    y=east,
                    z=down,
                    speed_ms=command["speed_ms"],
                    label=f"WP{len(waypoints) + 1}",
                )
            )

        return waypoints

    def _extract_mission_item_waypoints(self, calls: list[ast.Call]) -> list[Waypoint]:
        raw_items: list[tuple[float, float, float, float]] = []
        for node in calls:
            if self._call_name(node) != "MissionItem":
                continue

            numeric_args = self._numeric_args(node)
            lat = self._extract_keyword_value(node, ("latitude_deg", "lat_deg", "lat"))
            lon = self._extract_keyword_value(node, ("longitude_deg", "lon_deg", "lon"))
            alt = self._extract_keyword_value(node, ("relative_altitude_m", "altitude_m", "alt"))
            speed = self._extract_keyword_value(node, ("speed_m_s", "speed_ms", "speed"))

            if lat is None and len(numeric_args) >= 1:
                lat = numeric_args[0]
            if lon is None and len(numeric_args) >= 2:
                lon = numeric_args[1]
            if alt is None and len(numeric_args) >= 3:
                alt = numeric_args[2]
            if speed is None and len(numeric_args) >= 4:
                speed = numeric_args[3]

            if lat is None or lon is None or alt is None:
                continue
            raw_items.append((lat, lon, max(0.0, alt), speed or 5.0))

        if not raw_items:
            return []

        home_lat, home_lon, _home_alt, _home_speed = raw_items[0]
        waypoints: list[Waypoint] = []
        for index, (lat, lon, alt, speed) in enumerate(raw_items, start=1):
            north_m, east_m = self._lat_lon_to_ne(home_lat, home_lon, lat, lon)
            waypoints.append(
                Waypoint(
                    x=north_m,
                    y=east_m,
                    z=-alt,
                    speed_ms=speed,
                    label=f"MissionItem{index}",
                )
            )
        return waypoints

    @staticmethod
    def _lat_lon_to_ne(
        home_lat: float, home_lon: float, lat: float, lon: float
    ) -> tuple[float, float]:
        north_m = (lat - home_lat) * 111_111.0
        avg_lat_rad = math.radians((home_lat + lat) / 2.0)
        east_m = (lon - home_lon) * 111_111.0 * math.cos(avg_lat_rad)
        return north_m, east_m

    @staticmethod
    def _path_length(path: list[tuple[float, float]]) -> float:
        total = 0.0
        for (x1, y1), (x2, y2) in zip(path, path[1:]):
            total += math.hypot(x2 - x1, y2 - y1)
        return total

    @staticmethod
    def _call_name(node: ast.Call) -> str | None:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None

    def _extract_takeoff_altitude(self, calls: list[ast.Call]) -> float:
        for node in calls:
            if self._call_name(node) != "takeoff":
                continue
            altitude = self._extract_keyword_value(
                node, ("altitude_m", "altitude", "alt", "height")
            )
            if altitude is not None:
                return max(0.0, altitude)

            args = self._numeric_args(node)
            if self._has_context_arg(node) and args:
                args = args[1:]
            if args and args[0] is not None:
                return max(0.0, args[0])
        return 10.0

    def _extract_motion_command(
        self,
        node: ast.Call,
        fname: str,
    ) -> dict[str, float] | None:
        numeric_args = self._numeric_args(node)
        skip_first = self._has_context_arg(node)
        if skip_first and numeric_args:
            numeric_args = numeric_args[1:]

        if fname == "fly_to_relative":
            north = self._extract_keyword_value(node, ("north_m", "north"))
            east = self._extract_keyword_value(node, ("east_m", "east"))
            down = self._extract_keyword_value(node, ("down_m", "down", "z"))
            speed = self._extract_keyword_value(node, ("speed_ms", "speed"))

            if north is None and len(numeric_args) >= 1 and numeric_args[0] is not None:
                north = numeric_args[0]
            if east is None and len(numeric_args) >= 2 and numeric_args[1] is not None:
                east = numeric_args[1]
            if down is None and len(numeric_args) >= 3 and numeric_args[2] is not None:
                down = numeric_args[2]
            if speed is None and len(numeric_args) >= 4 and numeric_args[3] is not None:
                speed = numeric_args[3]

            if north is None or east is None:
                return None

            return {
                "north_m": north,
                "east_m": east,
                "down_m": down or 0.0,
                "speed_ms": speed or 5.0,
            }

        north = self._extract_keyword_value(node, ("north_m", "north", "x"))
        east = self._extract_keyword_value(node, ("east_m", "east", "y"))
        down = self._extract_keyword_value(node, ("down_m", "down", "z"))
        speed = self._extract_keyword_value(node, ("speed_ms", "speed"))

        if north is None and len(numeric_args) >= 1 and numeric_args[0] is not None:
            north = numeric_args[0]
        if east is None and len(numeric_args) >= 2 and numeric_args[1] is not None:
            east = numeric_args[1]
        if down is None and len(numeric_args) >= 3 and numeric_args[2] is not None:
            down = numeric_args[2]
        if speed is None and len(numeric_args) >= 4 and numeric_args[3] is not None:
            speed = numeric_args[3]

        if north is None or east is None or down is None:
            return None

        return {
            "north_m": north,
            "east_m": east,
            "down_m": down,
            "speed_ms": speed or 5.0,
        }

    def _extract_keyword_value(self, node: ast.Call, names: tuple[str, ...]) -> float | None:
        for keyword in node.keywords:
            if keyword.arg in names:
                return _StaticAnalyser._const_value(keyword.value)
        return None

    def _numeric_args(self, node: ast.Call) -> list[float | None]:
        values: list[float | None] = []
        for arg in node.args:
            values.append(_StaticAnalyser._const_value(arg))
        return values

    @staticmethod
    def _has_context_arg(node: ast.Call) -> bool:
        if isinstance(node.func, ast.Attribute):
            return False
        if not node.args:
            return False
        return _StaticAnalyser._const_value(node.args[0]) is None


# ═════════════════════════════════════════════════════════════════════════════
# Pillar 3 — RAG Compliance Filter
# ═════════════════════════════════════════════════════════════════════════════


class _ComplianceChecker:
    """Loads flight_rules from YAML/JSON and checks code line-by-line."""

    def __init__(self) -> None:
        self._rules: list[dict[str, Any]] = self._load_rules()

    # ── rule loading ──────────────────────────────────────────────────────────

    def _load_rules(self) -> list[dict[str, Any]]:
        rules: list[dict[str, Any]] = []

        # Prefer JSON (faster parse, richer patterns)
        if _JSON_RULES.exists():
            with _JSON_RULES.open(encoding="utf-8") as fh:
                data = json.load(fh)
            rules.extend(data.get("compliance_rules", []))
        elif _YAML_RULES.exists():
            with _YAML_RULES.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            # Flatten all rule sections into one list
            for section in ("static_analysis_rules", "reachability_rules", "compliance_rules"):
                rules.extend(data.get(section, []))
        else:
            log.warning("compliance_checker.no_rules_file")

        return rules

    # ── public ────────────────────────────────────────────────────────────────

    def check(self, code: str) -> ComplianceReport:
        violations: list[ComplianceViolation] = []
        warnings: list[ComplianceViolation] = []
        lines = code.splitlines()

        for rule in self._rules:
            rule.get("patterns", [])
            forbidden: list[str] = rule.get("forbidden_patterns", [])
            required: list[str] = rule.get("required_patterns", [])

            severity = Severity(rule.get("severity", "low"))

            # Forbidden pattern — flag every matching line
            for pat in forbidden:
                for lineno, line in enumerate(lines, 1):
                    if re.search(pat, line):
                        v = ComplianceViolation(
                            rule_id=rule["id"],
                            rule_name=rule.get("name", ""),
                            description=rule.get("description", ""),
                            severity=severity,
                            matched_pattern=pat,
                            line_number=lineno,
                            suggestion=rule.get("suggestion", ""),
                        )
                        if severity in (Severity.CRITICAL, Severity.HIGH):
                            violations.append(v)
                        else:
                            warnings.append(v)

            # Required pattern — flag absence
            for pat in required:
                if not re.search(pat, code):
                    v = ComplianceViolation(
                        rule_id=rule["id"],
                        rule_name=rule.get("name", ""),
                        description=f"Required pattern missing: {rule.get('description', pat)}",
                        severity=severity,
                        matched_pattern=pat,
                        suggestion=rule.get("suggestion", f"Add pattern: {pat}"),
                    )
                    if severity in (Severity.CRITICAL, Severity.HIGH):
                        violations.append(v)
                    else:
                        warnings.append(v)

        passed = len(violations) == 0
        return ComplianceReport(
            passed=passed,
            rules_checked=len(self._rules),
            violations=violations,
            warnings=warnings,
        )


# ═════════════════════════════════════════════════════════════════════════════
# Injection Engine
# ═════════════════════════════════════════════════════════════════════════════

# ── Protocol templates ────────────────────────────────────────────────────────

_BATTERY_PERCENT_HELPER = textwrap.dedent("""\
# Safety helper: normalize MAVSDK battery units
def _battery_remaining_pct(battery):
    \"\"\"Return battery as percent whether MAVSDK reports 0..1 or 0..100.\"\"\"
    value = float(battery.remaining_percent)
    return value * 100.0 if value <= 1.0 else value
""")

_RETURN_TO_LAUNCH_HELPER = textwrap.dedent("""\
# Safety helper: concrete RTL action wrapper
async def return_to_launch(drone):
    \"\"\"Invoke MAVSDK RTL, with adapter fallback for generated wrappers.\"\"\"
    try:
        await drone.action.return_to_launch()
    except AttributeError:
        await drone.return_to_launch()
""")

_EMERGENCY_STOP_HELPER = textwrap.dedent("""\
# Safety helper: concrete emergency-stop action wrapper
async def emergency_stop(drone):
    \"\"\"Invoke MAVSDK kill, with adapter fallback for generated wrappers.\"\"\"
    try:
        await drone.action.kill()
    except AttributeError:
        await drone.emergency_stop()
""")


def _battery_monitor_template(min_battery_pct: float) -> str:
    return textwrap.dedent(f"""\
# [IP001] Safety Injection: Battery Monitor
async def _battery_monitor(drone):
    \"\"\"RTL when battery drops below safe threshold.\"\"\"
    async for battery in drone.telemetry.battery():
        battery_pct = _battery_remaining_pct(battery)
        if battery_pct < {min_battery_pct:.1f}:
            import structlog as _log
            _log.get_logger(__name__).warning("safety.low_battery_rtl",
                                              level=battery_pct)
            await return_to_launch(drone)
            break
""")


_CONNECTION_WATCHDOG = textwrap.dedent("""\
# ── [IP002] Safety Injection: Connection Watchdog ─────────────────────────
async def _connection_watchdog(drone):
    \"\"\"Initiate Return to Launch (RTL) if connection is lost.
    
    NOTE: Onboard PX4 datalink-loss failsafe (e.g. COM_RC_LOSS_T -> RTL) is
    the primary protection. This companion-script watchdog is a secondary
    protection layer.
    \"\"\"
    async for state in drone.core.connection_state():
        if not state.is_connected:
            import structlog as _log
            _log.get_logger(__name__).warning("safety.connection_lost_rtl")
            await return_to_launch(drone)
            break
# ─────────────────────────────────────────────────────────────────────────────
""")


def _geofence_enforcer_template(max_radius_m: float) -> str:
    return textwrap.dedent(f"""\
# [IP003] Safety Injection: Geofence Enforcer
async def _geofence_enforcer(drone, max_radius_m={max_radius_m:.1f}):
    \"\"\"RTL when drone exits geofence.\"\"\"
    import math as _math
    home_n, home_e = 0.0, 0.0
    async for pos in drone.telemetry.position_velocity_ned():
        n = pos.position.north_m
        e = pos.position.east_m
        if _math.hypot(n - home_n, e - home_e) > max_radius_m:
            import structlog as _log
            _log.get_logger(__name__).warning("safety.geofence_breach_rtl",
                                              north=n, east=e)
            await return_to_launch(drone)
            break
""")


_EMERGENCY_HANDLER = textwrap.dedent("""\
# ── [IP004] Safety Injection: Emergency Stop Handler ─────────────────────
import signal as _signal
import asyncio as _asyncio

def _emergency_signal_handler(signum, frame):
    import structlog as _log
    _log.get_logger(__name__).critical("safety.emergency_signal_received",
                                       signal=signum)
    raise SystemExit(1)

_signal.signal(_signal.SIGTERM, _emergency_signal_handler)
_signal.signal(_signal.SIGINT,  _emergency_signal_handler)
# ─────────────────────────────────────────────────────────────────────────────
""")


_RUN_MISSION_SAFETY_WRAPPER = textwrap.dedent("""\
# Safety helper: run injected monitors during mission execution
import asyncio as _safety_asyncio

async def _cancel_safety_tasks(tasks):
    for task in tasks:
        task.cancel()
    await _safety_asyncio.gather(*tasks, return_exceptions=True)

_ORIGINAL_RUN_MISSION = run_mission

async def run_mission(drone):
    safety_tasks = [
        _safety_asyncio.create_task(_battery_monitor(drone)),
        _safety_asyncio.create_task(_connection_watchdog(drone)),
        _safety_asyncio.create_task(_geofence_enforcer(drone)),
    ]
    try:
        return await _ORIGINAL_RUN_MISSION(drone)
    finally:
        await _cancel_safety_tasks(safety_tasks)
""")


class _InjectionEngine:
    """Prepends/appends safety protocol boilerplate to generated code."""

    def __init__(self, constraints: FlightConstraints | None = None) -> None:
        self.c = constraints or FlightConstraints()

    def inject(self, code: str) -> InjectionReport:
        injected: list[str] = []
        injection_points: list[dict] = []
        header_blocks: list[str] = []
        tail_blocks: list[str] = []
        geofence_radius_m = self._extract_geofence_radius_m(code) or self.c.max_geofence_radius_m

        if "_battery_remaining_pct" not in code:
            header_blocks.append(_BATTERY_PERCENT_HELPER)

        if not self._defines_function(code, "return_to_launch"):
            header_blocks.append(_RETURN_TO_LAUNCH_HELPER)

        if not self._defines_function(code, "emergency_stop"):
            header_blocks.append(_EMERGENCY_STOP_HELPER)

        if "_battery_monitor" not in code:
            header_blocks.append(_battery_monitor_template(self.c.min_battery_pct))
            injected.append("battery_monitor")
            injection_points.append({"protocol": "IP001", "position": "header"})
        elif self._has_legacy_battery_monitor(code):
            tail_blocks.append(_battery_monitor_template(self.c.min_battery_pct))
            injected.append("battery_monitor")
            injection_points.append({"protocol": "IP001", "position": "tail_upgrade"})

        if "_connection_watchdog" not in code:
            header_blocks.append(_CONNECTION_WATCHDOG)
            injected.append("connection_watchdog")
            injection_points.append({"protocol": "IP002", "position": "header"})

        if "_geofence_enforcer" not in code:
            header_blocks.append(_geofence_enforcer_template(geofence_radius_m))
            injected.append("geofence_enforcer")
            injection_points.append({"protocol": "IP003", "position": "header"})
        elif self._has_legacy_geofence_enforcer(code, geofence_radius_m):
            tail_blocks.append(_geofence_enforcer_template(geofence_radius_m))
            injected.append("geofence_enforcer")
            injection_points.append({"protocol": "IP003", "position": "tail_upgrade"})

        if "_emergency_signal_handler" not in code:
            header_blocks.append(_EMERGENCY_HANDLER)
            injected.append("emergency_handler")
            injection_points.append({"protocol": "IP004", "position": "header"})

        if self._should_wrap_run_mission(code):
            tail_blocks.append(_RUN_MISSION_SAFETY_WRAPPER)
            injected.append("run_mission_safety_wrapper")
            injection_points.append({"protocol": "IP005", "position": "tail"})

        modified = code
        if header_blocks:
            modified = "\n".join(header_blocks) + "\n" + modified
        if tail_blocks:
            modified = modified.rstrip() + "\n\n" + "\n".join(tail_blocks)

        return InjectionReport(
            protocols_injected=injected,
            modified_code=modified,
            injection_points=injection_points,
        )

    @staticmethod
    def _has_legacy_battery_monitor(code: str) -> bool:
        return (
            "_battery_monitor" in code
            and "_battery_remaining_pct(battery)" not in code
            and re.search(r"remaining_percent\s*<\s*0(?:\.\d+)?", code) is not None
        )

    @staticmethod
    def _has_legacy_geofence_enforcer(code: str, expected_radius_m: float) -> bool:
        if "_geofence_enforcer" not in code:
            return False
        legacy_default = "max_radius_m=500.0" in code
        expected_default = f"max_radius_m={expected_radius_m:.1f}" in code
        return legacy_default and not expected_default

    def _should_wrap_run_mission(self, code: str) -> bool:
        return (
            self._defines_function(code, "run_mission")
            and "_ORIGINAL_RUN_MISSION = run_mission" not in code
        )

    @staticmethod
    def _defines_function(code: str, name: str) -> bool:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return f"def {name}" in code
        return any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
            for node in ast.walk(tree)
        )

    @staticmethod
    def _extract_geofence_radius_m(code: str) -> float | None:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return None

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _StaticAnalyser._call_name(node) != "set_geofence":
                continue
            for keyword in node.keywords:
                if keyword.arg == "radius_m" and isinstance(keyword.value, ast.Constant):
                    try:
                        return float(keyword.value.value)
                    except (TypeError, ValueError):
                        return None
            if node.args and isinstance(node.args[0], ast.Constant):
                try:
                    return float(node.args[0].value)
                except (TypeError, ValueError):
                    return None
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Public API — SafeLLMSupervisionLayer
# ═════════════════════════════════════════════════════════════════════════════


class SafeLLMSupervisionLayer:
    """Prompt 2.2: Three-pillar safety supervision for LLM-generated drone code.

    All public methods are ``async`` so they can be called directly from
    LangGraph async nodes without blocking the event loop.
    """

    def __init__(self, constraints: FlightConstraints | None = None) -> None:
        self._constraints = constraints or FlightConstraints()
        self._static = _StaticAnalyser(self._constraints)
        self._reach = _ReachabilityAnalyser(self._constraints)
        self._compliance = _ComplianceChecker()
        self._injection = _InjectionEngine(self._constraints)

    # ── Pillar 1 ──────────────────────────────────────────────────────────────

    async def analyze_code(
        self,
        code: str,
        constraints: FlightConstraints | None = None,
    ) -> StaticAnalysisReport:
        """Run AST-based static analysis on generated code."""
        if constraints:
            self._static = _StaticAnalyser(constraints)
        return self._static.analyse(code)

    # ── Pillar 2 ──────────────────────────────────────────────────────────────

    async def verify_waypoints(
        self,
        waypoints: list[Waypoint],
        constraints: FlightConstraints | None = None,
        code: str = "",
    ) -> WaypointSafetyReport:
        """Check every waypoint for geofence, altitude, and battery feasibility."""
        c = constraints or self._constraints
        analyser = _ReachabilityAnalyser(c)
        explicit = waypoints if waypoints else None
        return analyser.analyse(code, explicit_waypoints=explicit)

    # ── Pillar 3 ──────────────────────────────────────────────────────────────

    async def check_compliance(self, code: str) -> ComplianceReport:
        """RAG-style compliance check against flight_rules database."""
        return self._compliance.check(code)

    # ── Injection engine ──────────────────────────────────────────────────────

    async def inject_safety_protocols(self, code: str) -> str:
        """Add all four safety protocol blocks to the generated code."""
        report = _InjectionEngine(self._constraints).inject(code)
        return report.modified_code

    # ── Full check (orchestrates all three pillars) ───────────────────────────

    async def full_safety_check(
        self,
        code: str,
        constraints: FlightConstraints | None = None,
    ) -> FullSafetyReport:
        """Run all three pillars and the injection engine, return unified report."""
        c = constraints or self._constraints
        log.info("safety.full_check.start", code_len=len(code))

        # Pillar 1
        static_report = _StaticAnalyser(c).analyse(code)

        # Pillar 2 — extract waypoints from code and verify
        waypoint_report = _ReachabilityAnalyser(c).analyse(code)

        # Pillar 3
        compliance_report = self._compliance.check(code)

        # Injection
        injection_report = _InjectionEngine(c).inject(code)

        # Consolidate all issues
        all_issues: list[SafetyIssue] = []
        all_issues.extend(static_report.issues)
        all_issues.extend(waypoint_report.issues)
        all_issues.extend(
            SafetyIssue(
                rule_id=v.rule_id,
                severity=v.severity,
                description=v.description,
                line_number=v.line_number,
                suggestion=v.suggestion,
            )
            for v in compliance_report.violations
        )

        # Risk level
        risk = _compute_risk(all_issues)

        # Reject critical issues and explicit mission-bound/pre-flight violations
        passed = not any(_is_blocking_issue(i) for i in all_issues)

        # Recommendations: top 5 unique suggestions
        recs: list[str] = []
        seen: set[str] = set()
        for issue in sorted(all_issues, key=lambda i: -i.severity.weight):
            if issue.suggestion not in seen:
                recs.append(issue.suggestion)
                seen.add(issue.suggestion)
            if len(recs) >= 5:
                break

        log.info(
            "safety.full_check.complete",
            passed=passed,
            risk=risk,
            issues=len(all_issues),
            injected=len(injection_report.protocols_injected),
        )

        return FullSafetyReport(
            passed=passed,
            risk_level=risk,
            static_report=static_report,
            waypoint_report=waypoint_report,
            compliance_report=compliance_report,
            injection_report=injection_report,
            all_issues=all_issues,
            recommendations=recs,
            final_code=injection_report.modified_code,
        )


# ═════════════════════════════════════════════════════════════════════════════
# Backward-compatible shim — used by graph/nodes.py
# ═════════════════════════════════════════════════════════════════════════════


def _is_blocking_issue(issue: SafetyIssue) -> bool:
    # Mission bounds and missing pre-flight checks cannot be waived by a
    # medium/high severity label. Injection alone does not prove repair.
    return issue.severity == Severity.CRITICAL or issue.rule_id in {
        "SR004",
        "SR005",
        "SR007",
        "RR001",
        "RR002",
        "RR003",
        "RR004",
        "RR005",
    }


def _compute_risk(issues: list[SafetyIssue]) -> RiskLevel:
    if any(i.severity == Severity.CRITICAL for i in issues):
        return RiskLevel.CRITICAL
    if any(i.severity == Severity.HIGH for i in issues):
        return RiskLevel.HIGH
    if any(i.severity == Severity.MEDIUM for i in issues):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


class SafetyLayer:
    """Backward-compatible synchronous wrapper around SafeLLMSupervisionLayer.

    graph/nodes.py calls ``safety_layer.analyze(code)`` synchronously.
    This class preserves that contract while delegating to the new layer.
    """

    def __init__(
        self,
        max_altitude: float = 120.0,
        max_speed: float = 15.0,
        max_geofence_radius: float = 500.0,
    ) -> None:
        self._layer = SafeLLMSupervisionLayer(
            FlightConstraints(
                max_altitude_m=max_altitude,
                max_speed_ms=max_speed,
                max_geofence_radius_m=max_geofence_radius,
            )
        )

    def analyze(self, code: str) -> SafetyReport:
        """Synchronous full safety check ? returns simplified SafetyReport."""
        import asyncio

        async def _run():
            return await self._layer.full_safety_check(code)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            full = asyncio.run(_run())
        else:
            # Inside an async context ? run the sync wrapper on a dedicated loop.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _run())
                full = future.result()

        return SafetyReport(
            passed=full.passed,
            risk_level=full.risk_level.value,
            issues=full.all_issues,
            injected_protocols=full.injection_report.protocols_injected,
            modified_code=full.injection_report.modified_code or None,
            recommendations=full.recommendations,
        )
