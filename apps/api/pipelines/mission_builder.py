"""eljazari.ai — Phase 8: Mission item builder.

Translates a high-level :class:`MissionPlanSpec` (NED-based, mission-typed)
into a concrete sequence of MAVSDK ``MissionItem`` objects that PX4 can
execute directly. No skill-API adapter required.

The builder is split from the actual MAVSDK call so that:
  1. We can unit-test the conversion deterministically without needing a
     running PX4 SITL.
  2. The same builder powers both code-generator templates (which embed
     the resulting MissionItem list in generated Python) and the test
     agent (which uploads the plan via :class:`mavsdk.mission.MissionPlan`).

Use::

    spec = MissionPlanSpec(
        home=PX4_SITL_DEFAULT_HOME,
        default_speed_m_s=5.0,
        waypoints=[
            Waypoint(name="takeoff", north_m=0, east_m=0, alt_m=4),
            Waypoint(name="north_leg", north_m=6, east_m=0, alt_m=4),
            Waypoint(name="return", north_m=0, east_m=0, alt_m=4),
        ],
    )
    items = build_mission_items(spec)
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from utils.coordinates import LLA, NED, ned_to_lla


CameraAction = Literal[
    "none",
    "take_photo",
    "start_photo_interval",
    "stop_photo_interval",
    "start_video",
    "stop_video",
]

VehicleAction = Literal["none", "transition_to_fw", "transition_to_mc", "takeoff", "land"]


class Waypoint(BaseModel):
    """One mission waypoint specified in NED + altitude."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., description="Human label for logs / telemetry")
    north_m: float = 0.0
    east_m: float = 0.0
    alt_m: float = Field(..., ge=0.0, description="Above-takeoff altitude (m)")
    speed_m_s: float | None = None
    is_fly_through: bool = True
    loiter_time_s: float = 0.0
    yaw_deg: float | None = None
    gimbal_pitch_deg: float = 0.0
    gimbal_yaw_deg: float = 0.0
    camera_action: CameraAction = "none"
    camera_photo_interval_s: float = 0.0
    camera_photo_distance_m: float = 0.0
    acceptance_radius_m: float = 1.0
    vehicle_action: VehicleAction = "none"

    @field_validator("camera_action")
    @classmethod
    def _validate_camera(cls, v: CameraAction) -> CameraAction:
        return v


class MissionPlanSpec(BaseModel):
    """A complete plan: home position + ordered waypoints + global defaults."""

    home: LLA
    default_speed_m_s: float = Field(5.0, gt=0)
    waypoints: list[Waypoint]
    return_to_launch_after_mission: bool = True
    takeoff_altitude_m: float = Field(2.0, ge=1.0)

    @field_validator("waypoints")
    @classmethod
    def _at_least_one_waypoint(cls, v: list[Waypoint]) -> list[Waypoint]:
        if not v:
            raise ValueError("Mission must have at least one waypoint")
        return v


class MissionItemPayload(BaseModel):
    """Pure-Python mirror of ``mavsdk.mission.MissionItem``.

    We don't import MAVSDK here because the mission *builder* must be usable
    inside generated-code templates and unit tests that run without MAVSDK
    installed. The structure matches the MAVSDK constructor 1:1 so the
    generated mission code can simply unpack ``**payload.model_dump()``.
    """

    latitude_deg: float
    longitude_deg: float
    relative_altitude_m: float
    speed_m_s: float
    is_fly_through: bool
    gimbal_pitch_deg: float
    gimbal_yaw_deg: float
    camera_action: str
    loiter_time_s: float
    camera_photo_interval_s: float
    acceptance_radius_m: float
    yaw_deg: float
    camera_photo_distance_m: float
    vehicle_action: str


# ─────────────────────────────────────────────────────────────────────────────
# Builder
# ─────────────────────────────────────────────────────────────────────────────


def build_mission_items(spec: MissionPlanSpec) -> list[MissionItemPayload]:
    """Convert a high-level mission spec into MAVSDK-ready mission items.

    NED → LLA conversion uses the WGS84 ellipsoid for accuracy up to several
    kilometres. The ``home`` altitude is taken as 0 reference for
    ``relative_altitude_m`` (which is what PX4 expects).
    """
    items: list[MissionItemPayload] = []
    for wp in spec.waypoints:
        lla = ned_to_lla(
            spec.home,
            NED(north_m=wp.north_m, east_m=wp.east_m, down_m=-wp.alt_m),
        )
        items.append(
            MissionItemPayload(
                latitude_deg=lla.lat_deg,
                longitude_deg=lla.lon_deg,
                relative_altitude_m=wp.alt_m,
                speed_m_s=wp.speed_m_s if wp.speed_m_s is not None else spec.default_speed_m_s,
                is_fly_through=wp.is_fly_through,
                gimbal_pitch_deg=wp.gimbal_pitch_deg,
                gimbal_yaw_deg=wp.gimbal_yaw_deg,
                camera_action=wp.camera_action.upper(),
                loiter_time_s=wp.loiter_time_s,
                camera_photo_interval_s=wp.camera_photo_interval_s,
                acceptance_radius_m=wp.acceptance_radius_m,
                yaw_deg=wp.yaw_deg if wp.yaw_deg is not None else float("nan"),
                camera_photo_distance_m=wp.camera_photo_distance_m or float("nan"),
                vehicle_action=wp.vehicle_action.upper(),
            )
        )
    return items


def render_mission_items_python(spec: MissionPlanSpec) -> str:
    """Render mission items as Python source code that imports MAVSDK and
    constructs a real ``MissionPlan`` ready for ``drone.mission.upload_mission(plan)``.

    Used by the GSCE template to embed the plan inline so the generated
    Python file runs without further codegen.
    """
    items = build_mission_items(spec)
    lines: list[str] = ["mission_items = ["]
    for it in items:
        lines.append(
            "    MissionItem(\n"
            f"        latitude_deg={it.latitude_deg:.7f},\n"
            f"        longitude_deg={it.longitude_deg:.7f},\n"
            f"        relative_altitude_m={it.relative_altitude_m:.2f},\n"
            f"        speed_m_s={it.speed_m_s:.2f},\n"
            f"        is_fly_through={it.is_fly_through},\n"
            f"        gimbal_pitch_deg={it.gimbal_pitch_deg:.1f},\n"
            f"        gimbal_yaw_deg={it.gimbal_yaw_deg:.1f},\n"
            f"        camera_action=MissionItem.CameraAction.{it.camera_action},\n"
            f"        loiter_time_s={it.loiter_time_s:.2f},\n"
            f"        camera_photo_interval_s={it.camera_photo_interval_s:.2f},\n"
            f"        acceptance_radius_m={it.acceptance_radius_m:.2f},\n"
            f"        yaw_deg={_nan_or_value(it.yaw_deg)},\n"
            f"        camera_photo_distance_m={_nan_or_value(it.camera_photo_distance_m)},\n"
            f"        vehicle_action=MissionItem.VehicleAction.{it.vehicle_action},\n"
            "    ),"
        )
    lines.append("]")
    return "\n".join(lines)


def _nan_or_value(v: float) -> str:
    return "float('nan')" if math.isnan(v) else f"{v:.4f}"
