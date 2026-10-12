#!/usr/bin/env python3
"""Arm, take off, hover and land one PX4 SITL vehicle; print hover metrics as JSON.

Depends only on ``mavsdk`` so it runs anywhere a simulator is reachable,
including inside the simulation image (scripts/check_docker_sim.py copies it
there). Metrics come from PX4's own local-position estimate.

    python3 sitl_hover_probe.py udp://:14540 --altitude-m 5 --hover-s 15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from typing import Any


async def fly(address: str, altitude_m: float, hover_s: float) -> dict[str, Any]:
    from mavsdk import System

    drone = System()
    await drone.connect(system_address=address)
    async for state in drone.core.connection_state():
        if state.is_connected:
            break
    deadline = time.monotonic() + 90
    async for health in drone.telemetry.health():
        if health.is_global_position_ok and health.is_home_position_ok and health.is_armable:
            break
        if time.monotonic() > deadline:
            return {"error": "vehicle never became armable", "health": str(health)}

    samples: list[dict[str, Any]] = []
    phase = {"name": "arming"}
    started = time.monotonic()
    attitude: dict[str, Any] = {}

    async def track_attitude() -> None:
        async for euler in drone.telemetry.attitude_euler():
            attitude["latest"] = euler

    async def track_position() -> None:
        async for pv in drone.telemetry.position_velocity_ned():
            euler = attitude.get("latest")
            samples.append(
                {
                    "t": time.monotonic() - started,
                    "phase": phase["name"],
                    "n": pv.position.north_m,
                    "e": pv.position.east_m,
                    "alt": -pv.position.down_m,
                    "roll": euler.roll_deg if euler else None,
                    "pitch": euler.pitch_deg if euler else None,
                }
            )

    trackers = [asyncio.create_task(track_attitude()), asyncio.create_task(track_position())]
    try:
        await drone.action.set_takeoff_altitude(altitude_m)
        last_error: Exception | None = None
        for _ in range(10):
            try:
                await drone.action.arm()
                break
            except Exception as exc:  # noqa: BLE001 — PX4 refuses arming until EKF settles
                last_error = exc
                await asyncio.sleep(2)
        else:
            return {"error": f"arm failed: {last_error}"}
        phase["name"] = "takeoff"
        await drone.action.takeoff()
        climb_deadline = time.monotonic() + 30
        while time.monotonic() < climb_deadline:
            if samples and samples[-1]["alt"] >= altitude_m * 0.95:
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(3)
        phase["name"] = "hover"
        await asyncio.sleep(hover_s)
        phase["name"] = "land"
        await drone.action.land()
        landed = False
        land_deadline = time.monotonic() + 45
        async for in_air in drone.telemetry.in_air():
            if not in_air:
                landed = True
                break
            if time.monotonic() > land_deadline:
                break
        await asyncio.sleep(2)
    finally:
        for task in trackers:
            task.cancel()

    hover = [s for s in samples if s["phase"] == "hover"]
    altitudes = [s["alt"] for s in hover]
    drift = [math.hypot(s["n"] - hover[0]["n"], s["e"] - hover[0]["e"]) for s in hover]
    rolls = [s["roll"] for s in hover if s["roll"] is not None]
    pitches = [s["pitch"] for s in hover if s["pitch"] is not None]
    return {
        "samples": len(samples),
        "hover_samples": len(hover),
        "max_alt_m": max((s["alt"] for s in samples), default=None),
        "hover_alt_rmse_m": (
            math.sqrt(statistics.fmean((a - altitude_m) ** 2 for a in altitudes))
            if altitudes
            else None
        ),
        "hover_horizontal_max_drift_m": max(drift) if drift else None,
        "hover_abs_tilt_max_deg": max(
            (max(abs(r), abs(p)) for r, p in zip(rolls, pitches, strict=False)), default=None
        ),
        "landed": landed,
        "final_alt_m": samples[-1]["alt"] if samples else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("address", help="MAVSDK system address, e.g. udp://:14540")
    parser.add_argument("--altitude-m", type=float, default=5.0)
    parser.add_argument("--hover-s", type=float, default=15.0)
    parser.add_argument("--timeout-s", type=float, default=240.0)
    args = parser.parse_args()
    result = asyncio.run(
        asyncio.wait_for(fly(args.address, args.altitude_m, args.hover_s), args.timeout_s)
    )
    print("PROBE_RESULT " + json.dumps(result), flush=True)
    return 0 if result.get("landed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
