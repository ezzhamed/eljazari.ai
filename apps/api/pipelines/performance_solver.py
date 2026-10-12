"""eljazari.ai — Phase 6: Real flight-performance solver.

Computes preliminary performance from catalog curves and electrical ratings.
The accompanying evidence assessment checks source declarations against the
selected propulsion system. It does not independently verify measurements.
``require_matching_evidence=True`` refuses absent or inconsistent declarations;
even matching declarations do not make this a validated flight model.

All inputs come from the validated Pydantic models in ``component_models.py``;
the solver is pure (no LLM, no I/O) and deterministic so it can be unit-tested
against eCalc / measured data.

Reference equations used here:
  - Thrust-to-weight from per-motor curve interpolation
  - Hover throttle by 1D interpolation on (throttle_pct → thrust_g)
  - Hover endurance from usable_energy_wh / hover_power_w
  - Voltage sag = I_total * (R_internal_per_cell * cells_s)
  - Disc loading = (m * g) / (n_motors * disc_area_m2)
  - Power loading = m_g / hover_power_w

All numbers stay in SI-friendly units except for grams (g) which is the
convention in drone literature; conversions happen explicitly.
"""

from __future__ import annotations

import math
from typing import Iterable

from agents.component_models import (
    BatteryComponent,
    MotorComponent,
    MotorThrustPoint,
    RealPerformanceReport,
    SelectedComponents,
)


# Earth gravity for grams ↔ Newtons.
G_M_S2 = 9.80665

# Empirical correction factor: catalog hover thrust under-predicts real-world
# disturbance compensation needed. PX4 community data shows ~5% margin needed.
_HOVER_THRUST_MARGIN = 1.05


def _interpolate(points: Iterable[MotorThrustPoint], x_attr: str, y_attr: str, x: float) -> float:
    """Piecewise-linear interpolation on motor thrust curve points.

    Falls back to the bounding value if x is outside the curve range.
    """
    sorted_pts = sorted(points, key=lambda p: getattr(p, x_attr))
    if not sorted_pts:
        raise ValueError("Empty thrust curve")
    if x <= getattr(sorted_pts[0], x_attr):
        return getattr(sorted_pts[0], y_attr)
    if x >= getattr(sorted_pts[-1], x_attr):
        return getattr(sorted_pts[-1], y_attr)
    for lo, hi in zip(sorted_pts, sorted_pts[1:]):
        lo_x = getattr(lo, x_attr)
        hi_x = getattr(hi, x_attr)
        if lo_x <= x <= hi_x:
            t = (x - lo_x) / (hi_x - lo_x)
            return getattr(lo, y_attr) + t * (getattr(hi, y_attr) - getattr(lo, y_attr))
    return getattr(sorted_pts[-1], y_attr)  # pragma: no cover (logic guarantees)


def _motor_throttle_for_thrust(motor: MotorComponent, target_thrust_g: float) -> float:
    """Linear-interpolate throttle % needed to produce the requested thrust."""
    return _interpolate(motor.thrust_curve, "thrust_g", "throttle_pct", target_thrust_g)


def _motor_current_at_throttle(motor: MotorComponent, throttle_pct: float) -> float:
    return _interpolate(motor.thrust_curve, "throttle_pct", "current_a", throttle_pct)


def _motor_power_at_throttle(motor: MotorComponent, throttle_pct: float) -> float:
    return _interpolate(motor.thrust_curve, "throttle_pct", "power_w", throttle_pct)


def _voltage_sag_v(
    battery: BatteryComponent,
    total_current_a: float,
) -> float:
    """Pack voltage drop under load: I × R_total."""
    r_total_ohm = (battery.internal_resistance_mohm_per_cell / 1000.0) * battery.cells_s
    return total_current_a * r_total_ohm


