"""Did the vehicle fly the mission that the approved code commanded?

Release-protocol checks for a SITL flight (engineering review, section 6.3). The
basic gate in :mod:`pipelines.simulation_acceptance` establishes where the
telemetry came from and that the mission ran. This compares what the telemetry
shows with the mission itself:

=============  ==============================================================
Sampling       median interval ≤ 0.2 s and no gap over 1 s
Mission        PX4 reported MISSION mode
Horizontal     every MISSION sample within 1 m of the commanded path
Altitude       within 0.5 m of the commanded altitude no later than 10 s into
               the mission, then an RMSE ≤ 0.5 m for the rest of it
Hold           each waypoint with a loiter time held that long (1 s margin)
               within 1 m horizontally; altitude has its own check above
Landing        last sample ≤ 0.5 m high and within 1 m of where the mission
               lands: home after return-to-launch, else the last waypoint
Boundary       never farther from home than the code's geofence radius
=============  ==============================================================

The commanded mission is read from the approved code, as the ``MissionItem``
list and ``PLANNED_HOME_*`` constants that :mod:`pipelines.mavsdk_templates`
writes. It is not read from the code agent's plan: a reviewer may edit the code
before approval, and the code is what flew. Code whose mission cannot be read
fails closed.

The limits are the review's initial targets for SITL, not airworthiness
criteria. Real flights set how much margin they leave: 55 SITL flights in
September 2026 sampled every 0.1 s with no gap over 0.14 s; the published squares
(20 m and 25 m) were tracked within 0.52–0.60 m and the hovers within 0.08 m.
"""

from __future__ import annotations

import ast
import math
import statistics
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from utils.coordinates import LLA, lla_to_ned

MAX_MEDIAN_INTERVAL_S = 0.2
MAX_GAP_S = 1.0
MAX_HORIZONTAL_ERROR_M = 1.0
ALTITUDE_BAND_M = 0.5
MAX_ALTITUDE_CAPTURE_S = 10.0
MAX_ALTITUDE_RMSE_M = 0.5
HOLD_RADIUS_M = 1.0
HOLD_MARGIN_S = 1.0
LANDED_MAX_ALT_M = 0.5
MAX_LANDING_ERROR_M = 1.0

LIMITS = {
    "max_median_interval_s": MAX_MEDIAN_INTERVAL_S,
    "max_gap_s": MAX_GAP_S,
    "max_horizontal_error_m": MAX_HORIZONTAL_ERROR_M,
    "altitude_band_m": ALTITUDE_BAND_M,
    "max_altitude_capture_s": MAX_ALTITUDE_CAPTURE_S,
    "max_altitude_rmse_m": MAX_ALTITUDE_RMSE_M,
    "hold_radius_m": HOLD_RADIUS_M,
    "hold_margin_s": HOLD_MARGIN_S,
    "landed_max_alt_m": LANDED_MAX_ALT_M,
    "max_landing_error_m": MAX_LANDING_ERROR_M,
}


class MissionUnreadable(ValueError):
    """The approved code's mission is not in a form this check can read."""


@dataclass(frozen=True)
class CommandedWaypoint:
    north_m: float
    east_m: float
    alt_m: float
    loiter_s: float


@dataclass(frozen=True)
class CommandedMission:
    """NED offsets from home, as the vehicle flies them after anchoring."""

    waypoints: tuple[CommandedWaypoint, ...]
    takeoff_alt_m: float
    return_to_launch: bool
    geofence_radius_m: float | None


