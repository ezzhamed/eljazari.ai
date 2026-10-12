# eljazari.ai — Phase Map & Current Status

This document tracks the implemented phases of the physics-precise drone prompt pipeline.

## Implementation Checklist

```text
Phase 0: Infrastructure
  0.1  ✅  Project Scaffolding (Turborepo monorepo)
  0.2  ✅  Database Layer (PostgreSQL + SQLAlchemy 2.0 + Alembic)

Phase 1: MVP Core
  1.1  ✅  Code Generation Agent (GSCE Framework — 7.6% → 90.9% success)
  1.2  ✅  Simulation & Test Agent (Closed-Loop — 85% success, 98.5% completeness)
  1.3  ✅  MVP Frontend (Next.js 14 App Router — 6 dashboard pages)
  1.4  ✅  API + WebSocket Layer (FastAPI + Socket.io — 20+ endpoints)

Phase 2: Orchestration & Safety
  2.1  ✅  LangGraph Orchestrator (10-node StateGraph + HITL + PostgreSQL checkpointing)
  2.2  ✅  Safe-LLM Supervision Layer (3-Pillar: AST + Reachability + RAG)

Phase 3: Design & 3D
  3.1  ✅  Design Agent (AircraftVerse RAG) + JSON→URDF/SDF Pipeline
  3.2  ✅  Interactive 3D Viewer (DroneViewer + FlightPathViewer + TelemetryOverlay)

Phase 4: Monitor & GCS
  4.1  ✅  Monitor Agent (ReActEval) + Performance Monitor (Head+Worker)
  4.2  ✅  WebGCS Ground Control Station (Leaflet + MAVLink + Instruments + PID)

Phase 5: DevOps & Scale
  5.1  ✅  Deployment (Docker · Kubernetes · GitHub Actions CI/CD · Prometheus/Grafana)
  5.2  📋  Fleet Management (multi-drone coordination + K8s operator)

Phase 6: Real Component Engineering Layer
  6.1  ✅  Component Selector Agent + parts catalog (motors / ESCs / props / batteries)
  6.2  ✅  Aerodynamic & performance solver (hover endurance, T/W, disc loading)

Phase 8: Real MAVSDK Code Generation
  8.1  ✅  NED↔LLA coordinate utility (spherical + WGS84, pole-singularity guard)
  8.2  ✅  Mission Item builder (Waypoint → MissionPlan → real MAVSDK code)
  8.3  ✅  GSCE templates rewritten to emit pure MAVSDK — no skill-API adapter

Phase 9: High-Fidelity SITL Environment
  9.1  ✅  9 environment scenarios (calm_indoor … storm … gps_denied)
  9.2  ✅  PX4 parameter overrides + Gazebo `gz topic` command generation

Phase 10: PID Auto-Tune Agent
  10.1 ✅  Per-axis diagnosis (oscillation / lag / saturation) + 3 adjusters
  10.2 ✅  Ziegler-Nichols open-loop seed from a step-response fit
  10.3 ✅  PX4 `param set` script rendering with envelope clamping

Phase 11: Production-Grade Evaluator
  11.1 ✅  Cross-track error (mean + max), position/altitude RMSE, heading RMSE
  11.2 ✅  Roll/pitch oscillation FFT (frequency + power), attitude RMS
  11.3 ✅  Motor saturation peak/mean, hover drift, RTH closure error
  11.4 ✅  Per-pillar engineering grades (tracking / stability / overall A-F)

Phase 13: Failure Injection Harness
  13.1 ✅  7 scenarios: GPS jam, battery cell open, low-battery RTL,
           RC signal loss, motor failure, magnetic interference, ESC overheat
  13.2 ✅  MTTD / MTTSS budget validation per scenario
  13.3 ✅  Built-in probes for IP001 (battery) / IP002 (connection) / GPS-loss

Phase 14: Field-Ready Engineering Deliverables
  14.1 ✅  BOM (CSV + Markdown) with supplier / part-number / cost / weight
  14.2 ✅  Assembly guide (8-step canonical multicopter build order)
  14.3 ✅  Pre-flight checklist (4 sections, paper-usable)
  14.4 ✅  Maintenance schedule (per flight / 10 h / 50 h / 200 h-annual)
  14.5 ✅  Top-level engineering report tying every artefact + verdict

Phase 15: Physics Precision & Compliance Reporting
  15.1 ✅  Effective Propulsive Factor = actual_speed / pitch_speed (replaces TWR-based slip factor)
  15.2 ✅  Correct CdA separation: Frontal Area A + Drag Coefficient Cd + Effective CdA = Cd × A
  15.3 ✅  Unified canonical mass: single source (component table) used for physics, URDF, compliance
  15.4 ✅  Agility metrics: Wheelbase, Moment of Inertia, Max Angular Acceleration, Roll Rate
  15.5 ✅  CONDITIONAL compliance: hover endurance (8 min) vs racing endurance (5.6 min) split clearly
  15.6 ✅  Mission speed distinction: Commanded (27 m/s) vs Simulated Achieved (N/A — SITL required)
  15.7 ✅  Safety status: PASSED WITH WARNING (issues > 0) + dual-failsafe priority documentation
  15.8 ✅  Terminology: PX4 mission implementation using MAVSDK over MAVLink

Phase 12: Pixhawk HITL Bridge
  12.1 📋  Awaiting hardware: Pixhawk Cube Orange + USB-to-serial bench rig

Phase 5.3: Real-drone HIL flight testing
  5.3  📋  Field validation on bench-built drone
```

