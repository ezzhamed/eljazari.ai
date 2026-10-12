"""eljazari.ai — Pydantic schemas: Project"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from models.project import ProjectStatus


class ProjectCreate(BaseModel):
    user_id: uuid.UUID
    name: str = Field(..., min_length=1, max_length=255)
    prompt: str = Field(..., min_length=10)


class ProjectUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    prompt: str | None = None
    status: ProjectStatus | None = None


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    prompt: str
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime


class ProjectListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
