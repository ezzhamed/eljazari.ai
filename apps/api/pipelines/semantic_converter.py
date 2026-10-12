"""eljazari.ai — Semantic Converter
Converts numeric telemetry data → English natural-language observations.
Enables an LLM evaluator to reason about drone behaviour without raw numbers.

Architecture:
    TelemetryPoint (per snapshot) + TelemetryData (full run)
    ↓ SemanticConverter.convert()
    → list[SemanticObservation]  (English descriptions + severity)
"""

import logging
import math
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Telemetry models
# ─────────────────────────────────────────────────────────────────────────────


class TelemetryPoint(BaseModel):
    """Single telemetry snapshot captured at one instant."""

    timestamp_s: float = 0.0

    # Position — NED relative to home (positive north = forward)
    north_m: float = 0.0
    east_m: float = 0.0
    alt_m: float = 0.0  # positive = above home

    # Absolute GPS (optional — used for geo-fence checks)
    lat: float = 0.0
    lon: float = 0.0

    # Velocity (m/s, NED frame)
    vel_north_ms: float = 0.0
    vel_east_ms: float = 0.0
    vel_down_ms: float = 0.0
    speed_ms: float = 0.0  # horizontal ground speed

    # Attitude (degrees)
    roll_deg: float = 0.0
    pitch_deg: float = 0.0
    yaw_deg: float = 0.0  # 0 = North, 90 = East, clockwise

    # Power
    battery_percent: float = 100.0
    battery_voltage: float = 12.6

    # Status
    flight_mode: str = "unknown"
    armed: bool = False
    gps_satellites: int = 0

    # Motors (0.0–1.0 throttle per motor; may be empty if not reported)
    motors: list[float] = Field(default_factory=list)


class TelemetryData(BaseModel):
    """Complete telemetry record for one simulation run."""

    sim_id: str
    points: list[TelemetryPoint] = Field(default_factory=list)

    duration_s: float = 0.0
    mission_started: bool = False
    mission_completed: bool = False
    crash_detected: bool = False
    geofence_violation: bool = False

    # Home position (for geo-reference)
    home_lat: float = 0.0
    home_lon: float = 0.0
    home_alt_m: float = 0.0

    # Provenance and debugging metadata
    runtime_mode: str = "unknown"
    telemetry_source: str = "unknown"
    mission_error: str = ""
    # "linux_namespaces" when the mission ran in the sandbox; anything else means
    # generated code shared a process with (and could alter) this record.
    mission_isolation: str = "unknown"
    warnings: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Observation model
# ─────────────────────────────────────────────────────────────────────────────


class SemanticObservation(BaseModel):
    """One natural-language observation derived from telemetry."""

    timestamp_s: float
    description: str  # English narrative text
    event_type: str = "movement"  # movement | warning | event | status
    severity: str = "info"  # info | warning | critical
    raw_values: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# English look-up tables
# ─────────────────────────────────────────────────────────────────────────────

_COMPASS_16: list[tuple[float, str]] = [
    (11.25, "north"),
    (33.75, "north-northeast"),
    (56.25, "northeast"),
    (78.75, "east-northeast"),
    (101.25, "east"),
    (123.75, "east-southeast"),
    (146.25, "southeast"),
    (168.75, "south-southeast"),
    (191.25, "south"),
    (213.75, "south-southwest"),
    (236.25, "southwest"),
    (258.75, "west-southwest"),
    (281.25, "west"),
    (303.75, "west-northwest"),
    (326.25, "northwest"),
    (348.75, "north-northwest"),
]

_FLIGHT_MODES_AR: dict[str, str] = {
    "MANUAL": "manual",
    "ACRO": "acro",
    "ALTITUDE": "altitude control",
    "POSITION": "position mode",
    "MISSION": "mission mode",
    "AUTO.TAKEOFF": "auto takeoff",
    "AUTO.LAND": "auto land",
    "AUTO.RTL": "return to launch",
    "HOLD": "hold mode",
    "OFFBOARD": "offboard mode",
    "unknown": "unknown",
}


# ─────────────────────────────────────────────────────────────────────────────
# SemanticConverter
# ─────────────────────────────────────────────────────────────────────────────


