"""Calibration tooling on SYNTHETIC logs: parsing, settling and comparison only.

These fixtures are generated here and are not measurements of any hardware.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date

import pytest

from pipelines.calibration import (
    GRAVITY,
    HoverEvidence,
    bench_test_case,
    compare_bench_to_curve,
    compare_hover,
    read_thrust_stand_csv,
    steady_points,
)

HEADER = (
    "Time (s),ESC signal (µs),Torque (N·m),Thrust (kgf),Voltage (V),Current (A),"
    "Motor Electrical Speed (RPM),Motor Optical Speed (RPM)"
)
K = 1.4e-5  # N / (rad/s)^2, synthetic


def _row(t, esc, rpm, volts=14.8, transient=False):
    omega = rpm * 2 * math.pi / 60
    thrust_n = K * omega**2 * (0.5 if transient else 1.0)
    torque = 0.016 * thrust_n
    amps = thrust_n * 1.1
    return f"{t:.2f},{esc},{torque:.6f},{thrust_n / GRAVITY:.6f},{volts},{amps:.4f},{rpm * 7},{rpm}"


def _csv(tmp_path, steps=((1300, 4000), (1500, 5200), (1700, 6200), (1900, 7000)), volts=14.8):
    lines = [HEADER]
    t = 0.0
    for esc, rpm in steps:
        for i in range(20):
            lines.append(_row(t, esc, rpm, volts, transient=i < 4))
            t += 0.1
    path = tmp_path / "stand.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_steps_settle_and_convert_units(tmp_path):
    log = read_thrust_stand_csv(_csv(tmp_path))
    assert log.sha256 == hashlib.sha256((tmp_path / "stand.csv").read_bytes()).hexdigest()
    points = steady_points(log)
    assert [p.throttle_pct for p in points] == [30.0, 50.0, 70.0, 90.0]
    # Transient samples (first 20%) fall inside the discarded 30%: medians are exact.
    omega = 5200 * 2 * math.pi / 60
    assert points[1].thrust_g == pytest.approx(K * omega**2 * 1000 / GRAVITY, rel=1e-4)
    assert points[1].rpm == 5200  # optical, not electrical
    assert points[1].torque_nm == pytest.approx(0.016 * K * omega**2, rel=1e-3)


def test_bench_test_matches_the_catalog_contract(tmp_path):
    log = read_thrust_stand_csv(_csv(tmp_path))
    case = bench_test_case(
        log,
        steady_points(log),
        motor_part_number="tmotor_mn3110_700kv",
        propeller_part_number="tmotor_p12_4",
        nominal_voltage_v=14.8,
        measured_on=date(2026, 10, 1),
        reference="synthetic fixture, not hardware",
    )
    from agents.component_models import PropulsionTestCase

    fields = {k: v for k, v in case.items() if not k.startswith("measured_")}
    test = PropulsionTestCase(**fields)
    assert test.source_kind == "bench_test" and test.source_sha256 == log.sha256
    assert test.declaration().curve_sha256


def test_a_bench_run_off_nominal_voltage_is_refused(tmp_path):
    log = read_thrust_stand_csv(_csv(tmp_path, volts=16.2))
    with pytest.raises(ValueError, match="strays"):
        bench_test_case(
            log,
            steady_points(log),
            motor_part_number="m",
            propeller_part_number="p",
            nominal_voltage_v=14.8,
            measured_on=date(2026, 10, 1),
            reference="synthetic",
        )


@pytest.mark.parametrize(
    "header,message",
    [
        (HEADER.replace("Thrust (kgf)", "Thrust (oz)"), "unknown unit"),
        (HEADER.replace("Current (A)", "Amps"), "lacks columns: current"),
    ],
)
def test_units_are_read_from_headers_never_guessed(tmp_path, header, message):
    path = tmp_path / "bad.csv"
    path.write_text(
        header + "\n0,1500,0.1,0.5,14.8,5,35000,5000\n0,1500,0.1,0.5,14.8,5,35000,5000\n"
    )
    with pytest.raises(ValueError, match=message):
        read_thrust_stand_csv(path)


def test_bench_against_a_curve_reports_thrust_and_measured_torque(tmp_path):
    points = steady_points(read_thrust_stand_csv(_csv(tmp_path)))
    same = [{"rpm": p.rpm, "thrust_g": p.thrust_g, "throttle_pct": p.throttle_pct} for p in points]
    report = compare_bench_to_curve(points, same)
    assert report["thrust_coefficient_difference_pct"] == pytest.approx(0, abs=1e-6)
    assert all(row["difference_pct"] == pytest.approx(0, abs=1e-6) for row in report["by_step"])
    assert report["moment_constant_measured_m"] == pytest.approx(0.016, rel=1e-3)
    weaker = [dict(row, thrust_g=row["thrust_g"] * 1.1) for row in same]
    assert compare_bench_to_curve(points, weaker)["thrust_coefficient_difference_pct"] < -8


def test_hover_comparison_states_the_difference_and_tolerance():
    evidence = HoverEvidence("f.ulg", "a" * 64, 0.47, 900, 0.46, 14.9)
    inside = compare_hover(evidence, 0.448)
    assert inside["within_tolerance"] and inside["difference"] == pytest.approx(0.022)
    outside = compare_hover(evidence, 0.40)
    assert not outside["within_tolerance"]
