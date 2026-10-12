"""Deterministic checks before semantic evaluation of SITL runs.

Source labels are backend declarations, not cryptographic provenance. For a
generated design the run must also carry the simulator's attestation of the
exact flight bundle it loaded. A run that passes those basic checks must also
have flown the approved code's mission within the release protocol's limits
(:mod:`pipelines.flight_conformance`). None of this is airworthiness evidence.
"""

import math
from collections.abc import Mapping
from typing import Any

from pipelines.flight_conformance import assess_flight_conformance
from pipelines.request_requirements import conformance_reasons


SITL_SOURCES = {
    "wsl": "real_wsl_sitl",
    "native": "real_native_sitl",
    "docker": "real_docker_sitl",
}


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _contains_nonfinite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_nonfinite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_nonfinite(item) for item in value)
    return False


def assess_simulation(state: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    retryable = True
    if state.get("simulation_status") != "complete":
        reasons.append("Simulation collection did not complete")
        retryable = False
    runtime = state.get("runtime_mode")
    expected = SITL_SOURCES.get(runtime) if isinstance(runtime, str) else None
    if expected is None or state.get("telemetry_source") != expected:
        reasons.append(
            "Telemetry source is unknown, synthetic, partial or inconsistent with runtime"
        )
        retryable = False
    if state.get("mission_isolation") != "linux_namespaces":
        # Unsandboxed code shared a process with the telemetry collector and
        # could rewrite the very record this gate is judging.
        reasons.append("Mission code did not run in the isolated sandbox")
        retryable = False
    if state.get("mission_started") is not True:
        reasons.append("Mission start is not confirmed")
        retryable = False
    if state.get("mission_completed") is not True:
        reasons.append("Mission completion is not confirmed")
    if state.get("mission_error"):
        reasons.append(f"Mission error: {state['mission_error']}")
    for flag in ("crash_detected", "geofence_violation"):
        if state.get(flag) is not False:
            reasons.append(f"{flag} is true or not explicitly reported false")
    points = state.get("telemetry_data")
    telemetry_valid = False
    if not isinstance(points, list) or len(points) < 2:
        reasons.append("At least two telemetry samples are required")
        retryable = False
    else:
        telemetry_valid = True
        previous = -1.0
        for point in points:
            required = ("timestamp_s", "north_m", "east_m", "alt_m")
            if not isinstance(point, dict) or any(
                not _finite_number(point.get(key)) for key in required
            ):
                reasons.append("Telemetry has missing or non-finite time/position values")
                retryable = telemetry_valid = False
                break
            if _contains_nonfinite(point):
                reasons.append("Telemetry contains non-finite numeric values")
                retryable = telemetry_valid = False
                break
            timestamp = point["timestamp_s"]
            if timestamp < 0 or timestamp <= previous:
                reasons.append("Telemetry timestamps must be non-negative and strictly increasing")
                retryable = telemetry_valid = False
                break
            previous = timestamp
    generated, attested = _generated_identity(state, reasons)
    if generated and not attested:
        retryable = False
    # Only a complete, well-formed record is compared with the mission; a crash or
    # an unlabelled source already fails above, and its path would add only noise.
    if telemetry_valid and not reasons:
        flight = assess_flight_conformance(points, state.get("generated_code"))
        reasons.extend(flight["reasons"])
        if not flight["passed"] and not flight["retryable_with_code_change"]:
            retryable = False
    else:
        flight = {"evaluated": False, "passed": False, "reasons": ["Basic checks failed"]}
    # A flight that passed every check above may still not be the one that was
    # asked for: a lighter payload, another topology, a lower altitude.
    mismatches, code_can_fix, requirements = conformance_reasons(state, generated=generated)
    reasons.extend(mismatches)
    if mismatches and not code_can_fix:
        retryable = False
    return {
        "scope": "generated_sitl_basic_checks" if generated else "reference_sitl_basic_checks",
        "passed": not reasons,
        "reasons": reasons,
        "retryable_with_code_change": bool(reasons) and retryable,
        "mission_isolation": state.get("mission_isolation") or "unknown",
        "aircraft": "generated" if generated else "reference_x500",
        "generated_identity_attested": attested,
        "model_validation_scope": "sitl_catalog_estimate" if attested else None,
        # No calibrated or airworthiness validation exists at any scope.
        "generated_aircraft_validated": False,
        "requirements": requirements,
        "flight_conformance": flight,
    }


def _generated_identity(state: Mapping[str, Any], reasons: list[str]) -> tuple[bool, bool]:
    """A generated design passes only if the simulator attested that exact bundle."""
    attestation = state.get("simulation_model_attestation")
    expected_spec = state.get("resolved_drone_sha256")
    expected_bundle = state.get("flight_model_bundle_sha256")
    generated = bool(expected_spec or state.get("flight_model_manifest_path") or attestation)
    if not generated:
        return False, False
    problems = []
    if not isinstance(attestation, Mapping) or attestation.get("status") != "matched":
        problems.append("the simulator did not attest loading the generated aircraft")
    else:
        if expected_spec and attestation.get("resolved_spec_sha256") != expected_spec:
            problems.append("attested model belongs to a different resolved specification")
        if expected_bundle and attestation.get("bundle_sha256") != expected_bundle:
            problems.append("attested flight bundle differs from the current bundle")
        resolved = state.get("resolved_drone_spec") or {}
        motors = (resolved.get("components") or {}).get("motor_count", 4)
        if attestation.get("rotor_link_count") != motors or not attestation.get(
            "px4_params_checked"
        ):
            problems.append("attestation lacks rotor or PX4 parameter confirmation")
    if problems:
        reasons.append("Generated aircraft identity: " + "; ".join(problems))
    return True, not problems


def summarize_validation(findings: Any) -> dict[str, Any] | None:
    """Summarise the gate verdict and the aircraft under test for the UI.

    A score alone hid the difference between a blocked design, a run rejected
    by the deterministic checks and a pass on the reference x500.
    """
    if not isinstance(findings, dict):
        return None
    acceptance = findings.get("simulation_acceptance") or {}
    aircraft = findings.get("aircraft_under_test") or {}
    status = findings.get("flight_validation_status")
    if not (status or acceptance or aircraft):
        return None
    if not status:
        if acceptance.get("passed") is True:
            status = "passed"
        elif acceptance:
            status = "rejected_by_checks"
        else:
            status = "unknown"
    return {
        "status": status,
        "reason": findings.get("flight_validation_reason"),
        "aircraft": {
            "kind": aircraft.get("kind") or acceptance.get("aircraft") or "unknown",
            "model_instance": aircraft.get("model_instance"),
            "validation_scope": aircraft.get("validation_scope")
            or acceptance.get("model_validation_scope"),
            "simulated_total_mass_kg": aircraft.get("simulated_total_mass_kg"),
            "identity_attested": bool(
                aircraft.get("identity_attested") or acceptance.get("generated_identity_attested")
            ),
        },
        "mission_isolation": acceptance.get("mission_isolation"),
        "acceptance_reasons": [str(item) for item in acceptance.get("reasons") or []],
    }
