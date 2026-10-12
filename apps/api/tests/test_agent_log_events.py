"""Tests for agent_log socket events (services/pipeline_runtime.py)

graph/nodes.py has always recorded per-agent progress through _log_entry(), and
the frontend has always listened for `agent_log` to drive its activity feed —
but nothing bridged the two, so the feed only ever showed coarse phase
transitions while 24 log sites in the graph went nowhere.

Coverage:
- Entries become agent_log events with the payload the frontend reads
- They precede the node's own events, so the feed reads in causal order
- Emitted for every event type, not just node_complete
- Malformed entries are skipped rather than broadcast
- No duplication: the orchestrator streams deltas, not accumulated state
"""

from __future__ import annotations

from services.pipeline_runtime import _agent_log_messages, _ws_messages_from_event

PROJECT = "proj-1"


def _event(node: str = "generate_design", event: str = "node_complete", **data):
    return {"event": event, "node": node, "data": data}


def _names(messages) -> list[str]:
    return [name for name, _ in messages]


def _payloads(messages, event_name: str) -> list[dict]:
    return [payload for name, payload in messages if name == event_name]


class TestPayloadShape:
    def test_entry_becomes_an_agent_log_event(self):
        messages = _agent_log_messages(
            PROJECT,
            {
                "agent_logs": [
                    {"agent": "design_agent", "status": "success", "message": "ok", "ts": 1.0}
                ]
            },
        )

        assert messages == [
            (
                "agent_log",
                {
                    "project_id": PROJECT,
                    "agent": "design_agent",
                    "status": "success",
                    "message": "ok",
                    "ts": 1.0,
                },
            )
        ]

    def test_status_defaults_to_success(self):
        """ws-store maps status onto an activity badge; it must never be absent."""
        [(_, payload)] = _agent_log_messages(
            PROJECT, {"agent_logs": [{"agent": "a", "message": "m"}]}
        )

        assert payload["status"] == "success"

    def test_missing_message_becomes_empty_string(self):
        [(_, payload)] = _agent_log_messages(PROJECT, {"agent_logs": [{"agent": "a"}]})

        assert payload["message"] == ""

    def test_error_status_is_preserved(self):
        [(_, payload)] = _agent_log_messages(
            PROJECT,
            {"agent_logs": [{"agent": "design_agent", "status": "error", "message": "boom"}]},
        )

        assert payload["status"] == "error"


class TestFiltering:
    def test_entries_without_an_agent_are_skipped(self):
        assert _agent_log_messages(PROJECT, {"agent_logs": [{"message": "orphan"}]}) == []

    def test_non_dict_entries_are_skipped(self):
        assert _agent_log_messages(PROJECT, {"agent_logs": ["a string", None, 42]}) == []

    def test_absent_agent_logs_yields_nothing(self):
        assert _agent_log_messages(PROJECT, {}) == []
        assert _agent_log_messages(PROJECT, {"agent_logs": None}) == []


class TestOrdering:
    def test_agent_logs_precede_the_node_events(self):
        """The activity feed should show the work before the phase it produced."""
        messages = _ws_messages_from_event(
            PROJECT,
            _event(
                agent_logs=[{"agent": "design_agent", "status": "success", "message": "done"}],
                design_tree={"topology": "quadcopter"},
            ),
        )

        assert _names(messages)[0] == "agent_log"
        assert "design_ready" in _names(messages)
        assert "phase_changed" in _names(messages)

    def test_multiple_entries_keep_their_order(self):
        messages = _agent_log_messages(
            PROJECT,
            {
                "agent_logs": [
                    {"agent": "first", "message": "1"},
                    {"agent": "second", "message": "2"},
                ]
            },
        )

        assert [p["agent"] for _, p in messages] == ["first", "second"]


class TestEventTypes:
    def test_emitted_for_hitl_interrupt(self):
        messages = _ws_messages_from_event(
            PROJECT,
            _event(
                node="human_review_design",
                event="hitl_interrupt",
                agent_logs=[{"agent": "hitl", "message": "awaiting review"}],
            ),
        )

        assert _payloads(messages, "agent_log")

    def test_emitted_for_pipeline_started(self):
        messages = _ws_messages_from_event(
            PROJECT,
            _event(
                node=None,
                event="pipeline_started",
                agent_logs=[{"agent": "orchestrator", "message": "go"}],
            ),
        )

        assert _payloads(messages, "agent_log")

    def test_node_events_still_emitted_without_agent_logs(self):
        """Adding the bridge must not change the existing event stream."""
        messages = _ws_messages_from_event(PROJECT, _event(design_tree={"topology": "quadcopter"}))

        assert _names(messages) == ["design_ready", "phase_changed"]

    def test_unknown_node_emits_only_the_agent_logs(self):
        messages = _ws_messages_from_event(
            PROJECT, _event(node="some_new_node", agent_logs=[{"agent": "x", "message": "m"}])
        )

        assert _names(messages) == ["agent_log"]