---

### Local v1 status

- Backend tests currently pass locally: **`778 passed / 0 failed`** (up from 717 — added Phase 15 physics precision tests)
- Suite-by-suite headline tests:
  - `tests/test_mavsdk_codegen.py`              — `26 passed`  (Phase 8 — real MAVSDK)
  - `tests/test_flight_quality.py`              — `23 passed`  (Phase 11 — production evaluator)
  - `tests/test_failure_injector.py`            — `19 passed`  (Phase 13 — failure injection)
  - `tests/test_sitl_environment.py`            — `28 passed`  (Phase 9  — environment scenarios)
  - `tests/test_pid_tune.py`                    — `22 passed`  (Phase 10 — PID auto-tune)
  - `tests/test_engineering_deliverables.py`    — `25 passed`  (Phase 14 — BOM / assembly / preflight)
  - `tests/test_design_agent.py`                — `61 passed`  (Phase 15 — physics precision & compliance)
- Frontend production build still passes locally with `npm run build`
- Frontend has the `Live Mission Theater` dashboard plus an instant visual demo mode
- The end-to-end path now ships engineering-grade artefacts:
  `create -> design -> approve -> urdf -> code (real MAVSDK) -> safety -> approve -> simulation (50 Hz + env scenarios) -> evaluator (RMSE + oscillation + saturation) -> PID tune -> monitor -> deliverables (BOM / assembly / preflight / maintenance / report)`
- **Phase 15 (Physics Precision):** PerformanceCalculator now reports Effective Propulsive Factor
  (actual/pitch_speed = 0.763 for typical FPV racing quad) instead of the former TWR-based slip factor;
  CdA is correctly separated into Frontal Area A, Drag Coefficient Cd, and Effective CdA = Cd × A;
  mass is unified from one canonical component table (mass difference = 0.0 g); agility metrics
  (wheelbase, MoI, angular acceleration, roll rate) are computed from frame geometry.
- Compliance reporting is now `CONDITIONAL` when hover endurance passes but racing endurance differs,
  with explicit distinction between Commanded Mission Speed (27 m/s) and Simulated Achieved Speed
  (requires PX4 SITL). Safety status is `PASSED WITH WARNING` when issues > 0, with dual-failsafe
  priority (PX4 onboard RTL first, Python companion watchdog second).
- Windows supports a real `PX4/Gazebo SITL` path through `WSL`, in addition to the built-in fallback simulation path
- The `WSL` launcher, `Gazebo`, `PX4`, generated mission runner, and `MAVSDK` endpoint discovery are working
- Real WSL telemetry is now collected from live MAVSDK streams instead of flat fallback values
- If `wsl.exe` is inaccessible, runtime detection now falls back to `mock` with diagnostics instead of attempting a broken real launch
- `docker` SITL mode now probes for a running PX4 container before claiming `is_real=true`; with no container running, it falls back to `mock` with explicit diagnostics

### Windows WSL SITL status

The repository now includes an integrated Windows-to-`WSL` runtime for real local simulation. The current state is:

