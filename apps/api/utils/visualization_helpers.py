"""eljazari.ai — Phase 4.1 Visualization Helpers

Pure functions converting typed analysis objects into JSON-serialisable
chart/map data for the frontend (Recharts / Three.js / SVG).

Covers all 6 analysis dimensions:
  trajectory, battery, motors, stability, gps, anomaly
"""

from typing import Any, Optional

from agents.performance_models import (
    AnomalyReport,
    BatteryAnalysis,
    FlightStabilityAnalysis,
    GPSAccuracyAnalysis,
    MotorAnalysis,
    PerformanceScore,
    TrajectoryAnalysis,
)


# ── Entry point ────────────────────────────────────────────────────────────────


def generate_all_viz_data(
    *,
    trajectory: Optional[TrajectoryAnalysis] = None,
    battery: Optional[BatteryAnalysis] = None,
    motors: Optional[MotorAnalysis] = None,
    stability: Optional[FlightStabilityAnalysis] = None,
    gps: Optional[GPSAccuracyAnalysis] = None,
    anomalies: Optional[AnomalyReport] = None,
    scores: PerformanceScore,
) -> dict[str, Any]:
    """Generate all visualization payloads for the report page."""
    return {
        "score_breakdown": generate_score_breakdown(scores),
        "score_gauge": generate_score_gauge(scores.overall, scores.grade),
        "trajectory_plot": generate_trajectory_plot(trajectory),
        "battery_chart": generate_battery_chart(battery),
        "motor_radar": generate_motor_radar(motors),
        "stability_chart": generate_stability_chart(stability),
        "gps_chart": generate_gps_chart(gps),
        "anomaly_timeline": generate_anomaly_timeline(anomalies),
    }


# ── Score breakdown ────────────────────────────────────────────────────────────


def generate_score_breakdown(scores: PerformanceScore) -> dict[str, Any]:
    """RadarChart (6-axis) + grouped BarChart with dimension weights."""
    radar_data = [
        {"subject": "Trajectory", "score": scores.trajectory, "fullMark": 100},
        {"subject": "Battery", "score": scores.battery, "fullMark": 100},
        {"subject": "Stability", "score": scores.stability, "fullMark": 100},
        {"subject": "GPS", "score": scores.gps, "fullMark": 100},
        {"subject": "Anomaly", "score": scores.anomaly, "fullMark": 100},
        {"subject": "Motors", "score": scores.motors, "fullMark": 100},
    ]
    bar_data = [
        {"name": "Trajectory", "score": scores.trajectory, "weight": 25, "fill": "#60a5fa"},
        {"name": "Battery", "score": scores.battery, "weight": 20, "fill": "#34d399"},
        {"name": "Stability", "score": scores.stability, "weight": 20, "fill": "#f59e0b"},
        {"name": "Anomaly", "score": scores.anomaly, "weight": 20, "fill": "#f87171"},
        {"name": "GPS", "score": scores.gps, "weight": 10, "fill": "#a78bfa"},
        {"name": "Motors", "score": scores.motors, "weight": 5, "fill": "#38bdf8"},
    ]
    return {
        "radar": radar_data,
        "bar": bar_data,
        "overall": scores.overall,
        "grade": scores.grade,
    }


# ── Score gauge ────────────────────────────────────────────────────────────────


def generate_score_gauge(score: float, grade: str) -> dict[str, Any]:
    """Circular gauge arc + grade label."""
    _colors = {
        "A": "#22c55e",
        "B": "#84cc16",
        "C": "#eab308",
        "D": "#f97316",
        "F": "#ef4444",
    }
    _labels_ar = {
        "A": "Excellent",
        "B": "Very good",
        "C": "Good",
        "D": "Acceptable",
        "F": "Fail",
    }
    return {
        "score": score,
        "grade": grade,
        "grade_color": _colors.get(grade, "#888"),
        "grade_label_ar": _labels_ar.get(grade, ""),
        "arc_deg": score / 100 * 360,
        "passed": score >= 60.0,
        "thresholds": [
            {"score": 90, "grade": "A", "label": "Excellent"},
            {"score": 75, "grade": "B", "label": "Very good"},
            {"score": 60, "grade": "C", "label": "Good"},
            {"score": 45, "grade": "D", "label": "Acceptable"},
        ],
    }


# ── Trajectory ─────────────────────────────────────────────────────────────────


