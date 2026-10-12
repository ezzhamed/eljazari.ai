"""eljazari.ai — Safety Layer Pydantic Models (Prompt 2.2)

All data contracts shared between SafeLLMSupervisionLayer,
the LangGraph nodes, and the REST API.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def weight(self) -> int:
        return {"low": 1, "medium": 2, "high": 3, "critical": 4}[self.value]


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ─────────────────────────────────────────────────────────────────────────────
# Flight constraints (injected per-mission)
# ─────────────────────────────────────────────────────────────────────────────


class FlightConstraints(BaseModel):
    """Physical and regulatory limits for a specific mission."""

    # Altitude (metres AGL)
    max_altitude_m: float = Field(default=120.0, gt=0)
    min_altitude_m: float = Field(default=2.0, ge=0)

    # Speed
    max_speed_ms: float = Field(default=15.0, gt=0)

    # Geofence
    max_geofence_radius_m: float = Field(default=500.0, gt=0)

    # Battery thresholds (percentage 0–100)
    min_battery_pct: float = Field(default=20.0, ge=0, le=100)
    battery_rtl_margin_pct: float = Field(default=30.0, ge=0, le=100)

    # Home position (used for geofence + RTL distance calc)
    home_lat: float = Field(default=24.7136)
    home_lon: float = Field(default=46.6753)
    home_alt_m: float = Field(default=0.0)

    # Consumption estimate (% per metre of flight)
    battery_consumption_per_m: float = Field(default=0.005)  # 0.5% per 100 m

    @field_validator("min_altitude_m")
    @classmethod
    def min_below_max(cls, v: float, info: Any) -> float:
        data = info.data
        if "max_altitude_m" in data and v >= data["max_altitude_m"]:
            raise ValueError("min_altitude_m must be less than max_altitude_m")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# Waypoint
# ─────────────────────────────────────────────────────────────────────────────


class Waypoint(BaseModel):
    """NED-frame waypoint extracted from generated code."""

    x: float = 0.0  # North (m from home)
    y: float = 0.0  # East  (m from home)
    z: float = 0.0  # Down  (negative = above home)
    speed_ms: float = 5.0  # commanded speed to this waypoint
    label: str = ""  # optional annotation

    @property
    def altitude_agl(self) -> float:
        """Altitude above ground level (positive up)."""
        return -self.z

    @property
    def horizontal_distance_from_home(self) -> float:
        """2D Euclidean distance from origin (home)."""
        return (self.x**2 + self.y**2) ** 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Per-issue schema (shared across all sub-reports)
# ─────────────────────────────────────────────────────────────────────────────


class SafetyIssue(BaseModel):
    rule_id: str
    severity: Severity
    description: str
    line_number: int | None = None
    suggestion: str
    auto_fixable: bool = False  # True if injection engine can resolve this


# ─────────────────────────────────────────────────────────────────────────────
# Sub-report: Static AST analysis
# ─────────────────────────────────────────────────────────────────────────────


class StaticAnalysisReport(BaseModel):
    passed: bool
    issues: list[SafetyIssue] = Field(default_factory=list)
    unapproved_functions: list[str] = Field(default_factory=list)
    altitude_violations: list[dict[str, Any]] = Field(default_factory=list)
    speed_violations: list[dict[str, Any]] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Sub-report: Waypoint reachability
# ─────────────────────────────────────────────────────────────────────────────


class WaypointSafetyReport(BaseModel):
    passed: bool
    waypoints_checked: int = 0
    issues: list[SafetyIssue] = Field(default_factory=list)
    total_distance_m: float = 0.0
    estimated_battery_usage_pct: float = 0.0
    battery_return_feasible: bool = True
    geofence_violations: list[dict[str, Any]] = Field(default_factory=list)
    altitude_violations: list[dict[str, Any]] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Sub-report: RAG compliance
# ─────────────────────────────────────────────────────────────────────────────


class ComplianceViolation(BaseModel):
    rule_id: str
    rule_name: str
    description: str
    severity: Severity
    matched_pattern: str
    line_number: int | None = None
    suggestion: str


class ComplianceReport(BaseModel):
    passed: bool
    rules_checked: int = 0
    violations: list[ComplianceViolation] = Field(default_factory=list)
    warnings: list[ComplianceViolation] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Sub-report: Injection engine
# ─────────────────────────────────────────────────────────────────────────────


class InjectionReport(BaseModel):
    protocols_injected: list[str] = Field(default_factory=list)
    modified_code: str = ""
    injection_points: list[dict[str, Any]] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Top-level: Full safety report (all 3 pillars combined)
# ─────────────────────────────────────────────────────────────────────────────


class FullSafetyReport(BaseModel):
    """Unified output of SafeLLMSupervisionLayer.full_safety_check()."""

    passed: bool
    risk_level: RiskLevel

    # Sub-reports
    static_report: StaticAnalysisReport
    waypoint_report: WaypointSafetyReport
    compliance_report: ComplianceReport
    injection_report: InjectionReport

    # Consolidated view
    all_issues: list[SafetyIssue] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    final_code: str = ""  # code with all injections applied

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.all_issues if i.severity == Severity.CRITICAL)

    @property
    def high_count(self) -> int:
        return sum(1 for i in self.all_issues if i.severity == Severity.HIGH)


# ─────────────────────────────────────────────────────────────────────────────
# Backward-compat alias (used by graph/nodes.py via SafetyLayer.analyze())
# ─────────────────────────────────────────────────────────────────────────────


class SafetyReport(BaseModel):
    """Simplified report — kept for graph/nodes.py compatibility."""

    passed: bool
    risk_level: str
    issues: list[SafetyIssue]
    injected_protocols: list[str]
    modified_code: str | None
    recommendations: list[str]
