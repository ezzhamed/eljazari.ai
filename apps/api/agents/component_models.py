"""eljazari.ai — Phase 6: Real component specifications.

These are *instance* models that wrap real, purchaseable parts from
manufacturer catalogs (T-Motor, Holybro, Tattu, iFlight, HQ Prop, etc.).

They are intentionally rich: every numeric field listed here is needed by
the aerodynamic/electrical performance solver. A `Motor` without a thrust
curve is useless because we cannot compute hover throttle, hover endurance,
or motor saturation under load.

Compatible with Pydantic v2.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ─────────────────────────────────────────────────────────────────────────────
# Motor
# ─────────────────────────────────────────────────────────────────────────────


class MotorThrustPoint(BaseModel):
    """One catalog operating point; measurement provenance is stored separately."""

    model_config = ConfigDict(allow_inf_nan=False)

    throttle_pct: float = Field(..., ge=0, le=100)
    rpm: int = Field(..., ge=0)
    thrust_g: float = Field(..., ge=0)
    current_a: float = Field(..., ge=0)
    power_w: float = Field(..., ge=0)
    voltage_v: float = Field(..., gt=0)


class PropulsionCurveEvidence(BaseModel):
    """Declared source for one propulsion test; hashes are not proof of accuracy."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)
    motor_part_number: str = Field(min_length=1)
    propeller_part_number: str = Field(min_length=1)
    test_voltage_v: float = Field(gt=0)
    source_kind: Literal["manufacturer_test", "bench_test"]
    source_reference: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_date: date
    curve_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PropulsionTestCase(BaseModel):
    """One published measurement: this motor, this propeller, at this voltage.

    A thrust curve belongs to the combination it was measured with, not to the
    motor alone (data/components/propulsion_tests.json).
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    motor_part_number: str = Field(min_length=1)
    propeller_part_number: str = Field(min_length=1)
    test_voltage_v: float = Field(gt=0)
    source_kind: Literal["manufacturer_test", "bench_test"]
    source_reference: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_date: date
    points: list[MotorThrustPoint] = Field(min_length=2)

    def declaration(self) -> PropulsionCurveEvidence:
        from pipelines.propulsion_evidence import thrust_curve_sha256

        return PropulsionCurveEvidence(
            motor_part_number=self.motor_part_number,
            propeller_part_number=self.propeller_part_number,
            test_voltage_v=self.test_voltage_v,
            source_kind=self.source_kind,
            source_reference=self.source_reference,
            source_sha256=self.source_sha256,
            source_date=self.source_date,
            curve_sha256=thrust_curve_sha256(self.points),
        )


class PropulsionEvidenceAssessment(BaseModel):
    status: Literal["missing", "mismatch", "matched_declaration"] = "missing"
    curve_sha256: str | None = None
    reasons: list[str] = Field(default_factory=list)
    source_reference: str | None = None
    source_sha256: str | None = None
    # This tool checks declarations, not source authenticity or flight readiness.
    source_independently_verified: Literal[False] = False


class MotorComponent(BaseModel):
    """Catalog motor specification with optional propulsion source declaration.

    Existing catalog curves lack measurement provenance. They remain useful for
    preliminary calculations, but are not independently verified test data.
    """

    model_config = ConfigDict(frozen=False)

    part_number: str = Field(..., description="Stable identifier, e.g. 'tmotor_f80_pro_1900kv'")
    manufacturer: str
    model_name: str
    kv: int = Field(..., gt=0, description="RPM per volt with no load")
    weight_g: float = Field(..., gt=0)
    max_continuous_current_a: float = Field(..., gt=0)
    max_burst_current_a: float = Field(..., gt=0)
    stator_diameter_mm: float = Field(..., gt=0)
    stator_height_mm: float = Field(..., gt=0)
    shaft_diameter_mm: float = Field(..., gt=0)
    recommended_voltage_cells_s: list[int] = Field(default_factory=list)
    thrust_curve: list[MotorThrustPoint] = Field(default_factory=list)
    thrust_curve_evidence: PropulsionCurveEvidence | None = None
    price_usd: float | None = None
    url: str | None = None

    def max_thrust_g(self) -> float:
        """Return the peak thrust value seen in the published curve."""
        return max((p.thrust_g for p in self.thrust_curve), default=0.0)

    def hover_operating_point(self, target_thrust_g: float) -> MotorThrustPoint | None:
        """Return the smallest curve point that meets the target thrust.

        This is the operating point we'd hold during hover for a single motor.
        """
        candidates = sorted(
            (p for p in self.thrust_curve if p.thrust_g >= target_thrust_g),
            key=lambda p: p.throttle_pct,
        )
        return candidates[0] if candidates else None


# ─────────────────────────────────────────────────────────────────────────────
# Propeller
# ─────────────────────────────────────────────────────────────────────────────


class PropellerComponent(BaseModel):
    part_number: str
    manufacturer: str
    model_name: str
    diameter_inch: float = Field(..., gt=0)
    pitch_inch: float = Field(..., gt=0)
    blades: int = Field(..., ge=2, le=6)
    material: Literal["polycarbonate", "carbon_fiber", "nylon", "wood"]
    weight_g: float = Field(..., gt=0)
    max_rpm_safe: int = Field(..., gt=0, description="Manufacturer's rated max RPM")
    hub_id_mm: float = Field(5.0, description="Hub inner diameter for shaft fit")
    price_usd: float | None = None
    url: str | None = None

    @property
    def disc_area_m2(self) -> float:
        """Swept disc area in square metres (π r²)."""
        radius_m = (self.diameter_inch * 0.0254) / 2.0
        return 3.141592653589793 * radius_m * radius_m


# ─────────────────────────────────────────────────────────────────────────────
# Battery
# ─────────────────────────────────────────────────────────────────────────────


class BatteryDischargePoint(BaseModel):
    """Voltage vs state-of-charge at a given C-rate."""

    state_of_charge_pct: float = Field(..., ge=0, le=100)
    voltage_per_cell_v: float = Field(..., gt=0)


class BatteryComponent(BaseModel):
    part_number: str
    manufacturer: str
    model_name: str
    chemistry: Literal["LiPo", "LiHV", "Li-ion", "LiFePO4"]
    cells_s: int = Field(..., ge=1, le=14)
    capacity_mah: int = Field(..., gt=0)
    weight_g: float = Field(..., gt=0)
    max_continuous_c: float = Field(..., gt=0, description="Continuous discharge in C")
    max_burst_c: float = Field(..., gt=0, description="Burst discharge in C, ~10s")
    internal_resistance_mohm_per_cell: float = Field(..., gt=0)
    voltage_nominal_per_cell_v: float = Field(3.7, gt=0)
    voltage_full_per_cell_v: float = Field(4.2, gt=0)
    voltage_empty_per_cell_v: float = Field(3.5, gt=0)
    discharge_curve: list[BatteryDischargePoint] = Field(default_factory=list)
    price_usd: float | None = None
    url: str | None = None

    @property
    def pack_voltage_nominal_v(self) -> float:
        return self.cells_s * self.voltage_nominal_per_cell_v

    @property
    def pack_voltage_full_v(self) -> float:
        return self.cells_s * self.voltage_full_per_cell_v

    @property
    def pack_voltage_empty_v(self) -> float:
        return self.cells_s * self.voltage_empty_per_cell_v

    @property
    def max_continuous_current_a(self) -> float:
        return self.max_continuous_c * (self.capacity_mah / 1000.0)

    @property
    def usable_energy_wh(self) -> float:
        """Conservative usable energy at nominal voltage from full to 20% reserve."""
        return (
            self.pack_voltage_nominal_v
            * (self.capacity_mah / 1000.0)
            * 0.8  # 80% usable to keep 20% RTL margin
        )


# ─────────────────────────────────────────────────────────────────────────────
# Electronic Speed Controller (ESC)
# ─────────────────────────────────────────────────────────────────────────────


class ESCComponent(BaseModel):
    part_number: str
    manufacturer: str
    model_name: str
    channels: int = Field(..., ge=1, le=8, description="Number of motors driven")
    max_continuous_current_per_channel_a: float = Field(..., gt=0)
    max_burst_current_per_channel_a: float = Field(..., gt=0)
    firmware: Literal["BLHeli32", "BLHeli_S", "AM32", "BLHeli_M"]
    min_cells_s: int = Field(..., ge=1)
    max_cells_s: int = Field(..., ge=1)
    weight_g: float = Field(..., gt=0)
    bec_voltage_v: float | None = Field(None, description="Built-in BEC output voltage if any")
    bec_current_a: float | None = None
    price_usd: float | None = None
    url: str | None = None

    @field_validator("max_cells_s")
    @classmethod
    def _check_cells(cls, v: int, info) -> int:
        if "min_cells_s" in info.data and v < info.data["min_cells_s"]:
            raise ValueError("max_cells_s must be >= min_cells_s")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# Flight Controller
# ─────────────────────────────────────────────────────────────────────────────


class FlightControllerComponent(BaseModel):
    part_number: str
    manufacturer: str
    model_name: str
    mcu: str = Field(..., description="STM32H743 / STM32F745 / etc")
    flash_mb: int = Field(..., gt=0)
    ram_kb: int = Field(..., gt=0)
    imu_models: list[str] = Field(default_factory=list, description="e.g. ['ICM42688P','BMI088']")
    baro_models: list[str] = Field(default_factory=list)
    mag_models: list[str] = Field(default_factory=list)
    supports_px4: bool = True
    supports_ardupilot: bool = False
    supports_betaflight: bool = False
    mavsdk_compatible: bool = True
    uart_count: int = Field(..., ge=1)
    can_count: int = Field(0, ge=0)
    weight_g: float = Field(..., gt=0)
    voltage_input_min_v: float = Field(..., gt=0)
    voltage_input_max_v: float = Field(..., gt=0)
    price_usd: float | None = None
    url: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Frame
# ─────────────────────────────────────────────────────────────────────────────


class FrameComponent(BaseModel):
    part_number: str
    manufacturer: str
    model_name: str
    geometry: Literal["X", "H", "Y", "V", "X8", "Plus"]
    wheelbase_mm: float = Field(..., gt=0, description="Diagonal motor-to-motor distance")
    arm_length_mm: float = Field(..., gt=0)
    arm_thickness_mm: float = Field(..., gt=0)
    material: Literal["carbon_fiber", "aluminum", "plastic", "3d_printed_petg"]
    weight_g: float = Field(..., gt=0)
    motor_mount_pattern_mm: tuple[float, float] = Field(
        (16.0, 16.0),
        description="Motor mount hole spacing (square) in mm",
    )
    fc_stack_size_mm: tuple[int, int] = Field(
        (30, 30),
        description="Mounting stack hole pattern (e.g. 30.5x30.5)",
    )
    max_prop_size_inch: float = Field(..., gt=0)
    # Motor arms on the frame; a design's motor count must equal it.
    arm_count: int = Field(4, ge=3, le=8)
    price_usd: float | None = None
    url: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Full selected BOM (output of ComponentSelectorAgent)
# ─────────────────────────────────────────────────────────────────────────────


class SelectedComponents(BaseModel):
    """Coherent BOM picked by the selector for a single design tree."""

    model_config = ConfigDict(frozen=False)

    motor: MotorComponent
    propeller: PropellerComponent
    battery: BatteryComponent
    esc: ESCComponent
    flight_controller: FlightControllerComponent
    frame: FrameComponent
    motor_count: int = Field(..., ge=3, le=12)

    @property
    def esc_quantity(self) -> int:
        """One board per motor for single-channel ESCs; otherwise one board."""
        if self.esc.channels not in (1, self.motor_count):
            raise ValueError("ESC channels must be 1 or match the motor count")
        return self.motor_count if self.esc.channels == 1 else 1

    @property
    def total_propulsion_weight_g(self) -> float:
        return (self.motor.weight_g + self.propeller.weight_g) * self.motor_count

    @property
    def total_bom_weight_g(self) -> float:
        return (
            self.total_propulsion_weight_g
            + self.battery.weight_g
            + self.esc.weight_g * self.esc_quantity
            + self.flight_controller.weight_g
            + self.frame.weight_g
        )

    @property
    def total_bom_price_usd(self) -> float:
        unit_prices = [
            (self.motor.price_usd or 0.0) * self.motor_count,
            (self.propeller.price_usd or 0.0) * self.motor_count,
            self.battery.price_usd or 0.0,
            (self.esc.price_usd or 0.0) * self.esc_quantity,
            self.flight_controller.price_usd or 0.0,
            self.frame.price_usd or 0.0,
        ]
        return round(sum(unit_prices), 2)


# ─────────────────────────────────────────────────────────────────────────────
# Aerodynamic / electrical performance — output of PerformanceSolver
# ─────────────────────────────────────────────────────────────────────────────


class RealPerformanceReport(BaseModel):
    """Preliminary performance; source declaration matching is reported separately."""

    estimate_scope: Literal["preliminary_catalog_estimate"] = "preliminary_catalog_estimate"
    propulsion_evidence: PropulsionEvidenceAssessment = Field(
        default_factory=PropulsionEvidenceAssessment
    )

    # Mass
    total_mass_g: float
    payload_capacity_g: float = Field(..., description="Remaining lift before T:W < 1.5")

    # Thrust budget
    max_total_thrust_g: float
    thrust_to_weight_ratio: float = Field(
        ...,
        description=">2.0 = agile; 1.5-2.0 = stable; <1.5 = struggling",
    )
    hover_throttle_pct: float
    hover_thrust_per_motor_g: float

    # Power
    hover_current_a: float
    hover_power_w: float
    battery_c_rate_at_hover: float

    # Endurance
    hover_endurance_min: float
    cruise_endurance_min: float
    max_range_km: float

    # Disc-loading & efficiency
    disc_loading_n_m2: float
    power_loading_g_per_w: float

    # Margins / warnings
    warnings: list[str] = Field(default_factory=list)
    voltage_sag_at_max_throttle_v: float
    motor_saturation_headroom_pct: float
    battery_c_rate_burst_utilization_pct: float

    @property
    def is_flyable(self) -> bool:
        """Legacy numerical feasibility only, not source or flight validation."""
        critical = [w for w in self.warnings if w.startswith("[critical]")]
        return self.thrust_to_weight_ratio >= 1.5 and not critical
