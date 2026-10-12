"""Turn the simulator's observation lines into structured events.

The closed-loop observer produces prose — "[12.4s] drone in 'MISSION' mode,
5.9m north of launch, 4.1m up …", "Event: mission started — …" — and that is
what is persisted. The dashboard's observation feed wants a timestamp, a type
and a severity, and until this existed nothing on the backend ever sent it
one, so the panel read "Waiting for semantic observations…" for the life of
every run.
"""

from __future__ import annotations

import re
from typing import Any

_TIMESTAMP = re.compile(r"^\s*\[(?P<t>\d+(?:\.\d+)?)\s*s\]\s*")

_CRITICAL = ("crash", "emergency", "failsafe", "collision", "lost", "error", "unhandled")
_WARNING = ("warning", "low battery", "battery low", "denied", "timeout", "drift", "exceed")
_SUCCESS = ("mission completed", "mission complete", "landed", "mission started")


def observation_text(value: Any) -> str:
    """Accept the stored `{"description": …}` dict, a bare string, or a model."""
    if isinstance(value, dict):
        return str(value.get("description") or value.get("observation") or "")
    if hasattr(value, "model_dump"):
        return observation_text(value.model_dump())
    return str(value)


def parse_observation(value: Any, fallback_t: float = 0.0) -> dict[str, Any]:
    text = observation_text(value).strip()
    match = _TIMESTAMP.match(text)
    timestamp_s = float(match.group("t")) if match else fallback_t
    description = text[match.end() :] if match else text

    lowered = description.lower()
    if description.startswith("Event:"):
        event_type = "mission_event"
        description = description[len("Event:") :].strip()
    else:
        event_type = "telemetry"

    if any(word in lowered for word in _CRITICAL):
        severity = "critical"
    elif any(word in lowered for word in _WARNING):
        severity = "warning"
    elif event_type == "mission_event" and any(word in lowered for word in _SUCCESS):
        severity = "success"
    else:
        severity = "info"

    return {
        "timestamp_s": round(timestamp_s, 2),
        "description": description,
        "event_type": event_type,
        "severity": severity,
    }


def parse_observations(values: Any) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    last_t = 0.0
    for value in values or []:
        item = parse_observation(value, fallback_t=last_t)
        last_t = item["timestamp_s"]
        parsed.append(item)
    return parsed