class PerformanceSolver:
    """Stateless performance calculator.

    Usage:

        solver = PerformanceSolver()
        report = solver.solve(selected_components, extra_mass_g=200.0)
    """

    def solve(
        self,
        components: SelectedComponents,
        extra_mass_g: float = 0.0,
        cruise_throttle_pct: float = 60.0,
        *,
        require_matching_evidence: bool = False,
    ) -> RealPerformanceReport:
        if not math.isfinite(extra_mass_g) or extra_mass_g < 0:
            raise ValueError("extra_mass_g must be finite and non-negative")
        from pipelines.propulsion_evidence import assess_propulsion_evidence, validate_curve

        validate_curve(components.motor.thrust_curve)
        evidence = assess_propulsion_evidence(components)
        if require_matching_evidence and evidence.status != "matched_declaration":
            raise ValueError("Propulsion evidence does not match: " + "; ".join(evidence.reasons))
        warnings: list[str] = []
        if evidence.status != "matched_declaration":
            warnings.append(
                "[warning] Preliminary propulsion estimate: " + "; ".join(evidence.reasons)
            )
        else:
            warnings.append(
                "[warning] Propulsion source declaration matches; source authenticity, "
                "battery voltage variation and flight dynamics remain unvalidated."
            )
        motor = components.motor
        prop = components.propeller
        battery = components.battery
        esc = components.esc
        n = components.motor_count

        # ── 1. Mass ────────────────────────────────────────────────────────
        total_mass_g = components.total_bom_weight_g + extra_mass_g

        # ── 2. Thrust budget ───────────────────────────────────────────────
        max_thrust_per_motor = motor.max_thrust_g()
        max_total_thrust_g = max_thrust_per_motor * n
        thrust_per_motor_for_hover = (total_mass_g / n) * _HOVER_THRUST_MARGIN

        if thrust_per_motor_for_hover > max_thrust_per_motor:
            warnings.append(
                f"[critical] Motor cannot hover this mass: needs "
                f"{thrust_per_motor_for_hover:.0f}g, motor max "
                f"{max_thrust_per_motor:.0f}g."
            )
            hover_throttle_pct = 100.0
        else:
            hover_throttle_pct = _motor_throttle_for_thrust(motor, thrust_per_motor_for_hover)

        # _interpolate clamps below the first point, so a light aircraft would be
        # reported at the lowest listed throttle, current and power. Measured
        # curves often start at 50% throttle; say so instead of presenting the
        # clamp as data, and refuse it where measured data was demanded.
        lowest_point_g = min(p.thrust_g for p in motor.thrust_curve)
        if thrust_per_motor_for_hover < lowest_point_g:
            message = (
                f"hover needs {thrust_per_motor_for_hover:.0f}g per motor, below the "
                f"lowest curve point ({lowest_point_g:.0f}g); hover throttle, current and "
                "power are clamped to that point, not interpolated"
            )
            if require_matching_evidence:
                raise ValueError("Hover is outside the propulsion curve: " + message)
            warnings.append("[warning] Outside the propulsion curve: " + message + ".")

        thrust_to_weight = max_total_thrust_g / max(total_mass_g, 1.0)
        if thrust_to_weight < 1.5:
            warnings.append(
                f"[critical] T:W {thrust_to_weight:.2f} below 1.5 — drone "
                f"will struggle to maintain attitude."
            )
        elif thrust_to_weight < 2.0:
            warnings.append(f"[warning] T:W {thrust_to_weight:.2f} — stable but not agile.")

        # ── 3. Hover power & current ───────────────────────────────────────
        hover_current_per_motor_a = _motor_current_at_throttle(motor, hover_throttle_pct)
        hover_power_per_motor_w = _motor_power_at_throttle(motor, hover_throttle_pct)
        hover_current_total_a = hover_current_per_motor_a * n
        hover_power_total_w = hover_power_per_motor_w * n

        # ── 4. Battery checks ──────────────────────────────────────────────
        max_continuous_a = battery.max_continuous_current_a
        max_burst_a = max_continuous_a * (battery.max_burst_c / battery.max_continuous_c)

        if hover_current_total_a > max_continuous_a:
            warnings.append(
                f"[critical] Battery C-rate insufficient: hover draws "
                f"{hover_current_total_a:.1f}A, battery continuous {max_continuous_a:.1f}A."
            )
        elif hover_current_total_a > max_continuous_a * 0.8:
            warnings.append(
                f"[warning] Battery near continuous C-rate limit "
                f"({hover_current_total_a / max_continuous_a:.0%})."
            )

        # Burst (e.g. max-throttle climb)
        max_throttle_current_per_motor_a = motor.max_continuous_current_a
        burst_current_total_a = max_throttle_current_per_motor_a * n
        c_rate_burst_utilization_pct = (burst_current_total_a / max_burst_a) * 100.0
        if c_rate_burst_utilization_pct > 100.0:
            warnings.append(
                f"[critical] Burst current {burst_current_total_a:.1f}A "
                f"exceeds battery burst rating {max_burst_a:.1f}A."
            )

        # ── 5. ESC checks ──────────────────────────────────────────────────
        esc_capacity_per_motor_a = esc.max_continuous_current_per_channel_a
        if motor.max_continuous_current_a > esc_capacity_per_motor_a:
            warnings.append(
                f"[critical] ESC underrated: motor draws up to "
                f"{motor.max_continuous_current_a:.0f}A, ESC channel rated "
                f"{esc.max_continuous_current_per_channel_a:.0f}A."
            )
        if esc.channels not in (1, n):
            warnings.append(
                f"[warning] ESC channel count {esc.channels} does not match "
                f"motor count {n}; pick a {n}-in-1 or {n} single ESCs."
            )

        if not esc.min_cells_s <= battery.cells_s <= esc.max_cells_s:
            warnings.append("[critical] Battery cell count is outside the ESC voltage range.")
        if prop.diameter_inch > components.frame.max_prop_size_inch:
            warnings.append("[critical] Propeller exceeds the frame maximum propeller size.")

        # ── 6. Voltage sag ────────────────────────────────────────────────
        voltage_sag_v = _voltage_sag_v(battery, burst_current_total_a)
        if voltage_sag_v / battery.pack_voltage_nominal_v > 0.15:
            warnings.append(
                f"[warning] Voltage sag {voltage_sag_v:.1f}V at burst "
                f"({voltage_sag_v / battery.pack_voltage_nominal_v:.0%} of pack)."
            )

        # ── 7. Endurance ──────────────────────────────────────────────────
        usable_wh = battery.usable_energy_wh
        hover_endurance_min = (
            (usable_wh / hover_power_total_w) * 60.0 if hover_power_total_w > 0 else 0.0
        )
        cruise_throttle = max(hover_throttle_pct + 5.0, min(hover_throttle_pct * 1.4, 80.0))
        cruise_power_total_w = _motor_power_at_throttle(motor, cruise_throttle) * n
        cruise_endurance_min = (
            (usable_wh / cruise_power_total_w) * 60.0 if cruise_power_total_w > 0 else 0.0
        )

        # ── 8. Disc loading / power loading ───────────────────────────────
        total_disc_area_m2 = prop.disc_area_m2 * n
        disc_loading_n_m2 = (total_mass_g / 1000.0) * G_M_S2 / total_disc_area_m2
        power_loading_g_per_w = total_mass_g / max(hover_power_total_w, 1.0)

        # ── 9. Range estimate ─────────────────────────────────────────────
        # Assume cruise speed equals advance-ratio sweet spot for prop:
        # v ≈ 0.6 × pitch × max_safe_rpm / 60   (m/s)
        cruise_v_m_s = 0.6 * (prop.pitch_inch * 0.0254) * (prop.max_rpm_safe / 60.0)
        max_range_km = (
            (cruise_endurance_min / 60.0) * cruise_v_m_s * 3.6
        )  # min→h × m/s × 3.6 = km/h × h

        # ── 10. Payload capacity ──────────────────────────────────────────
        # Remaining lift before T:W drops to 1.5.
        max_takeoff_mass_for_t2w_1_5_g = max_total_thrust_g / 1.5
        payload_capacity_g = max(0.0, max_takeoff_mass_for_t2w_1_5_g - total_mass_g)

        # ── 11. Motor saturation headroom ─────────────────────────────────
        saturation_headroom_pct = max(0.0, 100.0 - hover_throttle_pct)

        return RealPerformanceReport(
            propulsion_evidence=evidence,
            total_mass_g=round(total_mass_g, 1),
            payload_capacity_g=round(payload_capacity_g, 1),
            max_total_thrust_g=round(max_total_thrust_g, 0),
            thrust_to_weight_ratio=round(thrust_to_weight, 2),
            hover_throttle_pct=round(hover_throttle_pct, 1),
            hover_thrust_per_motor_g=round(thrust_per_motor_for_hover, 1),
            hover_current_a=round(hover_current_total_a, 2),
            hover_power_w=round(hover_power_total_w, 1),
            battery_c_rate_at_hover=round(
                hover_current_total_a / (battery.capacity_mah / 1000.0), 1
            ),
            hover_endurance_min=round(hover_endurance_min, 1),
            cruise_endurance_min=round(cruise_endurance_min, 1),
            max_range_km=round(max_range_km, 1),
            disc_loading_n_m2=round(disc_loading_n_m2, 1),
            power_loading_g_per_w=round(power_loading_g_per_w, 2),
            warnings=warnings,
            voltage_sag_at_max_throttle_v=round(voltage_sag_v, 2),
            motor_saturation_headroom_pct=round(saturation_headroom_pct, 1),
            battery_c_rate_burst_utilization_pct=round(c_rate_burst_utilization_pct, 1),
        )
