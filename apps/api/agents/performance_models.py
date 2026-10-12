"""eljazari.ai — Phase 4.1 Performance Monitor Models

Structured schemas for the hierarchical PerformanceMonitorAgent.
Reference: Hitachi ReActEval (arxiv:2510.00259)

Analysis types:
  - Performance Analysis: path, battery, motor response, GPS accuracy, flight stability
  - Failure Detection: anomaly detection, near-miss, failure cause
  - Recommendations: code / design / mission / constraints
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, computed_field


# ─── Input primitives ─────────────────────────────────────────────────────────


class WaypointPlan(BaseModel):
    """Planned waypoint from the mission specification."""

    index: int = 0
    north_m: float = 0.0
    east_m: float = 0.0
    alt_m: float = 10.0
    speed_ms: float = 5.0
    hover_time_s: float = 0.0


class TelemetryPoint(BaseModel):
    """Single telemetry sample from one simulation step."""

    timestamp_s: float = 0.0
    north_m: float = 0.0
    east_m: float = 0.0
    alt_m: float = 0.0
    speed_ms: float = 0.0
    heading_deg: float = 0.0
    battery_pct: float = 100.0
    battery_voltage: float = 0.0
    roll_deg: float = 0.0
    pitch_deg: float = 0.0
    yaw_deg: float = 0.0
    # Motor RPMs (optional)
    motor_rpms: list[float] = Field(default_factory=list)
    # Vibration (IMU)
    vibration_x: float = 0.0
    vibration_y: float = 0.0
    vibration_z: float = 0.0
    # GPS quality
    gps_fix: int = 3  # 0=no fix, 3=3D fix
    hdop: float = 1.0
    satellites: int = 12


class SimulationRun(BaseModel):
    """One complete simulation attempt."""

    run_id: str
    passed: bool = False
    duration_s: float = 0.0
    code_iteration: int = 1
    telemetry: list[TelemetryPoint] = Field(default_factory=list)
    waypoints: list[WaypointPlan] = Field(default_factory=list)
    safety_violations: list[str] = Field(default_factory=list)
    constraints_total: int = 10
    evaluation: dict[str, Any] = Field(default_factory=dict)


class FlightLogs(BaseModel):
    """Complete flight logs for a project — primary input to PerformanceMonitorAgent."""

    project_id: str
    mission_description: str = ""
    planned_waypoints: list[WaypointPlan] = Field(default_factory=list)
    simulation_runs: list[SimulationRun] = Field(default_factory=list)
    drone_config: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ─── Analysis planning ────────────────────────────────────────────────────────


class AnalysisType(str, Enum):
    TRAJECTORY = "trajectory"
    BATTERY = "battery"
    MOTORS = "motors"
    STABILITY = "stability"  # flight stability: attitude + vibration
    GPS = "gps"  # GPS/navigation accuracy
    ANOMALY = "anomaly"


class AnalysisTask(BaseModel):
    """A single analysis task dispatched to a worker agent."""

    task_id: str
    analysis_type: AnalysisType
    priority: int = Field(ge=1, le=5, default=3)
    description: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class AnalysisPlan(BaseModel):
    """Head agent's plan for the analysis session."""

    project_id: str
    tasks: list[AnalysisTask]
    rationale: str = ""
    estimated_score_drivers: list[str] = Field(default_factory=list)


# ─── Worker analysis results ──────────────────────────────────────────────────


class TrajectoryAnalysis(BaseModel):
    """Actual vs planned path comparison."""

    mean_cross_track_error_m: float = 0.0
    max_cross_track_error_m: float = 0.0
    dtw_distance: float = 0.0
    path_completion_pct: float = 0.0
    alt_rmse_m: float = 0.0
    max_alt_deviation_m: float = 0.0
    path_smoothness: float = 0.0  # 0-1
    avg_path_efficiency: float = 0.0
    waypoints_reached: int = 0
    total_waypoints: int = 0
    waypoint_accuracy_m: float = 0.0
    findings: list[str] = Field(default_factory=list)
    score: float = 0.0


class BatteryAnalysis(BaseModel):
    """Energy consumption and safety margin analysis."""

    avg_consumption_mah_km: float = 0.0
    total_energy_used_wh: float = 0.0
    hover_efficiency_coeff: float = 0.0
    payload_fraction_impact: float = 0.0
    avg_voltage_sag_v: float = 0.0
    min_voltage_v: float = 0.0
    rtl_reserve_pct: float = 0.0
    min_battery_pct: float = 0.0
    avg_battery_remaining_pct: float = 0.0
    discharge_linearity: float = 0.0
    findings: list[str] = Field(default_factory=list)
    score: float = 0.0


class MotorStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    NO_DATA = "no_data"


class MotorAnalysis(BaseModel):
    """Motor response, RPM balance, thermal load."""

    motor_statuses: list[MotorStatus] = Field(default_factory=list)
    avg_rpm_balance_pct: float = 0.0
    max_rpm_imbalance_pct: float = 0.0
    thermal_load_index: float = 0.0
    estimated_motor_temp_c: list[float] = Field(default_factory=list)
    # Motor response (how quickly RPMs respond to command changes)
    avg_response_latency_ms: float = 0.0
    max_response_latency_ms: float = 0.0
    findings: list[str] = Field(default_factory=list)
    score: float = 0.0


class FlightStabilityAnalysis(BaseModel):
    """Flight stability: attitude deviations and IMU vibration."""

    # Attitude
    avg_roll_deg: float = 0.0
    max_roll_deg: float = 0.0
    avg_pitch_deg: float = 0.0
    max_pitch_deg: float = 0.0
    attitude_rmse_deg: float = 0.0
    # Vibration (from IMU)
    avg_vibration_index: float = 0.0
    peak_vibration: float = 0.0
    vibration_grade: Literal["low", "medium", "high", "critical"] = "low"
    # Hover stability (position hold error at hover points)
    hover_position_err_m: float = 0.0
    findings: list[str] = Field(default_factory=list)
    score: float = 0.0


