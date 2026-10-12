"""eljazari.ai — Main LangGraph Workflow Definition (Prompt 2.1)

Wires all nodes and conditional edges into a compiled StateGraph.

Checkpointing strategy
──────────────────────
• Development / testing  → MemorySaver (in-process, no external dep)
• Production             → AsyncPostgresSaver (same PostgreSQL used by the app)

The development graph is built at import. PostgreSQL connections open only
inside operation-scoped contexts; importing the module requires no database.

HITL interrupt points
─────────────────────
``interrupt_before=["human_review_design", "human_review_code"]``

When execution reaches one of these nodes the graph suspends and returns
control to the caller.  The orchestrator then waits for human input, patches
the state, and resumes by calling ``astream`` again with the same thread_id.
"""

from contextlib import asynccontextmanager, AsyncExitStack
from typing import Any

import structlog
from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from graph.state import ElJazariState
from graph.nodes import (
    parse_prompt,
    generate_design,
    human_review_design,
    select_components,
    convert_to_urdf,
    generate_code,
    safety_check,
    human_review_code,
    run_simulation,
    evaluate_simulation,
    generate_monitor_report,
)
from graph.edges import (
    route_after_parse,
    route_after_generate_design,
    route_after_human_review_design,
    route_after_select_components,
    route_after_convert_to_urdf,
    route_after_generate_code,
    route_after_safety_check,
    route_after_human_review_code,
    route_after_run_simulation,
    route_after_evaluate_simulation,
    route_after_monitor_report,
)

log = structlog.get_logger(__name__)

# ── HITL interrupt nodes ──────────────────────────────────────────────────────
HITL_INTERRUPT_NODES = ["human_review_design", "human_review_code"]


# ─────────────────────────────────────────────────────────────────────────────
# Checkpointer factory
# ─────────────────────────────────────────────────────────────────────────────


