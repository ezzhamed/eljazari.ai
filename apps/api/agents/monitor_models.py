"""eljazari.ai — Monitor Agent Pydantic Models

Structured schemas for the ReActEval performance evaluation pipeline.
Reference: Hitachi ReActEval (arxiv:2510.00259)
"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, computed_field


# ─────────────────────────────────────────────────────────────
# ReAct loop building blocks
# ─────────────────────────────────────────────────────────────


class ReActStep(BaseModel):
    """A single Thought → Action → Observation step in the ReAct evaluation loop."""

    step: int = Field(description="Step index starting at 1")
    thought: str = Field(description="LLM reasoning about the current analysis state")
    action: str = Field(description="Name of the evaluation tool called")
    action_input: dict = Field(default_factory=dict, description="Arguments passed to the tool")
    observation: str = Field(description="Result / finding returned by the tool")
    partial_score: float = Field(
        0.0, ge=0.0, le=100.0, description="Score contribution from this step"
    )


# ─────────────────────────────────────────────────────────────
# Mission metrics
# ─────────────────────────────────────────────────────────────


class MissionMetrics(BaseModel):
    """Quantitative performance metrics derived from simulation telemetry."""

    # Core success indicators
    mission_success_rate: float = Field(
        0.0, ge=0.0, le=1.0, description="Fraction of passed simulations"
    )
    simulation_iterations: int = Field(1, ge=0, description="Total simulation attempts")
    code_iterations_needed: int = Field(1, ge=1, description="Code refinement iterations")

    # Path quality
    path_efficiency: float = Field(
        0.0, ge=0.0, le=1.0, description="Ratio of optimal/actual path length (1=perfect)"
    )
    total_distance_m: float = Field(0.0, ge=0.0, description="Total path length in metres")
    waypoint_accuracy_m: float = Field(0.0, ge=0.0, description="Mean position error at waypoints")
    coverage_pattern_rate: float = Field(
        0.0, ge=0.0, le=1.0, description="Fraction of runs that look like survey/coverage paths"
    )
    closed_loop_rate: float = Field(
        0.0, ge=0.0, le=1.0, description="Fraction of runs that returned near their start point"
    )
    return_to_home_error_m: float = Field(
        0.0, ge=0.0, description="Mean horizontal start/end separation in metres"
    )
    north_span_m: float = Field(0.0, ge=0.0, description="Mean north/south coverage span")
    east_span_m: float = Field(0.0, ge=0.0, description="Mean east/west coverage span")

    # Energy
    battery_efficiency: float = Field(
        0.0, ge=0.0, le=1.0, description="1 - wasted_capacity / total_capacity"
    )
    battery_remaining_pct: float = Field(
        0.0, ge=0.0, le=100.0, description="Battery % at mission end"
    )

    # Safety & compliance
    safety_score: float = Field(
        0.0, ge=0.0, le=1.0, description="1 - violations / max_tolerated_violations"
    )
    constraint_compliance: float = Field(
        0.0, ge=0.0, le=1.0, description="Fraction of constraints respected throughout"
    )
    safety_violations_count: int = Field(0, ge=0)

    # Timing
    avg_flight_duration_s: float = Field(0.0, ge=0.0)
    avg_speed_ms: float = Field(0.0, ge=0.0)
    max_altitude_m: float = Field(0.0, ge=0.0)
    mission_completed_rate: float = Field(0.0, ge=0.0, le=1.0)
    telemetry_real_rate: float = Field(0.0, ge=0.0, le=1.0)
    avg_telemetry_points: float = Field(0.0, ge=0.0)
    mission_started: bool = Field(True)
    telemetry_points: int = Field(0, ge=0)
    mission_error_count: int = Field(0, ge=0)
    warning_count: int = Field(0, ge=0)

    # Semantic quality
    avg_semantic_score: float = Field(
        0.0, ge=0.0, le=100.0, description="Mean LLM evaluation score across iterations"
    )

    # ── Phase 11: Production-grade Control Engineering metrics ────────────
    position_rmse_m: float = Field(0.0, ge=0.0)
    altitude_rmse_m: float = Field(0.0, ge=0.0)
    cross_track_error_max_m: float = Field(0.0, ge=0.0)
    cross_track_error_mean_m: float = Field(0.0, ge=0.0)
    heading_rmse_deg: float = Field(0.0, ge=0.0)

    roll_oscillation_freq_hz: float = Field(0.0, ge=0.0)
    pitch_oscillation_freq_hz: float = Field(0.0, ge=0.0)
    roll_oscillation_power: float = Field(0.0, ge=0.0)
    pitch_oscillation_power: float = Field(0.0, ge=0.0)
    attitude_rms_deg: float = Field(0.0, ge=0.0)

    motor_saturation_percent_peak: float = Field(0.0, ge=0.0)
    motor_saturation_percent_mean: float = Field(0.0, ge=0.0)

    hover_drift_m_per_min: float = Field(0.0, ge=0.0)
    flight_quality_rth_error_m: float = Field(0.0, ge=0.0)
    setpoint_tracking_grade: str = Field("F")
    stability_grade: str = Field("F")
    flight_quality_score: float = Field(0.0, ge=0.0, le=100.0)
    flight_quality_grade: str = Field("F")

    @computed_field
    @property
    def composite_efficiency(self) -> float:
        """Weighted average of path + battery + safety efficiency."""
        return round(
            self.path_efficiency * 0.4 + self.battery_efficiency * 0.3 + self.safety_score * 0.3,
            3,
        )


# ─────────────────────────────────────────────────────────────
# Issue & recommendation records
# ─────────────────────────────────────────────────────────────

IssueCategory = Literal["safety", "battery", "path", "code", "timing", "compliance"]
IssueSeverity = Literal["critical", "high", "medium", "low"]


class IssueRecord(BaseModel):
    category: IssueCategory
    severity: IssueSeverity
    description: str
    suggestion: str = ""


# ─────────────────────────────────────────────────────────────
# Full ReActEval report
# ─────────────────────────────────────────────────────────────

Grade = Literal["A", "B", "C", "D", "F", "INDETERMINATE"]

_GRADE_MAP: list[tuple[float, Grade]] = [
    (90, "A"),
    (75, "B"),
    (60, "C"),
    (45, "D"),
]


def score_to_grade(score: float | None) -> Grade:
    if score is None:
        return "INDETERMINATE"
    for threshold, grade in _GRADE_MAP:
        if score >= threshold:
            return grade
    return "F"


GRADE_COLORS: dict[Grade, str] = {
    "A": "#22c55e",  # green
    "B": "#84cc16",  # lime
    "C": "#eab308",  # yellow
    "D": "#f97316",  # orange
    "F": "#ef4444",  # red
    "INDETERMINATE": "#94a3b8",  # slate
}

GRADE_LABELS_AR: dict[Grade, str] = {
    "A": "Excellent",
    "B": "Very good",
    "C": "Good",
    "D": "Acceptable",
    "F": "Fail",
    "INDETERMINATE": "Not evaluable",
}


class ReActEvalReport(BaseModel):
    """Complete ReActEval output — the canonical Monitor Agent result."""

    overall_score: float | None = Field(default=None, ge=0.0, le=100.0)
    grade: Grade
    metrics: MissionMetrics
    react_chain: list[ReActStep] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    issues: list[IssueRecord] = Field(default_factory=list)
    mission_summary: str = Field(default="")
    grade_label_override_ar: str = Field(default="", exclude=True)
    evaluation_method: str = Field(default="ReActEval (Hitachi — arxiv:2510.00259)")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @computed_field
    @property
    def grade_color(self) -> str:
        return GRADE_COLORS.get(self.grade, "#888")

    @computed_field
    @property
    def grade_label_ar(self) -> str:
        if self.grade_label_override_ar:
            return self.grade_label_override_ar
        return GRADE_LABELS_AR.get(self.grade, "")

    @computed_field
    @property
    def critical_issues(self) -> list[IssueRecord]:
        return [i for i in self.issues if i.severity == "critical"]

    @computed_field
    @property
    def passed(self) -> bool:
        return self.overall_score is not None and self.overall_score >= 60.0
