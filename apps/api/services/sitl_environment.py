"""eljazari.ai — Phase 9: High-fidelity SITL environment configuration.

The existing PX4/Gazebo SITL stack runs in dead calm with ideal GPS — that
isn't how real outdoor missions behave. This module configures the
environment knobs that turn a smoke test into a meaningful design
verification:

  • wind_north_m_s / wind_east_m_s — steady wind in NED
  • wind_gust_m_s + gust_period_s — stochastic gusts on top of the steady
    field
  • turbulence_intensity — Dryden turbulence (0..1)
  • gps_jitter_m — random walk on GPS position
  • gps_multipath_enabled — toggle reflected-signal degradation
  • compass_interference_uT — magnetic offset injected on the boom
  • battery_thermal_drift — voltage sag scales with C-rate utilisation
  • barometer_drift_pa_per_min — slow baro drift

Each `SITLEnvironmentSpec` translates to a list of PX4 parameter
overrides + a Gazebo `gz topic` plugin command. The generator is *pure*:
it produces commands without executing anything, so we can unit-test the
parameter→command conversion and the orchestrator can stage them before
launching SITL.

Scenarios are bundled so users can pick "calm_indoor" or "windy_outdoor"
by name and see consistent results across runs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Wind = Literal["calm", "light", "moderate", "strong", "extreme"]
GpsMode = Literal["clean", "indoor", "multipath", "denied", "gps_denied"]


# ─────────────────────────────────────────────────────────────────────────────
# Environment spec
# ─────────────────────────────────────────────────────────────────────────────


class SITLEnvironmentSpec(BaseModel):
    """Knobs for one SITL environment run."""

    model_config = ConfigDict(frozen=True)

    name: str = "default"

    # Wind / turbulence
    wind_north_m_s: float = 0.0
    wind_east_m_s: float = 0.0
    wind_gust_m_s: float = Field(0.0, ge=0.0)
    gust_period_s: float = Field(5.0, gt=0.0)
    turbulence_intensity: float = Field(0.0, ge=0.0, le=1.0)

    # GPS
    gps_jitter_m: float = Field(0.5, ge=0.0)
    gps_multipath_enabled: bool = False
    gps_satellites_min: int = Field(12, ge=0, le=24)

    # Compass / magnetic
    compass_interference_uT: float = Field(0.0, ge=0.0)

    # Battery / thermal
    battery_thermal_drift_enabled: bool = False
    barometer_drift_pa_per_min: float = Field(0.0, ge=0.0)

    # Telemetry rate
    telemetry_rate_hz: float = Field(50.0, gt=0.0, le=200.0)

    # Mission outage simulation (RC, datalink)
    rc_signal_loss_at_t_s: float | None = None

    @field_validator("wind_north_m_s", "wind_east_m_s")
    @classmethod
    def _abs_wind(cls, v: float) -> float:
        if abs(v) > 25.0:
            raise ValueError("Wind component > 25 m/s — unrealistic for sub-25 kg UAV testing")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# Wind speed magnitude
# ─────────────────────────────────────────────────────────────────────────────


def total_wind_m_s(spec: SITLEnvironmentSpec) -> float:
    return (spec.wind_north_m_s**2 + spec.wind_east_m_s**2) ** 0.5


def classify_wind(spec: SITLEnvironmentSpec) -> Wind:
    w = total_wind_m_s(spec)
    if w < 0.5:
        return "calm"
    if w < 3.0:
        return "light"
    if w < 8.0:
        return "moderate"
    if w < 15.0:
        return "strong"
    return "extreme"


def classify_gps(spec: SITLEnvironmentSpec) -> GpsMode:
    if spec.gps_satellites_min == 0:
        return "gps_denied"
    if spec.gps_multipath_enabled:
        return "multipath"
    if spec.gps_satellites_min < 6:
        return "indoor"
    return "clean"


# ─────────────────────────────────────────────────────────────────────────────
# Pre-built scenarios
# ─────────────────────────────────────────────────────────────────────────────

SCENARIOS: dict[str, SITLEnvironmentSpec] = {
    "calm_indoor": SITLEnvironmentSpec(
        name="calm_indoor",
        gps_satellites_min=0,  # indoor → no GPS
        gps_multipath_enabled=False,
        telemetry_rate_hz=100.0,
    ),
    "ideal_outdoor": SITLEnvironmentSpec(
        name="ideal_outdoor",
        gps_satellites_min=14,
    ),
    "light_breeze": SITLEnvironmentSpec(
        name="light_breeze",
        wind_north_m_s=2.0,
        wind_east_m_s=1.0,
        wind_gust_m_s=1.0,
        turbulence_intensity=0.1,
    ),
    "windy_outdoor": SITLEnvironmentSpec(
        name="windy_outdoor",
        wind_north_m_s=6.0,
        wind_east_m_s=3.0,
        wind_gust_m_s=4.0,
        turbulence_intensity=0.3,
        gust_period_s=4.0,
    ),
    "storm": SITLEnvironmentSpec(
        name="storm",
        wind_north_m_s=12.0,
        wind_east_m_s=8.0,
        wind_gust_m_s=6.0,
        turbulence_intensity=0.6,
        gust_period_s=2.5,
        gps_satellites_min=8,
    ),
    "gps_denied": SITLEnvironmentSpec(
        name="gps_denied",
        gps_satellites_min=0,
        gps_jitter_m=0.0,
        rc_signal_loss_at_t_s=None,
    ),
    "gps_multipath_urban": SITLEnvironmentSpec(
        name="gps_multipath_urban",
        gps_satellites_min=6,
        gps_multipath_enabled=True,
        gps_jitter_m=2.5,
    ),
    "magnetic_interference": SITLEnvironmentSpec(
        name="magnetic_interference",
        compass_interference_uT=50.0,
        gps_satellites_min=12,
    ),
    "hot_day_thermal_battery": SITLEnvironmentSpec(
        name="hot_day_thermal_battery",
        battery_thermal_drift_enabled=True,
        barometer_drift_pa_per_min=15.0,
        wind_north_m_s=1.5,
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# PX4 parameter generation
# ─────────────────────────────────────────────────────────────────────────────


class PX4ParamOverride(BaseModel):
    name: str
    value: float
    comment: str = ""


def px4_parameter_overrides(spec: SITLEnvironmentSpec) -> list[PX4ParamOverride]:
    """Translate the spec into PX4 ``param set`` commands.

    Maps to the actual PX4 parameter names so generated commands work
    against a stock PX4 v1.16 SITL build.
    """
    params: list[PX4ParamOverride] = []

    # GPS satellite minimum (EKF2_GPS_CHECK enables more strict checks).
    if spec.gps_satellites_min == 0:
        params.append(
            PX4ParamOverride(
                name="EKF2_GPS_CTRL",
                value=0,
                comment="Disable GPS for indoor / GPS-denied runs",
            )
        )
        params.append(
            PX4ParamOverride(
                name="COM_ARM_WO_GPS",
                value=1,
                comment="Allow arming without GPS (indoor)",
            )
        )
    else:
        params.append(
            PX4ParamOverride(
                name="EKF2_REQ_NSATS",
                value=max(6, min(spec.gps_satellites_min, 12)),
                comment="Minimum satellites for EKF GPS fusion",
            )
        )

    # Compass interference modelled via EKF2_MAG_NOISE.
    if spec.compass_interference_uT > 0:
        params.append(
            PX4ParamOverride(
                name="EKF2_MAG_NOISE",
                value=min(1.0, 0.05 + spec.compass_interference_uT / 200.0),
                comment="Magnetometer measurement noise (T)",
            )
        )

    # Barometer drift via EKF2_BARO_NOISE.
    if spec.barometer_drift_pa_per_min > 0:
        params.append(
            PX4ParamOverride(
                name="EKF2_BARO_NOISE",
                value=min(10.0, 2.0 + spec.barometer_drift_pa_per_min / 5.0),
                comment="Barometer height noise (m)",
            )
        )

    # Telemetry rate via SDLOG_PROFILE (high rate) + MAVLink stream config.
    # We can't directly set MAV_*_RATE here since it's transport-dependent,
    # but we *can* enable the high-rate logging profile.
    if spec.telemetry_rate_hz >= 50:
        params.append(
            PX4ParamOverride(
                name="SDLOG_PROFILE",
                value=129,
                comment="High-rate profile (50-200 Hz IMU, attitude, position)",
            )
        )

    return params


# ─────────────────────────────────────────────────────────────────────────────
# Gazebo plugin / topic commands
# ─────────────────────────────────────────────────────────────────────────────


def gazebo_commands(spec: SITLEnvironmentSpec) -> list[str]:
    """Return a list of `gz` commands that activate the requested environment.

    The orchestrator runs these via the WSL bridge before bringing PX4 up;
    they configure the wind/turbulence plugin on the running world.
    """
    cmds: list[str] = []
    if spec.wind_north_m_s != 0.0 or spec.wind_east_m_s != 0.0:
        cmds.append(
            f"gz topic -t /wind -m gz.msgs.Wind "
            f"-p 'linear_velocity: {{x: {spec.wind_north_m_s:.2f}, "
            f"y: {spec.wind_east_m_s:.2f}, z: 0.0}}'"
        )
    if spec.wind_gust_m_s > 0:
        cmds.append(f"gz topic -t /wind/gust -m gz.msgs.Float -p 'data: {spec.wind_gust_m_s:.2f}'")
    if spec.turbulence_intensity > 0:
        cmds.append(
            f"gz topic -t /wind/turbulence -m gz.msgs.Float "
            f"-p 'data: {spec.turbulence_intensity:.2f}'"
        )
    if spec.gps_multipath_enabled:
        cmds.append("gz topic -t /gps/multipath -m gz.msgs.Boolean -p 'data: true'")
    return cmds


def render_param_file(spec: SITLEnvironmentSpec) -> str:
    """Render a PX4 ``param set`` script that can be sourced inside the SITL shell."""
    lines: list[str] = [f"# SITL environment: {spec.name}"]
    for p in px4_parameter_overrides(spec):
        if p.comment:
            lines.append(f"# {p.comment}")
        lines.append(f"param set {p.name} {p.value:g}")
    lines.append("param save")
    return "\n".join(lines) + "\n"


def get_scenario(name: str) -> SITLEnvironmentSpec:
    """Look up a pre-built scenario by name."""
    if name not in SCENARIOS:
        raise KeyError(f"Unknown SITL scenario: {name!r}. Available: {sorted(SCENARIOS)}")
    return SCENARIOS[name]
