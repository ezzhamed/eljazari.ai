"""eljazari.ai — Model: GeneratedCode"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from .project import Project
    from .design import Design
    from .simulation import Simulation


class CodeType(str, enum.Enum):
    mission = "mission"
    control = "control"
    navigation = "navigation"
    safety = "safety"


class GeneratedCode(Base):
    __tablename__ = "generated_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    design_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("designs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_content: Mapped[str] = mapped_column(Text, nullable=False)
    code_type: Mapped[CodeType] = mapped_column(
        Enum(CodeType, name="code_type"),
        nullable=False,
        default=CodeType.mission,
        index=True,
    )
    # GSCE configuration used
    gsce_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    safety_validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Safety-layer report
    safety_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    project: Mapped[Project] = relationship("Project", back_populates="generated_codes")
    design: Mapped[Design] = relationship("Design", back_populates="generated_codes")
    simulations: Mapped[list[Simulation]] = relationship(
        "Simulation", back_populates="code", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<GeneratedCode id={self.id} type={self.code_type} "
            f"v{self.version} validated={self.safety_validated}>"
        )
