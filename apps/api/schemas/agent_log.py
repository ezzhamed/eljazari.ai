"""eljazari.ai — Pydantic schemas: AgentLog"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from models.agent_log import AgentLogStatus


class AgentLogCreate(BaseModel):
    project_id: uuid.UUID
    agent_name: str = Field(..., max_length=128)
    action: str = Field(..., max_length=256)
    input_summary: str | None = None
    output_summary: str | None = None
    tokens_used: int | None = Field(None, ge=0)
    duration_ms: int | None = Field(None, ge=0)
    status: AgentLogStatus = AgentLogStatus.success
    error_message: str | None = None


class AgentLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    agent_name: str
    action: str
    input_summary: str | None
    output_summary: str | None
    tokens_used: int | None
    duration_ms: int | None
    status: AgentLogStatus
    error_message: str | None
    created_at: datetime
