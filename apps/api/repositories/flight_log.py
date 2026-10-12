"""eljazari.ai — Repository: FlightLog"""

import uuid

from sqlalchemy import func, select

from models.flight_log import FlightLog, LogType
from models.simulation import Simulation
from schemas.flight_log import FlightLogCreate
from .base import BaseRepository


class FlightLogRepository(BaseRepository[FlightLog]):
    model = FlightLog

    async def create_from_schema(self, data: FlightLogCreate) -> FlightLog:
        return await self.create(**data.model_dump())

    async def bulk_create(self, entries: list[FlightLogCreate]) -> list[FlightLog]:
        logs = [FlightLog(**e.model_dump()) for e in entries]
        self.session.add_all(logs)
        await self.session.flush()
        return logs

    async def list_by_simulation(
        self,
        simulation_id: uuid.UUID,
        *,
        log_type: LogType | None = None,
        from_ms: int | None = None,
        to_ms: int | None = None,
        offset: int = 0,
        limit: int = 500,
    ) -> list[FlightLog]:
        stmt = (
            select(FlightLog)
            .where(FlightLog.simulation_id == simulation_id)
            .order_by(FlightLog.timestamp_ms.asc())
            .offset(offset)
            .limit(limit)
        )
        if log_type is not None:
            stmt = stmt.where(FlightLog.log_type == log_type)
        if from_ms is not None:
            stmt = stmt.where(FlightLog.timestamp_ms >= from_ms)
        if to_ms is not None:
            stmt = stmt.where(FlightLog.timestamp_ms <= to_ms)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_project(
        self,
        project_id: uuid.UUID,
        *,
        log_type: LogType | None = None,
        offset: int = 0,
        limit: int = 500,
    ) -> list[FlightLog]:
        stmt = (
            select(FlightLog)
            .join(Simulation, Simulation.id == FlightLog.simulation_id)
            .where(Simulation.project_id == project_id)
            .order_by(FlightLog.timestamp_ms.asc())
            .offset(offset)
            .limit(limit)
        )
        if log_type is not None:
            stmt = stmt.where(FlightLog.log_type == log_type)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_for_project(self, project_id: uuid.UUID) -> int:
        stmt = (
            select(func.count(FlightLog.id))
            .select_from(FlightLog)
            .join(Simulation, Simulation.id == FlightLog.simulation_id)
            .where(Simulation.project_id == project_id)
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())
