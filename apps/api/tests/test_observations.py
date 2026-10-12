"""services.observations — the observer's prose becomes feed events."""

from __future__ import annotations

from services.observations import parse_observation, parse_observations


class TestParseObservation:
    def test_timestamped_line(self):
        item = parse_observation({"description": "[12.4s] drone in 'MISSION' mode, 4.1m up"})
        assert item["timestamp_s"] == 12.4
        assert item["description"].startswith("drone in")
        assert item["event_type"] == "telemetry"
        assert item["severity"] == "info"

    def test_mission_event_success(self):
        item = parse_observation("Event: mission completed successfully — landed at target")
        assert item["event_type"] == "mission_event"
        assert item["severity"] == "success"
        assert item["timestamp_s"] == 0.0

    def test_warning_and_critical(self):
        assert parse_observation("[3s] low battery warning")["severity"] == "warning"
        assert parse_observation("[4s] crash detected on landing")["severity"] == "critical"

    def test_untimed_lines_inherit_the_previous_timestamp(self):
        items = parse_observations(["[5.0s] hovering", "Event: mission completed", "[6.0s] landed"])
        assert [i["timestamp_s"] for i in items] == [5.0, 5.0, 6.0]

    def test_empty_and_none(self):
        assert parse_observations(None) == []
        assert parse_observations([]) == []