def _build_checkpoint_serde():
    """Build a JsonPlusSerializer that knows about every custom Pydantic /
    Enum type the orchestrator state carries.

    LangGraph 1.x emits a deprecation warning on each checkpoint load —
    ``Deserializing unregistered type agents.safety_models.Severity from
    checkpoint`` — and a future major version will block deserialisation
    outright, causing the orchestrator to fail mid-resume. We register
    every relevant type with ``with_msgpack_allowlist`` to prevent that.

    Older LangGraph releases (0.2.x, pinned by the CI image) do not have
    the ``with_msgpack_allowlist`` method and don't emit the warning
    either; in that case we return a plain :class:`JsonPlusSerializer`
    which behaves correctly without the explicit registration.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    register = getattr(serde, "with_msgpack_allowlist", None)
    if register is None:
        # LangGraph 0.2.x — no strict msgpack mode, nothing to register.
        return serde

    from agents.safety_models import (
        ComplianceReport,
        ComplianceViolation,
        FlightConstraints,
        FullSafetyReport,
        InjectionReport,
        RiskLevel,
        SafetyIssue,
        SafetyReport,
        Severity,
        StaticAnalysisReport,
        WaypointSafetyReport,
    )

    return register(
        [
            Severity,
            RiskLevel,
            SafetyIssue,
            StaticAnalysisReport,
            WaypointSafetyReport,
            ComplianceViolation,
            ComplianceReport,
            InjectionReport,
            FullSafetyReport,
            SafetyReport,
            FlightConstraints,
        ]
    )


def _make_memory_saver() -> MemorySaver:
    return MemorySaver(serde=_build_checkpoint_serde())


def _postgres_saver_context(dsn: str):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    return AsyncPostgresSaver.from_conn_string(dsn)


@asynccontextmanager
async def _make_postgres_saver(*, setup: bool = False):
    """Own a connection on this event loop. Never fall back to volatile memory."""
    from config.settings import settings

    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    async with AsyncExitStack() as stack:
        try:
            saver = await stack.enter_async_context(_postgres_saver_context(dsn))
            saver.serde = _build_checkpoint_serde()
            if setup:
                await saver.setup()
        except Exception as exc:
            raise RuntimeError(
                "PostgreSQL checkpoint storage is unavailable. Check its dependency, "
                "connection and schema; memory fallback is disabled."
            ) from exc
        yield saver


# ─────────────────────────────────────────────────────────────────────────────
# Graph builder
# ─────────────────────────────────────────────────────────────────────────────


def _build_graph(checkpointer: Any) -> Any:
    """Construct, wire, and compile the eljazari.ai StateGraph."""
    builder = StateGraph(ElJazariState)

    # ── Register nodes ────────────────────────────────────────────────────────
    builder.add_node("parse_prompt", parse_prompt)
    builder.add_node("generate_design", generate_design)
    builder.add_node("human_review_design", human_review_design)
    builder.add_node("select_components", select_components)
    builder.add_node("convert_to_urdf", convert_to_urdf)
    builder.add_node("generate_code", generate_code)
    builder.add_node("safety_check", safety_check)
    builder.add_node("human_review_code", human_review_code)
    builder.add_node("run_simulation", run_simulation)
    builder.add_node("evaluate_simulation", evaluate_simulation)
    builder.add_node("generate_monitor_report", generate_monitor_report)

    # ── Entry point ───────────────────────────────────────────────────────────
    builder.set_entry_point("parse_prompt")

    # ── Edges ─────────────────────────────────────────────────────────────────
    builder.add_conditional_edges(
        "parse_prompt",
        route_after_parse,
        {"generate_design": "generate_design", "end": END},
    )
    builder.add_conditional_edges(
        "generate_design",
        route_after_generate_design,
        {"human_review_design": "human_review_design", "end": END},
    )
    builder.add_conditional_edges(
        "human_review_design",
        route_after_human_review_design,
        {
            "select_components": "select_components",
            "generate_design": "generate_design",
        },
    )
    builder.add_conditional_edges(
        "select_components",
        route_after_select_components,
        {"convert_to_urdf": "convert_to_urdf", "end": END},
    )
    builder.add_conditional_edges(
        "convert_to_urdf",
        route_after_convert_to_urdf,
        {"generate_code": "generate_code", "end": END},
    )
    builder.add_conditional_edges(
        "generate_code",
        route_after_generate_code,
        {"safety_check": "safety_check", "end": END},
    )
    builder.add_conditional_edges(
        "safety_check",
        route_after_safety_check,
        {"human_review_code": "human_review_code", "generate_code": "generate_code", "end": END},
    )
    builder.add_conditional_edges(
        "human_review_code",
        route_after_human_review_code,
        {"run_simulation": "run_simulation", "generate_code": "generate_code"},
    )
    builder.add_conditional_edges(
        "run_simulation",
        route_after_run_simulation,
        {
            "evaluate_simulation": "evaluate_simulation",
            "generate_code": "generate_code",
            "generate_monitor_report": "generate_monitor_report",
        },
    )
    builder.add_conditional_edges(
        "evaluate_simulation",
        route_after_evaluate_simulation,
        {"generate_monitor_report": "generate_monitor_report", "generate_code": "generate_code"},
    )
    builder.add_conditional_edges(
        "generate_monitor_report",
        route_after_monitor_report,
        {"end": END},
    )

    # ── Compile ───────────────────────────────────────────────────────────────
    graph = builder.compile(
        checkpointer=checkpointer,
        interrupt_before=HITL_INTERRUPT_NODES,
    )
    log.info("workflow.compiled", interrupt_before=HITL_INTERRUPT_NODES)
    return graph


# ─────────────────────────────────────────────────────────────────────────────
# Public singletons
# ─────────────────────────────────────────────────────────────────────────────

# Synchronous dev/test graph (MemorySaver — no async setup needed)
_dev_checkpointer = _make_memory_saver()
workflow = _build_graph(_dev_checkpointer)


@asynccontextmanager
async def get_workflow_async(*, setup: bool = False):
    """Use with async with; the graph must not outlive its database connection.

    Startup calls setup=True. Each operation gets its own connection so worker
    threads do not share psycopg connections or asyncio locks across loops.
    """
    async with _make_postgres_saver(setup=setup) as saver:
        yield _build_graph(saver)
