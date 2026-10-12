"""eljazari.ai — Pydantic schemas: Design"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from models.design import DesignStatus


class DesignCreate(BaseModel):
    project_id: uuid.UUID
    design_tree: dict[str, Any] | None = None
    design_params: dict[str, Any] | None = None
    performance_estimate: dict[str, Any] | None = None
    urdf_file_path: str | None = None
    sdf_file_path: str | None = None
    stl_files: dict[str, Any] | None = None
    version: int = Field(default=1, ge=1)


class DesignUpdate(BaseModel):
    design_tree: dict[str, Any] | None = None
    design_params: dict[str, Any] | None = None
    performance_estimate: dict[str, Any] | None = None
    urdf_file_path: str | None = None
    sdf_file_path: str | None = None
    stl_files: dict[str, Any] | None = None
    status: DesignStatus | None = None


class DesignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    design_tree: dict[str, Any] | None
    design_params: dict[str, Any] | None
    performance_estimate: dict[str, Any] | None
    urdf_file_path: str | None
    sdf_file_path: str | None
    stl_files: dict[str, Any] | None
    version: int
    status: DesignStatus
    created_at: datetime
