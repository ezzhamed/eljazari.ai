"""What the request asked for, whether it can be flown at all, and whether it was.

The benchmark (docs/BENCHMARK.md) showed two ways an impossible request became an
accepted flight: requirements that were never extracted, and a safety violation
that the code-regeneration loop «fixed» by flying a weaker mission. This module
closes both with deterministic checks on the parsed request:

* :func:`request_limit_violations` runs before any agent: a request that itself
  exceeds a configured limit is infeasible and ends the run, instead of reaching
  code generation where the violation would be retried away.
* :func:`conformance_reasons` runs in the acceptance gate: the flown aircraft and
  mission must match the requested topology, payload, total mass and altitude.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from utils.mission_parser import MissionSpec, parse_mission_spec

# Flown altitude may differ from the request by this much (m, or fraction of it).
ALTITUDE_ABS_TOLERANCE_M = 1.0
ALTITUDE_REL_TOLERANCE = 0.10
# Grams of rounding allowed between requested and modelled masses.
MASS_TOLERANCE_G = 0.5


def requested_requirements(spec: MissionSpec) -> dict[str, Any]:
    """JSON-safe record of the request's explicit values (None where not stated)."""
    return {
        "language": spec.language,
        "topology": spec.topology,
        "payload_g": spec.payload_g,
        "max_total_mass_g": spec.max_total_mass_g,
        "altitude_m": spec.altitude_m,
        "endurance_min": spec.duration_min,
        "hover_s": spec.hover_duration_s,
        "square_size_m": spec.square_size_m,
        "unresolved": list(spec.unresolved),
    }


def request_limit_violations(
    spec: MissionSpec,
    *,
    max_altitude_m: float,
    min_altitude_m: float,
    max_geofence_radius_m: float,
) -> list[str]:
    """Reasons the request cannot be flown as stated, before any code exists."""
    reasons: list[str] = []
    if spec.altitude_m is not None:
        if spec.altitude_m > max_altitude_m:
            reasons.append(
                f"requested altitude {spec.altitude_m:g} m exceeds the {max_altitude_m:g} m limit"
            )
        elif spec.altitude_m < min_altitude_m:
            reasons.append(
                f"requested altitude {spec.altitude_m:g} m is below the {min_altitude_m:g} m minimum"
            )
    if spec.square_size_m is not None:
        # The far corner of a square flown from home is side·√2 away.
        reach = spec.square_size_m * math.sqrt(2)
        if reach > max_geofence_radius_m:
            reasons.append(
                f"a {spec.square_size_m:g} m square reaches {reach:.0f} m from home, beyond the "
                f"{max_geofence_radius_m:g} m geofence"
            )
    for item in spec.unresolved:
        if item.startswith("conflicting"):
            reasons.append(f"the request states {item}; state one value")
    return reasons


def _flown_max_altitude_m(state: Mapping[str, Any]) -> float | None:
    points = state.get("telemetry_data")
    if not isinstance(points, list):
        return None
    altitudes = [
        p["alt_m"]
        for p in points
        if isinstance(p, dict)
        and type(p.get("alt_m")) in (int, float)
        and math.isfinite(p["alt_m"])
    ]
    return max(altitudes) if altitudes else None


def conformance_reasons(
    state: Mapping[str, Any], *, generated: bool
) -> tuple[list[str], bool, dict[str, Any]]:
    """Differences between the request and what flew.

    Returns (reasons, retryable_with_code_change, record). Aircraft differences
    (topology, payload, mass) need a new design, so they are not retryable;
    a wrong altitude is the mission code's, so it is.
    """
    spec = parse_mission_spec(str(state.get("user_prompt") or ""))
    requested = requested_requirements(spec)
    reasons: list[str] = []
    retryable = True
    resolved = state.get("resolved_drone_spec") or {}
    attestation = state.get("simulation_model_attestation") or {}

    if generated:
        flown_topology = resolved.get("topology")
        if spec.topology and flown_topology and flown_topology != spec.topology:
            reasons.append(
                f"Flown aircraft is a {flown_topology}; the request asked for a {spec.topology}"
            )
            retryable = False
        flown_payload = resolved.get("payload_g")
        if spec.payload_g is not None and type(flown_payload) in (int, float):
            if flown_payload + MASS_TOLERANCE_G < spec.payload_g:
                reasons.append(
                    f"Flown aircraft carries {flown_payload:g} g of payload; the request "
                    f"asked for {spec.payload_g:g} g"
                )
                retryable = False
        flown_mass_kg = attestation.get("simulated_total_mass_kg")
        if spec.max_total_mass_g is not None and type(flown_mass_kg) in (int, float):
            if flown_mass_kg * 1000.0 > spec.max_total_mass_g + MASS_TOLERANCE_G:
                reasons.append(
                    f"Flown aircraft weighs {flown_mass_kg * 1000.0:.0f} g; the request "
                    f"limits it to {spec.max_total_mass_g:g} g"
                )
                retryable = False

    flown_altitude = _flown_max_altitude_m(state)
    if spec.altitude_m is not None and flown_altitude is not None:
        tolerance = max(ALTITUDE_ABS_TOLERANCE_M, ALTITUDE_REL_TOLERANCE * spec.altitude_m)
        if abs(flown_altitude - spec.altitude_m) > tolerance:
            reasons.append(
                f"Flew to {flown_altitude:.1f} m; the request asked for {spec.altitude_m:g} m "
                f"(±{tolerance:.1f} m)"
            )

    record = {
        "requested": requested,
        "flown": {
            "topology": resolved.get("topology") if generated else None,
            "payload_g": resolved.get("payload_g") if generated else None,
            "total_mass_g": (
                round(attestation["simulated_total_mass_kg"] * 1000.0, 1)
                if generated and type(attestation.get("simulated_total_mass_kg")) in (int, float)
                else None
            ),
            "max_altitude_m": round(flown_altitude, 2) if flown_altitude is not None else None,
        },
    }
    return reasons, retryable, record
