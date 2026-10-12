"""eljazari.ai — LangGraph Shared State Object (Prompt 2.1)

Pure TypedDict so LangGraph can merge partial updates from every node.
`Annotated[list[X], add]` fields are append-only accumulators.
"""

from operator import add
from typing import Annotated, Any, TypedDict


# ── Sentinel default for optional fields ─────────────────────────────────────
_NONE: None = None


class ElJazariState(TypedDict, total=False):
    """Single shared state object for all agents in the eljazari.ai pipeline.

    Fields marked ``Annotated[list, add]`` are merged by LangGraph using list
    concatenation — every node appends, nobody overwrites.
    All other fields are replaced by the latest node's partial return dict.
    """

    # ── Input ─────────────────────────────────────────────────────────────────
    user_prompt: str
    project_id: str

    # ── Design phase ──────────────────────────────────────────────────────────
    design_tree: dict[str, Any] | None
    design_params: dict[str, Any] | None
    design_status: str  # pending | generated | approved | rejected
    design_feedback: str | None
    # Preserve whether generation used retrieval, LLM only, or a fallback.
    design_generation: dict[str, Any] | None
    design_diagnostics: dict[str, Any] | None
    # Physics estimate from the design calculator (hover_time_s, max_speed_ms,
    # max_distance_m, total_weight_g, thrust_to_weight_ratio). Declared here so
    # LangGraph keeps it on the channel for the Phase 14 deliverables writer.
    performance_estimate: dict[str, Any] | None

    # ── Real component BOM (Phase 6) ──────────────────────────────────────────
    # Populated by select_components node; consumed by convert_to_urdf
    # (for real mass) and by generate_code (for accurate constraints).
    selected_components: dict[str, Any] | None
    component_alternatives: list[dict[str, Any]]
    component_search: dict[str, Any] | None
    real_performance: dict[str, Any] | None
    bom_status: str  # pending | selected | failed

    # ── URDF conversion ───────────────────────────────────────────────────────
    urdf_path: str | None
    sdf_path: str | None
    conversion_status: str  # pending | complete | failed
    resolved_drone_spec: dict[str, Any] | None
    resolved_drone_sha256: str | None
    model_manifest_path: str | None
    model_validation_scope: str  # unavailable | legacy_visualization | structural_only
    # PX4/Gazebo flight bundle derived from the resolved spec (sitl_catalog_estimate).
    flight_model_manifest_path: str | None
    flight_model_bundle_sha256: str | None
    flight_model_status: str  # unavailable | generated | failed
    flight_model_block_reason: str | None

    # ── Code generation ───────────────────────────────────────────────────────
    generated_code: str | None
    gsce_config: dict[str, Any] | None
    code_status: str  # pending | generated | validated | approved | rejected
    code_feedback: str | None
    # SHA-256 of generated_code as approved at human_review_code; execution
    # requires an exact match, and any new generation clears it.
    approved_code_sha256: str | None

    # ── Safety ────────────────────────────────────────────────────────────────
    safety_passed: bool
    safety_report: dict[str, Any] | None
    safety_issues: Annotated[list[str], add]
    # Counts safety-triggered regenerations. Distinct from simulation_iteration
    # on purpose: the safety loop (generate_code -> safety_check -> generate_code)
    # never reaches run_simulation, so bounding it on that counter meant it was
    # bounded on a number that could not change — and it span forever.
    safety_retry_count: int

    # ── Simulation ────────────────────────────────────────────────────────────
    simulation_record_id: str | None
    simulation_status: str  # pending | running | complete | failed | blocked
    simulation_block_reason: str | None
    telemetry_data: list[dict[str, Any]]
    semantic_observations: list[str]  # Current attempt only; history lives in simulation records
    simulation_acceptance: dict[str, Any] | None
    # What the simulator attested it loaded for a generated aircraft (None for x500).
    simulation_model_attestation: dict[str, Any] | None
    simulation_success: bool
    simulation_score: float | None
    evaluation_status: str | None  # completed | completed_with_failure | skipped | failed
    evaluation_skip_reason: str | None
    simulation_iteration: int
    max_iterations: int

    # SITL provenance and outcome flags persisted by run_simulation.
    # Without these, LangGraph drops the keys (TypedDict channels) and
    # generate_monitor_report always reads the defaults — making the report
    # claim mission_started=False / telemetry_real_rate=0 even after a real
    # WSL/Docker SITL run.
    mission_started: bool
    mission_completed: bool
    crash_detected: bool
    geofence_violation: bool
    runtime_mode: str
    telemetry_source: str
    mission_error: str
    simulation_warnings: list[str]
    mission_isolation: str  # linux_namespaces | none | unknown
    simulation_artifacts: dict[str, str]
    duration_s: float | None

    # ── Monitor ───────────────────────────────────────────────────────────────
    monitor_report: dict[str, Any] | None
    recommendations: Annotated[list[str], add]

    # ── Phase 14: engineering deliverables paths ─────────────────────────────
    deliverables_paths: list[str]
    deliverables_status: str  # pending | complete | failed

    # ── Pipeline control ──────────────────────────────────────────────────────
    current_phase: str
    errors: Annotated[list[str], add]
    agent_logs: Annotated[list[dict[str, Any]], add]
    human_approval_needed: bool
    human_feedback: str | None


def initial_state(user_prompt: str, project_id: str, max_iterations: int = 5) -> ElJazariState:
    """Return a fully-initialised state for a new pipeline run."""
    return ElJazariState(
        user_prompt=user_prompt,
        project_id=project_id,
        design_tree=None,
        design_params=None,
        design_status="pending",
        design_feedback=None,
        performance_estimate=None,
        selected_components=None,
        component_alternatives=[],
        real_performance=None,
        bom_status="pending",
        urdf_path=None,
        sdf_path=None,
        conversion_status="pending",
        resolved_drone_spec=None,
        resolved_drone_sha256=None,
        model_manifest_path=None,
        model_validation_scope="unavailable",
        flight_model_manifest_path=None,
        flight_model_bundle_sha256=None,
        flight_model_status="unavailable",
        flight_model_block_reason=None,
        generated_code=None,
        gsce_config=None,
        code_status="pending",
        code_feedback=None,
        approved_code_sha256=None,
        safety_passed=False,
        safety_report=None,
        safety_issues=[],
        safety_retry_count=0,
        simulation_record_id=None,
        simulation_status="pending",
        simulation_block_reason=None,
        telemetry_data=[],
        semantic_observations=[],
        simulation_acceptance=None,
        simulation_model_attestation=None,
        simulation_success=False,
        simulation_score=None,
        evaluation_status=None,
        evaluation_skip_reason=None,
        simulation_iteration=0,
        max_iterations=max_iterations,
        mission_started=False,
        mission_completed=False,
        crash_detected=False,
        geofence_violation=False,
        runtime_mode="unknown",
        telemetry_source="unknown",
        mission_error="",
        simulation_warnings=[],
        mission_isolation="unknown",
        simulation_artifacts={},
        duration_s=None,
        monitor_report=None,
        recommendations=[],
        deliverables_paths=[],
        deliverables_status="pending",
        current_phase="init",
        errors=[],
        agent_logs=[],
        human_approval_needed=False,
        human_feedback=None,
    )
