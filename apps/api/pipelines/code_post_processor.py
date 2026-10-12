"""eljazari.ai — Phase 8.5: Post-LLM code rewriter.

Phase 8's GSCE prompt rewrite told the LLM to use real MAVSDK. The live
end-to-end run revealed that qwen2.5-coder:7b still emits legacy skill-API
calls (``drone.fly_to_relative(...)``, ``drone.takeoff(altitude_m=...)``,
``drone.set_geofence(...)``) — it mimics its training data rather than
following the new prompt. The prompt update was necessary but not
sufficient.

This module is the *deterministic enforcement* step: it AST-scans the
LLM's output, extracts the intended mission (waypoints, altitudes,
geofence radius), and **rewrites** the mission body using the real-MAVSDK
template from :mod:`pipelines.mavsdk_templates`. Safety injections from
the SafetyLayer are preserved verbatim — they already use real MAVSDK
calls (``drone.action.return_to_launch()``, ``drone.telemetry.battery()``).

Strategy:
  1. Parse the LLM-produced code as AST.
  2. Find every ``await drone.fly_to_relative(north_m=N, east_m=E, ...)``
     call and read the literal argument values.
  3. Find the ``await drone.takeoff(altitude_m=A)`` call.
  4. Find ``await drone.set_geofence(radius_m=R, ...)``.
  5. Build a :class:`MissionPlanSpec` from those values.
  6. If any legacy calls were detected, return ``rewrite_to_real_mavsdk``
     output (the SafetyLayer's injection block prepended, then the real
     MAVSDK mission body). Otherwise return the code unchanged.

The rewriter is *idempotent*: running it on code that already uses real
MAVSDK is a no-op.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass

from pipelines.mavsdk_templates import render_real_mavsdk_mission
from pipelines.mission_builder import MissionPlanSpec, Waypoint
from utils.coordinates import PX4_SITL_DEFAULT_HOME

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Legacy skill-API call names that must be rewritten
# ─────────────────────────────────────────────────────────────────────────────

_LEGACY_CALLS: frozenset[str] = frozenset(
    {
        "fly_to_relative",
        "fly_to",
        "takeoff",  # only when called with altitude_m kwarg
        "set_geofence",
        "check_pre_flight",
        "emergency_stop",
        "set_return_to_launch_alt",
    }
)


@dataclass
class ExtractedPlan:
    """Mission plan reconstructed from legacy skill-API calls."""

    takeoff_altitude_m: float = 4.0
    waypoints: list[Waypoint] = None  # type: ignore[assignment]
    geofence_radius_m: float | None = None
    has_legacy_calls: bool = False
    legacy_call_names: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.waypoints is None:
            self.waypoints = []
        if self.legacy_call_names is None:
            self.legacy_call_names = []


# ─────────────────────────────────────────────────────────────────────────────
# Extraction
# ─────────────────────────────────────────────────────────────────────────────


def _node_to_float(node: ast.AST) -> float | None:
    """Decode an AST node as a numeric literal (handles negative numbers).

    Python parses ``-40.0`` as ``UnaryOp(USub, Constant(40.0))``, so a
    naive ``isinstance(node, Constant)`` check misses every negative
    literal. That bug let the rewriter accumulate wrong NED positions on
    return-leg waypoints.
    """
    if isinstance(node, ast.Constant):
        try:
            return float(node.value)
        except (TypeError, ValueError):
            return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _node_to_float(node.operand)
        return -inner if inner is not None else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
        return _node_to_float(node.operand)
    return None


def _literal_arg(call: ast.Call, name: str, position: int = -1) -> float | None:
    """Extract a numeric literal from a kwarg (``name``) or positional arg."""
    for kw in call.keywords:
        if kw.arg == name:
            v = _node_to_float(kw.value)
            if v is not None:
                return v
    if position >= 0 and position < len(call.args):
        v = _node_to_float(call.args[position])
        if v is not None:
            return v
    return None


def _has_kwarg(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in call.keywords)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def extract_plan_from_legacy_code(code: str) -> ExtractedPlan:
    """Walk the LLM's output AST and reconstruct the intended mission.

    Returns an :class:`ExtractedPlan` describing what the LLM *meant* to
    do. ``has_legacy_calls`` is True when at least one rewrite-worthy
    call was detected.
    """
    plan = ExtractedPlan()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        logger.warning("code_post_processor.syntax_error")
        return plan

    # Track cumulative NED position so successive fly_to_relative calls
    # become absolute waypoints. Start at (0, 0).
    cur_n, cur_e = 0.0, 0.0
    wp_idx = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in _LEGACY_CALLS:
            continue
        # ``takeoff`` is ambiguous: ``drone.action.takeoff()`` is real
        # MAVSDK while ``drone.takeoff(altitude_m=X)`` is the legacy
        # skill-API call. Only treat the latter as a legacy pattern.
        if name == "takeoff" and not (_has_kwarg(node, "altitude_m") or node.args):
            continue
        plan.has_legacy_calls = True
        plan.legacy_call_names.append(name)

        if name == "takeoff":
            alt = _literal_arg(node, "altitude_m", position=0)
            if alt is not None:
                plan.takeoff_altitude_m = alt
        elif name == "fly_to_relative":
            n = _literal_arg(node, "north_m", position=0) or 0.0
            e = _literal_arg(node, "east_m", position=1) or 0.0
            d = _literal_arg(node, "down_m", position=2) or 0.0
            cur_n += n
            cur_e += e
            wp_idx += 1
            plan.waypoints.append(
                Waypoint(
                    name=f"leg_{wp_idx}",
                    north_m=cur_n,
                    east_m=cur_e,
                    alt_m=max(plan.takeoff_altitude_m - d, 1.0),
                )
            )
        elif name == "fly_to":
            # Absolute lat/lon — too complex for skill-API rewrite; fall
            # back to a simple "stay at home" waypoint and let the human
            # fix it.
            wp_idx += 1
            plan.waypoints.append(
                Waypoint(
                    name=f"absolute_{wp_idx}",
                    north_m=cur_n,
                    east_m=cur_e,
                    alt_m=plan.takeoff_altitude_m,
                )
            )
        elif name == "set_geofence":
            r = _literal_arg(node, "radius_m", position=0)
            if r is not None:
                plan.geofence_radius_m = r

    # Always end at home so the generated mission has an RTL waypoint.
    if plan.waypoints and (plan.waypoints[-1].north_m != 0 or plan.waypoints[-1].east_m != 0):
        wp_idx += 1
        plan.waypoints.append(
            Waypoint(
                name="return_home",
                north_m=0.0,
                east_m=0.0,
                alt_m=plan.takeoff_altitude_m,
            )
        )

    return plan


# ─────────────────────────────────────────────────────────────────────────────
# Distance bounds extracted from the prompt (Item #4 — distance validator)
# ─────────────────────────────────────────────────────────────────────────────

import re

_PROMPT_DISTANCE_RX = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:m|metre|meter|metres|meters)\b",
    re.IGNORECASE,
)


def extract_max_distance_from_prompt(prompt: str) -> float | None:
    """Return the largest distance referenced in the prompt (m), or None.

    The result is an upper-bound estimate. Used by
    :func:`validate_plan_distance` to catch LLM 6 m → 40 m drift.
    """
    if not prompt:
        return None
    distances = []
    for m in _PROMPT_DISTANCE_RX.finditer(prompt):
        try:
            distances.append(float(m.group(1)))
        except (TypeError, ValueError):
            continue
    return max(distances) if distances else None


def validate_plan_distance(
    plan: ExtractedPlan,
    prompt: str,
    *,
    tolerance_factor: float = 2.0,
) -> tuple[bool, str | None]:
    """Reject plans whose furthest waypoint exceeds the prompt by > factor x.

    Returns (ok, reason). When ``ok`` is False, the caller should
    regenerate or clamp the plan; this is what prevents the live-run
    drift where the prompt said "6 m north" and the LLM emitted 40 m.
    """
    max_prompt_distance = extract_max_distance_from_prompt(prompt)
    if max_prompt_distance is None or not plan.waypoints:
        return True, None
    actual_furthest = max(max(abs(wp.north_m), abs(wp.east_m)) for wp in plan.waypoints)
    if actual_furthest <= max_prompt_distance * tolerance_factor:
        return True, None
    return False, (
        f"Plan distance {actual_furthest:.1f} m exceeds prompt budget "
        f"{max_prompt_distance:.1f} m by more than {tolerance_factor:.1f}x — "
        f"LLM ignored the prompt's distance constraint."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rewriter
# ─────────────────────────────────────────────────────────────────────────────


def rewrite_to_real_mavsdk(
    code: str,
    *,
    mission_prompt: str = "",
    max_speed_m_s: float = 5.0,
) -> tuple[str, ExtractedPlan]:
    """Rewrite a legacy skill-API mission to real MAVSDK.

    Returns ``(rewritten_code, plan)``. If the input has no legacy calls
    the input is returned unchanged.
    """
    plan = extract_plan_from_legacy_code(code)
    if not plan.has_legacy_calls:
        return code, plan
    if not plan.waypoints:
        logger.warning("code_post_processor.no_waypoints_extracted")
        return code, plan

    distance_ok, distance_reason = validate_plan_distance(plan, mission_prompt)
    if not distance_ok:
        logger.warning(
            "code_post_processor.distance_violation",
            extra={"reason": distance_reason},
        )
        # Clamp every waypoint so the mission stays within the prompt budget.
        budget = extract_max_distance_from_prompt(mission_prompt) or 100.0
        # Scale so that the furthest point sits at the budget.
        actual_furthest = max(max(abs(wp.north_m), abs(wp.east_m)) for wp in plan.waypoints)
        scale = budget / actual_furthest
        plan.waypoints = [
            Waypoint(
                name=wp.name,
                north_m=wp.north_m * scale,
                east_m=wp.east_m * scale,
                alt_m=wp.alt_m,
                speed_m_s=wp.speed_m_s,
                is_fly_through=wp.is_fly_through,
                loiter_time_s=wp.loiter_time_s,
                yaw_deg=wp.yaw_deg,
                gimbal_pitch_deg=wp.gimbal_pitch_deg,
                gimbal_yaw_deg=wp.gimbal_yaw_deg,
                camera_action=wp.camera_action,
                camera_photo_interval_s=wp.camera_photo_interval_s,
                camera_photo_distance_m=wp.camera_photo_distance_m,
                acceptance_radius_m=wp.acceptance_radius_m,
                vehicle_action=wp.vehicle_action,
            )
            for wp in plan.waypoints
        ]

    plan.waypoints = [
        wp.model_copy(update={"speed_m_s": min(wp.speed_m_s or max_speed_m_s, max_speed_m_s)})
        for wp in plan.waypoints
    ]
    spec = MissionPlanSpec(
        home=PX4_SITL_DEFAULT_HOME,
        default_speed_m_s=max_speed_m_s,
        takeoff_altitude_m=max(plan.takeoff_altitude_m, 1.0),
        waypoints=plan.waypoints,
    )
    rewritten = render_real_mavsdk_mission(
        spec,
        geofence_radius_m=plan.geofence_radius_m,
    )
    logger.info(
        "code_post_processor.rewritten",
        extra={
            "waypoints": len(plan.waypoints),
            "takeoff_alt_m": plan.takeoff_altitude_m,
            "geofence_m": plan.geofence_radius_m,
            "legacy_calls": plan.legacy_call_names,
            "distance_clamped": not distance_ok,
        },
    )
    return rewritten, plan


# ─────────────────────────────────────────────────────────────────────────────
# Geometry enforcement — the model reasons, it does not author coordinates
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_MISSION_ALTITUDE_M = 10.0


def _absolute_waypoints_from_prompt(mission_prompt: str) -> list[Waypoint] | None:
    """Turn a mission prompt into absolute NED waypoints, or None.

    :func:`utils.mission_parser.parse_mission_spec` describes a path as
    *relative* legs — a 30 m square is ``(30,0) (0,30) (-30,0) (0,-30)`` —
    whereas :class:`~pipelines.mission_builder.Waypoint` is an offset from
    home, because that is what ``ned_to_lla`` converts. Accumulating the legs
    is therefore the whole translation: those four legs become the corners
    ``(30,0) (30,30) (0,30) (0,0)``, which closes the square.
    """
    from utils.mission_parser import parse_mission_spec

    spec = parse_mission_spec(mission_prompt)
    if not spec.waypoints:
        return None

    altitude_m = spec.altitude_m or _DEFAULT_MISSION_ALTITUDE_M
    waypoints: list[Waypoint] = []
    north = east = 0.0
    for leg in spec.waypoints:
        north += leg.north_m
        east += leg.east_m
        waypoints.append(
            Waypoint(
                name=leg.name,
                north_m=round(north, 3),
                east_m=round(east, 3),
                alt_m=max(altitude_m - leg.down_m, 1.0),
            )
        )
    return waypoints or None


def _find_mission_items_assignment(code: str) -> tuple[int, int, int] | None:
    """Locate ``mission_items = [...]``as (start_line, end_line, indent).

    Lines are 1-based and inclusive, matching :mod:`ast`. Parsed rather than
    matched with a regex so a nested ``]`` inside a waypoint cannot truncate
    the block.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or node.end_lineno is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "mission_items":
                return node.lineno, node.end_lineno, target.col_offset
    return None


