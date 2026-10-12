# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A clean manufacturer-data import now gives each motor its documented default curve. With no earlier declaration to keep, the importer fell back to the highest-voltage test by part number and declared the MN3508 KV380 with the 15*5 propeller instead of 13*4.4, so a fresh clone (and CI) failed `test_bundled_declaration_matches_only_its_own_test_condition`.
- `source-map-js` 1.2.2 in the lockfile (GHSA-68fv-2mgg-jv7q, high), which failed the production npm audit.

## [0.1.0-alpha.1] — first public release

The first public, experimental release. Results are software-in-the-loop simulation only; see the README's scope.

### Release preparation

- English documentation and interface text, with multilingual engineering fixtures retained.
- Updated frontend dependencies and Node 22 container builds using the workspace lockfile.
- Embedded SQLite/NumPy retrieval, PyJWT authentication, and patched checkpoint storage dependencies; blocking dependency audits.
- Stable map home across long flights, valid zero-coordinate handling, and container-compatible project paths.
- Private local import of manufacturer data and a clean source exporter with a checksum manifest.
- Version numbering restarts at `0.1.0-alpha.1`; the entries below record development before the public release.
- Vulnerabilities and conduct reports go through GitHub private reporting; the staging deploy runs only when enabled.

### Fixed

- The simulation record of a pipeline run is saved again. The graph ran on a worker thread with its own event loop but saved the record through the API loop's connection pool, which failed with "attached to a different loop"; the evaluation and report of that attempt were then not stored either. Graph nodes now use the run's own session factory.
- The Overview and Simulation pages no longer show "Rejected" between the end of a flight and the gate's evaluation.
- After a page reload the dashboard pages say the run is loading while it is restored, instead of "No design yet".

## Development history before the public release

These milestones were numbered 1.0.0 and 1.1.0 in the private development repository. They were never published as releases.

### Internal milestone 1.1.0 — 2026-05-12

#### Added — Phase 8: Real MAVSDK Code Generation
- **`apps/api/utils/coordinates.py`** — pure NED↔LLA conversions (spherical + WGS84 ellipsoid), haversine distance, polar-singularity guard. No external dependencies so it can run inside generated mission code on Pixhawk-embedded targets.
- **`apps/api/pipelines/mission_builder.py`** — `Waypoint` (NED + altitude + speed + camera/gimbal/loiter knobs), `MissionPlanSpec`, `MissionItemPayload` (1:1 mirror of `mavsdk.mission.MissionItem`), `build_mission_items()`, `render_mission_items_python()`.
- **`apps/api/pipelines/mavsdk_templates.py`** — `render_real_mavsdk_mission()` produces a complete runnable Python file: imports mavsdk, reads `DRONEPROMPT_MAVSDK_ADDRESS` from env, waits for connection + `is_global_position_ok`, runs preflight (gyro/accel/GPS≥3D≥8sats/battery≥50%), optionally uploads a circular geofence, arms, takes off, uploads + starts the mission, tracks `mission_progress`, RTLs, waits for `LandedState.ON_GROUND`.
- GSCE prompt templates (`code_generator.py`) rewritten — Layer 2 (Skill APIs) and Layer 4 (Examples) now teach the LLM real MAVSDK with an explicit "NOT ALLOWED" list of legacy skill-API names.
- SafetyLayer `APPROVED_FUNCTIONS` extended with every real MAVSDK plugin method plus stdlib helpers the template needs.

#### Added — Phase 9: High-Fidelity SITL Environment
- **`apps/api/services/sitl_environment.py`** — `SITLEnvironmentSpec` covering wind (N/E + gust + Dryden turbulence), GPS (jitter / multipath / sat count), compass interference, baro drift, telemetry rate, RC signal-loss schedule.
- 9 pre-built scenarios: `calm_indoor`, `ideal_outdoor`, `light_breeze`, `windy_outdoor`, `storm`, `gps_denied`, `gps_multipath_urban`, `magnetic_interference`, `hot_day_thermal_battery`.
- PX4 parameter mapping: emits `param set` commands for `EKF2_GPS_CTRL`, `EKF2_REQ_NSATS`, `EKF2_MAG_NOISE`, `EKF2_BARO_NOISE`, `COM_ARM_WO_GPS`, `SDLOG_PROFILE` (canonical PX4 v1.16 names).
- Gazebo commands: emits `gz topic -t /wind -m gz.msgs.Wind …` commands the orchestrator pipes into the live world.

