"""eljazari.ai — Database seeding script

Usage (from apps/api/):
    python -m scripts.seed
"""

import asyncio
import uuid
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.project import Project, ProjectStatus
from models.design import Design, DesignStatus
from models.generated_code import GeneratedCode, CodeType
from models.simulation import Simulation, SimulatorType, SimulationStatus
from models.flight_log import FlightLog, LogType
from models.monitor_report import MonitorReport, AnalysisType, Severity
from models.agent_log import AgentLog, AgentLogStatus

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Fixed UUIDs for reproducible seeds ─────────────────────────────────────
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
DESIGN_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
CODE_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
SIM_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")


async def seed(session: AsyncSession) -> None:
    log.info("Seeding projects…")
    project = Project(
        id=PROJECT_ID,
        user_id=USER_ID,
        name="Delivery Drone — Riyadh",
        prompt=(
            "Build a quadcopter drone for package delivery in urban areas, "
            "flight time 25 minutes, payload weight 2 kg, wind resistance up to 40 km/h."
        ),
        status=ProjectStatus.completed,
    )
    session.add(project)

    # Extra demo projects
    for i in range(2, 5):
        session.add(
            Project(
                user_id=USER_ID,
                name=f"Survey Drone #{i}",
                prompt=f"Aerial survey drone #{i} for surveying agricultural areas.",
                status=ProjectStatus.designing,
            )
        )

    log.info("Seeding designs…")
    design = Design(
        id=DESIGN_ID,
        project_id=PROJECT_ID,
        design_tree={
            "frame": "quadrotor_x",
            "motors": {"count": 4, "kv": 920, "size_mm": 2306},
            "propellers": {"size_inch": 9.5, "pitch": 4.3},
            "battery": {"cells": 4, "capacity_mah": 5000, "chemistry": "LiPo"},
            "fc": "Pixhawk 6C",
            "esc": {"type": "BLHeli_32", "amp": 45},
        },
        design_params={
            "num_rotors": 4,
            "wheelbase_mm": 450,
            "takeoff_weight_g": 1850,
            "max_payload_g": 2000,
            "battery_voltage": 14.8,
        },
        performance_estimate={
            "hover_time_min": 26.4,
            "max_speed_kmh": 72,
            "max_distance_km": 18,
            "weight_g": 1850,
        },
        urdf_file_path="/data/storage/designs/delivery_v1.urdf",
        sdf_file_path="/data/storage/designs/delivery_v1.sdf",
        stl_files={
            "frame": "/data/storage/stl/frame.stl",
            "arm": "/data/storage/stl/arm.stl",
        },
        version=1,
        status=DesignStatus.approved,
    )
    session.add(design)

    log.info("Seeding generated_codes…")
    code = GeneratedCode(
        id=CODE_ID,
        project_id=PROJECT_ID,
        design_id=DESIGN_ID,
        code_type=CodeType.mission,
        code_content="""\
#!/usr/bin/env python3
\"\"\"Auto-generated PX4 mission: Delivery Drone — Riyadh\"\"\"
import asyncio
from mavsdk import System

async def run():
    drone = System()
    await drone.connect(system_address="udp://:14540")
    print("Waiting for drone to connect...")
    async for state in drone.core.connection_state():
        if state.is_connected:
            break
    await drone.action.arm()
    await drone.action.takeoff()
    await asyncio.sleep(5)
    # Waypoint mission goes here
    await drone.action.land()

asyncio.run(run())
""",
        gsce_config={
            "safety_layer": "v1",
            "max_altitude_m": 100,
            "max_speed_ms": 12,
            "geofence_radius_m": 300,
        },
        safety_validated=True,
        safety_report={
            "passed": True,
            "checks": {
                "altitude_limit": "ok",
                "speed_limit": "ok",
                "geofence": "ok",
                "battery_reserve": "ok",
            },
            "warnings": [],
        },
        version=1,
    )
    session.add(code)

    log.info("Seeding simulations…")
    sim = Simulation(
        id=SIM_ID,
        project_id=PROJECT_ID,
        code_id=CODE_ID,
        simulator=SimulatorType.sitl,
        status=SimulationStatus.completed,
        config={
            "world": "riyadh_urban",
            "wind_speed_ms": 5.0,
            "obstacles": True,
            "time_of_day": "morning",
        },
        semantic_observations=[
            {"step": 1, "event": "takeoff", "altitude_m": 0.0, "ok": True},
            {"step": 2, "event": "climb", "altitude_m": 50.0, "ok": True},
            {"step": 3, "event": "waypoint_reached", "wp": 1, "ok": True},
            {"step": 4, "event": "land", "altitude_m": 0.0, "ok": True},
        ],
        raw_telemetry={
            "max_altitude_m": 52.3,
            "avg_speed_ms": 9.8,
            "battery_consumed_pct": 38,
            "distance_km": 4.2,
        },
        success_rate=0.97,
        completeness=1.0,
        duration_seconds=312.5,
        iteration=1,
    )
    session.add(sim)

    log.info("Seeding flight_logs…")
    log_entries = [
        FlightLog(
            simulation_id=SIM_ID,
            log_type=LogType.position,
            timestamp_ms=t * 1000,
            data={"lat": 24.7136 + t * 0.0001, "lon": 46.6753, "alt_m": min(t * 2, 50)},
        )
        for t in range(0, 310, 10)
    ]
    log_entries += [
        FlightLog(
            simulation_id=SIM_ID,
            log_type=LogType.battery,
            timestamp_ms=t * 1000,
            data={"voltage_v": 16.8 - t * 0.01, "current_a": 22.5, "remaining_pct": 100 - t // 3},
        )
        for t in range(0, 310, 30)
    ]
    session.add_all(log_entries)

    log.info("Seeding monitor_reports…")
    session.add(
        MonitorReport(
            project_id=PROJECT_ID,
            simulation_id=SIM_ID,
            analysis_type=AnalysisType.performance,
            findings={
                "hover_time_actual_min": 26.1,
                "max_speed_kmh": 70.4,
                "mission_completion_pct": 100,
            },
            recommendations=[
                {
                    "priority": "low",
                    "text": "Increase takeoff speed by 10% to reduce mission time.",
                },
            ],
            severity=Severity.info,
        )
    )
    session.add(
        MonitorReport(
            project_id=PROJECT_ID,
            simulation_id=SIM_ID,
            analysis_type=AnalysisType.recommendation,
            findings={"battery_usage_pct": 38, "margin_pct": 42},
            recommendations=[
                {
                    "priority": "medium",
                    "text": "Expected consumption in stormy conditions may reach 55% — reviewing the flight path is advised.",
                }
            ],
            severity=Severity.warning,
        )
    )

    log.info("Seeding agent_logs…")
    agents_data = [
        ("DesignAgent", "generate_design", "prompt text", "design_tree JSON", 1240, 4200),
        ("CodeAgent", "generate_mission_code", "design_tree", "PX4 Python code", 870, 2900),
        ("SafetyAgent", "validate_code", "generated code", "safety_report", 310, 1100),
        (
            "SimAgent",
            "run_sitl_simulation",
            "code + config",
            "telemetry + observations",
            450,
            312500,
        ),
        ("MonitorAgent", "analyze_performance", "telemetry", "performance report", 520, 1800),
    ]
    for agent_name, action, inp, out, tokens, dur in agents_data:
        session.add(
            AgentLog(
                project_id=PROJECT_ID,
                agent_name=agent_name,
                action=action,
                input_summary=inp,
                output_summary=out,
                tokens_used=tokens,
                duration_ms=dur,
                status=AgentLogStatus.success,
            )
        )

    await session.commit()
    log.info("✓ Database seeded successfully.")


async def main() -> None:
    async with AsyncSessionLocal() as session:
        await seed(session)


if __name__ == "__main__":
    asyncio.run(main())
