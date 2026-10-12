"""eljazari.ai — Phase 6: Component catalog loader.

Reads the JSON files under ``data/components/`` and exposes them as in-memory,
indexed-by-part-number caches of validated Pydantic models.

Designed so it can later be swapped for an HTTP-backed catalog (eCalc API,
manufacturer database) without changing the agent code that consumes it.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from agents.component_models import (
    BatteryComponent,
    ESCComponent,
    FlightControllerComponent,
    FrameComponent,
    MotorComponent,
    PropellerComponent,
    PropulsionTestCase,
    SelectedComponents,
)
from config.paths import PROJECT_ROOT
from config.settings import settings


# Order matters: when we infer the catalog root we look in these locations.
def _candidate_catalog_dirs() -> list[Path]:
    here = Path(__file__).resolve()
    override = getattr(settings, "components_catalog_path", "") or ""
    candidates: list[Path] = []
    if override:
        candidates.append((PROJECT_ROOT / override).resolve())
    candidates.extend(
        [
            PROJECT_ROOT / "data" / "components",  # repository or container data
            here.parents[2] / "data" / "components",  # apps/ / data
            here.parents[1]
            / "data"
            / "components",  # apps/api / data (when api/ is checked out alone)
            Path.cwd() / "data" / "components",  # cwd / data
        ]
    )
    return candidates


def _catalog_root() -> Path:
    for candidate in _candidate_catalog_dirs():
        # Skip empty paths and only accept directories that actually contain
        # the catalog files we need.
        if not candidate or str(candidate) in ("", "."):
            continue
        if candidate.is_dir() and (candidate / "motors.json").is_file():
            return candidate
    raise FileNotFoundError(
        "No components/ directory with motors.json found. Looked at: "
        + ", ".join(str(c) for c in _candidate_catalog_dirs() if str(c))
    )


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


class ComponentCatalog:
    """In-memory, validated component catalog.

    Construction is cheap (no I/O after the JSON files are read once).
    Use the module-level :func:`get_catalog` to share a single instance.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _catalog_root()
        self.motors: list[MotorComponent] = self._load_motors()
        self.propellers: list[PropellerComponent] = self._load_propellers()
        self.batteries: list[BatteryComponent] = self._load_batteries()
        self.escs: list[ESCComponent] = self._load_escs()
        self.flight_controllers: list[FlightControllerComponent] = self._load_flight_controllers()
        self.frames: list[FrameComponent] = self._load_frames()
        self.propulsion_tests: list[PropulsionTestCase] = self._load_propulsion_tests()

    # ── loaders ───────────────────────────────────────────────────────────
    def _load_motors(self) -> list[MotorComponent]:
        return [MotorComponent(**d) for d in _load(self.root / "motors.json")]

    def _load_propellers(self) -> list[PropellerComponent]:
        return [PropellerComponent(**d) for d in _load(self.root / "propellers.json")]

    def _load_batteries(self) -> list[BatteryComponent]:
        return [BatteryComponent(**d) for d in _load(self.root / "batteries.json")]

    def _load_escs(self) -> list[ESCComponent]:
        return [ESCComponent(**d) for d in _load(self.root / "escs.json")]

    def _load_flight_controllers(self) -> list[FlightControllerComponent]:
        return [
            FlightControllerComponent(**d) for d in _load(self.root / "flight_controllers.json")
        ]

    def _load_frames(self) -> list[FrameComponent]:
        return [FrameComponent(**d) for d in _load(self.root / "frames.json")]

    def _load_propulsion_tests(self) -> list[PropulsionTestCase]:
        path = self.root / "propulsion_tests.json"
        if not path.is_file():
            return []
        tests = [PropulsionTestCase(**d) for d in json.loads(path.read_text("utf-8"))["tests"]]
        for test in tests:
            # A test must name catalog parts and one voltage; fail at load, not
            # when a design happens to reach the combination.
            self.motor(test.motor_part_number)
            self.propeller(test.propeller_part_number)
            if any(p.voltage_v != test.test_voltage_v for p in test.points):
                raise ValueError(f"Propulsion test mixes voltages: {test.source_reference}")
        return tests

    # ── queries ───────────────────────────────────────────────────────────
    def motor(self, part_number: str) -> MotorComponent:
        return self._lookup(self.motors, part_number, "motor")

    def propeller(self, part_number: str) -> PropellerComponent:
        return self._lookup(self.propellers, part_number, "propeller")

    def battery(self, part_number: str) -> BatteryComponent:
        return self._lookup(self.batteries, part_number, "battery")

    def esc(self, part_number: str) -> ESCComponent:
        return self._lookup(self.escs, part_number, "esc")

    def flight_controller(self, part_number: str) -> FlightControllerComponent:
        return self._lookup(self.flight_controllers, part_number, "flight_controller")

    def frame(self, part_number: str) -> FrameComponent:
        return self._lookup(self.frames, part_number, "frame")

    @staticmethod
    def _lookup(items: list, part_number: str, kind: str):
        for it in items:
            if it.part_number == part_number:
                return it
        raise KeyError(f"{kind} not in catalog: {part_number!r}")

    def propulsion_test(
        self, motor_part_number: str, propeller_part_number: str, voltage_v: float
    ) -> PropulsionTestCase | None:
        """The published test of exactly this motor and propeller at this voltage."""
        from pipelines.propulsion_evidence import VOLTAGE_ABS_TOLERANCE_V

        for test in self.propulsion_tests:
            if (
                test.motor_part_number == motor_part_number
                and test.propeller_part_number == propeller_part_number
                and abs(test.test_voltage_v - voltage_v) <= VOLTAGE_ABS_TOLERANCE_V
            ):
                return test
        return None

    def with_propulsion_test(self, components: SelectedComponents) -> SelectedComponents:
        """Give the motor the curve measured with this propeller at this battery's voltage.

        Unchanged when no such test exists; the motor's own curve then describes
        another condition and the solver reports it as unsourced for this pairing.
        """
        test = self.propulsion_test(
            components.motor.part_number,
            components.propeller.part_number,
            components.battery.pack_voltage_nominal_v,
        )
        if test is None:
            return components
        motor = components.motor.model_copy(
            update={"thrust_curve": list(test.points), "thrust_curve_evidence": test.declaration()}
        )
        return components.model_copy(update={"motor": motor})

    # ── filters used by selector ──────────────────────────────────────────
    def motors_for_cells(self, cells_s: int) -> list[MotorComponent]:
        return [m for m in self.motors if cells_s in m.recommended_voltage_cells_s]

    def propellers_for_motor(self, motor: MotorComponent) -> list[PropellerComponent]:
        """Heuristic shaft-fit + size compatibility filter."""
        return [p for p in self.propellers if abs(p.hub_id_mm - motor.shaft_diameter_mm) < 1.0]

    def escs_for_motor(self, motor: MotorComponent, motor_count: int) -> list[ESCComponent]:
        """Return ESCs whose per-channel rating exceeds the motor's continuous draw
        with at least 20% headroom and whose channel count matches motor_count.
        """
        required_a = motor.max_continuous_current_a * 1.2
        return [
            e
            for e in self.escs
            if e.channels in (motor_count, 1)
            and e.max_continuous_current_per_channel_a >= required_a
        ]

    def batteries_for_cells(self, cells_s: int) -> list[BatteryComponent]:
        return [b for b in self.batteries if b.cells_s == cells_s]

    def frames_for_wheelbase(
        self, wheelbase_mm: float, tolerance_pct: float = 30.0
    ) -> list[FrameComponent]:
        lo = wheelbase_mm * (1.0 - tolerance_pct / 100.0)
        hi = wheelbase_mm * (1.0 + tolerance_pct / 100.0)
        return [f for f in self.frames if lo <= f.wheelbase_mm <= hi]


@lru_cache(maxsize=1)
def get_catalog() -> ComponentCatalog:
    """Return the shared, lazily-loaded catalog instance."""
    return ComponentCatalog()


def reset_catalog_cache() -> None:
    """For tests that need to reload after touching disk."""
    get_catalog.cache_clear()  # type: ignore[attr-defined]
