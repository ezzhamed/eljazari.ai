"""eljazari.ai — Model: MonitorReport"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from .project import Project
    from .simulation import Simulation


class AnalysisType(str, enum.Enum):
    performance = "performance"
    failure = "failure"
    recommendation = "recommendation"
    comparison = "comparison"


class Severity(str, enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class MonitorReport(Base):
    __tablename__ = "monitor_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    simulation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_type: Mapped[AnalysisType] = mapped_column(
        Enum(AnalysisType, name="analysis_type"),
        nullable=False,
        index=True,
    )
    findings: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # JSONB array of recommendation objects
    recommendations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    severity: Mapped[Severity] = mapped_column(
        Enum(Severity, name="report_severity"),
        nullable=False,
        default=Severity.info,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    project: Mapped[Project] = relationship("Project", back_populates="monitor_reports")
    simulation: Mapped[Simulation] = relationship("Simulation", back_populates="monitor_reports")

    def __repr__(self) -> str:
        return f"<MonitorReport id={self.id} type={self.analysis_type} severity={self.severity}>"
