"""eljazari.ai — Phase 6: ComponentSelectorAgent.

Enumerate catalog combinations under voltage, frame-size, shaft-fit and ESC
filters, then apply the preliminary performance solver. Return up to three
unique feasible trade-offs: best score, lowest known cost and longest estimated
cruise endurance. A bounded search fails explicitly rather than returning a
partial result as a completed search. Catalog estimates are not flight validation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from itertools import product
from typing import Any

from agents.component_models import (
    RealPerformanceReport,
    SelectedComponents,
)
from agents.design_models import DroneDesignTree
from pipelines.performance_solver import PerformanceSolver
from pipelines.propulsion_evidence import curve_matches_battery
from pipelines.resolved_drone import SUPPORTED_MULTIROTORS
from services.component_catalog import ComponentCatalog, get_catalog

logger = logging.getLogger(__name__)


# Motor counts inferred from topology.
_TOPOLOGY_MOTOR_COUNT = {
    "tricopter": 3,
    "quadcopter": 4,
    "pentacopter": 5,
    "hexacopter": 6,
    "y6": 6,
    "octocopter": 8,
    "x8": 8,
}


@dataclass(frozen=True)
class SelectionTier:
    """One scored candidate BOM with its computed performance."""

    name: str  # economy | balanced | performance
    components: SelectedComponents
    performance: RealPerformanceReport
    score: float  # 0-100, higher = better fit


@dataclass(frozen=True)
class SelectionResult:
    """Final output: a chosen tier + the alternatives."""

    chosen: SelectionTier
    alternatives: list[SelectionTier]
    evaluated_candidates: int = 0
    feasible_candidates: int = 0
    # Feasible candidates whose thrust curve is a published test of that exact
    # motor, propeller and voltage. When any exist, only they are offered.
    sourced_candidates: int = 0
    # Choices the search made that the design did not state (a wider frame search).
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated_candidates": self.evaluated_candidates,
            "feasible_candidates": self.feasible_candidates,
            "sourced_candidates": self.sourced_candidates,
            "notes": list(self.notes),
            "chosen": _tier_to_dict(self.chosen),
            "alternatives": [_tier_to_dict(t) for t in self.alternatives],
        }


def _tier_to_dict(tier: SelectionTier) -> dict[str, Any]:
    return {
        "name": tier.name,
        "score": tier.score,
        "components": tier.components.model_dump(),
        "performance": tier.performance.model_dump(),
        "bom_summary": {
            "motor": f"{tier.components.motor.manufacturer} {tier.components.motor.model_name} × {tier.components.motor_count}",
            "propeller": f"{tier.components.propeller.manufacturer} {tier.components.propeller.model_name}",
            "battery": f"{tier.components.battery.manufacturer} {tier.components.battery.model_name}",
            "esc": f"{tier.components.esc.manufacturer} {tier.components.esc.model_name}",
            "fc": f"{tier.components.flight_controller.manufacturer} {tier.components.flight_controller.model_name}",
            "frame": f"{tier.components.frame.manufacturer} {tier.components.frame.model_name}",
            "total_weight_g": tier.components.total_bom_weight_g,
            "total_price_usd": tier.components.total_bom_price_usd,
        },
    }


@dataclass
class _Search:
    """What one pass over a set of frames found."""

    candidates: list[SelectionTier] = field(default_factory=list)
    evaluated: int = 0
    best_endurance: float = 0.0
    otherwise_feasible: int = 0
    voltage_mismatch: int = 0
    over_mass: int = 0

    @property
    def sourced(self) -> list[SelectionTier]:
        return [
            t
            for t in self.candidates
            if t.performance.propulsion_evidence.status == "matched_declaration"
        ]


class ComponentSelectorAgent:
    """Pick real components matching a design tree + mission constraints."""

    def __init__(
        self,
        catalog: ComponentCatalog | None = None,
        solver: PerformanceSolver | None = None,
        max_candidates: int = 50000,
    ) -> None:
        self.catalog = catalog or get_catalog()
        self.solver = solver or PerformanceSolver()
        if max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        self.max_candidates = max_candidates

    # ── public API ────────────────────────────────────────────────────────
    def select(
        self,
        design: DroneDesignTree,
        payload_g: float = 0.0,
        min_endurance_min: float = 5.0,
        require_curve_voltage_match: bool = True,
        max_total_mass_g: float | None = None,
    ) -> SelectionResult:
        """Return only feasible candidates; there may be fewer than three tiers.

        Each candidate gets the curve published for its exact motor, propeller and
        battery voltage when one exists. If any feasible candidate is sourced that
        way, only sourced candidates are offered: generated-aircraft SITL flies
        documented propulsion data only, and an unsourced BOM would be blocked there.

        ``require_curve_voltage_match`` skips pairings whose thrust curve was
        recorded at another voltage than the battery's nominal. The generated
        flight model refuses those, so selecting one blocked the simulation of an
        otherwise supported request (docs/BENCHMARK.md, finding 3).

        The design's wheelbase is an estimate. Frames near it are searched first.
        When none of them carries a documented combination and another frame with
        the same arm count does, the search widens to those frames and ``notes``
        says so. Otherwise a supported request passed or failed on the design
        model's size guess: «a small quadcopter» got a 295 mm frame, only
        unsourced FPV motors fit it, and SITL refused the result (run 3, aborted).

        ``max_total_mass_g`` is the request's limit on the whole aircraft with its
        payload; heavier candidates are skipped, so a wider search can never pick an
        aircraft the request rules out.
        """
        for name, value in (("payload_g", payload_g), ("min_endurance_min", min_endurance_min)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if max_total_mass_g is not None and not (
            math.isfinite(max_total_mass_g) and max_total_mass_g > 0
        ):
            raise ValueError("max_total_mass_g must be finite and positive")
        if design.topology.lower() not in SUPPORTED_MULTIROTORS:
            # Before, an unknown topology defaulted to four motors (a fixed wing got
            # quadcopter parts) and a tricopter found «0 compatible candidates».
            kinds = [f"{name}s" for name in SUPPORTED_MULTIROTORS]
            raise RuntimeError(
                f"A {design.topology.replace('_', ' ')} is not supported: generated aircraft "
                f"are flat X {', '.join(kinds[:-1])} or {kinds[-1]}. Tricopters, bicopters and "
                "fixed wings need tilting rotors or control surfaces, and are refused rather "
                "than approximated"
            )
        motor_count = self._motor_count(design)
        cells_s = design.battery.cells
        near, others = self._frames(design, motor_count)
        if not near and not others:
            raise RuntimeError(f"No catalog frame has {motor_count} arms for a {design.topology}")
        args = (design, motor_count, cells_s, payload_g, min_endurance_min)
        search = self._search(
            near, *args, require_curve_voltage_match, max_total_mass_g, evaluated=0
        )
        notes: tuple[str, ...] = ()
        if not search.sourced and others:
            wider = self._search(
                others,
                *args,
                require_curve_voltage_match,
                max_total_mass_g,
                evaluated=search.evaluated,
            )
            if wider.sourced:
                frames = sorted({t.components.frame.part_number for t in wider.sourced})
                notes = (
                    "No documented motor, propeller and voltage test fits a frame near the "
                    f"design's {design.frame.arm_length_mm * 2.0:.0f} mm wheelbase; chose among "
                    f"other {motor_count}-arm frames that have one: {', '.join(frames)}",
                )
                search = wider
            else:
                # Report the failure over everything searched.
                search = _Search(
                    candidates=search.candidates,
                    evaluated=wider.evaluated,
                    best_endurance=max(search.best_endurance, wider.best_endurance),
                    otherwise_feasible=search.otherwise_feasible + wider.otherwise_feasible,
                    voltage_mismatch=search.voltage_mismatch + wider.voltage_mismatch,
                    over_mass=search.over_mass + wider.over_mass,
                )

        candidates = search.candidates
        if not candidates:
            reason = (
                f"best cruise endurance {search.best_endurance:.1f} min below required "
                f"{min_endurance_min:g} min"
                if search.otherwise_feasible
                else "not flyable or critical compatibility warnings; no otherwise feasible candidate"
            )
            if search.voltage_mismatch:
                reason += (
                    f"; {search.voltage_mismatch} skipped because the motor's thrust curve was "
                    "recorded at another voltage than the battery's"
                )
            if search.over_mass:
                reason += (
                    f"; {search.over_mass} skipped because the aircraft with its payload would "
                    f"exceed the requested {max_total_mass_g:g} g"
                )
            raise RuntimeError(
                f"No viable BOM found among {search.evaluated} compatible catalog candidates: "
                + reason
            )

        sourced = search.sourced
        feasible = len(candidates)
        if sourced:
            candidates = sourced

        # Represent three different trade-offs, never duplicate a BOM.
        balanced = max(candidates, key=lambda t: t.score)
        economy = min(candidates, key=lambda t: self._cost(t.components))
        performance = max(candidates, key=lambda t: (t.performance.cruise_endurance_min, t.score))
        tiers = []
        seen = set()
        for name, tier in (
            ("balanced", balanced),
            ("economy", economy),
            ("performance", performance),
        ):
            identity = self._identity(tier.components)
            if identity not in seen:
                tiers.append(replace(tier, name=name))
                seen.add(identity)
        tiers.sort(key=lambda t: t.score, reverse=True)
        return SelectionResult(tiers[0], tiers[1:], search.evaluated, feasible, len(sourced), notes)

    def _search(
        self,
        frames,
        design: DroneDesignTree,
        motor_count: int,
        cells_s: int,
        payload_g: float,
        min_endurance_min: float,
        require_curve_voltage_match: bool,
        max_total_mass_g: float | None,
        *,
        evaluated: int,
    ) -> _Search:
        """Evaluate every candidate on ``frames``; ``evaluated`` counts earlier passes."""
        found = _Search(evaluated=evaluated)
        for components in self._combinations(design, motor_count, cells_s, frames):
            if found.evaluated >= self.max_candidates:
                raise RuntimeError(
                    f"Catalog search limit reached after {found.evaluated} candidates; "
                    "search incomplete, no selection returned"
                )
            found.evaluated += 1
            if (
                max_total_mass_g is not None
                and components.total_bom_weight_g + payload_g > max_total_mass_g
            ):
                found.over_mass += 1
                continue
            components = self.catalog.with_propulsion_test(components)
            if require_curve_voltage_match and not curve_matches_battery(
                components.motor.thrust_curve, components.battery.pack_voltage_nominal_v
            ):
                found.voltage_mismatch += 1
                continue
            perf = self.solver.solve(components, extra_mass_g=payload_g)
            if not perf.is_flyable or any(w.startswith("[critical]") for w in perf.warnings):
                continue
            found.otherwise_feasible += 1
            found.best_endurance = max(found.best_endurance, perf.cruise_endurance_min)
            if perf.cruise_endurance_min < min_endurance_min:
                continue
            found.candidates.append(
                SelectionTier(
                    name="balanced",
                    components=components,
                    performance=perf,
                    score=self._score(components, perf, min_endurance_min),
                )
            )
        return found

    def _frames(self, design: DroneDesignTree, motor_count: int) -> tuple[list, list]:
        """Frames with the design's arm count: (near its wheelbase, the rest).

        When none of that arm count is near, the nearest one counts as near, since
        the design's arm length is an estimate and the frame is real.
        """
        wheelbase = design.frame.arm_length_mm * 2.0
        fitting = [f for f in self.catalog.frames if f.arm_count == motor_count]
        near = [f for f in self.catalog.frames_for_wheelbase(wheelbase) if f in fitting]
        if not near and fitting:
            near = [min(fitting, key=lambda f: abs(f.wheelbase_mm - wheelbase))]
        others = [f for f in fitting if f not in near]
        key = lambda frame: frame.part_number  # noqa: E731
        return sorted(near, key=key), sorted(others, key=key)

    @staticmethod
    def _identity(components: SelectedComponents) -> tuple[str, ...]:
        return tuple(
            getattr(components, kind).part_number
            for kind in ("motor", "propeller", "battery", "esc", "flight_controller", "frame")
        )

    @staticmethod
    def _cost(components: SelectedComponents) -> float:
        # Missing prices cannot make a BOM look like the cheapest known option.
        if any(
            getattr(components, kind).price_usd is None
            for kind in ("motor", "propeller", "battery", "esc", "flight_controller", "frame")
        ):
            return math.inf
        return components.total_bom_price_usd

    def _combinations(self, design, motor_count, cells_s, frames):
        """Enumerate catalog candidates on ``frames`` under the compatibility filters.

        Shaft fit remains a catalog heuristic; this is not mechanical validation.
        Sorting makes equal-score choices independent of catalog file order.
        """

        def ordered(items):
            return sorted(items, key=lambda item: item.part_number)

        batteries = ordered(self.catalog.batteries_for_cells(cells_s))
        fcs = ordered(
            fc for fc in self.catalog.flight_controllers if fc.supports_px4 and fc.mavsdk_compatible
        )
        frames = ordered(frames)
        for motor in ordered(self.catalog.motors_for_cells(cells_s)):
            escs = ordered(
                e
                for e in self.catalog.escs_for_motor(motor, motor_count)
                if e.min_cells_s <= cells_s <= e.max_cells_s
            )
            for frame in frames:
                # The structural model's own clearance rule: adjacent rotors on a
                # regular N-arm frame are 2*arm*sin(pi/N) apart. A frame's stated
                # maximum propeller does not guarantee it (Tarot 680 hex: 17").
                clearance_m = 2 * frame.arm_length_mm / 1000 * math.sin(math.pi / motor_count)
                props = ordered(
                    p
                    for p in self.catalog.propellers_for_motor(motor)
                    if p.diameter_inch <= frame.max_prop_size_inch
                    and p.diameter_inch * 0.0254 < clearance_m
                )
                for prop, battery, esc, fc in product(props, batteries, escs, fcs):
                    yield SelectedComponents(
                        motor=motor,
                        propeller=prop,
                        battery=battery,
                        esc=esc,
                        flight_controller=fc,
                        frame=frame,
                        motor_count=motor_count,
                    )

    # ── scoring ───────────────────────────────────────────────────────────
    def _score(
        self,
        components: SelectedComponents,
        perf: RealPerformanceReport,
        min_endurance_min: float,
    ) -> float:
        if not perf.is_flyable:
            return 0.0
        # 0-100. Higher = better fit.
        # Components:
        #  - T:W headroom above 1.5     (up to 30)
        #  - Endurance vs requirement   (up to 30)
        #  - Number of warnings         (subtractive)
        #  - Cost-effectiveness         (up to 15 for economy, 0 for performance)
        #  - Saturation headroom        (up to 25)
        score = 0.0
        score += min(30.0, max(0.0, (perf.thrust_to_weight_ratio - 1.5) * 30.0))
        if min_endurance_min > 0:
            endurance_ratio = perf.cruise_endurance_min / min_endurance_min
            score += min(30.0, endurance_ratio * 15.0)
        else:
            score += 15.0
        score += min(25.0, perf.motor_saturation_headroom_pct * 0.25)
        warning_penalty = sum(10.0 if w.startswith("[critical]") else 3.0 for w in perf.warnings)
        score -= warning_penalty
        if components.total_bom_price_usd > 0:
            score += max(0.0, 15.0 - components.total_bom_price_usd / 50.0)
        return round(max(0.0, min(100.0, score)), 1)

    @staticmethod
    def _motor_count(design: DroneDesignTree) -> int:
        n = _TOPOLOGY_MOTOR_COUNT.get(design.topology.lower(), 4)
        if design.propulsion:
            # Trust explicit motor list if provided.
            n = max(n, len(design.propulsion))
        return n
