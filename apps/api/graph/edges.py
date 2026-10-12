"""eljazari.ai — LangGraph Conditional Edge Logic (Prompt 2.1)

Each function inspects the current state and returns the *name of the next node*
(or END). LangGraph uses the returned string as a key in the ``path_map`` dict
passed to ``add_conditional_edges``.
"""

from graph.state import ElJazariState
from config.settings import settings


# ─────────────────────────────────────────────────────────────────────────────
# After parse_prompt
# ─────────────────────────────────────────────────────────────────────────────


def route_after_parse(state: ElJazariState) -> str:
    """Abort immediately if prompt is empty or caused an error."""
    if state.get("current_phase") == "error":
        return "end"
    return "generate_design"


# ─────────────────────────────────────────────────────────────────────────────
# After generate_design
# ─────────────────────────────────────────────────────────────────────────────


def route_after_generate_design(state: ElJazariState) -> str:
    """Route to HITL review unless the design agent threw a hard error."""
    if state.get("design_status") == "rejected" and state.get("errors"):
        # Hard error (exception in agent) — stop pipeline
        return "end"
    # Always go to human review (HITL interrupt will pause here)
    return "human_review_design"


# ─────────────────────────────────────────────────────────────────────────────
# After human_review_design
# ─────────────────────────────────────────────────────────────────────────────


def route_after_human_review_design(state: ElJazariState) -> str:
    """Approved → component selection → URDF. Rejected → regenerate design."""
    if state.get("design_status") == "approved":
        return "select_components"
    # Rejected with feedback — redo design
    return "generate_design"


# ─────────────────────────────────────────────────────────────────────────────
# After select_components  (Phase 6)
# ─────────────────────────────────────────────────────────────────────────────


def route_after_select_components(state: ElJazariState) -> str:
    """Successful BOM selection proceeds to URDF; failures end the run.

    Selection failure means no viable real components for the requested
    design (e.g. cells_s mismatch). Better to surface the failure than to
    fall through and let URDF produce a templated airframe.
    """
    if state.get("bom_status") == "selected":
        return "convert_to_urdf"
    return "end"


# ─────────────────────────────────────────────────────────────────────────────
# After convert_to_urdf
# ─────────────────────────────────────────────────────────────────────────────


def route_after_convert_to_urdf(state: ElJazariState) -> str:
    """URDF conversion must succeed before code generation can proceed."""
    if state.get("conversion_status") == "failed":
        return "end"
    return "generate_code"


# ─────────────────────────────────────────────────────────────────────────────
# After generate_code
# ─────────────────────────────────────────────────────────────────────────────


def route_after_generate_code(state: ElJazariState) -> str:
    """Always run safety check after code is generated (or regenerated)."""
    if state.get("code_status") == "rejected" and state.get("errors"):
        return "end"
    return "safety_check"


# ─────────────────────────────────────────────────────────────────────────────
# After safety_check
# ─────────────────────────────────────────────────────────────────────────────


def route_after_safety_check(state: ElJazariState) -> str:
    """Safety passed → HITL code review.
    Safety failed → recode (up to max_iterations), then hard stop.

    The bound is ``safety_retry_count``, not ``simulation_iteration``. This
    loop is ``generate_code → safety_check → generate_code`` and never touches
    ``run_simulation``, the only node that advances the simulation counter — so
    the old guard compared 0 against the limit on every pass and never fired.
    A September 2026 run regenerated the same 13,525-character file eleven
    times over an unfixable geofence violation, showing "iter 0" throughout.
    """
    if state.get("safety_passed"):
        return "human_review_code"

    retries = state.get("safety_retry_count", 0)
    max_iter = state.get("max_iterations") or settings.max_simulation_iterations
    if retries >= max_iter:
        return "end"
    return "generate_code"


# ─────────────────────────────────────────────────────────────────────────────
# After human_review_code
# ─────────────────────────────────────────────────────────────────────────────


def route_after_human_review_code(state: ElJazariState) -> str:
    """Approved → run simulation. Rejected → regenerate code with feedback."""
    if state.get("code_status") == "approved":
        return "run_simulation"
    return "generate_code"


# ─────────────────────────────────────────────────────────────────────────────
# After run_simulation
# ─────────────────────────────────────────────────────────────────────────────


def route_after_run_simulation(state: ElJazariState) -> str:
    """Simulation must collect telemetry before evaluation."""
    if state.get("simulation_status") == "blocked":
        return "generate_monitor_report"
    if state.get("simulation_status") == "failed":
        # If crash on first attempt, retry code generation; otherwise give up
        iteration = state.get("simulation_iteration", 0)
        max_iter = state.get("max_iterations") or settings.max_simulation_iterations
        if iteration < max_iter:
            return "generate_code"
        return "generate_monitor_report"
    return "evaluate_simulation"


# ─────────────────────────────────────────────────────────────────────────────
# After evaluate_simulation
# ─────────────────────────────────────────────────────────────────────────────


def route_after_evaluate_simulation(state: ElJazariState) -> str:
    """Mission passed → final report.
    Mission failed → recode up to max_iterations, then generate report anyway.
    """
    if state.get("simulation_status") == "blocked":
        return "generate_monitor_report"
    acceptance = state.get("simulation_acceptance")
    if (
        acceptance
        and not acceptance.get("passed")
        and not acceptance.get("retryable_with_code_change")
    ):
        return "generate_monitor_report"
    if state.get("simulation_success"):
        return "generate_monitor_report"

    iteration = state.get("simulation_iteration", 0)
    max_iter = state.get("max_iterations") or settings.max_simulation_iterations
    if iteration < max_iter:
        return "generate_code"  # Back to Code Agent with feedback

    # Max iterations exhausted — still produce a report
    return "generate_monitor_report"


# ─────────────────────────────────────────────────────────────────────────────
# After generate_monitor_report
# ─────────────────────────────────────────────────────────────────────────────


def route_after_monitor_report(state: ElJazariState) -> str:
    """Pipeline always ends after the monitor report."""
    return "end"
