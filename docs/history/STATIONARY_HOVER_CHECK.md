# Stationary hover and mission constraints

Historical changes: 21 September 2026.

A simple stationary-hover request now produces one waypoint at takeoff position, with the requested hold time, instead of accepting an invented travel route. The parser recognizes explicit seconds and minutes. A 20-second hover with a 90-second maximum stays a 20-second hover. The planner adds a fixed 25-second takeoff/landing estimate and rejects an impossible upper duration. Explicit altitude must fit the limits.

The MAVSDK template preserves hold time, position and speed. Its preflight helper name agrees with the safety checker. Violations of altitude, speed, geofence, return constraints or required preflight checks cause rejection independently of the severity label. The workflow passes the stricter combination of mission and system limits.

Historical validation: 215 focused tests passed. Live Ollama generation took 66.84 seconds and produced one MissionItem at 10 m, capped at 3 m/s, holding for 20 seconds with `fly_through=false`. Planned horizontal travel was zero, estimated total duration was 45 seconds and static analysis reported no violations (`passed=true`, `risk=low`).

The generated file was not executed. The fixed takeoff/landing estimate is not a measurement, and static analysis is not a proof of every runtime value or call order. Later flight-model, sandbox and acceptance work is documented separately. No project approval or publication occurred in that historical check.