class SemanticConverter:
    """
    Converts TelemetryData → list[SemanticObservation] (English language).

    Strategy:
    - Sample every N points to avoid flooding the LLM context
    - Always include first + last points
    - Append event-based observations (crash, geofence, low battery, mission end)
    """

    def __init__(self, sample_every: int = 5) -> None:
        self.sample_every = sample_every

    def convert(
        self,
        telemetry: TelemetryData,
        sample_every: int | None = None,
    ) -> list[SemanticObservation]:
        """Convert full telemetry run to semantic observations."""
        step = sample_every or self.sample_every
        points = telemetry.points

        if not points:
            return [
                SemanticObservation(
                    timestamp_s=0.0,
                    description="No data collected from the simulation — the mission did not start",
                    event_type="event",
                    severity="critical",
                )
            ]

        home = points[0]

        # Sample indices: every N, plus always first and last
        indices: list[int] = list(range(0, len(points), step))
        if (len(points) - 1) not in indices:
            indices.append(len(points) - 1)

        observations: list[SemanticObservation] = []
        for i in indices:
            prev = points[i - 1] if i > 0 else None
            observations.append(self._convert_point(points[i], home, prev))

        # Append event-level observations (geofence, crash, battery, etc.)
        observations.extend(self._extract_events(telemetry))

        observations.sort(key=lambda o: o.timestamp_s)
        return observations

    # ── Per-point conversion ─────────────────────────────────────────────────

    def _convert_point(
        self,
        point: TelemetryPoint,
        home: TelemetryPoint,
        prev: TelemetryPoint | None,
    ) -> SemanticObservation:
        # Distance & direction from home
        dn = point.north_m - home.north_m
        de = point.east_m - home.east_m
        dist = math.sqrt(dn**2 + de**2)
        from_home = self._compass_from_ned(dn, de) if dist > 0.5 else "near the launch point"
        heading = self._compass_from_yaw(point.yaw_deg)
        mode_ar = _FLIGHT_MODES_AR.get(point.flight_mode, point.flight_mode)

        # Delta movement description
        delta = ""
        if prev is not None:
            d_n = point.north_m - prev.north_m
            d_e = point.east_m - prev.east_m
            d_a = point.alt_m - prev.alt_m
            if abs(d_n) > 0.5 or abs(d_e) > 0.5 or abs(d_a) > 0.3:
                delta = (
                    f", moved {abs(d_n):.1f}m {'north' if d_n >= 0 else 'south'}"
                    f" and {abs(d_e):.1f}m {'east' if d_e >= 0 else 'west'}"
                    + (
                        f" and climbed {abs(d_a):.1f}m"
                        if d_a > 0.3
                        else f" and descended {abs(d_a):.1f}m"
                        if d_a < -0.3
                        else ""
                    )
                )

        desc = (
            f"[{point.timestamp_s:.1f}s] drone {'armed' if point.armed else 'disarmed'}"
            f" in '{mode_ar}' mode, "
            f"{dist:.1f}m away {from_home}, "
            f"{self._altitude_description(point.alt_m)}, "
            f"heading {heading}, "
            f"{self._speed_description(point.speed_ms)}, "
            f"{self._battery_status(point.battery_percent)}, "
            f"GPS: {point.gps_satellites} satellites"
            f"{delta}"
        )

        return SemanticObservation(
            timestamp_s=point.timestamp_s,
            description=desc,
            event_type="movement",
            severity="info",
            raw_values={
                "north_m": point.north_m,
                "east_m": point.east_m,
                "alt_m": point.alt_m,
                "speed_ms": point.speed_ms,
                "battery_pct": point.battery_percent,
                "yaw_deg": point.yaw_deg,
            },
        )

    # ── Event extraction ──────────────────────────────────────────────────────

    def _extract_events(self, telemetry: TelemetryData) -> list[SemanticObservation]:
        events: list[SemanticObservation] = []

        if telemetry.mission_started:
            events.append(
                SemanticObservation(
                    timestamp_s=0.1,
                    description="Event: mission started — the drone began execution successfully",
                    event_type="event",
                    severity="info",
                )
            )

        if telemetry.mission_completed:
            events.append(
                SemanticObservation(
                    timestamp_s=max(0.0, telemetry.duration_s - 0.1),
                    description="Event: mission completed successfully — the drone landed at the target position",
                    event_type="event",
                    severity="info",
                )
            )
        else:
            events.append(
                SemanticObservation(
                    timestamp_s=telemetry.duration_s,
                    description="Warning: mission not completed — execution stopped before finishing all phases",
                    event_type="warning",
                    severity="warning",
                )
            )

        if telemetry.crash_detected:
            events.append(
                SemanticObservation(
                    timestamp_s=telemetry.duration_s,
                    description="Danger: crash detected — the aircraft stopped abruptly",
                    event_type="event",
                    severity="critical",
                )
            )

        if telemetry.geofence_violation:
            events.append(
                SemanticObservation(
                    timestamp_s=telemetry.duration_s * 0.5,
                    description="Danger: geofence violation — the drone exceeded the allowed bounds",
                    event_type="warning",
                    severity="critical",
                )
            )

        # Low battery events (first occurrence only per threshold)
        saw_critical = saw_low = False
        for pt in telemetry.points:
            if pt.battery_percent < 20 and not saw_critical:
                events.append(
                    SemanticObservation(
                        timestamp_s=pt.timestamp_s,
                        description=f"Danger: critical battery ({pt.battery_percent:.0f}%) — must land immediately",
                        event_type="warning",
                        severity="critical",
                    )
                )
                saw_critical = True
            elif pt.battery_percent < 30 and not saw_low:
                events.append(
                    SemanticObservation(
                        timestamp_s=pt.timestamp_s,
                        description=f"Warning: low battery ({pt.battery_percent:.0f}%) — return recommended",
                        event_type="warning",
                        severity="warning",
                    )
                )
                saw_low = True

        # Motor status summary (if available)
        if telemetry.points and telemetry.points[0].motors:
            last = telemetry.points[-1]
            if last.motors:
                avg_throttle = sum(last.motors) / len(last.motors) * 100
                events.append(
                    SemanticObservation(
                        timestamp_s=last.timestamp_s,
                        description=(
                            f"Motor status: {len(last.motors)} motors running "
                            f"at an average of {avg_throttle:.0f}% throttle"
                        ),
                        event_type="status",
                        severity="info",
                    )
                )

        return events

    # ── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _compass_from_ned(north: float, east: float) -> str:
        angle = math.degrees(math.atan2(east, north)) % 360
        return SemanticConverter._compass_from_yaw(angle)

    @staticmethod
    def _compass_from_yaw(yaw_deg: float) -> str:
        yaw = yaw_deg % 360
        for threshold, label in _COMPASS_16:
            if yaw < threshold:
                return label
        return "north"  # wraps past 348.75°

    @staticmethod
    def _altitude_description(alt_m: float) -> str:
        if alt_m < 0.5:
            return f"on the ground ({alt_m:.1f}m)"
        elif alt_m < 5.0:
            return f"very low altitude ({alt_m:.1f}m)"
        elif alt_m < 30.0:
            return f"altitude {alt_m:.1f}m"
        elif alt_m < 80.0:
            return f"medium altitude ({alt_m:.1f}m)"
        elif alt_m <= 120.0:
            return f"high altitude ({alt_m:.1f}m)"
        else:
            return f"excessive altitude ({alt_m:.1f}m) — exceeded the legal limit!"

    @staticmethod
    def _speed_description(speed_ms: float) -> str:
        if speed_ms < 0.5:
            return "stopped or nearly stationary"
        elif speed_ms < 3.0:
            return f"moving slowly ({speed_ms:.1f}m/s)"
        elif speed_ms < 8.0:
            return f"moving at medium speed ({speed_ms:.1f}m/s)"
        elif speed_ms < 15.0:
            return f"moving fast ({speed_ms:.1f}m/s)"
        else:
            return f"excessive speed! ({speed_ms:.1f}m/s)"

    @staticmethod
    def _battery_status(percent: float) -> str:
        if percent >= 80:
            return f"excellent battery ({percent:.0f}%)"
        elif percent >= 50:
            return f"good battery ({percent:.0f}%)"
        elif percent >= 30:
            return f"low battery ({percent:.0f}%)"
        elif percent >= 20:
            return f"critical battery ({percent:.0f}%) — must return!"
        else:
            return f"empty battery ({percent:.0f}%) — danger!"
