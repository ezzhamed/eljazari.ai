"""Compare the simulated aircraft with measurements from real hardware.

Two measurements close the loop that catalog data cannot:

* **Bench:** a thrust-stand log of one motor, propeller and voltage becomes a
  ``bench_test`` propulsion test (the same contract as a manufacturer table,
  keyed by the SHA-256 of the log) and is compared with the curve the catalog
  uses. Measured torque replaces the power-based yaw estimate.
* **Flight:** a real PX4 log (ULog) of a hover gives the hover thrust PX4 itself
  estimated; it is compared with the flight model's prediction for that aircraft.

Nothing here makes a design «calibrated» on its own: a report states what was
measured, the difference and the tolerance it was judged against. Without real
logs these tools produce no result at all — they never fall back to a default.
"""

from __future__ import annotations

import csv
import hashlib
import math
import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

GRAVITY = 9.80665

# Thrust-stand CSV headers (Tyto Robotics / RCbenchmark style): the unit must be
# in the header; a column with an unknown unit is rejected, never guessed.
_THRUST_UNITS_TO_G = {"kgf": 1000.0, "gf": 1.0, "g": 1.0, "n": 1000.0 / GRAVITY, "lbf": 453.59237}
_TORQUE_UNITS_TO_NM = {"n·m": 1.0, "n.m": 1.0, "nm": 1.0, "n m": 1.0}
_COLUMN = re.compile(r"^\s*(?P<name>[^(]+?)\s*\((?P<unit>[^)]*)\)\s*$")


@dataclass(frozen=True)
class BenchSample:
    esc_us: float | None
    throttle_pct: float | None
    thrust_g: float
    rpm: float
    voltage_v: float
    current_a: float
    torque_nm: float | None


@dataclass(frozen=True)
class BenchPoint:
    """One steady throttle step: medians of its settled samples."""

    throttle_pct: float
    rpm: float
    thrust_g: float
    current_a: float
    power_w: float
    voltage_v: float
    torque_nm: float | None
    samples: int


@dataclass
class BenchLog:
    path: str
    sha256: str
    samples: list[BenchSample]
    columns: dict[str, str] = field(default_factory=dict)


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("µ", "u").split())


