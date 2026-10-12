from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from config.env_names import env_alias
from config.paths import PROJECT_ROOT as _PROJECT_ROOT


# NOTE: No default secret — API_SECRET_KEY MUST be supplied via environment variable.
# Intentionally left unset so the process fails fast if the variable is missing.
_DEFAULT_AIRCRAFTVERSE_PATH = _PROJECT_ROOT / "data" / "aircraftverse" / "designs.json"
_STRING_LIST_FIELDS = {"cors_origins", "trusted_hosts"}


def _from_project_root(value: str) -> Path:
    """A relative path in `.env` is relative to the repository root, where `.env` lives,
    not to whichever directory the process started in (the API runs from apps/api)."""
    path = Path(value).expanduser()
    return (path if path.is_absolute() else _PROJECT_ROOT / path).resolve()


def _parse_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise ValueError("Expected a JSON array")
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in text.split(",") if item.strip()]
    raise TypeError(f"Unsupported list value: {type(value)!r}")


class _RawStringListSourceMixin:
    """Keep selected env values raw so model validators can parse CSV and JSON."""

    def prepare_field_value(
        self, field_name: str, field, value: Any, value_is_complex: bool
    ) -> Any:
        if field_name in _STRING_LIST_FIELDS and isinstance(value, str):
            return value
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class _RawStringListEnvSource(_RawStringListSourceMixin, EnvSettingsSource):
    pass


