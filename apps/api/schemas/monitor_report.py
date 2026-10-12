"""eljazari.ai — Pydantic schemas: MonitorReport"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from models.monitor_report import AnalysisType, Severity


class MonitorReportCreate(BaseModel):
    project_id: uuid.UUID
    simulation_id: uuid.UUID
    analysis_type: AnalysisType
    findings: dict[str, Any] | None = None
    recommendations: list[dict[str, Any]] | None = None
    severity: Severity = Severity.info


class MonitorReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    simulation_id: uuid.UUID
    analysis_type: AnalysisType
    findings: dict[str, Any] | None
    recommendations: list[dict[str, Any]] | None
    severity: Severity
    created_at: datetime