def _number(node: ast.AST | None) -> float | None:
    value = node.value if isinstance(node, ast.Constant) else None
    # True is an int, and not a number here.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _number(node.operand)
        if inner is not None:
            return -inner if isinstance(node.op, ast.USub) else inner
    return None


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _live_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Every node except the bodies of ``if False:``.

    The template renders its options as ``if True:`` / ``if False:`` blocks, and a
    geofence under ``if False:`` was never uploaded.
    """
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.If) and isinstance(node.test, ast.Constant):
            stack.extend(node.body if node.test.value else node.orelse)
        else:
            stack.extend(ast.iter_child_nodes(node))


def _waypoint(index: int, node: ast.AST, home: LLA) -> CommandedWaypoint:
    if not isinstance(node, ast.Call) or _call_name(node) != "MissionItem" or node.args:
        raise MissionUnreadable(f"mission item {index} is not MissionItem(...) with keywords")
    values = {kw.arg: kw.value for kw in node.keywords if kw.arg}
    action = values.get("vehicle_action")
    if isinstance(action, ast.Attribute) and action.attr != "NONE":
        # Only waypoint missions are generated; a LAND or TAKEOFF item changes
        # what the vehicle should be doing between waypoints.
        raise MissionUnreadable(f"mission item {index} has vehicle action {action.attr}")
    lat = _number(values.get("latitude_deg"))
    lon = _number(values.get("longitude_deg"))
    alt = _number(values.get("relative_altitude_m"))
    if lat is None or lon is None or alt is None:
        raise MissionUnreadable(f"mission item {index} lacks a numeric position")
    offset = lla_to_ned(home, LLA(lat_deg=lat, lon_deg=lon, alt_m=home.alt_m))
    loiter = _number(values.get("loiter_time_s")) or 0.0
    return CommandedWaypoint(offset.north_m, offset.east_m, alt, max(0.0, loiter))


def read_commanded_mission(code: str) -> CommandedMission:
    """The mission the code uploads, as offsets from the vehicle's home.

    Raises :class:`MissionUnreadable` for code that is not the template's shape.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise MissionUnreadable(f"the code does not parse ({exc.msg})") from exc

    constants: dict[str, float | None] = {}
    items: list[ast.AST] = []
    calls: dict[str, list[ast.Call]] = {}
    for node in _live_nodes(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "mission_items":
                items.append(node.value)
            elif isinstance(target, ast.Name) and target.id.startswith("PLANNED_HOME_"):
                constants[target.id] = _number(node.value)
        elif isinstance(node, ast.Call):
            calls.setdefault(_call_name(node), []).append(node)

    if len(items) != 1 or not isinstance(items[0], ast.List) or not items[0].elts:
        raise MissionUnreadable("it does not assign one non-empty mission_items list")
    lat = constants.get("PLANNED_HOME_LATITUDE_DEG")
    lon = constants.get("PLANNED_HOME_LONGITUDE_DEG")
    if lat is None or lon is None:
        raise MissionUnreadable("it has no PLANNED_HOME_LATITUDE_DEG/LONGITUDE_DEG")
    if "anchor_mission_to_home" not in calls:
        # Without anchoring, the items are absolute coordinates around a nominal
        # home, and where they lie relative to the vehicle depends on the world.
        raise MissionUnreadable("it does not anchor its waypoints on the vehicle's home")
    home = LLA(lat_deg=lat, lon_deg=lon, alt_m=0.0)
    waypoints = tuple(_waypoint(i + 1, node, home) for i, node in enumerate(items[0].elts))

    takeoff = next(
        (
            value
            for call in calls.get("set_takeoff_altitude", [])
            if (value := _number(call.args[0] if call.args else None)) is not None
        ),
        waypoints[0].alt_m,
    )
    rtl = any(
        isinstance(call.args[0], ast.Constant) and call.args[0].value is True
        for call in calls.get("set_return_to_launch_after_mission", [])
        if call.args
    )
    radius = next(
        (
            value
            for call in calls.get("upload_circular_geofence", [])
            for kw in call.keywords
            if kw.arg == "radius_m" and (value := _number(kw.value)) is not None
        ),
        None,
    )
    return CommandedMission(waypoints, takeoff, rtl, radius)


def _nothing_to_fly(mission: CommandedMission) -> bool:
    """Every waypoint where takeoff already leaves the vehicle, with no hold.

    PX4 counts such a mission as reached the moment it starts: in benchmark run 3
    it logged «Executing Mission» and, 65 ms later, «Mission finished».
    """
    return all(
        math.hypot(w.north_m, w.east_m) <= HOLD_RADIUS_M
        and abs(w.alt_m - mission.takeoff_alt_m) <= ALTITUDE_BAND_M
        and w.loiter_s <= 0
        for w in mission.waypoints
    )


def _mode(point: Mapping[str, Any]) -> str:
    return str(point.get("flight_mode") or "").rsplit(".", 1)[-1].upper()


def _nearest_on_path(
    north: float, east: float, vertices: Sequence[tuple[float, float, float]]
) -> tuple[float, float]:
    """(horizontal distance to the path, commanded altitude at the nearest point)."""
    best = (math.inf, vertices[0][2])
    for (n0, e0, a0), (n1, e1, a1) in zip(vertices, vertices[1:]):
        dn, de = n1 - n0, e1 - e0
        length2 = dn * dn + de * de
        t = 0.0 if length2 < 1e-9 else ((north - n0) * dn + (east - e0) * de) / length2
        t = min(1.0, max(0.0, t))
        distance = math.hypot(north - (n0 + t * dn), east - (e0 + t * de))
        if distance < best[0]:
            best = (distance, a0 + t * (a1 - a0))
    return best


def assess_flight_conformance(
    points: Sequence[Mapping[str, Any]], code: str | None
) -> dict[str, Any]:
    """Compare validated telemetry samples with the approved code's mission."""
    reasons: list[str] = []
    result: dict[str, Any] = {
        "evaluated": False,
        "passed": False,
        "reasons": reasons,
        "retryable_with_code_change": False,
        "metrics": {},
        "limits": LIMITS,
    }
    if not code:
        reasons.append("No approved mission code to compare the flight with")
        return result
    try:
        mission = read_commanded_mission(code)
    except MissionUnreadable as exc:
        # Regenerated template code is readable, so a code change can fix this.
        reasons.append(f"The commanded mission cannot be read from the approved code: {exc}")
        result["retryable_with_code_change"] = True
        return result
    result["evaluated"] = True
    metrics: dict[str, Any] = result["metrics"]
    times = [float(p["timestamp_s"]) for p in points]

    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    worst = max(range(len(gaps)), key=gaps.__getitem__)
    metrics["sample_interval_median_s"] = round(statistics.median(gaps), 3)
    metrics["max_gap_s"] = round(gaps[worst], 3)
    if metrics["sample_interval_median_s"] > MAX_MEDIAN_INTERVAL_S:
        reasons.append(
            f"Telemetry sampled every {metrics['sample_interval_median_s']:.2f} s (median); "
            f"the limit is {MAX_MEDIAN_INTERVAL_S:g} s"
        )
    if gaps[worst] > MAX_GAP_S:
        reasons.append(
            f"Telemetry has a {gaps[worst]:.1f} s gap after {times[worst]:.1f} s; "
            f"the limit is {MAX_GAP_S:g} s"
        )

    vertices = [(0.0, 0.0, mission.takeoff_alt_m)] + [
        (w.north_m, w.east_m, w.alt_m) for w in mission.waypoints
    ]
    flown = [i for i, p in enumerate(points) if _mode(p) == "MISSION"]
    metrics["mission_samples"] = len(flown)
    if not flown:
        # MAVSDK reports the flight mode from 1 Hz heartbeats, so a mission PX4
        # starts and ends within a second never appears in the samples.
        if _nothing_to_fly(mission):
            reasons.append(
                "The approved code's mission has nothing to fly: its only waypoints lie where "
                "takeoff leaves the vehicle and it holds them 0 s, so PX4 finished it at once "
                "and the vehicle never held the requested position. Give the hover a duration "
                "or the path its waypoints"
            )
        else:
            reasons.append(
                "PX4 never reported MISSION mode: it refused the mission, or started and ended "
                "it within one telemetry sample, so the commanded path was not flown"
            )
        # The mission comes from the code, so a regenerated one can fix either.
        result["retryable_with_code_change"] = True
    else:
        horizontal: list[float] = []
        altitude: list[tuple[float, float]] = []  # (time, error)
        for i in flown:
            distance, commanded_alt = _nearest_on_path(
                points[i]["north_m"], points[i]["east_m"], vertices
            )
            horizontal.append(distance)
            altitude.append((times[i], points[i]["alt_m"] - commanded_alt))
        worst_h = max(range(len(horizontal)), key=horizontal.__getitem__)
        metrics["horizontal_error_max_m"] = round(horizontal[worst_h], 3)
        metrics["horizontal_error_rms_m"] = round(
            math.sqrt(statistics.fmean(d * d for d in horizontal)), 3
        )
        if horizontal[worst_h] > MAX_HORIZONTAL_ERROR_M:
            reasons.append(
                f"Horizontal tracking: {horizontal[worst_h]:.2f} m from the commanded path at "
                f"{times[flown[worst_h]]:.1f} s; the limit is {MAX_HORIZONTAL_ERROR_M:g} m"
            )

        start = times[flown[0]]
        captured = next(
            (k for k, (_, error) in enumerate(altitude) if abs(error) <= ALTITUDE_BAND_M), None
        )
        if captured is None:
            reasons.append(
                f"Altitude never came within {ALTITUDE_BAND_M:g} m of the commanded altitude "
                "during the mission"
            )
        else:
            capture_s = altitude[captured][0] - start
            rmse = math.sqrt(statistics.fmean(e * e for _, e in altitude[captured:]))
            metrics["altitude_capture_s"] = round(capture_s, 2)
            metrics["altitude_rmse_m"] = round(rmse, 3)
            if capture_s > MAX_ALTITUDE_CAPTURE_S:
                reasons.append(
                    f"Altitude reached the commanded {ALTITUDE_BAND_M:g} m band only "
                    f"{capture_s:.1f} s into the mission; the limit is "
                    f"{MAX_ALTITUDE_CAPTURE_S:g} s"
                )
            if rmse > MAX_ALTITUDE_RMSE_M:
                reasons.append(
                    f"Altitude RMSE {rmse:.2f} m against the commanded altitude; the limit is "
                    f"{MAX_ALTITUDE_RMSE_M:g} m"
                )

        in_mission = set(flown)
        holds = []
        for number, waypoint in enumerate(mission.waypoints, start=1):
            if waypoint.loiter_s <= 0:
                continue

            def near(p: Mapping[str, Any], w: CommandedWaypoint = waypoint) -> bool:
                # Horizontal only: altitude is judged by the RMSE above, and the
                # barometer-biased recordings before 2026-09-28 spiked 0.8 m up
                # while PX4 held the waypoint the whole time.
                return math.hypot(p["north_m"] - w.north_m, p["east_m"] - w.east_m) <= HOLD_RADIUS_M

            held = sum(
                times[i + 1] - times[i]
                for i in range(len(points) - 1)
                if i in in_mission
                and i + 1 in in_mission
                and near(points[i])
                and near(points[i + 1])
            )
            holds.append(
                {"waypoint": number, "commanded_s": waypoint.loiter_s, "held_s": round(held, 2)}
            )
            if held < waypoint.loiter_s - HOLD_MARGIN_S:
                reasons.append(
                    f"Waypoint {number} was held {held:.1f} s within {HOLD_RADIUS_M:g} m; "
                    f"the code commands {waypoint.loiter_s:g} s"
                )
        metrics["holds"] = holds

    last = points[-1]
    land_n, land_e = (
        (0.0, 0.0)
        if mission.return_to_launch
        else (mission.waypoints[-1].north_m, mission.waypoints[-1].east_m)
    )
    landing_error = math.hypot(last["north_m"] - land_n, last["east_m"] - land_e)
    metrics["final_alt_m"] = round(float(last["alt_m"]), 3)
    metrics["landing_error_m"] = round(landing_error, 3)
    if last["alt_m"] > LANDED_MAX_ALT_M:
        reasons.append(f"The flight did not land: the last sample is at {last['alt_m']:.1f} m")
        # A mission without return-to-launch leaves the vehicle hovering.
        result["retryable_with_code_change"] = (
            result["retryable_with_code_change"] or not mission.return_to_launch
        )
    elif landing_error > MAX_LANDING_ERROR_M:
        where = "home" if mission.return_to_launch else "the last waypoint"
        reasons.append(
            f"Landed {landing_error:.1f} m from {where}; the limit is {MAX_LANDING_ERROR_M:g} m"
        )

    reach = max(math.hypot(p["north_m"], p["east_m"]) for p in points)
    metrics["max_distance_from_home_m"] = round(reach, 2)
    if mission.geofence_radius_m is not None and reach > mission.geofence_radius_m:
        reasons.append(
            f"Flew {reach:.1f} m from home, beyond the code's {mission.geofence_radius_m:g} m "
            "geofence"
        )

    result["passed"] = not reasons
    if result["passed"]:
        result["retryable_with_code_change"] = False
    return result
