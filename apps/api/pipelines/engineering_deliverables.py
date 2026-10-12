"""eljazari.ai — Phase 14: Field-ready engineering deliverables.

The orchestrator's job ends with a passing simulation, a graded report,
and a URDF on disk. But a real drone engineer also needs:

  • A Bill of Materials they can hand to procurement
  • An assembly guide so the build itself doesn't crash on day one
  • A pre-flight checklist for first power-up
  • A maintenance schedule keyed to motor hours
  • A failure-mode analysis (FMEA) summary for safety review

This module produces those deliverables from the orchestrator's already-
computed artifacts (design tree, component list, flight quality report,
PID tune results, failure-injection passes). Output is plain Markdown +
CSV — no PDF rendering dependency in CI — so the engineering bundle is
deterministic, diff-able, and immediately viewable on GitHub.

Markdown was chosen over PDF because:
  • Markdown renders natively on GitHub, GitLab, and most enterprise wikis
  • Conversion to PDF is one CLI call (pandoc / weasyprint) when needed,
    so we don't lock the pipeline to a heavy native dependency
  • Diff-friendly: a procurement engineer can review changes the same
    way developers review code
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from agents.component_models import SelectedComponents


# ─────────────────────────────────────────────────────────────────────────────
# Input bundle — the orchestrator hands us this
# ─────────────────────────────────────────────────────────────────────────────


class ComponentLine(BaseModel):
    """One line in the BOM. Either a selected real part or a spec placeholder."""

    model_config = ConfigDict(frozen=False)

    category: str
    part_name: str
    qty: int = Field(1, ge=1)
    supplier: str = ""
    part_number: str = ""
    unit_price_usd: float = Field(0.0, ge=0.0)
    weight_g: float = Field(0.0, ge=0.0)
    notes: str = ""

    @property
    def line_total_usd(self) -> float:
        return round(self.unit_price_usd * self.qty, 2)


def component_lines_from_selection(selected: SelectedComponents) -> list[ComponentLine]:
    """Export the exact selected parts; weights and prices are per unit."""
    parts = [
        ("Frame", selected.frame, 1),
        ("Motor", selected.motor, selected.motor_count),
        ("Propeller", selected.propeller, selected.motor_count),
        ("ESC", selected.esc, selected.esc_quantity),
        ("Flight controller", selected.flight_controller, 1),
        ("Battery", selected.battery, 1),
    ]
    return [
        ComponentLine(
            category=category,
            part_name=f"{part.manufacturer} {part.model_name}",
            part_number=part.part_number,
            qty=quantity,
            supplier=part.url or "",
            unit_price_usd=part.price_usd or 0.0,
            weight_g=part.weight_g,
            notes="Price unavailable; excluded from cost estimate"
            if part.price_usd is None
            else "",
        )
        for category, part, quantity in parts
    ]


class DeliverableBundle(BaseModel):
    """Everything the deliverables generator needs from the orchestrator."""

    model_config = ConfigDict(frozen=False)

    project_id: str
    project_name: str
    mission_prompt: str

    # Design output
    frame_type: str = ""
    arm_length_mm: float = 0.0
    total_weight_g: float = 0.0
    battery_summary: str = ""
    engineering_notes: list[str] = Field(default_factory=list)

    # Components — produced by Phase 6 selector
    components: list[ComponentLine] = Field(default_factory=list)

    # Performance — produced by Phase 11
    flight_quality_score: float | None = None
    flight_quality_grade: str | None = None
    position_rmse_m: float = 0.0
    altitude_rmse_m: float = 0.0
    tracking_evaluated: bool = False
    altitude_tracking_evaluated: bool = False
    return_to_home_error_m: float = 0.0
    hover_endurance_min: float | None = None
    max_speed_ms: float | None = None

    # Tuning — produced by Phase 10
    pid_tune_iterations: int = 0
    pid_final_gains_summary: str = ""

    # Failure resilience — produced by Phase 13
    failure_scenarios_passed: int = 0
    failure_scenarios_total: int = 0

    # Environment robustness — produced by Phase 9
    tested_scenarios: list[str] = Field(default_factory=list)

    # File paths produced earlier in the pipeline
    urdf_path: str = ""
    sdf_path: str = ""
    mission_code_path: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Bill of Materials
# ─────────────────────────────────────────────────────────────────────────────


def render_bom_csv(bundle: DeliverableBundle) -> str:
    """Render the BOM as RFC-4180-compliant CSV for spreadsheet import."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "Category",
            "Part Name",
            "Qty",
            "Supplier",
            "Part Number",
            "Unit Price (USD)",
            "Line Total (USD)",
            "Weight (g)",
            "Notes",
        ]
    )
    for line in bundle.components:
        writer.writerow(
            [
                line.category,
                line.part_name,
                line.qty,
                line.supplier,
                line.part_number,
                f"{line.unit_price_usd:.2f}",
                f"{line.line_total_usd:.2f}",
                f"{line.weight_g:.1f}",
                line.notes,
            ]
        )
    # Summary row
    total_cost = sum(line.line_total_usd for line in bundle.components)
    total_weight = sum(line.weight_g * line.qty for line in bundle.components)
    writer.writerow([])
    writer.writerow(["", "TOTAL", "", "", "", "", f"{total_cost:.2f}", f"{total_weight:.1f}", ""])
    return buf.getvalue()


