"""Tests for the end-to-end experiment script helpers."""

from types import SimpleNamespace

from scripts.run_experiment import (
    EXPERIMENT_NAME,
    _sitl_runtime_summary,
    generate_markdown_report,
)


def test_sitl_runtime_summary_is_json_safe_without_to_dict():
    runtime = SimpleNamespace(
        mode="wsl",
        is_real=True,
        host_platform="Windows",
        px4_dir="/home/droneprompt/PX4-Autopilot",
        worlds_path="/home/droneprompt/PX4-Autopilot/Tools/simulation/gz/worlds",
        wsl_distro="Ubuntu",
        wsl_user="droneprompt",
        wsl_python_bin="/home/droneprompt/droneprompt-mavsdk-venv/bin/python",
        wsl_gz_bin="gz",
        diagnostics=("ok",),
    )

    summary = _sitl_runtime_summary(runtime)

    assert summary["mode"] == "wsl"
    assert summary["is_real"] is True
    assert summary["diagnostics"] == ["ok"]


def test_markdown_report_uses_relative_links(tmp_path):
    data = {
        "experiment": EXPERIMENT_NAME,
        "timestamp": "2026-05-03T00:00:00",
        "project_id": "project-test",
        "total_duration_s": 12.3,
        "stages": [
            {"name": "generate_design", "status": "success", "duration_s": 1.2},
            {"name": "evaluate_simulation", "status": "skipped", "duration_s": 0.0},
        ],
        "final_status": {
            "safety_passed": True,
            "code_status": "approved",
            "simulation_status": "failed",
            "simulation_success": False,
            "simulation_score": None,
            "evaluation_status": "skipped",
            "evaluation_skip_reason": "no_telemetry_collected",
            "mission_completed": False,
            "telemetry_source": "real_wsl_sitl_empty",
        },
    }
    state = {
        "monitor_report": {"overall_score": None},
        "design_generation": {"diagnostics": {"llm_response_s": 42.0}},
    }
    output = tmp_path / "report.md"

    generate_markdown_report(data, state, output)

    text = output.read_text(encoding="utf-8")
    assert "http://" not in text
    assert f"[JSON]({EXPERIMENT_NAME}.json)" in text
    assert f"[HTML]({EXPERIMENT_NAME}-report.html)" in text
    assert f"[Mission Code]({EXPERIMENT_NAME}-mission-code.py)" in text
    assert "`skipped`" in text