def generate_trajectory_plot(
    analysis: Optional[TrajectoryAnalysis],
) -> dict[str, Any]:
    if not analysis:
        return {"available": False}

    def _st(v: float, thr: float, higher_ok: bool = False) -> str:
        ok = v >= thr if higher_ok else v <= thr
        return "ok" if ok else "warn"

    return {
        "available": True,
        "metrics": [
            {
                "name": "Mean CTE (m)",
                "value": analysis.mean_cross_track_error_m,
                "threshold": 2.0,
                "status": _st(analysis.mean_cross_track_error_m, 2.0),
            },
            {
                "name": "Max CTE (m)",
                "value": analysis.max_cross_track_error_m,
                "threshold": 5.0,
                "status": _st(analysis.max_cross_track_error_m, 5.0),
            },
            {
                "name": "Path Efficiency %",
                "value": round(analysis.avg_path_efficiency * 100, 1),
                "threshold": 80.0,
                "status": _st(analysis.avg_path_efficiency * 100, 80.0, higher_ok=True),
            },
            {
                "name": "Waypoint Accuracy (m)",
                "value": analysis.waypoint_accuracy_m,
                "threshold": 3.0,
                "status": _st(analysis.waypoint_accuracy_m, 3.0),
            },
            {
                "name": "Altitude RMSE (m)",
                "value": analysis.alt_rmse_m,
                "threshold": 2.0,
                "status": _st(analysis.alt_rmse_m, 2.0),
            },
            {
                "name": "Path Smoothness %",
                "value": round(analysis.path_smoothness * 100, 1),
                "threshold": 70.0,
                "status": _st(analysis.path_smoothness * 100, 70.0, higher_ok=True),
            },
        ],
        "waypoints_reached": analysis.waypoints_reached,
        "total_waypoints": analysis.total_waypoints,
        "completion_pct": analysis.path_completion_pct,
        "score": analysis.score,
        "findings": analysis.findings,
    }


# ── Battery ────────────────────────────────────────────────────────────────────


def generate_battery_chart(
    analysis: Optional[BatteryAnalysis],
) -> dict[str, Any]:
    if not analysis:
        return {"available": False}

    # Synthetic 10-point discharge curve
    s, e = 100.0, analysis.avg_battery_remaining_pct
    curve = [
        {"t": i * 10, "battery_pct": round(s - (s - e) * i / 9, 1), "rtl_threshold": 25.0}
        for i in range(10)
    ]

    def _st(v: float, thr: float, ok_above: bool = False) -> str:
        ok = v >= thr if ok_above else v <= thr
        return "ok" if ok else "critical" if (not ok_above and v < thr * 0.5) else "warn"

    return {
        "available": True,
        "discharge_curve": curve,
        "metrics": [
            {
                "name": "Avg Remaining %",
                "value": analysis.avg_battery_remaining_pct,
                "threshold": 25.0,
                "status": _st(analysis.avg_battery_remaining_pct, 25.0, ok_above=True),
            },
            {
                "name": "Energy Used (Wh)",
                "value": analysis.total_energy_used_wh,
                "threshold": None,
                "status": "ok",
            },
            {
                "name": "mAh / km",
                "value": analysis.avg_consumption_mah_km,
                "threshold": 1500.0,
                "status": _st(analysis.avg_consumption_mah_km, 1500.0),
            },
            {
                "name": "Voltage Sag (V)",
                "value": analysis.avg_voltage_sag_v,
                "threshold": 1.5,
                "status": _st(analysis.avg_voltage_sag_v, 1.5),
            },
            {
                "name": "Discharge Linearity %",
                "value": round(analysis.discharge_linearity * 100, 1),
                "threshold": 70.0,
                "status": _st(analysis.discharge_linearity * 100, 70.0, ok_above=True),
            },
        ],
        "rtl_reserve_pct": analysis.rtl_reserve_pct,
        "score": analysis.score,
        "findings": analysis.findings,
    }


# ── Motor radar ────────────────────────────────────────────────────────────────


def generate_motor_radar(
    analysis: Optional[MotorAnalysis],
) -> dict[str, Any]:
    if not analysis:
        return {"available": False}

    hmap = {"healthy": 100, "degraded": 60, "failed": 20, "no_data": 50}
    motor_bars = [
        {
            "motor": f"M{i + 1}",
            "status": s.value,
            "health": hmap.get(s.value, 50),
            "temp": analysis.estimated_motor_temp_c[i]
            if i < len(analysis.estimated_motor_temp_c)
            else None,
        }
        for i, s in enumerate(analysis.motor_statuses)
    ]
    radar_data = [
        {"subject": "RPM Balance", "score": max(0.0, 100.0 - analysis.avg_rpm_balance_pct * 5.0)},
        {"subject": "Thermal Load", "score": max(0.0, 100.0 - analysis.thermal_load_index * 100.0)},
        {
            "subject": "Motor Response",
            "score": max(0.0, 100.0 - analysis.avg_response_latency_ms * 0.3),
        },
    ]
    return {
        "available": True,
        "radar": radar_data,
        "motor_bars": motor_bars,
        "avg_response_ms": analysis.avg_response_latency_ms,
        "score": analysis.score,
        "findings": analysis.findings,
    }


# ── Stability chart ────────────────────────────────────────────────────────────