def render_bom_markdown(bundle: DeliverableBundle) -> str:
    """Render the BOM as a Markdown table."""
    lines = [
        f"# Bill of Materials — {bundle.project_name}",
        "",
        f"**Project ID:** `{bundle.project_id}`",
        f"**Mission:** {bundle.mission_prompt}",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}Z",
        "",
        "| Category | Part | Qty | Supplier | Part # | Unit (USD) | Line Total | Weight (g) |",
        "| --- | --- | ---: | --- | --- | ---: | ---: | ---: |",
    ]
    for line in bundle.components:
        lines.append(
            f"| {line.category} | {line.part_name} | {line.qty} | "
            f"{line.supplier or '—'} | {line.part_number or '—'} | "
            f"{line.unit_price_usd:.2f} | {line.line_total_usd:.2f} | "
            f"{line.weight_g:.1f} |"
        )
    total_cost = sum(line.line_total_usd for line in bundle.components)
    total_weight = sum(line.weight_g * line.qty for line in bundle.components)
    lines.extend(
        [
            f"| | | | | | | **{total_cost:.2f}** | **{total_weight:.1f}** |",
            "",
            f"**Total component cost:** ${total_cost:.2f}",
            f"**Total dry weight:** {total_weight:.1f} g",
        ]
    )
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Assembly guide
# ─────────────────────────────────────────────────────────────────────────────

#: Canonical assembly order for multicopters — same for every quad/hex/octo.
_ASSEMBLY_STEPS = [
    (
        "Frame",
        [
            "Inspect all carbon fiber arms and main plates for cracks or delamination.",
            "Apply thread-locker to all M3 frame screws (medium strength, blue).",
            "Tighten arm clamp bolts to manufacturer torque spec (typically 2 Nm).",
        ],
    ),
    (
        "Motor mounting",
        [
            "Identify motor rotation direction per the design (CW/CCW labels on arms).",
            "Bolt each motor to its arm using stainless M3 screws shorter than the motor's mounting plate thickness (avoid stator interference).",
            "Route motor leads through the arm cavity, leaving ~3 cm of slack at the ESC end.",
        ],
    ),
    (
        "ESC installation",
        [
            "Mount ESCs centrally on each arm or under the top plate as per the design layout.",
            "Solder motor leads to ESC outputs; do NOT match colour order — match the motor's required rotation by swapping any two of the three leads.",
            "Heat-shrink every solder joint individually.",
        ],
    ),
    (
        "Flight controller",
        [
            "Mount the FC on vibration-isolating foam tape, oriented arrow-forward.",
            "Connect ESC signal wires to the FC's motor outputs in the correct order (per the airframe config: M1=front-right, M2=back-left, etc.).",
            "Connect the GPS module via I2C/UART; align the compass arrow with the FC's forward arrow.",
        ],
    ),
    (
        "Power distribution",
        [
            "Solder the XT60/XT90 connector to the PDB with the correct polarity (red=+, black=−).",
            "Connect the PDB to each ESC and to the FC's BEC input.",
            "Smoke-test by powering the build via a current-limited bench supply (3 A limit) BEFORE plugging in a real battery.",
        ],
    ),
    (
        "Radio & telemetry",
        [
            "Bind the receiver to the transmitter following the brand-specific procedure.",
            "Verify all 4 sticks + AUX channels register correctly in the FC ground station.",
            "Mount the telemetry radio with its antenna at 90° to the FC IMU axis.",
        ],
    ),
    (
        "Calibration",
        [
            "Level the airframe on a flat surface and run accelerometer calibration.",
            "Rotate through 6 axes to complete the calibration cycle.",
            "Run compass calibration outdoors, away from metal structures.",
            "Run ESC calibration via FC ground station; verify all motors arm together.",
        ],
    ),
    (
        "Propellers",
        [
            "**Install propellers LAST**, after every other step is verified.",
            "Match prop direction to motor direction (CW props on CW motors).",
            "Hand-test motor rotation by arming on the bench without props first.",
        ],
    ),
]


