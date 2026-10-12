"""eljazari.ai — Pydantic models for Design Agent outputs.

Defines the structured schema for drone design trees and performance estimates.
These are the canonical types shared by DesignAgent, JsonToUrdfPipeline,
the LangGraph state, and the REST API.
"""

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────
# Frame
# ─────────────────────────────────────────────────────────────


class CenterBody(BaseModel):
    length: float = Field(180.0, description="Body length in mm")
    width: float = Field(120.0, description="Body width in mm")
    height: float = Field(45.0, description="Body height in mm")


class FrameSpec(BaseModel):
    type: str = Field("X", description="Frame geometry: X | H | Y | V | X8")
    arm_length_mm: float = Field(250.0, description="Arm length from centre to motor in mm")
    center_body: CenterBody = Field(default_factory=CenterBody)


# ─────────────────────────────────────────────────────────────
# Propulsion
# ─────────────────────────────────────────────────────────────


class MotorSpec(BaseModel):
    motor: str = Field("2206-2300KV", description="Motor model/spec string, e.g. '2206-2300KV'")
    propeller: str = Field("5045", description="Propeller spec, e.g. '5045' (5-inch, 4.5-pitch)")
    position: str = Field(
        "front_left",
        description="Mount position: front_left | front_right | rear_left | rear_right | ...",
    )


# ─────────────────────────────────────────────────────────────
# Battery
# ─────────────────────────────────────────────────────────────


class BatterySpec(BaseModel):
    type: str = Field("LiPo", description="Battery chemistry: LiPo | Li-Ion | LiFe")
    cells: int = Field(4, ge=1, le=12, description="Number of cells (S rating)")
    capacity_mah: int = Field(5000, gt=0, description="Capacity in mAh")
    weight_g: float = Field(480.0, gt=0, description="Battery weight in grams")


# ─────────────────────────────────────────────────────────────
# Payload & Electronics
# ─────────────────────────────────────────────────────────────


class PayloadSpec(BaseModel):
    max_weight_g: float = Field(500.0, ge=0, description="Maximum payload in grams")
    installed_weight_g: float | None = Field(
        None, ge=0, description="Actual installed payload weight in grams"
    )
    mount_type: str = Field("bottom", description="Payload mount: bottom | top | front | gimbal")

    @property
    def design_load_g(self) -> float:
        """Use actual payload when known; otherwise assess the full requested load."""
        return self.max_weight_g if self.installed_weight_g is None else self.installed_weight_g


class ElectronicsSpec(BaseModel):
    flight_controller: str = Field("PX4", description="FC firmware: PX4 | Betaflight | ArduPilot")
    gps: bool = Field(True, description="GPS module included")
    compass: bool = Field(True, description="Magnetometer/compass included")


# ─────────────────────────────────────────────────────────────
# Top-level Design Tree
# ─────────────────────────────────────────────────────────────


class DroneDesignTree(BaseModel):
    """Complete structural description of a UAV design.

    This is the canonical output of the DesignAgent and the primary input
    to the JsonToUrdfPipeline.
    """

    topology: str = Field(
        "quadcopter",
        description="UAV type: quadcopter | hexacopter | octocopter | tricopter | bicopter",
    )
    frame: FrameSpec = Field(default_factory=FrameSpec)
    propulsion: list[MotorSpec] = Field(
        default_factory=list,
        description="One entry per motor; list length must match topology",
    )
    battery: BatterySpec = Field(default_factory=BatterySpec)
    payload: PayloadSpec = Field(default_factory=PayloadSpec)
    electronics: ElectronicsSpec = Field(default_factory=ElectronicsSpec)


# ─────────────────────────────────────────────────────────────
# Performance Estimate
# ─────────────────────────────────────────────────────────────


class PerformanceEstimate(BaseModel):
    """Physics-based performance envelope derived from the design tree."""

    hover_time_s: float = Field(description="Estimated hover time in seconds")
    max_speed_ms: float = Field(description="Estimated maximum horizontal speed in m/s")
    max_distance_m: float = Field(description="Estimated maximum one-way range in metres")
    total_weight_g: float = Field(description="Estimated all-up weight in grams")
    thrust_to_weight_ratio: float = Field(description="Maximum thrust ÷ weight (≥2 for safe hover)")
    racing_time_s: float | None = Field(None, description="Estimated racing flight time in seconds")
    aerodynamics: dict[str, float | str] | None = Field(
        None, description="Detailed aerodynamic estimation parameters"
    )


# ─────────────────────────────────────────────────────────────
# Agent Result
# ─────────────────────────────────────────────────────────────


class DesignResult(BaseModel):
    """Complete output from a single DesignAgent.run() call."""

    design_tree: DroneDesignTree
    performance_estimate: PerformanceEstimate
    similar_designs_count: int = Field(
        0, description="Number of AircraftVerse designs retrieved via RAG"
    )
    generation_method: str = Field("rag_llm", description="'rag_llm' | 'llm_only' | 'rule_based'")
    repair_attempts: int = Field(
        0, ge=0, description="Number of deterministic design repair passes applied"
    )
    diagnostics: dict[str, float | str] = Field(
        default_factory=dict,
        description="Timing diagnostics for design generation stages "
        "(floats) plus optional string notes such as "
        "fallback_reason.",
    )
    raw_context: list[dict] = Field(
        default_factory=list,
        description="Top-3 RAG context designs stored for traceability",
    )
