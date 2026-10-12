"""eljazari.ai — Pydantic schemas: Simulation"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from models.simulation import SimulatorType, SimulationStatus


class SimulationCreate(BaseModel):
    project_id: uuid.UUID
    code_id: uuid.UUID
    simulator: SimulatorType = SimulatorType.sitl
    config: dict[str, Any] | None = None
    iteration: int = Field(default=1, ge=1)


class SimulationUpdate(BaseModel):
    status: SimulationStatus | None = None
    semantic_observations: list[dict[str, Any]] | None = None
    raw_telemetry: dict[str, Any] | None = None
    success_rate: float | None = Field(None, ge=0.0, le=1.0)
    completeness: float | None = Field(None, ge=0.0, le=1.0)
    duration_seconds: float | None = Field(None, ge=0.0)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class SimulationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    code_id: uuid.UUID
    simulator: SimulatorType
    status: SimulationStatus
    config: dict[str, Any] | None
    semantic_observations: list[dict[str, Any]] | None
    raw_telemetry: dict[str, Any] | None
    success_rate: float | None
    completeness: float | None
    duration_seconds: float | None
    iteration: int
    started_at: datetime | None
    completed_at: datetime | None
