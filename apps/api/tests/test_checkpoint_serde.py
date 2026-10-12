"""Phase 8.6 tests — LangGraph checkpoint serde registers our Pydantic
+ Enum types so the orchestrator's checkpoint replay no longer warns
"Deserializing unregistered type agents.safety_models.Severity".

Round-trips every type the workflow state carries through msgpack so a
deserialisation regression is caught in CI long before a long-running
orchestrator hits it.
"""

from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
# Serde builder
# ─────────────────────────────────────────────────────────────────────────────


class TestSerdeBuilder:
    def test_build_returns_jsonplus_serializer(self):
        from graph.workflow import _build_checkpoint_serde

        serde = _build_checkpoint_serde()
        assert type(serde).__name__ == "JsonPlusSerializer"

    def test_memory_saver_uses_custom_serde(self):
        from graph.workflow import _make_memory_saver

        saver = _make_memory_saver()
        assert type(saver.serde).__name__ == "JsonPlusSerializer"


# ─────────────────────────────────────────────────────────────────────────────
# Round-trip every registered type
# ─────────────────────────────────────────────────────────────────────────────


class TestRoundTrip:
    def _roundtrip(self, value):
        from graph.workflow import _build_checkpoint_serde

        serde = _build_checkpoint_serde()
        tag, blob = serde.dumps_typed(value)
        out = serde.loads_typed((tag, blob))
        return out

    def test_severity_enum_roundtrip(self):
        from agents.safety_models import Severity

        out = self._roundtrip(Severity.CRITICAL)
        assert out == Severity.CRITICAL

    def test_risk_level_enum_roundtrip(self):
        from agents.safety_models import RiskLevel

        out = self._roundtrip(RiskLevel.MEDIUM)
        assert out == RiskLevel.MEDIUM

    def test_safety_issue_roundtrip(self):
        from agents.safety_models import SafetyIssue, Severity

        original = SafetyIssue(
            rule_id="SR001",
            severity=Severity.CRITICAL,
            description="unapproved function",
            line_number=42,
            suggestion="use approved API",
        )
        out = self._roundtrip(original)
        assert out.rule_id == "SR001"
        assert out.severity == Severity.CRITICAL
        assert out.line_number == 42

    def test_flight_constraints_roundtrip(self):
        from agents.safety_models import FlightConstraints

        original = FlightConstraints(
            max_altitude_m=120.0,
            min_altitude_m=2.0,
            max_speed_ms=15.0,
            max_geofence_radius_m=250.0,
            min_battery_pct=20.0,
        )
        out = self._roundtrip(original)
        assert out.max_geofence_radius_m == 250.0
        assert out.max_altitude_m == 120.0

    @staticmethod
    def _empty_sub_reports():
        from agents.safety_models import (
            ComplianceReport,
            InjectionReport,
            StaticAnalysisReport,
            WaypointSafetyReport,
        )

        return dict(
            static_report=StaticAnalysisReport(passed=True),
            waypoint_report=WaypointSafetyReport(passed=True),
            compliance_report=ComplianceReport(passed=True),
            injection_report=InjectionReport(),
        )

    def test_full_safety_report_roundtrip(self):
        from agents.safety_models import (
            FullSafetyReport,
            InjectionReport,
            RiskLevel,
            SafetyIssue,
            Severity,
        )

        sub = self._empty_sub_reports()
        sub["injection_report"] = InjectionReport(
            protocols_injected=["battery_monitor"],
        )
        original = FullSafetyReport(
            passed=True,
            risk_level=RiskLevel.LOW,
            all_issues=[
                SafetyIssue(
                    rule_id="SR002",
                    severity=Severity.HIGH,
                    description="no landing sequence",
                    suggestion="add drone.action.land()",
                )
            ],
            final_code="async def run():\n    pass",
            **sub,
        )
        out = self._roundtrip(original)
        assert out.passed is True
        assert out.risk_level == RiskLevel.LOW
        assert len(out.all_issues) == 1
        assert out.all_issues[0].rule_id == "SR002"
        assert out.injection_report.protocols_injected == ["battery_monitor"]

    def test_nested_state_dict_roundtrip(self):
        """A typical orchestrator partial state — dict with nested Pydantic +
        Enum values. This is exactly the shape LangGraph stores between
        nodes, so a regression here is the one users will hit first."""
        from agents.safety_models import FullSafetyReport, RiskLevel, Severity

        partial_state = {
            "safety_passed": True,
            "safety_report": FullSafetyReport(
                passed=True,
                risk_level=RiskLevel.MEDIUM,
                **self._empty_sub_reports(),
            ).model_dump(),
            "severity_marker": Severity.LOW,
        }
        out = self._roundtrip(partial_state)
        assert out["safety_passed"] is True
        assert out["severity_marker"] == Severity.LOW