#### Added — Phase 10: PID Auto-Tune Agent
- **`apps/api/agents/pid_tune_agent.py`** — `PIDGains` (with `MC_*_RATE_*` PX4 prefix mapping), `diagnose_axis()` three-tier classifier (oscillation / lag / saturation), adjusters (`adjust_for_oscillation` reduces P by 15% + raises D by 20%, `adjust_for_lag` raises P by 10%, `adjust_for_saturation` reduces all three terms by 15%), envelope clamping.
- Ziegler-Nichols seed: `fit_step_response()` fits a FOPDT model, `ziegler_nichols_seed()` emits initial P/I/D using classic open-loop rules.
- `PIDTuneAgent.tune()` — runs roll/pitch diagnosis, overrides with vehicle-wide pullback when motor saturation ≥ 95%.
- `render_param_script()` — produces a runnable PX4 `param set` script with one comment line per update.

#### Added — Phase 11: Production-Grade Evaluator
- **`apps/api/pipelines/flight_quality_solver.py`** — pure-Python solver (no numpy/scipy) producing `FlightQualityMetrics`: cross-track error (mean + max), position/altitude RMSE, heading RMSE, roll/pitch oscillation freq + power (naive DFT), attitude RMS, motor saturation peak/mean, hover drift, RTH closure error, tracking/stability/overall grades A-F.
- `MonitorAgent` integration — new `compute_flight_quality_metrics` tool in `_TOOL_DISPATCH` that aggregates across iterations (best tracking, worst stability, worst saturation, worst RTH).
- `MissionMetrics` extended with all Phase 11 fields; `_compute_score` blends in `flight_quality_score` with a renormalised weight, falling back to the legacy `path_efficiency` when Phase 11 metrics weren't computed.

#### Added — Phase 13: Failure Injection Harness
- **`apps/api/services/failure_injector.py`** — 7 `FailureScenario` cases (GPS jam, battery cell open, low-battery RTL, RC signal loss, motor failure, compass offset, ESC overheat), each with **MTTD / MTTSS budgets**.
- 3 synthetic telemetry stream generators (`make_battery_drain_stream`, `make_gps_loss_stream`, `make_connection_loss_stream`) — pure functions, no SITL needed.
- 3 reference protocol probes mirroring IP001-IP004: `battery_monitor_probe`, `connection_watchdog_probe`, `gps_loss_probe`.
- Battery stream design: linear drain crosses the trigger threshold exactly at `inject_at_t_s`, so MTTD measures the protocol latency from the fault crossing.

#### Added — Phase 14: Field-Ready Engineering Deliverables
- **`apps/api/pipelines/engineering_deliverables.py`** — `DeliverableBundle` Pydantic input model + 6 renderers:
  - `render_bom_csv` (RFC-4180 with totals row)
  - `render_bom_markdown` (GFM table)
  - `render_assembly_guide` (8-step canonical multicopter build order, propellers LAST)
  - `render_preflight_checklist` (4 sections, paper-usable `- [ ]` checkboxes)
  - `render_maintenance_schedule` (per flight / 10 h / 50 h / 200 h-annual)
  - `render_engineering_report` (top-level verdict + artefact paths)
- `render_all_deliverables()` returns `{filename: contents}` for all 6 files.