def enforce_mission_geometry(code: str, mission_prompt: str) -> tuple[str, bool]:
    """Replace LLM-authored mission coordinates with computed ones.

    Returns ``(code, replaced)``.

    :func:`rewrite_to_real_mavsdk` only fires on legacy skill-API output. Once
    the model started emitting real MAVSDK directly it wrote its own
    ``latitude_deg`` literals, and nothing checked them: a "30 m square" run in
    September 2026 produced corners ~1000 m apart, tripped the geofence with
    ``auto_fixable: false``, and regenerated the identical file eleven times.
    Geometry is arithmetic, so it is computed here from the prompt and spliced
    over whatever the model wrote, leaving the rest of its code intact.
    """
    span = _find_mission_items_assignment(code)
    if span is None:
        return code, False

    waypoints = _absolute_waypoints_from_prompt(mission_prompt)
    if not waypoints:
        return code, False

    from pipelines.mission_builder import render_mission_items_python

    spec = MissionPlanSpec(
        home=PX4_SITL_DEFAULT_HOME,
        default_speed_m_s=5.0,
        takeoff_altitude_m=max(waypoints[0].alt_m, 1.0),
        waypoints=waypoints,
    )

    start_line, end_line, indent = span
    pad = " " * indent
    block = "\n".join(
        pad + line if line else line for line in render_mission_items_python(spec).split("\n")
    )

    lines = code.split("\n")
    rebuilt = "\n".join(lines[: start_line - 1] + [block] + lines[end_line:])

    logger.info(
        "code_post_processor.geometry_enforced",
        extra={
            "waypoints": len(waypoints),
            "altitude_m": waypoints[0].alt_m,
            "furthest_m": max(max(abs(w.north_m), abs(w.east_m)) for w in waypoints),
        },
    )
    return rebuilt, True
