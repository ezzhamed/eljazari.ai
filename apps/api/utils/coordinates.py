"""eljazari.ai — Phase 8: NED ↔ LLA coordinate conversions.

Real MAVSDK ``MissionItem`` takes ``latitude_deg`` / ``longitude_deg``, not
NED offsets. The generated PX4 mission code therefore needs to translate
the high-level "go 6 m north" plan into absolute lat/lon at the drone's
home position.

These utilities are pure, deterministic, and free of external dependencies
(no pyproj / geographiclib) so they can run inside generated mission code
on a Pixhawk-embedded environment without extra packages.

Reference:
  - Spherical Earth approximation good to < 0.5 m over 1 km. For ranges
    beyond a few km use the WGS84 ellipsoid (``haversine_precision='wgs84'``).

All angles are in degrees; all distances are in metres.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# Mean Earth radius for spherical approximation (IUGG mean radius)
EARTH_RADIUS_M: float = 6_371_000.0

# WGS84 ellipsoid constants
WGS84_A: float = 6_378_137.0  # semi-major axis (m)
WGS84_F: float = 1 / 298.257223563  # flattening
WGS84_E2: float = WGS84_F * (2.0 - WGS84_F)  # first eccentricity squared


PrecisionMode = Literal["spherical", "wgs84"]


@dataclass(frozen=True)
class LLA:
    """Latitude / longitude / altitude (degrees, degrees, metres AMSL)."""

    lat_deg: float
    lon_deg: float
    alt_m: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.lat_deg <= 90.0:
            raise ValueError(f"latitude out of range: {self.lat_deg}")
        if not -180.0 <= self.lon_deg <= 180.0:
            raise ValueError(f"longitude out of range: {self.lon_deg}")


@dataclass(frozen=True)
class NED:
    """Local-tangent-plane offset from a home position (metres).

    Convention follows PX4: North=+X, East=+Y, Down=+Z. A drone climbing
    has *negative* ``down_m`` because Down is positive.
    """

    north_m: float
    east_m: float
    down_m: float = 0.0


def _meridian_radius(lat_rad: float) -> float:
    """Radius of curvature in the meridian (m)."""
    s = math.sin(lat_rad)
    return WGS84_A * (1.0 - WGS84_E2) / (1.0 - WGS84_E2 * s * s) ** 1.5


def _prime_vertical_radius(lat_rad: float) -> float:
    """Radius of curvature in the prime vertical (m)."""
    s = math.sin(lat_rad)
    return WGS84_A / math.sqrt(1.0 - WGS84_E2 * s * s)


def ned_to_lla(
    home: LLA,
    offset: NED,
    *,
    precision: PrecisionMode = "wgs84",
) -> LLA:
    """Translate a NED offset measured from ``home`` to an absolute LLA point.

    Spherical mode is faster and adequate up to ~1 km. WGS84 mode (the
    default) uses ellipsoid radii and is correct for arbitrary range.
    """
    lat_rad = math.radians(home.lat_deg)
    cos_lat = math.cos(lat_rad)
    if abs(cos_lat) < 1e-10:
        # At the poles longitude is undefined.
        raise ValueError("Cannot project NED → LLA at the pole (cos(lat) ≈ 0).")
    if precision == "spherical":
        r_lat = EARTH_RADIUS_M
        r_lon = EARTH_RADIUS_M * cos_lat
    else:
        r_lat = _meridian_radius(lat_rad)
        r_lon = _prime_vertical_radius(lat_rad) * cos_lat
    dlat_deg = math.degrees(offset.north_m / r_lat)
    dlon_deg = math.degrees(offset.east_m / r_lon)
    return LLA(
        lat_deg=home.lat_deg + dlat_deg,
        lon_deg=_wrap_longitude(home.lon_deg + dlon_deg),
        alt_m=home.alt_m + (-offset.down_m),
    )


def lla_to_ned(
    home: LLA,
    point: LLA,
    *,
    precision: PrecisionMode = "wgs84",
) -> NED:
    """Inverse of :func:`ned_to_lla` — needed for path-tracking error metrics."""
    lat_rad = math.radians(home.lat_deg)
    cos_lat = math.cos(lat_rad)
    if abs(cos_lat) < 1e-10:
        raise ValueError("Cannot project LLA → NED at the pole.")
    if precision == "spherical":
        r_lat = EARTH_RADIUS_M
        r_lon = EARTH_RADIUS_M * cos_lat
    else:
        r_lat = _meridian_radius(lat_rad)
        r_lon = _prime_vertical_radius(lat_rad) * cos_lat
    n = math.radians(point.lat_deg - home.lat_deg) * r_lat
    e = math.radians(_wrap_longitude(point.lon_deg - home.lon_deg)) * r_lon
    d = -(point.alt_m - home.alt_m)
    return NED(north_m=n, east_m=e, down_m=d)


def haversine_distance_m(a: LLA, b: LLA) -> float:
    """Great-circle distance between two LLA points (spherical model)."""
    lat1 = math.radians(a.lat_deg)
    lat2 = math.radians(b.lat_deg)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon_deg - a.lon_deg)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _wrap_longitude(lon_deg: float) -> float:
    """Wrap longitude into the canonical (-180, 180] range."""
    if lon_deg > 180.0:
        return lon_deg - 360.0
    if lon_deg < -180.0:
        return lon_deg + 360.0
    return lon_deg


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: default PX4 SITL home position
# ─────────────────────────────────────────────────────────────────────────────

#: Default PX4 SITL home (Zurich Iron Bird, used by the gz_x500 world).
PX4_SITL_DEFAULT_HOME = LLA(lat_deg=47.397742, lon_deg=8.545594, alt_m=488.0)