#### Added — Tests (+143 regression tests)
- `tests/test_mavsdk_codegen.py` — **26 new tests**: coordinate round-trip, pole-singularity guard, spherical vs WGS84 agreement, mission builder, "no skill-API leak" template guard, every required real MAVSDK call present.
- `tests/test_flight_quality.py` — **23 new tests**: PathSegment cross-track, peak-frequency DFT, perfect tracking grades A, drift > 5 m drops grade, oscillation/saturation warnings, MonitorAgent dispatch + score blending, regression for the May-2026 e2e drift case.
- `tests/test_failure_injector.py` — **19 new tests**: catalog completeness, MTTD ≤ MTTSS invariants, stream shapes, per-probe behaviour, full-scenario passes (low_battery / cell_open / radio_signal / gps_jam), failure paths.
- `tests/test_sitl_environment.py` — **28 new tests**: validation, wind/GPS classification, 9 scenarios retrievable, PX4 param mapping, Gazebo command rendering.
- `tests/test_pid_tune.py` — **22 new tests**: diagnosis, adjusters, FOPDT step-response fit, Z-N seed, agent end-to-end, Phase 11 integration.
- `tests/test_engineering_deliverables.py` — **25 new tests**: ComponentLine arithmetic, BOM (CSV + Markdown), assembly guide, preflight, maintenance, engineering report, all-in-one renderer.

#### Fixed
- Monitor report no longer claims "this report is not based on real SITL telemetry" after a successful `real_wsl_sitl` run. `ElJazariState` was missing `mission_started`, `mission_completed`, `telemetry_source`, etc., so LangGraph silently dropped them when `run_simulation` returned its partial dict. Added the missing channels to `graph/state.py`.
- `/api/projects/{id}/simulate/results` returned an empty list after orchestrator-driven simulations because only the manual `/simulate/start` router persisted Simulation rows. Added `_persist_simulation_record()` to `graph/nodes.py`, called from `run_simulation` as best-effort persistence (a DB error never aborts the pipeline).
- `resolve_sitl_runtime()` reported `is_real=true` for `mode=docker` without verifying that any PX4 SITL container was actually running. Added `_probe_docker_sitl_container()` that runs `docker ps` against configurable filters; falls back to mock with explicit diagnostics when nothing is found. `DRONEPROMPT_SKIP_DOCKER_PROBE=1` bypasses for users managing container lifecycle externally.

#### Test suite
- Backend: **717 passed / 0 failed** (was 540 → +143 new regression tests, +4 fixes in pre-existing tests)
- Frontend: `npm run lint`, `npx tsc --noEmit`, and `npm run build` continue to pass.

### Internal milestone 1.0.0 — 2026-05-09

#### Added
- **5 AI Agents + LangGraph Orchestrator**: Design, Code (GSCE), Safety, Test (Closed-Loop), Monitor (ReActEval)
- **Safe-LLM Supervision Layer**: 3-pillar safety (AST + Reachability + RAG Compliance) with code injection
- **Next.js 14 Dashboard**: 7 pages — Landing, Overview, Design, Code, Simulate, Monitor, WebGCS
- **Interactive 3D Viewer**: React Three Fiber with DroneViewer, FlightPathViewer, TelemetryOverlay, URDF loader
- **WebGCS Ground Control Station**: Leaflet map, SVG instruments, PID tuning, mission controls, event log
- **Real-time Pipeline**: FastAPI + Socket.io WebSocket layer with 20+ REST endpoints
- **HITL (Human-in-the-Loop)**: Design and code approval gates with LangGraph `interrupt_before`
- **AircraftVerse RAG**: ChromaDB-powered design search over 27,714 UAV designs
- **JSON → URDF/SDF Pipeline**: Full conversion with physics validation
- **Telemetry Normalization**: `normalizeTelemetryPoint()` for crash-free UI rendering
- **Windows WSL SITL**: Real PX4/Gazebo simulation through WSL with MAVSDK telemetry bridge
- **Docker Production Stack**: 10 services (API, Web, PostgreSQL, Redis, Ollama, Celery, Flower, Prometheus, Grafana, Nginx)
- **Kubernetes Manifests**: 8 service directories with HPA and GPU scheduling
- **CI/CD**: GitHub Actions — lint, test, build, Docker push (staging + production)
- **Monitoring**: Prometheus metrics scraping + Grafana dashboards + alerting rules
- **540+ Backend Tests**: 8 test suites covering all agents and pipelines
- **English**: Prompt support with an LTR interface

#### Security
- JWT authentication with configurable expiry
- Rate limiting (SlowAPI)
- Security headers (CSP, HSTS, X-Frame-Options)
- CORS and Trusted Host middleware
- Docker: no-new-privileges, cap_drop ALL, read-only filesystems
