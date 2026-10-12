from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.paths import project_root
from config.settings import Settings
from scripts.bootstrap_aircraftverse import _validate_dataset


def test_settings_default_env_file_points_to_repo_root() -> None:
    expected = Path(__file__).resolve().parents[3] / ".env"
    actual = Path(Settings.model_config["env_file"]).resolve()

    assert actual == expected.resolve()


@pytest.mark.parametrize(
    ("settings_file", "expected_root"),
    [
        ("/repo/apps/api/config/settings.py", "/repo"),
        ("/app/config/settings.py", "/app"),
        ("/srv/service/config/settings.py", "/srv/service"),
    ],
)
def test_settings_support_repo_and_container_layouts(
    settings_file: str, expected_root: str
) -> None:
    assert project_root(Path(settings_file)) == Path(expected_root)


def test_settings_parse_string_lists() -> None:
    settings = Settings(
        _env_file=None,
        api_secret_key="x" * 32,
        cors_origins="https://app.example.com,https://ops.example.com",
        trusted_hosts='["api.example.com", "app.example.com"]',
    )

    assert settings.cors_origins == [
        "https://app.example.com",
        "https://ops.example.com",
    ]
    assert settings.trusted_hosts == ["api.example.com", "app.example.com"]


def test_settings_parse_csv_values_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "API_SECRET_KEY=abcde12345abcde12345abcde1234512",
                "CORS_ORIGINS=http://localhost:3000,http://localhost:8000",
                "TRUSTED_HOSTS=localhost,127.0.0.1",
                "MIN_BATTERY_PCT=33.3",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.cors_origins == ["http://localhost:3000", "http://localhost:8000"]
    assert settings.trusted_hosts == ["localhost", "127.0.0.1"]
    assert settings.min_battery_pct == pytest.approx(33.3)


def test_settings_read_droneprompt_sitl_env_names(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "API_SECRET_KEY=abcde12345abcde12345abcde1234512",
                "DRONEPROMPT_SITL_RUNTIME=wsl",
                "DRONEPROMPT_FORCE_SITL=wsl",
                "DRONEPROMPT_REQUIRE_REAL_SITL=true",
                "DRONEPROMPT_WSL_DISTRO=Ubuntu",
                "DRONEPROMPT_WSL_USER=droneprompt",
                "DRONEPROMPT_WSL_PX4_DIR=/home/droneprompt/PX4-Autopilot",
                "DRONEPROMPT_WSL_WORLDS_PATH=/home/droneprompt/PX4-Autopilot/Tools/simulation/gz/worlds",
                "DRONEPROMPT_WSL_PYTHON_BIN=/home/droneprompt/droneprompt-mavsdk-venv/bin/python",
                "DRONEPROMPT_WSL_GZ_BIN=/usr/bin/gz",
                "DRONEPROMPT_WSL_MAVLINK_TARGET_HOST=127.0.0.1",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.sitl_runtime == "wsl"
    assert settings.sitl_force_runtime == "wsl"
    assert settings.sitl_require_real is True
    assert settings.sitl_wsl_distro == "Ubuntu"
    assert settings.sitl_wsl_user == "droneprompt"
    assert settings.sitl_wsl_px4_dir == "/home/droneprompt/PX4-Autopilot"
    assert settings.sitl_wsl_worlds_path.endswith("/Tools/simulation/gz/worlds")
    assert settings.sitl_wsl_python_bin.endswith("/droneprompt-mavsdk-venv/bin/python")
    assert settings.sitl_wsl_gz_bin == "/usr/bin/gz"
    assert settings.sitl_wsl_mavlink_target_host == "127.0.0.1"


def test_production_rejects_default_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            api_secret_key="drone-prompt-dev-secret-key-2026",
            trusted_hosts="api.example.com",
        )


def test_anonymous_auth_defaults_to_debug_only() -> None:
    dev_settings = Settings(_env_file=None, api_secret_key="x" * 32, debug=True)
    prod_settings = Settings(
        _env_file=None,
        environment="production",
        api_secret_key="x" * 32,
        trusted_hosts="api.example.com",
    )

    assert dev_settings.anonymous_auth_enabled is True
    assert prod_settings.anonymous_auth_enabled is False


