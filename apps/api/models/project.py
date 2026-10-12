"""eljazari.ai — Model: Project"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from .design import Design
    from .generated_code import GeneratedCode
    from .simulation import Simulation
    from .monitor_report import MonitorReport
    from .agent_log import AgentLog


class ProjectStatus(str, enum.Enum):
    draft = "draft"
    designing = "designing"
    coding = "coding"
    simulating = "simulating"
    testing = "testing"
    monitoring = "monitoring"
    completed = "completed"
    failed = "failed"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, name="project_status"),
        nullable=False,
        default=ProjectStatus.draft,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    designs: Mapped[list[Design]] = relationship(
        "Design", back_populates="project", cascade="all, delete-orphan"
    )
    generated_codes: Mapped[list[GeneratedCode]] = relationship(
        "GeneratedCode", back_populates="project", cascade="all, delete-orphan"
    )
    simulations: Mapped[list[Simulation]] = relationship(
        "Simulation", back_populates="project", cascade="all, delete-orphan"
    )
    monitor_reports: Mapped[list[MonitorReport]] = relationship(
        "MonitorReport", back_populates="project", cascade="all, delete-orphan"
    )
    agent_logs: Mapped[list[AgentLog]] = relationship(
        "AgentLog", back_populates="project", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Project id={self.id} name={self.name!r} status={self.status}>"
