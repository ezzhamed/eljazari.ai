"""Match propulsion declarations without treating them as verified measurements."""

import hashlib
import json
import math
from collections.abc import Sequence

from agents.component_models import (
    MotorThrustPoint,
    PropulsionCurveEvidence,
    PropulsionEvidenceAssessment,
    SelectedComponents,
)

# Rounding tolerance only; this is not a validated battery operating range.
VOLTAGE_ABS_TOLERANCE_V = 0.05


def validate_curve(points: Sequence[MotorThrustPoint]) -> None:
    """Reject curves unsuitable for the solver's one-dimensional interpolation."""
    if len(points) < 2:
        raise ValueError("At least two operating points are required")
    for point in points:
        MotorThrustPoint.model_validate(point.model_dump())
    ordered = sorted(points, key=lambda p: p.throttle_pct)
    for lo, hi in zip(ordered, ordered[1:]):
        if hi.throttle_pct <= lo.throttle_pct or hi.thrust_g <= lo.thrust_g:
            raise ValueError("Throttle and thrust must be strictly increasing")


def thrust_curve_sha256(points: Sequence[MotorThrustPoint]) -> str:
    """Hash normalized numeric values in throttle order; reject invalid curves."""
    validate_curve(points)
    rows = [
        MotorThrustPoint.model_validate(p.model_dump()).model_dump(mode="json")
        for p in sorted(points, key=lambda p: p.throttle_pct)
    ]
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _same_voltage(a: float, b: float) -> bool:
    return math.isclose(a, b, abs_tol=VOLTAGE_ABS_TOLERANCE_V, rel_tol=0)


def curve_matches_battery(points: Sequence[MotorThrustPoint], battery_nominal_v: float) -> bool:
    """True when the curve was recorded at one voltage equal to the battery's nominal.

    The same rule the generated flight model enforces (pipelines/flight_model.py):
    a curve is never reused at another voltage.
    """
    voltages = {round(p.voltage_v, 3) for p in points}
    return len(voltages) == 1 and _same_voltage(points[0].voltage_v, battery_nominal_v)


def assess_propulsion_evidence(parts: SelectedComponents) -> PropulsionEvidenceAssessment:
    points = parts.motor.thrust_curve
    reasons: list[str] = []
    digest = None
    try:
        digest = thrust_curve_sha256(points)
    except ValueError as exc:
        reasons.append(f"Invalid thrust curve: {exc}")
    if any(not _same_voltage(p.voltage_v, parts.battery.pack_voltage_nominal_v) for p in points):
        reasons.append("Curve voltage does not match selected battery nominal voltage")
    if any(p.rpm > parts.propeller.max_rpm_safe for p in points):
        reasons.append("Curve RPM exceeds the selected propeller rating")
    declared = parts.motor.thrust_curve_evidence
    if declared is None:
        reasons.insert(0, "No motor/propeller/source declaration for this thrust curve")
        return PropulsionEvidenceAssessment(status="missing", curve_sha256=digest, reasons=reasons)
    # Models are mutable: revalidate before using a declaration.
    try:
        declared = PropulsionCurveEvidence.model_validate(declared.model_dump(mode="json"))
    except ValueError:
        reasons.append("Malformed propulsion source declaration")
        return PropulsionEvidenceAssessment(status="mismatch", curve_sha256=digest, reasons=reasons)
    if declared.motor_part_number != parts.motor.part_number:
        reasons.append("Declared motor does not match selected motor")
    if declared.propeller_part_number != parts.propeller.part_number:
        reasons.append("Declared propeller does not match selected propeller")
    if declared.curve_sha256 != digest:
        reasons.append("Curve fingerprint differs from the source declaration")
    if not _same_voltage(declared.test_voltage_v, parts.battery.pack_voltage_nominal_v):
        reasons.append("Declared test voltage does not match selected battery nominal voltage")
    if any(not _same_voltage(p.voltage_v, declared.test_voltage_v) for p in points):
        reasons.append("Curve contains voltages outside the declared test condition")
    return PropulsionEvidenceAssessment(
        status="mismatch" if reasons else "matched_declaration",
        curve_sha256=digest,
        reasons=reasons,
        source_reference=declared.source_reference,
        source_sha256=declared.source_sha256,
    )