- `WSL` runtime selection is configurable via `DRONEPROMPT_SITL_RUNTIME`
- `Gazebo Harmonic` and `PX4 SITL` can be launched from the backend through `WSL`
- Direct `MAVSDK` control in the configured Ubuntu environment has been verified with `arm -> takeoff -> land`
- Generated mission code now reads the MAVSDK endpoint from `DRONEPROMPT_MAVSDK_ADDRESS`
- SafetyLayer permits only the approved MAVSDK endpoint env var and rejects unrelated `getenv()` calls
- Re-checking SafetyLayer-injected mission code is now idempotent and no longer flags its own safety protocols
- SafetyLayer now injects concrete RTL/kill wrappers, normalizes MAVSDK battery units to percent, upgrades stale injected guards, infers the mission geofence radius from generated `set_geofence(...)`, and wraps `run_mission()` so battery/connection/geofence guards run during the real mission
- The end-to-end experiment script now records safety, approval, simulation, and evaluation failures as real failed stages
- Experiment runtime metadata is serialized through a JSON-safe summary, so successful SITL runs no longer fail while writing reports
- Mission simulation timeout is now derived from generated mission duration metadata/prompt text, so a 20-minute mission is not cut off by the old altitude-based timeout
- Simulation reports now expose `mission_completed`, `telemetry_source`, `mission_error`, warnings, and SITL artifacts for evaluator and monitor stages
- The WSL mission runner now writes live telemetry snapshots during long missions, and the Windows wrapper allows extra PX4/MAVSDK bootstrap time before killing the runner
- `PX4Service.execute_mission()` can run generated helper-style missions through the WSL runner and return `telemetry_source=real_wsl_sitl`
- The WSL telemetry bridge fuses `altitude`, `odometry`, and `position_velocity_ned` streams so altitude and local path reflect the actual PX4/Gazebo run
- `MonitorAgent` now distinguishes square/coverage/survey routes from point-to-point routes, so a completed 100x100m photo loop is not penalized as a failed shortest-path trip
- Monitor reports now include telemetry provenance, mission completion rate, telemetry sample count, coverage span, closed-loop rate, and return-to-home error
- When Windows denies access to WSL, `resolve_sitl_runtime()` returns `mode=mock` and records the WSL error in diagnostics

Latest real smoke check:

```text
mission: takeoff 5m -> move 10m north / 5m east -> land
telemetry_source: real_wsl_sitl
mission_started: true
mission_completed: true
max_alt_m: 5.0
north_span_m: 9.71
east_span_m: 5.0
warnings: []
```

Latest full end-to-end WSL SITL experiment:

```text
prompt: aerial-photo quadcopter, 15m altitude, 100x100m square path, 20-minute flight, aerial camera
project_id: 129ba673-52f2-43cc-b91e-5369bea6b79e
total_duration: 2139.71s
stages: 11 passed, 0 failed, 0 skipped
safety_passed: true
code_status: approved
runtime: wsl, real=true
telemetry_source: real_wsl_sitl
mission_completed: true
telemetry_points: 2450
semantic_observations: 493
simulation_score: 85.0
monitor_score: 96.5
monitor_recommendation: report requested-vs-actual square size, corner error, lap count, and cruise-speed consistency from telemetry
mission_error: none
```

`apps/api/scripts/run_experiment.py` writes its artifacts to `experiment-results/`,
which is not tracked — the reports are a product of a run, not source:

```text
experiment-001-full-pipeline-report.html    rendered report
experiment-001-full-pipeline.json           raw run data
experiment-001-full-pipeline-summary.md     summary
experiment-001-full-pipeline-mission-code.py  the mission that was flown
```

Recommended `.env` values for the Windows `WSL` path:

- `DRONEPROMPT_SITL_RUNTIME=docker` (Recommended) or `wsl`
- `DRONEPROMPT_WSL_DISTRO=Ubuntu`
- `DRONEPROMPT_WSL_USER=<your Linux user>`
- `DRONEPROMPT_WSL_PX4_DIR=/home/<your Linux user>/PX4-Autopilot`
- `DRONEPROMPT_WSL_WORLDS_PATH=/home/<your Linux user>/PX4-Autopilot/Tools/simulation/gz/worlds`
- `DRONEPROMPT_WSL_PYTHON_BIN=/home/<your Linux user>/droneprompt-mavsdk-venv/bin/python`

Check the runtime before running a real SITL experiment:

```bash
cd apps/api
python -m scripts.check_sitl_runtime
python -m scripts.check_sitl_runtime --strict
```

Use `--strict` when you want the command to fail unless `native` or `wsl` real SITL is actually available.