def render_assembly_guide(bundle: DeliverableBundle) -> str:
    """Render a Markdown assembly guide tailored to the bundle's design."""
    lines = [
        f"# Assembly Guide — {bundle.project_name}",
        "",
        "> **Safety first.** Always work with the battery disconnected. "
        "Wear eye protection during ESC calibration and arming tests. "
        "Propellers go on LAST, after every other step is verified.",
        "",
        "## Pre-build checklist",
        "",
        f"- Frame: `{bundle.frame_type or 'X-quad'}`, arm length `{bundle.arm_length_mm:.0f} mm`",
        f"- Estimated takeoff mass: `{bundle.total_weight_g:.0f} g`",
        f"- Battery: `{bundle.battery_summary or '4S LiPo'}`",
        "- All BOM items received and weighed (see `bom.csv`)",
        f"- URDF reviewed: `{bundle.urdf_path or '—'}`",
        "",
    ]
    lines.extend(f"- {note}" for note in bundle.engineering_notes)
    for i, (title, steps) in enumerate(_ASSEMBLY_STEPS, start=1):
        lines.append(f"## Step {i} — {title}")
        lines.append("")
        for s in steps:
            lines.append(f"- {s}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight checklist
# ─────────────────────────────────────────────────────────────────────────────

_PREFLIGHT_ITEMS = [
    (
        "Vehicle",
        [
            "Frame inspection: no cracks, no loose bolts.",
            "Props installed correctly (CW/CCW match) and torqued.",
            "Battery secured with hook-and-loop straps + safety strap.",
            "Centre of gravity within ±5 mm of the design CG.",
        ],
    ),
    (
        "Avionics",
        [
            "FC arrow points forward.",
            "Compass cal validated (yaw error < 10°).",
            "Accel cal validated (level on flat surface, no roll/pitch readout > 1°).",
            "All ESCs respond to throttle hand-test.",
            "Telemetry link RSSI > 90 % at 5 m.",
        ],
    ),
    (
        "GPS / radio",
        [
            "Outdoor: ≥ 12 satellites, HDOP < 1.2.",
            "Indoor: optical-flow lock confirmed.",
            "Failsafe armed: RTL on signal loss, EMERGENCY LAND on battery critical.",
            "Geofence loaded and verified on map.",
        ],
    ),
    (
        "Crew & site",
        [
            "Pilot has visual line of sight clear to the planned waypoints.",
            "No people, vehicles, or critical infrastructure within the geofence.",
            "Wind speed < 70 % of vehicle max ground speed (measured with an anemometer).",
            "No precipitation, no temperature outside the battery's operating range.",
            "Notams checked; airspace clear of crewed traffic.",
        ],
    ),
]


def render_preflight_checklist(bundle: DeliverableBundle) -> str:
    lines = [
        f"# Pre-flight Checklist — {bundle.project_name}",
        "",
        "> Tick every item before arming. If any item fails, do not fly.",
        "",
    ]
    for title, items in _PREFLIGHT_ITEMS:
        lines.append(f"## {title}")
        lines.append("")
        for it in items:
            lines.append(f"- [ ] {it}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance schedule
# ─────────────────────────────────────────────────────────────────────────────

_MAINTENANCE_INTERVALS = [
    (
        "Every flight",
        [
            "Visual inspection of frame, props, motors.",
            "Battery voltage check (storage charge vs flight charge).",
            "Telemetry self-test before takeoff.",
        ],
    ),
    (
        "Every 10 flight hours",
        [
            "Motor bearing inspection — replace if rough.",
            "Prop balancing on a balancer; replace cracked or chipped props.",
            "Tighten all frame screws to torque spec.",
            "Inspect solder joints under magnification.",
        ],
    ),
    (
        "Every 50 flight hours",
        [
            "Replace motor bearings (preventative).",
            "Re-calibrate IMU and compass.",
            "Recalibrate ESC throttle range.",
            "Full BMS health log review.",
        ],
    ),
    (
        "Every 200 flight hours / annually",
        [
            "Replace ESC capacitors.",
            "Replace battery if cycle count > 200 or capacity < 80 %.",
            "Inspect airframe for fatigue under polarised light.",
        ],
    ),
]


def render_maintenance_schedule(bundle: DeliverableBundle) -> str:
    lines = [f"# Maintenance Schedule — {bundle.project_name}", ""]
    for interval, items in _MAINTENANCE_INTERVALS:
        lines.append(f"## {interval}")
        lines.append("")
        for it in items:
            lines.append(f"- {it}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Engineering summary report (top-level)
# ─────────────────────────────────────────────────────────────────────────────


def render_engineering_report(bundle: DeliverableBundle) -> str:
    """Top-level summary that ties every artefact together."""
    qual = bundle.flight_quality_grade or "—"
    score = (
        f"{bundle.flight_quality_score:.1f} / 100"
        if bundle.flight_quality_score is not None
        else "—"
    )
    fail_ratio = (
        f"{bundle.failure_scenarios_passed} / {bundle.failure_scenarios_total}"
        if bundle.failure_scenarios_total
        else "not run"
    )
    lines = [
        f"# Engineering Report — {bundle.project_name}",
        "",
        f"**Project ID:** `{bundle.project_id}`",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}Z",
        "",
        "## Mission",
        "",
        f"> {bundle.mission_prompt}",
        "",
        "## Verdict",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Flight quality grade | **{qual}** |",
        f"| Flight quality score | {score} |",
        f"| Position RMSE | {f'{bundle.position_rmse_m:.2f} m' if bundle.tracking_evaluated else 'not evaluated'} |",
        f"| Altitude RMSE | {f'{bundle.altitude_rmse_m:.2f} m' if bundle.altitude_tracking_evaluated else 'not evaluated'} |",
        f"| RTH closure error | {bundle.return_to_home_error_m:.2f} m |",
        f"| Hover endurance (estimated) | "
        f"{f'{bundle.hover_endurance_min:.1f} min' if bundle.hover_endurance_min is not None else '—'} |",
        f"| Max speed (estimated) | "
        f"{f'{bundle.max_speed_ms:.1f} m/s' if bundle.max_speed_ms is not None else '—'} |",
        f"| PID tune iterations | {bundle.pid_tune_iterations} |",
        f"| Failure-injection scenarios passed | {fail_ratio} |",
        f"| Environments tested | "
        f"{', '.join(bundle.tested_scenarios) if bundle.tested_scenarios else '—'} |",
        "",
        "## Design",
        "",
        *(f"- {note}" for note in bundle.engineering_notes),
        f"- Frame: `{bundle.frame_type or '—'}`",
        f"- Arm length: `{bundle.arm_length_mm:.1f} mm`",
        f"- Estimated takeoff mass: `{bundle.total_weight_g:.0f} g`",
        f"- Battery: `{bundle.battery_summary or '—'}`",
        "",
        "## Tuning",
        "",
        f"- Final gain summary: `{bundle.pid_final_gains_summary or '—'}`",
        "",
        "## Artefacts",
        "",
        f"- URDF: `{bundle.urdf_path or '—'}`",
        f"- SDF: `{bundle.sdf_path or '—'}`",
        f"- Mission code: `{bundle.mission_code_path or '—'}`",
        "- BOM CSV: `bom.csv`",
        "- BOM Markdown: `bom.md`",
        "- Assembly guide: `assembly.md`",
        "- Pre-flight checklist: `preflight.md`",
        "- Maintenance schedule: `maintenance.md`",
        "",
    ]
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# All-in-one renderer
# ─────────────────────────────────────────────────────────────────────────────


def render_all_deliverables(bundle: DeliverableBundle) -> dict[str, str]:
    """Render every deliverable; returns ``{filename: contents}``."""
    return {
        "report.md": render_engineering_report(bundle),
        "bom.csv": render_bom_csv(bundle),
        "bom.md": render_bom_markdown(bundle),
        "assembly.md": render_assembly_guide(bundle),
        "preflight.md": render_preflight_checklist(bundle),
        "maintenance.md": render_maintenance_schedule(bundle),
    }
