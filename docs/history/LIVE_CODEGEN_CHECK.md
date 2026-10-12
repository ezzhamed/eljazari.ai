# Live code-generation check

Historical test: 21 September 2026. This note records generation and static inspection, not flight.

The real Ollama request used a resolved 4S/6,000 mAh battery, 100 g payload, 1,458.2 g structural mass and matching fingerprint. The mission requested takeoff to 10 m, a 20-second hover and landing, with a 3 m/s speed cap, 90-second upper time limit and 30 m geofence.

The first `ollama_plan` result contained MAVSDK waypoints at 5 m/s despite the 3 m/s configuration. Legacy command conversion and geometry repair both restored a 5 m/s default. Structured-plan conversion now carries the speed constraint and avoids the legacy geometry rewrite.

After the correction, 67 focused tests passed. Live generation took 48.76 seconds; AST inspection found two MissionItems, both capped at 3 m/s. Static analysis returned `passed=true`, `risk=medium` and one preflight-name warning. The generated file was not executed or approved.

This did not prove mission timing, stationary hover behavior, safe execution or aircraft feasibility. Subsequent fixes for those specific hover and preflight findings are recorded in [the stationary-hover note](STATIONARY_HOVER_CHECK.md). Generated artifacts remained local development outputs.
