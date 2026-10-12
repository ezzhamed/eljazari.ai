"""Generation provenance must survive graph channel filtering and checkpoint reads."""

from langgraph.graph import END, START, StateGraph

from graph.state import ElJazariState
from graph.workflow import _make_memory_saver


def test_generation_provenance_survives_new_graph_instance():
    provenance = {
        "method": "rule_based",
        "repair_attempts": 0,
        "diagnostics": {"fallback_reason": "llm_timeout"},
    }
    builder = StateGraph(ElJazariState)
    builder.add_node(
        "design",
        lambda state: {
            "design_status": "generated",
            "design_generation": provenance,
            "design_diagnostics": provenance["diagnostics"],
        },
    )
    builder.add_edge(START, "design")
    builder.add_edge("design", END)
    saver = _make_memory_saver()
    config = {"configurable": {"thread_id": "design-provenance-test"}}
    builder.compile(checkpointer=saver).invoke({"user_prompt": "Test design"}, config)
    restored = builder.compile(checkpointer=saver).get_state(config).values
    assert restored["design_generation"] == provenance
    assert restored["design_diagnostics"]["fallback_reason"] == "llm_timeout"
