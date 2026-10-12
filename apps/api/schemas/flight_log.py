"""eljazari.ai — Pydantic schemas: FlightLog"""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from models.flight_log import LogType


class FlightLogCreate(BaseModel):
    simulation_id: uuid.UUID
    log_type: LogType
    timestamp_ms: int = Field(..., ge=0)
    data: dict[str, Any] | None = None


class FlightLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    simulation_id: uuid.UUID
    log_type: LogType
    timestamp_ms: int
    data: dict[str, Any] | None
