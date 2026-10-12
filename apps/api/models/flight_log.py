"""eljazari.ai — Model: FlightLog"""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, Enum, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from .simulation import Simulation


class LogType(str, enum.Enum):
    position = "position"
    attitude = "attitude"
    battery = "battery"
    motor = "motor"
    error = "error"
    event = "event"


class FlightLog(Base):
    __tablename__ = "flight_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    simulation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    log_type: Mapped[LogType] = mapped_column(
        Enum(LogType, name="log_type"),
        nullable=False,
        index=True,
    )
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Relationships
    simulation: Mapped[Simulation] = relationship("Simulation", back_populates="flight_logs")

    def __repr__(self) -> str:
        return f"<FlightLog id={self.id} type={self.log_type} ts={self.timestamp_ms}>"