def test_relative_data_paths_are_taken_from_the_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean clone's `.env.example` names the corpus as ./data/...; the API starts
    in apps/api, where that path does not exist, and it served designs without RAG."""
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.chdir(tmp_path)
    relative = Settings(
        _env_file=None,
        api_secret_key="x" * 32,
        aircraftverse_path="./data/aircraftverse/designs.json",
        aircraftverse_chroma_path="data/storage/chroma-local",
    )
    assert relative.resolved_aircraftverse_path == (
        repo_root / "data" / "aircraftverse" / "designs.json"
    )
    assert relative.resolved_aircraftverse_chroma_path == (
        repo_root / "data" / "storage" / "chroma-local"
    )
    absolute = Settings(
        _env_file=None, api_secret_key="x" * 32, aircraftverse_path=str(tmp_path / "designs.json")
    )
    assert absolute.resolved_aircraftverse_path == tmp_path / "designs.json"
    assert absolute.resolved_aircraftverse_chroma_path is None


def test_validate_dataset_accepts_expected_shape(tmp_path: Path) -> None:
    dataset_path = tmp_path / "designs.json"
    dataset_path.write_text(
        """
        [
          {
            "topology": "quadcopter",
            "frame": {"type": "X", "arm_length_mm": 250},
            "battery": {"cells": 4, "capacity_mah": 5000, "weight_g": 480},
            "payload": {"max_weight_g": 500}
          }
        ]
        """.strip(),
        encoding="utf-8",
    )

    ok, detail = _validate_dataset(dataset_path)
    assert ok is True
    assert "1 designs" in detail


def test_simulation_pass_threshold_setting(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "API_SECRET_KEY=abcde12345abcde12345abcde1234512",
                "SIMULATION_PASS_THRESHOLD=75.5",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)
    assert settings.simulation_pass_threshold == 75.5


# ── Dev token endpoint hardening ─────────────────────────────────────────────
# POST /api/auth/token signs a JWT for any user_id without verifying a
# credential. These tests pin the guards that keep it out of production.


def test_dev_token_endpoint_defaults_on_outside_production() -> None:
    assert Settings(_env_file=None, api_secret_key="x" * 32).dev_token_endpoint_enabled is True


def test_dev_token_endpoint_defaults_off_in_production() -> None:
    prod = Settings(
        _env_file=None,
        environment="production",
        api_secret_key="x" * 32,
        trusted_hosts="api.example.com",
    )

    assert prod.dev_token_endpoint_enabled is False


def test_production_refuses_to_start_with_dev_token_endpoint_enabled() -> None:
    """Enabling an open token mint in production is not a supported config."""
    with pytest.raises(ValidationError, match="ENABLE_DEV_TOKEN_ENDPOINT"):
        Settings(
            _env_file=None,
            environment="production",
            api_secret_key="x" * 32,
            trusted_hosts="api.example.com",
            enable_dev_token_endpoint=True,
        )


def test_dev_token_endpoint_can_be_disabled_outside_production() -> None:
    settings = Settings(_env_file=None, api_secret_key="x" * 32, enable_dev_token_endpoint=False)

    assert settings.dev_token_endpoint_enabled is False


def test_flattened_api_startup_imports(tmp_path: Path) -> None:
    """Import the API and worker from the layout copied by both Dockerfiles.

    No services are started. The old fixed parent offsets either raised at
    /app or silently searched outside the packaged data at deeper paths.
    """
    source = Path(__file__).resolve().parents[1]
    app = tmp_path / "app"
    for package in (
        "config",
        "agents",
        "services",
        "utils",
        "pipelines",
        "models",
        "db",
        "schemas",
        "repositories",
        "routers",
        "middleware",
        "graph",
        "ws",
        "tasks",
    ):
        for module in (source / package).rglob("*.py"):
            if any(
                part.startswith(".") or part == "__pycache__"
                for part in module.relative_to(source).parts
            ):
                continue
            destination = app / module.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(module, destination)
    for name in ("main.py", "celery_app.py"):
        shutil.copyfile(source / name, app / name)
    probe = """
from pathlib import Path
from config.settings import settings
from agents.safety_layer import _RULES_DIR
from services.component_catalog import _candidate_catalog_dirs
from services.docker_sim import _REPO_ROOT
from utils.wsl_mission_runner import _REPO_ROOT as runner_root
import main
import celery_app
root = Path.cwd()
assert settings.resolved_aircraftverse_path == root / 'data/aircraftverse/designs.json'
assert _RULES_DIR == root / 'data/safety-rules'
assert root / 'data/components' in _candidate_catalog_dirs()
assert _REPO_ROOT == root
assert runner_root == root
"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME"}
    }
    env.update(
        API_SECRET_KEY="container-import-test-key-32-chars",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_index_path_environment_alias_prefers_new_name(monkeypatch):
    monkeypatch.setenv("AIRCRAFTVERSE_INDEX_PATH", "data/new-index")
    monkeypatch.setenv("AIRCRAFTVERSE_CHROMA_PATH", "data/old-index")
    settings = Settings(_env_file=None, api_secret_key="x" * 32)
    assert settings.resolved_aircraftverse_chroma_path.name == "new-index"
