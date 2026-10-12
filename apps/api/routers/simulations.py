"""eljazari.ai — Simulations Router"""

from typing import Any

from fastapi import APIRouter

router = APIRouter()


@router.get("/{simulation_id}")
async def get_simulation(simulation_id: str) -> dict[str, Any]:
    # TODO: Load from DB
    return {"simulation_id": simulation_id, "status": "pending"}


@router.get("/{simulation_id}/telemetry")
async def get_telemetry(simulation_id: str) -> dict[str, Any]:
    # TODO: Load telemetry from DB
    return {"simulation_id": simulation_id, "telemetry": []}