class _RawStringListDotEnvSource(_RawStringListSourceMixin, DotEnvSettingsSource):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _RawStringListEnvSource(
                settings_cls,
                case_sensitive=getattr(env_settings, "case_sensitive", None),
                env_prefix=getattr(env_settings, "env_prefix", None),
                env_nested_delimiter=getattr(env_settings, "env_nested_delimiter", None),
                env_ignore_empty=getattr(env_settings, "env_ignore_empty", None),
                env_parse_none_str=getattr(env_settings, "env_parse_none_str", None),
                env_parse_enums=getattr(env_settings, "env_parse_enums", None),
            ),
            _RawStringListDotEnvSource(
                settings_cls,
                env_file=getattr(dotenv_settings, "env_file", None),
                env_file_encoding=getattr(dotenv_settings, "env_file_encoding", None),
                case_sensitive=getattr(dotenv_settings, "case_sensitive", None),
                env_prefix=getattr(dotenv_settings, "env_prefix", None),
                env_nested_delimiter=getattr(dotenv_settings, "env_nested_delimiter", None),
                env_ignore_empty=getattr(dotenv_settings, "env_ignore_empty", None),
                env_parse_none_str=getattr(dotenv_settings, "env_parse_none_str", None),
                env_parse_enums=getattr(dotenv_settings, "env_parse_enums", None),
            ),
            file_secret_settings,
        )

    # App
    app_name: str = "eljazari.ai API"
    environment: str = "development"
    checkpoint_backend: Literal["auto", "memory", "postgres"] = "auto"
    auto_start_docker_services: bool = True
    debug: bool = False
    log_level: str = "info"
    # API_SECRET_KEY has NO default — must be set via environment variable.
    # In development, set it in .env (see .env.example).
    # In production, inject it via secrets manager or CI/CD.
    api_secret_key: str = Field(min_length=32)
    enable_api_docs: bool = True
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:8000"]
    )
    trusted_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    enable_https_redirect: bool = False
    allow_anonymous_auth: bool | None = None
    # POST /api/auth/token issues a signed JWT for any user_id without checking
    # any credential — it is a development convenience, not authentication.
    # Defaults to on outside production and off in production.
    enable_dev_token_endpoint: bool | None = None
    access_token_expire_hours: int = Field(default=24 * 7, ge=1, le=24 * 30)

    # Database
    database_url: str = "postgresql+asyncpg://drone:drone@localhost:5432/droneprompt"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    # Uvicorn worker processes. More than one needs a shared Socket.IO message
    # queue (events raised in one worker must reach clients on another) and
    # durable checkpoints; see _validate_production_guards.
    api_workers: int = Field(default=1, ge=1, le=32, validation_alias=env_alias("API_WORKERS"))
    # e.g. redis://localhost:6379/3 — enables socketio.AsyncRedisManager.
    socketio_message_queue: str = Field(
        default="", validation_alias=env_alias("SOCKETIO_MESSAGE_QUEUE")
    )
    # One PX4/Gazebo instance per host: later runs wait this long, then fail.
    sitl_lock_timeout_s: float = Field(
        default=120.0, ge=0, validation_alias=env_alias("SITL_LOCK_TIMEOUT_S")
    )

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_default_model: str = "qwen2.5:7b"
    ollama_design_model: str = "gemma3:4b"
    ollama_code_model: str = "qwen2.5-coder:7b"
    ollama_eval_model: str = "llama3.2:3b"
    # Dedicated embedding model for AircraftVerse RAG. Chat models (llama3.2,
    # gemma3) do not expose an embeddings endpoint in Ollama and return HTTP
    # 501, which silently disables RAG. nomic-embed-text is a small, purpose-
    # built embedder (`ollama pull nomic-embed-text`).
    ollama_embed_model: str = "nomic-embed-text"
    ollama_design_timeout_s: int = Field(
        default=90,
        ge=1,
        validation_alias=env_alias("DESIGN_TIMEOUT_S"),
    )
    design_max_repair_attempts: int = Field(
        default=1,
        ge=0,
        le=3,
        validation_alias=env_alias("DESIGN_MAX_REPAIR_ATTEMPTS"),
    )

    # AircraftVerse / RAG
    # Optional, locally imported catalog. Public fallback has no sourced curves.
    components_catalog_path: str = str(_PROJECT_ROOT / ".local" / "components")

    aircraftverse_path: str = str(_DEFAULT_AIRCRAFTVERSE_PATH)
    aircraftverse_require_real: bool = False
    aircraftverse_rag_mode: str = "memory"
    aircraftverse_chroma_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AIRCRAFTVERSE_INDEX_PATH", "AIRCRAFTVERSE_CHROMA_PATH", "aircraftverse_chroma_path"
        ),
    )

    # LangSmith
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "eljazari"

    # Safety limits
    max_altitude_m: float = 120.0
    max_speed_ms: float = 15.0
    max_geofence_radius_m: float = 500.0
    min_battery_pct: float = 20.0
    max_simulation_iterations: int = 5
    simulation_pass_threshold: float = Field(
        default=50.0,
        validation_alias="SIMULATION_PASS_THRESHOLD",
    )

    # PX4
    px4_home_lat: float = 24.7136
    px4_home_lon: float = 46.6753
    px4_home_alt: float = 620.0
    mavlink_port: int = 14550
    sitl_runtime: str = Field(default="auto", validation_alias=env_alias("SITL_RUNTIME"))
    sitl_force_runtime: str = Field(default="", validation_alias=env_alias("FORCE_SITL"))
    sitl_require_real: bool = Field(
        default=False,
        validation_alias=env_alias("REQUIRE_REAL_SITL"),
    )
    sitl_native_px4_dir: str = Field(
        default="/opt/px4/PX4-Autopilot",
        validation_alias=env_alias("NATIVE_PX4_DIR"),
    )
    sitl_native_worlds_path: str = Field(
        default="/opt/ros/humble/share/gazebo_ros/worlds",
        validation_alias=env_alias("NATIVE_WORLDS_PATH"),
    )
    sitl_wsl_distro: str = Field(default="", validation_alias=env_alias("WSL_DISTRO"))
    sitl_wsl_user: str = Field(default="", validation_alias=env_alias("WSL_USER"))
    sitl_wsl_px4_dir: str = Field(
        default="/opt/px4/PX4-Autopilot",
        validation_alias=env_alias("WSL_PX4_DIR"),
    )
    sitl_wsl_worlds_path: str = Field(
        default="/opt/ros/humble/share/gazebo_ros/worlds",
        validation_alias=env_alias("WSL_WORLDS_PATH"),
    )
    sitl_wsl_python_bin: str = Field(
        default="python3",
        validation_alias=env_alias("WSL_PYTHON_BIN"),
    )
    sitl_wsl_gz_bin: str = Field(default="gz", validation_alias=env_alias("WSL_GZ_BIN"))
    # Docker runtime: one container per run from this image (infrastructure/Dockerfile.sim).
    sitl_docker_image: str = Field(
        default="eljazari-sim:latest", validation_alias=env_alias("SITL_DOCKER_IMAGE")
    )
    # Empty = infrastructure/seccomp/sim-sandbox.json in this repository.
    sitl_docker_seccomp_profile: str = Field(
        default="", validation_alias=env_alias("SITL_DOCKER_SECCOMP_PROFILE")
    )
    sitl_wsl_mavlink_target_host: str = Field(
        default="127.0.0.1",
        validation_alias=env_alias("WSL_MAVLINK_TARGET_HOST"),
    )

    # WebGCS / MAVLink bridge
    # Address the ground station connects to. Defaults to the PX4 SITL UDP port;
    # use serial:///dev/ttyUSB0:57600 or tcp://<ip>:5760 for real hardware.
    gcs_mavlink_address: str = Field(
        default="udp://:14540",
        validation_alias="GCS_MAVLINK_ADDRESS",
    )
    gcs_connect_timeout_s: float = Field(
        default=30.0,
        gt=0,
        validation_alias="GCS_CONNECT_TIMEOUT_S",
    )
    gcs_takeoff_altitude_m: float = Field(
        default=10.0,
        gt=0,
        validation_alias="GCS_TAKEOFF_ALTITUDE_M",
    )

    # Storage. The containers set their own absolute STORAGE_PATH. A relative one
    # is taken from the repository root. `/data/storage` used to be the default:
    # on Windows it became C:\data\storage, which the WSL simulator cannot see,
    # and on Linux a normal user cannot create it.
    storage_path: str = str(_PROJECT_ROOT / "data" / "storage")

    @field_validator("storage_path")
    @classmethod
    def _storage_from_project_root(cls, value: str) -> str:
        return str(_from_project_root(value))

    @field_validator("cors_origins", "trusted_hosts", mode="before")
    @classmethod
    def _validate_string_lists(cls, value: Any) -> list[str]:
        return _parse_str_list(value)

    @field_validator("environment")
    @classmethod
    def _normalize_environment(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("aircraftverse_rag_mode")
    @classmethod
    def _validate_rag_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"memory", "persistent"}:
            raise ValueError("aircraftverse_rag_mode must be 'memory' or 'persistent'")
        return normalized

    @field_validator("sitl_runtime")
    @classmethod
    def _validate_sitl_runtime(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"auto", "native", "wsl", "mock", "docker"}:
            raise ValueError("sitl_runtime must be 'auto', 'native', 'wsl', 'mock', or 'docker'")
        return normalized

    @field_validator("sitl_force_runtime")
    @classmethod
    def _validate_sitl_force_runtime(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and normalized not in {"native", "wsl", "docker", "mock"}:
            raise ValueError(
                "sitl_force_runtime must be empty, 'native', 'wsl', 'docker', or 'mock'"
            )
        return normalized

    @model_validator(mode="after")
    def _validate_production_guards(self) -> "Settings":
        if self.is_production and self.checkpoint_backend == "memory":
            raise ValueError("Production cannot use in-memory checkpoints")
        if self.api_workers > 1 and not self.socketio_message_queue.strip():
            raise ValueError(
                "API_WORKERS > 1 requires SOCKETIO_MESSAGE_QUEUE (Redis) so pipeline "
                "events reach clients connected to other workers"
            )
        if self.api_workers > 1 and self.resolved_checkpoint_backend != "postgres":
            raise ValueError(
                "API_WORKERS > 1 requires PostgreSQL checkpoints; in-memory state is per process"
            )
        # Guard: wildcard CORS is never safe in production
        if self.is_production and ("*" in self.cors_origins or not self.cors_origins):
            raise ValueError(
                "Production requires explicit CORS_ORIGINS — wildcard '*' is not allowed"
            )
        if self.is_production and not self.trusted_hosts:
            raise ValueError("Production requires TRUSTED_HOSTS to be configured")
        if self.is_production and self.api_secret_key == "drone-prompt-dev-secret-key-2026":
            raise ValueError("Default developer secret key is not allowed in production mode")
        # The dev token endpoint hands a valid JWT to anyone who asks. Enabling
        # it in production is indistinguishable from disabling authentication,
        # so refuse to start rather than serve an open token mint.
        if self.is_production and self.enable_dev_token_endpoint:
            raise ValueError(
                "ENABLE_DEV_TOKEN_ENDPOINT cannot be true in production — "
                "POST /api/auth/token issues tokens without verifying any credential. "
                "Front the API with a real identity provider instead."
            )
        return self

    @property
    def resolved_checkpoint_backend(self) -> str:
        if self.checkpoint_backend == "auto":
            return "postgres" if self.is_production else "memory"
        return self.checkpoint_backend

    @property
    def is_production(self) -> bool:
        return self.environment in {"prod", "production"}

    @property
    def anonymous_auth_enabled(self) -> bool:
        if self.allow_anonymous_auth is not None:
            return self.allow_anonymous_auth
        return self.debug and not self.is_production

    @property
    def dev_token_endpoint_enabled(self) -> bool:
        if self.enable_dev_token_endpoint is not None:
            return self.enable_dev_token_endpoint and not self.is_production
        return not self.is_production

    @property
    def resolved_aircraftverse_path(self) -> Path:
        return _from_project_root(self.aircraftverse_path)

    @property
    def resolved_aircraftverse_chroma_path(self) -> Path | None:
        if not self.aircraftverse_chroma_path:
            return None
        return _from_project_root(self.aircraftverse_chroma_path)


settings = Settings()
