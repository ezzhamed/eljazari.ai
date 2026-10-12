"""eljazari.ai — Repository: Simulation"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from models.simulation import Simulation, SimulationStatus
from schemas.simulation import SimulationCreate, SimulationUpdate
from .base import BaseRepository


class SimulationRepository(BaseRepository[Simulation]):
    model = Simulation

    async def create_from_schema(self, data: SimulationCreate) -> Simulation:
        return await self.create(**data.model_dump())

    async def update_from_schema(self, sim: Simulation, data: SimulationUpdate) -> Simulation:
        return await self.update(sim, **data.model_dump(exclude_unset=True))

    async def start(self, id: uuid.UUID) -> Simulation:
        sim = await self.get_or_raise(id)
        return await self.update(
            sim,
            status=SimulationStatus.running,
            started_at=datetime.now(timezone.utc),
        )

    async def complete(
        self,
        id: uuid.UUID,
        *,
        success_rate: float,
        completeness: float,
        duration_seconds: float,
    ) -> Simulation:
        sim = await self.get_or_raise(id)
        return await self.update(
            sim,
            status=SimulationStatus.completed,
            completed_at=datetime.now(timezone.utc),
            success_rate=success_rate,
            completeness=completeness,
            duration_seconds=duration_seconds,
        )

    async def fail(self, id: uuid.UUID, crashed: bool = False) -> Simulation:
        sim = await self.get_or_raise(id)
        status = SimulationStatus.crashed if crashed else SimulationStatus.failed
        return await self.update(
            sim,
            status=status,
            completed_at=datetime.now(timezone.utc),
        )

    async def list_by_project(
        self,
        project_id: uuid.UUID,
        *,
        status: SimulationStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Simulation]:
        stmt = (
            select(Simulation)
            .where(Simulation.project_id == project_id)
            .order_by(Simulation.iteration.desc())
            .offset(offset)
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(Simulation.status == status)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def latest_iteration(self, project_id: uuid.UUID) -> int:
        sims = await self.list_by_project(project_id, limit=1)
        return sims[0].iteration if sims else 0

    async def list_for_project(
        self,
        project_id: uuid.UUID,
        *,
        status: SimulationStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[Simulation]:
        return await self.list_by_project(
            project_id,
            status=status,
            offset=offset,
            limit=limit,
        )

    async def get_latest_for_project(self, project_id: uuid.UUID) -> Simulation | None:
        sims = await self.list_by_project(project_id, limit=1)
        return sims[0] if sims else None