def read_thrust_stand_csv(path: str | Path) -> BenchLog:
    """Parse a thrust-stand CSV; every used column must carry its unit."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Windows exports write «µs» as the single byte 0xB5 (Latin-1).
        text = raw.decode("latin-1")
    rows = list(csv.reader(text.splitlines()))
    if len(rows) < 2:
        raise ValueError("Thrust-stand log has no data rows")
    header = rows[0]
    found: dict[str, tuple[int, str]] = {}
    for index, cell in enumerate(header):
        match = _COLUMN.match(cell)
        if not match:
            continue
        name, unit = _norm(match.group("name")), _norm(match.group("unit"))
        if name.startswith("thrust"):
            if unit not in _THRUST_UNITS_TO_G:
                raise ValueError(f"Thrust column {cell!r} has an unknown unit")
            found["thrust"] = (index, unit)
        elif name.startswith("torque"):
            if unit not in _TORQUE_UNITS_TO_NM:
                raise ValueError(f"Torque column {cell!r} has an unknown unit")
            found["torque"] = (index, unit)
        elif name.startswith("esc signal") and unit in ("us", "μs"):
            found["esc"] = (index, unit)
        elif name.startswith("throttle") and unit == "%":
            found["throttle"] = (index, unit)
        elif name == "voltage" and unit == "v":
            found["voltage"] = (index, unit)
        elif name == "current" and unit == "a":
            found["current"] = (index, unit)
        elif "optical speed" in name and unit == "rpm":
            found["rpm_optical"] = (index, unit)
        elif "electrical speed" in name and unit == "rpm":
            found["rpm_electrical"] = (index, unit)
        elif name in ("speed", "rpm", "motor speed") and unit == "rpm":
            found["rpm_optical"] = (index, unit)
    missing = [k for k in ("thrust", "voltage", "current") if k not in found]
    if not ("rpm_optical" in found or "rpm_electrical" in found):
        missing.append("rpm")
    if not ("esc" in found or "throttle" in found):
        missing.append("esc signal (µs) or throttle (%)")
    if missing:
        raise ValueError(f"Thrust-stand log lacks columns: {', '.join(missing)}; header {header}")
    # Optical speed is measured at the propeller; electrical speed needs the
    # pole count, which a log does not state reliably.
    rpm_key = "rpm_optical" if "rpm_optical" in found else "rpm_electrical"

    def value(row: list[str], key: str) -> float | None:
        if key not in found:
            return None
        cell = row[found[key][0]].strip() if found[key][0] < len(row) else ""
        if not cell:
            return None
        number = float(cell)
        return number if math.isfinite(number) else None

    samples = []
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        thrust = value(row, "thrust")
        rpm = value(row, rpm_key)
        volts = value(row, "voltage")
        amps = value(row, "current")
        if None in (thrust, rpm, volts, amps):
            continue
        torque = value(row, "torque")
        samples.append(
            BenchSample(
                esc_us=value(row, "esc"),
                throttle_pct=value(row, "throttle"),
                thrust_g=abs(thrust) * _THRUST_UNITS_TO_G[found["thrust"][1]],
                rpm=abs(rpm),
                voltage_v=volts,
                current_a=amps,
                torque_nm=None
                if torque is None
                else abs(torque) * _TORQUE_UNITS_TO_NM[found["torque"][1]],
            )
        )
    if len(samples) < 2:
        raise ValueError("Thrust-stand log has fewer than two complete samples")
    columns = {key: header[index] for key, (index, _) in found.items()}
    return BenchLog(str(path), hashlib.sha256(raw).hexdigest(), samples, columns)


def steady_points(
    log: BenchLog,
    *,
    esc_min_us: float = 1000.0,
    esc_max_us: float = 2000.0,
    settle_fraction: float = 0.3,
    min_samples: int = 3,
) -> list[BenchPoint]:
    """Median of each throttle step after its first ``settle_fraction`` of samples.

    Steps are runs of consecutive samples at one command (ESC µs within 2 µs,
    or throttle % within 0.5). Idle steps without thrust are dropped.
    """
    steps: list[list[BenchSample]] = []
    previous: float | None = None
    for sample in log.samples:
        command = sample.esc_us if sample.esc_us is not None else sample.throttle_pct
        if command is None:
            continue
        tolerance = 2.0 if sample.esc_us is not None else 0.5
        if previous is None or abs(command - previous) > tolerance:
            steps.append([])
        steps[-1].append(sample)
        previous = command
    points = []
    for step in steps:
        settled = step[int(len(step) * settle_fraction) :]
        if len(settled) < min_samples:
            continue
        first = settled[0]
        if first.esc_us is not None:
            throttle = (first.esc_us - esc_min_us) / (esc_max_us - esc_min_us) * 100.0
        else:
            throttle = float(first.throttle_pct or 0.0)
        thrust = statistics.median(s.thrust_g for s in settled)
        if thrust <= 0 or throttle <= 0:
            continue
        volts = statistics.median(s.voltage_v for s in settled)
        amps = statistics.median(s.current_a for s in settled)
        torques = [s.torque_nm for s in settled if s.torque_nm is not None]
        points.append(
            BenchPoint(
                throttle_pct=round(min(100.0, throttle), 2),
                rpm=statistics.median(s.rpm for s in settled),
                thrust_g=thrust,
                current_a=amps,
                power_w=volts * amps,
                voltage_v=volts,
                torque_nm=statistics.median(torques) if torques else None,
                samples=len(settled),
            )
        )
    points.sort(key=lambda p: p.throttle_pct)
    return points


def bench_test_case(
    log: BenchLog,
    points: Sequence[BenchPoint],
    *,
    motor_part_number: str,
    propeller_part_number: str,
    nominal_voltage_v: float,
    measured_on: date,
    reference: str,
    voltage_tolerance_v: float = 0.3,
) -> dict[str, Any]:
    """A ``bench_test`` propulsion test in the catalog's contract.

    The bench voltage must stay near the battery's nominal voltage; the test is
    declared at that nominal so it matches the same batteries as a
    manufacturer table would, and the measured spread is kept in the record.
    """
    if len(points) < 2:
        raise ValueError("A propulsion test needs at least two steady steps")
    spread = max(abs(p.voltage_v - nominal_voltage_v) for p in points)
    if spread > voltage_tolerance_v:
        raise ValueError(
            f"Bench voltage strays {spread:.2f} V from the {nominal_voltage_v:g} V nominal "
            f"(tolerance {voltage_tolerance_v:g} V); log a supply-held run at nominal"
        )
    ordered = sorted(points, key=lambda p: p.throttle_pct)
    for a, b in zip(ordered, ordered[1:], strict=False):
        if b.thrust_g <= a.thrust_g:
            raise ValueError("Bench thrust is not strictly increasing with throttle")
    return {
        "motor_part_number": motor_part_number,
        "propeller_part_number": propeller_part_number,
        "test_voltage_v": nominal_voltage_v,
        "source_kind": "bench_test",
        "source_reference": reference,
        "source_sha256": log.sha256,
        "source_date": measured_on.isoformat(),
        "points": [
            {
                "throttle_pct": p.throttle_pct,
                "rpm": int(round(p.rpm)),
                "thrust_g": round(p.thrust_g, 1),
                "current_a": round(p.current_a, 3),
                "power_w": round(p.power_w, 2),
                "voltage_v": nominal_voltage_v,
            }
            for p in ordered
        ],
        "measured_voltage_v": [round(p.voltage_v, 3) for p in ordered],
        "measured_torque_nm": [
            None if p.torque_nm is None else round(p.torque_nm, 5) for p in ordered
        ],
    }


def _fit_k(rpm: Iterable[float], thrust_g: Iterable[float]) -> float:
    omegas = [r * 2 * math.pi / 60 for r in rpm]
    thrusts = [t * GRAVITY / 1000 for t in thrust_g]
    return sum(t * w * w for t, w in zip(thrusts, omegas, strict=True)) / sum(w**4 for w in omegas)


def compare_bench_to_curve(
    points: Sequence[BenchPoint], curve: Sequence[dict[str, float]]
) -> dict[str, Any]:
    """Measured versus catalog curve: thrust coefficient, thrust at equal RPM, torque."""
    bench_k = _fit_k([p.rpm for p in points], [p.thrust_g for p in points])
    curve_k = _fit_k([c["rpm"] for c in curve], [c["thrust_g"] for c in curve])
    rows = []
    for point in points:
        omega = point.rpm * 2 * math.pi / 60
        predicted_g = curve_k * omega**2 * 1000 / GRAVITY
        rows.append(
            {
                "rpm": round(point.rpm),
                "measured_thrust_g": round(point.thrust_g, 1),
                "curve_thrust_g": round(predicted_g, 1),
                "difference_pct": round((point.thrust_g - predicted_g) / predicted_g * 100, 2),
            }
        )
    torque = [(p.torque_nm, p.thrust_g * GRAVITY / 1000) for p in points if p.torque_nm]
    moment = statistics.fmean(q / t for q, t in torque) if torque else None
    return {
        "thrust_coefficient_bench": bench_k,
        "thrust_coefficient_curve": curve_k,
        "thrust_coefficient_difference_pct": round((bench_k - curve_k) / curve_k * 100, 2),
        "by_step": rows,
        # Measured yaw moment ratio (m): torque over thrust, replacing the
        # flight model's estimate from electrical power.
        "moment_constant_measured_m": None if moment is None else round(moment, 6),
    }


# ── Flight logs ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HoverEvidence:
    ulog: str
    ulog_sha256: str
    hover_thrust_median: float
    hover_samples: int
    motor_command_median: float | None
    battery_voltage_median: float | None


def hover_from_ulog(path: str | Path, *, motor_count: int) -> HoverEvidence:
    """PX4's own hover-thrust estimate and motor commands from a flight log.

    Uses ``hover_thrust_estimate`` samples PX4 marked valid and, while airborne
    (mean motor command above 5%), the median motor command.
    """
    try:
        from pyulog import ULog
    except ImportError as exc:  # pragma: no cover — depends on the environment
        raise RuntimeError("Reading ULog files needs pyulog: pip install pyulog") from exc
    raw = Path(path).read_bytes()
    log = ULog(str(path), ["hover_thrust_estimate", "actuator_motors", "battery_status"])
    hover: list[float] = []
    command: float | None = None
    voltage: float | None = None
    for data in log.data_list:
        if data.name == "hover_thrust_estimate":
            hover += [
                float(h) for h, ok in zip(data.data["hover_thrust"], data.data["valid"]) if ok
            ]
        elif data.name == "actuator_motors":
            columns = [data.data[f"control[{i}]"] for i in range(motor_count)]
            means = [sum(c[j] for c in columns) / motor_count for j in range(len(columns[0]))]
            airborne = [m for m in means if m > 0.05]
            command = float(statistics.median(airborne)) if airborne else None
        elif data.name == "battery_status" and "voltage_v" in data.data:
            values = [float(v) for v in data.data["voltage_v"] if v > 0]
            voltage = statistics.median(values) if values else None
    if not hover:
        raise ValueError("The log has no valid hover_thrust_estimate samples; hover longer")
    return HoverEvidence(
        ulog=str(path),
        ulog_sha256=hashlib.sha256(raw).hexdigest(),
        hover_thrust_median=float(statistics.median(hover)),
        hover_samples=len(hover),
        motor_command_median=command,
        battery_voltage_median=voltage,
    )


def compare_hover(
    evidence: HoverEvidence, predicted_hover_command: float, *, tolerance: float = 0.05
) -> dict[str, Any]:
    """Real hover thrust (PX4's estimate) versus the model's predicted command.

    ``tolerance`` is an absolute difference in normalised thrust command; a
    mismatch says the thrust curve, mass or motor model does not describe the
    aircraft, not which of them is wrong.
    """
    difference = evidence.hover_thrust_median - predicted_hover_command
    return {
        "measured_hover_thrust": round(evidence.hover_thrust_median, 4),
        "predicted_hover_command": round(predicted_hover_command, 4),
        "difference": round(difference, 4),
        "tolerance": tolerance,
        "within_tolerance": abs(difference) <= tolerance,
        "hover_samples": evidence.hover_samples,
        "motor_command_median": evidence.motor_command_median,
        "battery_voltage_median": evidence.battery_voltage_median,
        "ulog_sha256": evidence.ulog_sha256,
    }
