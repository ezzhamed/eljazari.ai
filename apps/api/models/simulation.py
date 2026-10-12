"""eljazari.ai — Model: Simulation"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from .project import Project
    from .generated_code import GeneratedCode
    from .flight_log import FlightLog
    from .monitor_report import MonitorReport


class SimulatorType(str, enum.Enum):
    gazebo = "gazebo"
    airsim = "airsim"
    sitl = "sitl"


class SimulationStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    crashed = "crashed"


class Simulation(Base):
    __tablename__ = "simulations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generated_codes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    simulator: Mapped[SimulatorType] = mapped_column(
        Enum(SimulatorType, name="simulator_type"),
        nullable=False,
        default=SimulatorType.sitl,
    )
    status: Mapped[SimulationStatus] = mapped_column(
        Enum(SimulationStatus, name="simulation_status"),
        nullable=False,
        default=SimulationStatus.pending,
        index=True,
    )
    # Simulation config: world, weather, obstacles
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Generated semantic observations (JSONB array)
    semantic_observations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Raw telemetry data
    raw_telemetry: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    success_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    completeness: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Iteration number in the closed loop
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    project: Mapped[Project] = relationship("Project", back_populates="simulations")
    code: Mapped[GeneratedCode] = relationship("GeneratedCode", back_populates="simulations")
    flight_logs: Mapped[list[FlightLog]] = relationship(
        "FlightLog", back_populates="simulation", cascade="all, delete-orphan"
    )
    monitor_reports: Mapped[list[MonitorReport]] = relationship(
        "MonitorReport", back_populates="simulation", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Simulation id={self.id} simulator={self.simulator} "
            f"status={self.status} iter={self.iteration}>"
        )
