"""eljazari.ai — Pydantic schemas: GeneratedCode"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from models.generated_code import CodeType


class GeneratedCodeCreate(BaseModel):
    project_id: uuid.UUID
    design_id: uuid.UUID
    code_content: str = Field(..., min_length=1)
    code_type: CodeType = CodeType.mission
    gsce_config: dict[str, Any] | None = None
    version: int = Field(default=1, ge=1)


class GeneratedCodeUpdate(BaseModel):
    code_content: str | None = None
    gsce_config: dict[str, Any] | None = None
    safety_validated: bool | None = None
    safety_report: dict[str, Any] | None = None


class GeneratedCodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    design_id: uuid.UUID
    code_content: str
    code_type: CodeType
    gsce_config: dict[str, Any] | None
    safety_validated: bool
    safety_report: dict[str, Any] | None
    version: int
    created_at: datetime