class GPSAccuracyAnalysis(BaseModel):
    """GPS/Navigation accuracy analysis."""

    avg_hdop: float = 0.0
    max_hdop: float = 0.0
    avg_satellites: float = 0.0
    min_satellites: int = 0
    gps_fix_loss_count: int = 0
    gps_loss_duration_s: float = 0.0
    avg_position_noise_m: float = 0.0  # estimated from hdop
    navigation_reliability: float = 0.0  # 0-1
    findings: list[str] = Field(default_factory=list)
    score: float = 0.0


class AnomalyType(str, Enum):
    ALTITUDE_DROP = "altitude_drop"
    BATTERY_SPIKE = "battery_spike"
    GPS_LOSS = "gps_loss"
    MOTOR_FAILURE = "motor_failure"
    WIND_DISTURBANCE = "wind_disturbance"
    SPEED_VIOLATION = "speed_violation"
    GEOFENCE_BREACH = "geofence_breach"
    NEAR_MISS = "near_miss"
    ATTITUDE_UPSET = "attitude_upset"


class AnomalyEvent(BaseModel):
    """A single detected anomaly event."""

    event_type: AnomalyType
    severity: Literal["critical", "high", "medium", "low"]
    timestamp_s: float
    run_id: str
    description: str
    value: float = 0.0
    threshold: float = 0.0
    # Failure cause (if determinable)
    probable_cause: str = ""


class AnomalyReport(BaseModel):
    """Anomaly detection + near-miss + failure cause analysis."""

    total_anomalies: int = 0
    critical_count: int = 0
    high_count: int = 0
    events: list[AnomalyEvent] = Field(default_factory=list)
    z_score_threshold: float = 3.0
    near_miss_count: int = 0
    gps_loss_duration_s: float = 0.0
    # Failure cause analysis
    identified_failure_causes: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    score: float = 0.0


# ─── ReActEval cycle ──────────────────────────────────────────────────────────


class ReActDecision(str, Enum):
    CONTINUE = "continue"  # run another iteration
    STOP = "stop"  # result is sufficient


class ReActEvalStep(BaseModel):
    """One step of the 4-phase ReActEval cycle."""

    step: int
    phase: Literal["plan", "reason", "act", "evaluate"]
    content: str
    tool_called: str = ""
    tool_result: dict[str, Any] = Field(default_factory=dict)
    score_contribution: float = 0.0
    decision: ReActDecision = ReActDecision.STOP


class TaskResult(BaseModel):
    """Complete result from executing one AnalysisTask."""

    task: AnalysisTask
    react_steps: list[ReActEvalStep] = Field(default_factory=list)
    analysis_output: dict[str, Any] = Field(default_factory=dict)
    task_score: float = 0.0
    iterations: int = 1  # how many react cycles ran
    completed: bool = False
    error: str = ""


# ─── Recommendations ──────────────────────────────────────────────────────────


class RecommendationTarget(str, Enum):
    CODE = "code"
    DESIGN = "design"
    MISSION = "mission"
    CONSTRAINTS = "constraints"
    HARDWARE = "hardware"


class Recommendation(BaseModel):
    """Actionable improvement recommendation."""

    target: RecommendationTarget
    priority: Literal["critical", "high", "medium", "low"] = "medium"
    title: str
    description: str
    expected_improvement: str = ""
    code_snippet: str = ""


# ─── Composite score ──────────────────────────────────────────────────────────


class PerformanceScore(BaseModel):
    """Hierarchical score breakdown (0-100 per dimension)."""

    trajectory: float = 0.0
    battery: float = 0.0
    motors: float = 0.0
    stability: float = 0.0
    gps: float = 0.0
    anomaly: float = 0.0
    overall: float = 0.0
    grade: Literal["A", "B", "C", "D", "F"] = "F"


# ─── Full report ──────────────────────────────────────────────────────────────


class FullMonitorReport(BaseModel):
    """Complete Phase 4 performance monitoring output."""

    project_id: str
    mission_description: str = ""

    # Sub-analyses
    trajectory: Optional[TrajectoryAnalysis] = None
    battery: Optional[BatteryAnalysis] = None
    motors: Optional[MotorAnalysis] = None
    stability: Optional[FlightStabilityAnalysis] = None
    gps: Optional[GPSAccuracyAnalysis] = None
    anomalies: Optional[AnomalyReport] = None

    # Planning + execution trace
    analysis_plan: Optional[AnalysisPlan] = None
    task_results: list[TaskResult] = Field(default_factory=list)

    # Scores + recommendations
    scores: PerformanceScore = Field(default_factory=PerformanceScore)
    recommendations: list[Recommendation] = Field(default_factory=list)
    executive_summary: str = ""

    # Pre-computed visualization data
    viz_data: dict[str, Any] = Field(default_factory=dict)

    # Metadata
    analysis_method: str = "PerformanceMonitorAgent v4.1 (ReActEval Hierarchical)"
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @computed_field
    @property
    def passed(self) -> bool:
        return self.scores.overall >= 60.0

    @computed_field
    @property
    def critical_anomalies(self) -> list[AnomalyEvent]:
        if not self.anomalies:
            return []
        return [e for e in self.anomalies.events if e.severity == "critical"]

    @computed_field
    @property
    def critical_recommendations(self) -> list[Recommendation]:
        return [r for r in self.recommendations if r.priority == "critical"]


# Backwards-compat alias
MonitorReport = FullMonitorReport
