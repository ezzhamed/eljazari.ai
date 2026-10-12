"""eljazari.ai — Model: Design"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from .project import Project
    from .generated_code import GeneratedCode


class DesignStatus(str, enum.Enum):
    generated = "generated"
    validated = "validated"
    converted = "converted"
    approved = "approved"
    rejected = "rejected"


class Design(Base):
    __tablename__ = "designs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Full design tree from the agent
    design_tree: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Rotor count, dimensions, battery ...
    design_params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # hover_time, max_speed, max_distance, weight
    performance_estimate: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # File paths
    urdf_file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sdf_file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    stl_files: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[DesignStatus] = mapped_column(
        Enum(DesignStatus, name="design_status"),
        nullable=False,
        default=DesignStatus.generated,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    project: Mapped[Project] = relationship("Project", back_populates="designs")
    generated_codes: Mapped[list[GeneratedCode]] = relationship(
        "GeneratedCode", back_populates="design", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Design id={self.id} project={self.project_id} v{self.version} {self.status}>"