def generate_stability_chart(
    analysis: Optional[FlightStabilityAnalysis],
) -> dict[str, Any]:
    """Attitude + vibration gauges for flight stability panel."""
    if not analysis:
        return {"available": False}

    _vib_colors = {
        "low": "#22c55e",
        "medium": "#eab308",
        "high": "#f97316",
        "critical": "#ef4444",
    }

    return {
        "available": True,
        "attitude": [
            {
                "name": "Avg Roll (°)",
                "value": analysis.avg_roll_deg,
                "max_value": analysis.max_roll_deg,
                "threshold": 10.0,
                "status": "ok" if analysis.avg_roll_deg <= 10 else "warn",
            },
            {
                "name": "Avg Pitch (°)",
                "value": analysis.avg_pitch_deg,
                "max_value": analysis.max_pitch_deg,
                "threshold": 10.0,
                "status": "ok" if analysis.avg_pitch_deg <= 10 else "warn",
            },
            {
                "name": "Attitude RMSE (°)",
                "value": analysis.attitude_rmse_deg,
                "threshold": 5.0,
                "status": "ok" if analysis.attitude_rmse_deg <= 5 else "warn",
            },
        ],
        "vibration": {
            "avg": analysis.avg_vibration_index,
            "peak": analysis.peak_vibration,
            "grade": analysis.vibration_grade,
            "color": _vib_colors.get(analysis.vibration_grade, "#888"),
        },
        "hover_position_err_m": analysis.hover_position_err_m,
        "score": analysis.score,
        "findings": analysis.findings,
    }


# ── GPS chart ──────────────────────────────────────────────────────────────────


def generate_gps_chart(
    analysis: Optional[GPSAccuracyAnalysis],
) -> dict[str, Any]:
    """HDOP, satellite count, fix reliability panel."""
    if not analysis:
        return {"available": False}

    def _st(v: float, thr: float, ok_above: bool = False) -> str:
        ok = v >= thr if ok_above else v <= thr
        return "ok" if ok else "warn"

    return {
        "available": True,
        "metrics": [
            {
                "name": "Avg HDOP",
                "value": analysis.avg_hdop,
                "threshold": 1.5,
                "status": _st(analysis.avg_hdop, 1.5),
            },
            {
                "name": "Max HDOP",
                "value": analysis.max_hdop,
                "threshold": 2.5,
                "status": _st(analysis.max_hdop, 2.5),
            },
            {
                "name": "Avg Satellites",
                "value": analysis.avg_satellites,
                "threshold": 8.0,
                "status": _st(analysis.avg_satellites, 8.0, ok_above=True),
            },
            {
                "name": "Min Satellites",
                "value": analysis.min_satellites,
                "threshold": 6,
                "status": _st(analysis.min_satellites, 6, ok_above=True),
            },
            {
                "name": "Position Noise (m)",
                "value": analysis.avg_position_noise_m,
                "threshold": 3.0,
                "status": _st(analysis.avg_position_noise_m, 3.0),
            },
            {
                "name": "Nav Reliability %",
                "value": round(analysis.navigation_reliability * 100, 1),
                "threshold": 90.0,
                "status": _st(analysis.navigation_reliability * 100, 90.0, ok_above=True),
            },
        ],
        "fix_loss_events": analysis.gps_fix_loss_count,
        "fix_loss_s": analysis.gps_loss_duration_s,
        "score": analysis.score,
        "findings": analysis.findings,
    }


# ── Anomaly timeline ───────────────────────────────────────────────────────────


def generate_anomaly_timeline(
    analysis: Optional[AnomalyReport],
) -> dict[str, Any]:
    if not analysis:
        return {"available": False}

    _sev_colors = {
        "critical": "#ef4444",
        "high": "#f97316",
        "medium": "#eab308",
        "low": "#22c55e",
    }
    _type_icons = {
        "altitude_drop": "↓",
        "battery_spike": "⚡",
        "gps_loss": "📡",
        "motor_failure": "⚙",
        "wind_disturbance": "💨",
        "speed_violation": "🚀",
        "geofence_breach": "🚧",
        "near_miss": "⚠",
        "attitude_upset": "↺",
    }

    markers = [
        {
            "id": i,
            "type": e.event_type.value,
            "severity": e.severity,
            "timestamp_s": e.timestamp_s,
            "run_id": e.run_id,
            "description": e.description,
            "probable_cause": e.probable_cause,
            "color": _sev_colors.get(e.severity, "#888"),
            "icon": _type_icons.get(e.event_type.value, "!"),
            "value": e.value,
        }
        for i, e in enumerate(analysis.events[:30])
    ]

    summary = [
        {
            "label": "Total Anomalies",
            "value": analysis.total_anomalies,
            "severity": "critical" if analysis.total_anomalies > 10 else "ok",
        },
        {
            "label": "Critical Events",
            "value": analysis.critical_count,
            "severity": "critical" if analysis.critical_count > 0 else "ok",
        },
        {
            "label": "Near-Misses",
            "value": analysis.near_miss_count,
            "severity": "critical" if analysis.near_miss_count > 0 else "ok",
        },
        {
            "label": "GPS Loss (s)",
            "value": analysis.gps_loss_duration_s,
            "severity": "high" if analysis.gps_loss_duration_s > 2 else "ok",
        },
    ]

    return {
        "available": True,
        "markers": markers,
        "summary": summary,
        "failure_causes": analysis.identified_failure_causes,
        "score": analysis.score,
        "findings": analysis.findings,
    }
