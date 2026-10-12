"""Scoring rules of the published prompt benchmark (scripts/run_benchmark.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_benchmark import (
    DEFAULT_SUITE,
    build_record,
    classify,
    design_source,
    expected_met,
    nearest_rank,
    plan_runs,
    render_markdown,
    summarize,
    wilson_interval,
)


@pytest.mark.parametrize(
    ("values", "outcome"),
    [
        ({"simulation_acceptance": {"passed": True}, "simulation_status": "complete"}, "accepted"),
        ({"simulation_status": "blocked", "safety_passed": True}, "blocked_simulation"),
        ({"simulation_acceptance": {"passed": False}}, "failed_simulation"),
        ({"simulation_status": "failed"}, "failed_simulation"),
        ({"current_phase": "error"}, "rejected_prompt"),
        ({"design_status": "rejected"}, "rejected_design"),
        ({"bom_status": "failed", "simulation_status": "pending"}, "rejected_components"),
        ({"conversion_status": "failed"}, "rejected_model"),
        ({"code_status": "rejected"}, "rejected_code"),
        ({"generated_code": "x", "safety_passed": False}, "rejected_safety"),
        ({"simulation_status": "pending"}, "incomplete"),
    ],
)
def test_classify_terminal_states(values, outcome):
    assert classify(values) == outcome


def test_timeouts_and_crashes_are_outcomes_not_skips():
    accepted = {"simulation_acceptance": {"passed": True}}
    assert classify(accepted, timed_out=True) == "timeout"
    assert classify(accepted, harness_error="boom") == "harness_error"


def test_flying_an_impossible_request_is_not_a_correct_rejection():
    assert expected_met("supported", "accepted") is True
    assert expected_met("supported", "blocked_simulation") is False
    assert expected_met("impossible", "rejected_components") is True
    assert expected_met("impossible", "accepted") is False
    # It flew instead of stopping at a gate, even though the gate then failed it.
    assert expected_met("unsupported", "failed_simulation") is False
    assert expected_met("impossible", "timeout") is False
    assert expected_met("ambiguous", "accepted") is None


def test_repaired_design_is_the_baseline_not_the_llm_design():
    assert design_source("rag_llm") == "llm"
    assert design_source("llm_only") == "llm"
    assert design_source("rag_llm_repaired") == "baseline_after_llm_candidate_rejected"
    assert design_source(None) is None


def test_wilson_interval_and_nearest_rank():
    low, high = wilson_interval(18, 18)
    assert high == pytest.approx(1.0)
    assert low == pytest.approx(0.8241, abs=1e-3)
    assert wilson_interval(0, 0) is None
    assert nearest_rank([5.0, 1.0, 3.0, 2.0, 4.0], 0.5) == 3.0
    assert nearest_rank([1.0, 2.0, 3.0], 0.95) == 3.0
    assert nearest_rank([], 0.5) is None


def _record(case_id, category, outcome, wall, retries=0, language="en"):
    return {
        "case_id": case_id,
        "category": category,
        "language": language,
        "outcome": outcome,
        "expected_met": expected_met(category, outcome),
        "wall_time_s": wall,
        "retries": retries,
    }


def test_summary_keeps_failures_in_the_denominator():
    records = [
        _record("a", "supported", "accepted", 100.0),
        _record("a", "supported", "timeout", 2400.0),
        _record("a", "supported", "failed_simulation", 300.0, retries=2),
        _record("b", "ambiguous", "accepted", 50.0, language="ar"),
    ]
    summary = summarize(records)
    supported = summary["categories"]["supported"]
    assert supported["runs"] == 3
    assert supported["correct"] == 1
    assert supported["correct_rate"] == pytest.approx(1 / 3)
    assert supported["wall_time_s"]["p95"] == 2400.0
    assert supported["decided"] == 2
    assert supported["retries"] == {"total": 2, "max": 2, "runs_with_retry": 1}
    ambiguous = summary["categories"]["ambiguous"]
    assert ambiguous["correct"] is None and ambiguous["correct_rate"] is None
    assert summary["cases"]["b"]["correct"] is None
    assert summary["cases"]["a"]["correct"] == 1
    assert summary["languages"]["ar"]["scored_runs"] == 0


def test_summary_lists_how_closely_accepted_flights_followed_their_mission():
    metrics = {"horizontal_error_max_m": 0.52, "altitude_rmse_m": 0.08, "landing_error_m": 0.05}
    accepted = _record("a", "supported", "accepted", 100.0)
    accepted["acceptance"] = {"flight_conformance": {"passed": True, "metrics": metrics}}
    failed = _record("a", "supported", "failed_simulation", 100.0)
    failed["acceptance"] = {"flight_conformance": {"passed": False, "metrics": metrics}}
    summary = summarize([accepted, failed, _record("a", "supported", "timeout", 2400.0)])
    assert summary["cases"]["a"]["accepted_flights"] == [metrics]


def test_record_counts_retries_and_logged_code_fallback():
    case = {"id": "c", "category": "supported", "language": "en", "prompt": "p"}
    timeline = [
        ("parse_prompt", 1.0),
        ("generate_code", 2.0),
        ("run_simulation", 3.0),
        ("generate_code", 4.0),
        ("run_simulation", 5.0),
    ]
    record = build_record(
        case,
        1,
        "bench-c",
        {
            "design_generation": {"method": "rule_based"},
            "simulation_acceptance": {"passed": True},
        },
        wall_time_s=5.0,
        timeline=timeline,
        approvals={},
        warnings=["agents.code_agent: Code generation fallback engaged: timeout"],
        timed_out=False,
        harness_error="",
    )
    assert record["retries"] == 2
    assert record["design"]["source"] == "rule_based"
    assert record["design_fallback"] is True
    assert record["code_fallback"] is True
    assert record["outcome"] == "accepted"
    json.dumps(record)


def test_published_suite_meets_the_plan():
    suite = json.loads(Path(DEFAULT_SUITE).read_text(encoding="utf-8"))
    cases = suite["cases"]
    assert 10 <= len(cases) <= 20
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["language"] for case in cases} == {"ar", "en"}
    categories = {case["category"] for case in cases}
    assert {"supported", "impossible", "ambiguous"} <= categories
    assert categories <= set(suite["categories"])
    runs = plan_runs(suite, [], repeats=3, repeats_other=1)
    supported = [case for case in cases if case["category"] == "supported"]
    assert len(runs) == 3 * len(supported) + (len(cases) - len(supported))


def test_report_renders_every_category_and_unscored_ambiguity():
    records = [
        _record("a", "supported", "accepted", 100.0),
        _record("b", "ambiguous", "failed_simulation", 50.0, language="ar"),
    ]
    for record in records:
        record.update(design_fallback=False, code_fallback=False)
    text = render_markdown({"suite": "s", **summarize(records)}, {})
    assert "| supported | 1 | 1/1 |" in text
    assert "| ambiguous | 1 | — | not scored |" in text
    assert "`b` | ar | ambiguous | failed_simulation" in text


def test_a_run_the_event_loop_cannot_end_is_recorded_as_a_timeout(tmp_path, monkeypatch):
    """2026-09-29: a run sat in a Chroma query for 17 hours and asyncio's timeout
    never fired; the watchdog records it and ends the process for --supervise."""
    import scripts.run_benchmark as bench

    exits = []
    monkeypatch.setattr(bench.os, "_exit", exits.append)
    runs = tmp_path / "runs.jsonl"
    case = {"id": "c", "category": "supported", "language": "en", "prompt": "p"}
    bench.abandon_run(runs, case, 2, bench.time.monotonic() - 2700.0, 16)
    record = json.loads(runs.read_text(encoding="utf-8"))
    assert record["outcome"] == "timeout" and record["expected_met"] is False
    assert record["repeat"] == 2 and record["wall_time_s"] >= 2700.0
    assert exits == [bench.WATCHDOG_EXIT_CODE]


def test_the_supervisor_resumes_only_after_a_watchdog_exit(monkeypatch):
    import scripts.run_benchmark as bench

    codes = iter([bench.WATCHDOG_EXIT_CODE, bench.WATCHDOG_EXIT_CODE, 0])
    commands = []

    def fake_run(command, check):
        commands.append(command)
        return type("Done", (), {"returncode": next(codes)})()

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    assert bench.supervise(["--out", "x"]) == 0
    assert len(commands) == 3 and all(c[-1] == "--resume" for c in commands)
