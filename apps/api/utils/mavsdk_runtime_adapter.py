"""High-level mission helpers backed by a MAVSDK System connection."""

from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace
from typing import Any, Callable


class MAVSDKRuntimeAdapter:
    """Provide the mission helper API expected by generated code."""

    def __init__(self, system: Any) -> None:
        self._system = system
        self.action = system.action
        self.core = system.core
        self.telemetry = system.telemetry
        self._geofence_radius_m = 500.0
        self._geofence_max_alt_m = 120.0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._system, name)

    async def check_pre_flight(self) -> Any:
        health = await self._wait_for_health()
        gps = await self._sample(self.telemetry.gps_info)
        battery = await self.get_battery()
        battery_percent = self._battery_percent(getattr(battery, "remaining_percent", 0.0))
        passed = bool(
            getattr(health, "is_global_position_ok", False)
            and getattr(health, "is_home_position_ok", False)
            and getattr(gps, "num_satellites", 0) >= 6
            and battery_percent >= 20.0
        )
        return SimpleNamespace(
            passed=passed,
            gps_satellites=getattr(gps, "num_satellites", 0),
            battery_percent=battery_percent,
        )

    async def get_battery(self) -> Any:
        battery = await self._sample(self.telemetry.battery)
        remaining = getattr(battery, "remaining_percent", None)
        if remaining is None:
            return battery

        normalized = self._battery_percent(float(remaining))
        try:
            setattr(battery, "remaining_percent", normalized)
            return battery
        except Exception:
            attrs: dict[str, Any] = {}
            if hasattr(battery, "__dict__"):
                attrs.update(vars(battery))
            for name in ("voltage_v", "remaining_percent"):
                if hasattr(battery, name):
                    attrs[name] = getattr(battery, name)
            attrs["remaining_percent"] = normalized
            return SimpleNamespace(**attrs)

    async def set_geofence(self, radius_m: float, max_alt_m: float) -> bool:
        self._geofence_radius_m = radius_m
        self._geofence_max_alt_m = max_alt_m
        return True

    async def set_return_to_launch_alt(self, alt_m: float) -> bool:
        await self.action.set_return_to_launch_altitude(alt_m)
        return True

    async def takeoff(self, altitude_m: float) -> bool:
        health = await self._wait_for_health(timeout_s=40.0)
        if not (
            getattr(health, "is_global_position_ok", False)
            and getattr(health, "is_home_position_ok", False)
        ):
            raise RuntimeError("Vehicle health is not ready for takeoff")
        await self.action.set_takeoff_altitude(altitude_m)
        await self._arm_with_retries()
        await self.action.takeoff()
        await self._wait_for_altitude(min_alt_m=max(1.0, altitude_m * 0.5), timeout_s=30.0)
        return True

    async def fly_to_relative(self, north_m: float, east_m: float, down_m: float) -> bool:
        position = await self._sample(self.telemetry.position)
        target_rel_alt_m = max(
            2.0,
            min(
                self._geofence_max_alt_m,
                float(getattr(position, "relative_altitude_m", 0.0)) - down_m,
            ),
        )
        target_lat, target_lon = self._offset_lat_lon(
            float(getattr(position, "latitude_deg", 0.0)),
            float(getattr(position, "longitude_deg", 0.0)),
            north_m,
            east_m,
        )
        absolute_alt_m = float(getattr(position, "absolute_altitude_m", 0.0))
        current_rel_alt_m = float(getattr(position, "relative_altitude_m", 0.0))
        target_abs_alt_m = absolute_alt_m + (target_rel_alt_m - current_rel_alt_m)

        await self.action.goto_location(target_lat, target_lon, target_abs_alt_m, 0.0)
        await self._wait_for_position(
            target_lat,
            target_lon,
            target_rel_alt_m,
            timeout_s=max(25.0, math.hypot(north_m, east_m) / 3.0),
        )
        return True

    async def land(self) -> bool:
        await self.action.land()
        await self._wait_for_altitude(max_alt_m=0.5, timeout_s=45.0)
        return True

    async def emergency_stop(self) -> bool:
        try:
            await self.action.kill()
        except Exception:
            await self.action.land()
        return True

    async def return_to_launch(self) -> bool:
        await self.action.return_to_launch()
        await self._wait_for_altitude(max_alt_m=0.5, timeout_s=60.0)
        return True

    async def _arm_with_retries(self, retries: int = 4, delay_s: float = 2.0) -> None:
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                await self.action.arm()
                return
            except Exception as exc:
                last_error = exc
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(delay_s)
                await self._wait_for_health(timeout_s=10.0)
        if last_error is not None:
            raise last_error

    async def _wait_for_health(self, timeout_s: float = 25.0) -> Any:
        deadline = time.monotonic() + timeout_s
        iterator = self.telemetry.health().__aiter__()
        last = SimpleNamespace(is_global_position_ok=False, is_home_position_ok=False)
        try:
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    last = await asyncio.wait_for(iterator.__anext__(), timeout=min(2.0, remaining))
                except asyncio.TimeoutError:
                    continue
                if getattr(last, "is_global_position_ok", False) and getattr(
                    last, "is_home_position_ok", False
                ):
                    return last
        finally:
            await self._close_iterator(iterator)
        return last

    async def _wait_for_altitude(
        self,
        *,
        min_alt_m: float | None = None,
        max_alt_m: float | None = None,
        timeout_s: float,
    ) -> Any:
        predicate: Callable[[Any], bool]
        if min_alt_m is not None:

            def predicate(pos: Any) -> bool:
                return float(getattr(pos, "relative_altitude_m", 0.0)) >= min_alt_m
        else:

            def predicate(pos: Any) -> bool:
                return float(getattr(pos, "relative_altitude_m", 0.0)) <= (max_alt_m or 0.0)

        return await self._wait_for_position_sample(predicate, timeout_s)

    async def _wait_for_position(
        self,
        target_lat: float,
        target_lon: float,
        target_rel_alt_m: float,
        *,
        timeout_s: float,
    ) -> Any:
        def reached(position: Any) -> bool:
            rel_alt_m = float(getattr(position, "relative_altitude_m", 0.0))
            lat = float(getattr(position, "latitude_deg", 0.0))
            lon = float(getattr(position, "longitude_deg", 0.0))
            horizontal_m = self._horizontal_distance_m(lat, lon, target_lat, target_lon)
            return horizontal_m <= 4.0 and abs(rel_alt_m - target_rel_alt_m) <= 1.5

        return await self._wait_for_position_sample(reached, timeout_s)

    async def _wait_for_position_sample(
        self, predicate: Callable[[Any], bool], timeout_s: float
    ) -> Any:
        deadline = time.monotonic() + timeout_s
        iterator = self.telemetry.position().__aiter__()
        last = None
        try:
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    last = await asyncio.wait_for(iterator.__anext__(), timeout=min(2.0, remaining))
                except asyncio.TimeoutError:
                    continue
                if predicate(last):
                    return last
        finally:
            await self._close_iterator(iterator)
        return last

    async def _sample(self, stream_factory: Callable[[], Any], timeout_s: float = 5.0) -> Any:
        iterator = stream_factory().__aiter__()
        try:
            return await asyncio.wait_for(iterator.__anext__(), timeout=timeout_s)
        finally:
            await self._close_iterator(iterator)

    @staticmethod
    def _battery_percent(remaining_percent: float) -> float:
        value = float(remaining_percent)
        return value if value > 1.0 else value * 100.0

    @staticmethod
    async def _close_iterator(iterator: Any) -> None:
        close_fn = getattr(iterator, "aclose", None)
        if not callable(close_fn):
            return
        try:
            result = close_fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    @staticmethod
    def _offset_lat_lon(
        lat_deg: float, lon_deg: float, north_m: float, east_m: float
    ) -> tuple[float, float]:
        lat_offset = north_m / 111_111.0
        lon_scale = max(math.cos(math.radians(lat_deg)), 0.01)
        lon_offset = east_m / (111_111.0 * lon_scale)
        return lat_deg + lat_offset, lon_deg + lon_offset

    @staticmethod
    def _horizontal_distance_m(
        lat_deg: float,
        lon_deg: float,
        target_lat_deg: float,
        target_lon_deg: float,
    ) -> float:
        north_m = (target_lat_deg - lat_deg) * 111_111.0
        avg_lat = math.radians((lat_deg + target_lat_deg) / 2.0)
        east_m = (target_lon_deg - lon_deg) * 111_111.0 * math.cos(avg_lat)
        return math.hypot(north_m, east_m)
