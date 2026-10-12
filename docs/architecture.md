# eljazari.ai — Architecture & Implementation Details

This document covers the deep architectural principles, data flows, and module-level implementation details of the eljazari.ai multi-agent system.

---

## Architecture

### Core Principle
**5 agents + 1 orchestrator**, coordinated by LangGraph as the sole orchestration engine. Every agent communicates through a shared `ElJazariState` — never directly with each other.

### Agent Map

| Agent | Role | Technology | Status | Paper |
|-------|------|------------|--------|-------|
| **Orchestrator** | Central coordinator, HITL, checkpointing | LangGraph + LangSmith | ✅ Built | — |
| **Agent 1: Design** | Text → JSON design tree | AircraftVerse RAG + SQLite/NumPy | ✅ Built | arxiv:2504.08981 |
| **Agent 2: Code** | Design + mission → PX4 code | GSCE + Ollama (Qwen2.5) | ✅ Built | arxiv:2502.12531 |
| **Agent 3: Safety** | 3-pillar AST + reachability + RAG | SafeLLMSupervisionLayer | ✅ Built | — |
| **Agent 4: Test** | Closed-loop sim + semantic eval | Gazebo + MAVSDK | ✅ Built | arxiv:2507.01930 |
| **Agent 5: Monitor** | Logs → performance analysis | ReActEval (Hitachi) + Head/Worker | ✅ Built | arxiv:2510.00259 |

### Data Flow

```
User Prompt (English)
    │
    ▼
[Orchestrator] — LangGraph 10-node StateGraph + AsyncPostgresSaver
    │
    ├─ parse_prompt
    │
    ├─ generate_design ◄──────────────────────── (rejected + feedback loop)
    │       │
    │  ◄── HITL: human_review_design ──────────► interrupt_before
    │       │ approved
    ├─ convert_to_urdf ──────────────────────► URDF/SDF files
    │
    ├─ generate_code ◄──── (safety fail / code rejected / sim fail loop)
    │       │
    ├─ safety_check  ─────────────────────── SafeLLMSupervisionLayer
    │   │  Pillar 1: Static AST (SR001–SR008)
    │   │  Pillar 2: Reachability (RR001–RR005)
    │   │  Pillar 3: RAG Compliance (CR001–CR010)
    │   │  Injection: IP001–IP004 (battery/conn/geofence/emergency)
    │   │  if critical → back to generate_code (max 5×)
    │   │
    │  ◄── HITL: human_review_code ────────────► interrupt_before
    │       │ approved
    ├─ run_simulation ─── Gazebo SITL ──────► telemetry stream
    │
    ├─ evaluate_simulation
    │       │  Semantic Converter → English NL observations
    │       │  LLM Evaluator → pass/fail + score
    │       │  if fail (max 5×) → back to generate_code
    │
    └─ generate_monitor_report ──────────────► ReActEval performance report
           │
           ▼
    Output: URDF · Code · FullSafetyReport · PDF · WebGCS
```

### Real-time Architecture

```
Browser (Socket.io-client)
    ↕  /socket.io/
FastAPI + python-socketio (ASGIApp)
    ↕  broadcast_to_project()
LangGraph pipeline background tasks
```

---

## Implementation Details

### Phase 1.1 — Code Generation Agent (GSCE)

GSCE (Guidelines · Skill APIs · Constraints · Examples) 4-layer framework for PX4/MAVSDK code generation. **Key metric:** 7.6% → 90.9% success rate (arxiv:2502.12531).

| Component | File | Description |
|-----------|------|-------------|
| GSCE Prompt Builder | `pipelines/code_generator.py` | 4-layer prompt: Guidelines + Skill APIs + Constraints + Few-shot examples |
| Code Generation Agent | `agents/code_agent.py` | Pydantic structured output, Tenacity retry, LangSmith tracing |
| AST Validation | `agents/code_agent.py` | 7-check validator: syntax, landing, async, battery, preflight, approved-only functions |

---

### Phase 1.2 — Simulation & Test Agent (Closed-Loop)

5-step closed-loop pipeline: Code → Gazebo SITL → Telemetry → Semantic Observations → LLM Evaluation → Feedback → Refinement. **Key metric:** 85% success, 98.5% completeness (arxiv:2507.01930).

| Component | File | Description |
|-----------|------|-------------|
| Semantic Converter | `pipelines/semantic_converter.py` | Numeric telemetry → English natural language observations |
| Gazebo Service | `services/gazebo_service.py` | Subprocess lifecycle management, port allocation, platform-agnostic |
| PX4 Service | `services/px4_service.py` | MAVSDK mission execution, simulated telemetry fallback |
| Test Agent | `agents/test_agent.py` | `SimulationTestAgent` with `run_closed_loop()`, max 5 iterations |

**English semantic output example:**
- `{alt: 15.2}` → `"altitude 15.2m"`
- `{battery: 65}` → `"good battery (65%)"`
- `{yaw: 45}` → `"heading northeast"`

---

### Phase 1.3 — MVP Frontend (Next.js 14)

Full App Router dashboard with real-time pipeline visualization.

| Page / Component | File | Description |
|-----------------|------|-------------|
| Hero / Landing | `app/page.tsx` | Prompt input, quick examples, animated pipeline steps |
| Dashboard | `app/dashboard/` | Two-column layout with animated sidebar timeline |
| Design Page | `dashboard/design/page.tsx` | 3D viewer + recursive JSON tree + SpecCard grid + HITLModal |
| Code Page | `dashboard/code/page.tsx` | Monaco editor + GSCE constraints + Safety report |
| Simulate Page | `dashboard/simulate/page.tsx` | XTerm terminal + 3D/2D flight path + Recharts + Semantic feed |
| Monitor Page | `dashboard/monitor/page.tsx` | KPI cards + ScoreRing + QualityRadar + Recommendations |
| 3D Viewer | `dashboard/viewer/page.tsx` | Full-screen drone model / flight path toggle |

**DroneViewer features:** parametric mesh, URDF overlay, view modes (solid/wireframe/transparent), animated propellers, click annotations, screenshot export, camera presets, ground grid + compass.

**FlightPathViewer features:** smooth TubeGeometry path, animated drone follower, waypoint hover tooltips, altitude profile chart, playback controls (play/pause/reset/seek).

---

### Phase 1.4 — API + WebSocket Layer

Complete connection layer between Backend and Frontend.

| File | Description |
|------|-------------|
| `middleware/auth.py` | JWT via `python-jose`: `AuthUser` dependency, dev-mode passthrough |
| `middleware/rate_limit.py` | SlowAPI: 200 req/min default, per-endpoint limits |
| `ws/socket_manager.py` | `python-socketio` `AsyncServer` singleton + `broadcast_to_project()` |
| `ws/events.py` | All Socket.io event handlers |
| `lib/api.ts` | Typed API client: `ApiError`, `getToken/setToken`, all 20+ endpoints |
| `lib/stores/ws-store.ts` | Typed Socket.io client, auto `join_project`, routes all events to store |

---

### Phase 2.1 — LangGraph Orchestrator

Central multi-agent coordinator using LangGraph StateGraph.

| Component | File | Description |
|-----------|------|-------------|
| Shared State | `graph/state.py` | `ElJazariState` TypedDict — 30+ fields, `Annotated[list, add]` accumulators |
| Node Implementations | `graph/nodes.py` | 10 async nodes: `parse_prompt → generate_design → ... → generate_monitor_report` |
| Conditional Edges | `graph/edges.py` | 10 routing functions: redesign, recode, retry simulation, max-iteration guards |
| Workflow Definition | `graph/workflow.py` | `StateGraph` compiled with `interrupt_before` + PostgreSQL checkpointer |
| Orchestrator Class | `agents/orchestrator.py` | `run()` / `resume()` / `approve()` / `reject()` / `get_state()` / `get_history()` |

**Key capabilities:**
- `interrupt_before` HITL — graph suspends before human review nodes
- `AsyncPostgresSaver` checkpointing — pipeline survives process restarts
- LangSmith tracing — activated when `LANGCHAIN_TRACING_V2=true`
- Max 5 simulation iterations before forcing a monitor report

---

### Phase 2.2 — Safe-LLM Supervision Layer

3-pillar proactive safety system intercepting every piece of LLM-generated code before it reaches the simulator.

```
Input: LLM-generated PX4/MAVSDK code
          │
          ▼
┌─────────────────────────────────────────────────────────┐
│ Pillar 1: Static AST Analysis                           │
│  SR001 function whitelist · SR002 landing · SR003 try   │
│  SR004 altitude · SR005 speed · SR006 await             │
│  SR007 preflight · SR008 connection                     │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ Pillar 2: Reachability Analysis                         │
│  RR001 battery RTL margin (≥30%)                        │
│  RR002 geofence per-waypoint · RR003-004 altitude       │
│  RR005 speed per-waypoint · Total path + battery math   │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ Pillar 3: RAG Compliance Filter                         │
│  CR001-CR010 pattern-match vs flight_rules.json         │
│  Forbidden: while True, set_motor_speed, eval/exec      │
│  Required: async def run(), get_battery                 │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ Injection Engine                                        │
│  IP001 battery_monitor · IP002 connection_watchdog      │
│  IP003 geofence_enforcer · IP004 emergency_handler      │
└─────────────────────────────────────────────────────────┘
          │
          ▼
Output: FullSafetyReport + patched code (or rejection + feedback)
```

| Component | File | Description |
|-----------|------|-------------|
| Main Layer | `agents/safety_layer.py` | `SafeLLMSupervisionLayer` — async API orchestrating all 3 pillars |
| Pydantic Models | `agents/safety_models.py` | `FlightConstraints`, `SafetyIssue`, `FullSafetyReport`, etc. |
| Rules DB | `data/safety-rules/flight_rules.yaml` + `.json` | Human-readable + machine-readable safety rules |

---

### Phase 3.1 — Design Agent + JSON→URDF Pipeline

Design Agent (AGENT model — Option B RAG) + complete JSON→URDF/SDF conversion pipeline.

| Component | File | Description |
|-----------|------|-------------|
| `DesignAgent` | `agents/design_agent.py` | Prompt → `DesignResult` (RAG + LLM + physics) |
| `AircraftVerseRAG` | `agents/design_agent.py` | Embedded SQLite/NumPy index over the bundled real 27,714-design AircraftVerse corpus; bounded indexing and evaluated-multirotor filters |
| `PerformanceCalculator` | `agents/design_agent.py` | Physics: hover time, max speed, range, weight, T/W |
| `JsonToUrdfPipeline` | `pipelines/json_to_urdf.py` | Links + joints + meshes + validate + SDF generation |

**RAG Pipeline:** `User prompt → sentence-transformer/Ollama embeddings → SQLite/NumPy retrieval (k=5) → LLM → DroneDesignTree → PerformanceEstimate`

**Fallback chain:** `rag_llm` → `llm_only` → `rule_based`

---

### Phase 3.2 — Interactive 3D Viewer

Full-featured interactive 3D drone design and flight-path viewer (React Three Fiber).

| Component | File | Description |
|-----------|------|-------------|
| `DroneViewer` | `components/drone-viewer/DroneViewer.tsx` | Parametric drone mesh + URDF overlay + toolbar |
| `FlightPathViewer` | `components/drone-viewer/FlightPathViewer.tsx` | TubeGeometry path + animated drone + altitude profile |
| `TelemetryOverlay` | `components/drone-viewer/TelemetryOverlay.tsx` | Real-time HUD: speed/alt/battery/compass/status |
| `URDFLoader` | `components/drone-viewer/URDFLoader.tsx` | Custom XML URDF parser → Three.js meshes |

**DroneViewer features:** parametric mesh, URDF overlay, view modes (solid/wireframe/transparent), animated propellers, click annotations, screenshot export, camera presets, ground grid + compass.

**FlightPathViewer features:** smooth TubeGeometry path, animated drone follower, waypoint hover tooltips, altitude profile chart, playback controls (play/pause/reset/seek).

---

### Phase 4.1 — Performance Monitor Agent (Hierarchical ReActEval)

Head + Worker architecture with per-task 4-phase ReActEval cycle for deep UAV performance analysis.

| Component | File | Description |
|-----------|------|-------------|
| Head Agent | `agents/performance_monitor.py` | Plan → ReActEval cycle → Recommendations → Compile report |
| Models | `agents/performance_models.py` | 16+ Pydantic schemas for analysis pipeline |
| Viz Helpers | `utils/visualization_helpers.py` | Pre-computed chart data for Recharts/Three.js/SVG |

**6 Worker Agents:**

| Worker | Key Metrics |
|--------|-------------|
| Trajectory | CTE, path efficiency, altitude RMSE, waypoint accuracy, DTW |
| Battery | mAh/km, voltage sag, RTL reserve %, discharge linearity |
| Motors | RPM balance, thermal load, response latency, per-motor health |
| Stability | Roll/pitch RMSE, vibration grade, hover hold error |
| GPS | HDOP, satellite count, fix loss, position noise |
| Anomaly | Z-score, near-misses, GPS loss, battery spike, speed violation |

**Scoring:** `trajectory×0.25 + battery×0.20 + stability×0.20 + anomaly×0.20 + gps×0.10 + motors×0.05`
**Grades:** A(≥90) | B(≥75) | C(≥60) | D(≥45) | F(<45)

---

### Phase 4.2 — WebGCS Ground Control Station

Full web-based GCS with real-time Leaflet map, telemetry instruments, MAVLink bridge, PID tuning, CSV event log.

| Component | File | Description |
|-----------|------|-------------|
| `LeafletMap` | `components/gcs/LeafletMap.tsx` | ESRI satellite tiles, drone marker, flight path, geofence, waypoint drawing |
| `TelemetryDashboard` | `components/gcs/TelemetryDashboard.tsx` | SVG instruments: compass, altitude, speed, battery, GPS, motors |
| `ParameterTuning` | `components/gcs/ParameterTuning.tsx` | PID gains panel: Roll/Pitch/Yaw/Throttle sliders |
| `MissionControls` | `components/gcs/MissionControls.tsx` | ARM/TAKEOFF/LAND/RTL/PAUSE/E-STOP with confirm dialogs |
| `EventLog` | `components/gcs/EventLog.tsx` | 200-entry ring buffer, severity filters, CSV export |
| `MAVLinkBridge` | `ws/mavlink_bridge.py` | MAVSDK-Python ↔ Socket.io bridge (stub mode if no MAVSDK) |

**Layout:**
```
┌─────────────────────┬─────────────────────┐
│  Leaflet Map        │  Telemetry Dashboard│
│  + Waypoint Drawing │  + Attitude + HUD   │
├─────────────────────┼─────────────────────┤
│  Mission Controls   │  PID Tuning Panel   │
├─────────────────────┴─────────────────────┤
│  Event Log (full width, 200-entry buffer) │
└───────────────────────────────────────────┘
```

---

### Phase 5.1 — DevOps & Scale

| Component | Files | Description |
|-----------|-------|-------------|
| Docker | `Dockerfile.api`, `.web`, `.sim`, `.celery` | Multi-stage production builds |
| Compose | `docker-compose.yml`, `docker-compose.dev.yml` | Production (6 services) + Dev (hot reload) |
| Kubernetes | `infrastructure/k8s/` | 8 service directories + HPA + GPU scheduling |
| CI/CD | `.github/workflows/` | `ci.yml` + `cd-staging.yml` + `cd-production.yml` |
| Monitoring | `infrastructure/monitoring/` | Prometheus config + Grafana dashboards + alerting rules |

---

### Phase 8 — Real MAVSDK Code Generation

Replaces the abstract `drone.fly_to_relative(...)`, `drone.takeoff(altitude_m=...)`, `drone.set_geofence(...)` "skill API" layer with **direct MAVSDK calls** so the generated mission file runs unchanged on SITL, HITL, or real hardware.

| Component | File | Description |
|-----------|------|-------------|
| Coordinate utility | `apps/api/utils/coordinates.py` | NED↔LLA (spherical + WGS84), haversine, pole-singularity guard |
| Mission Item builder | `apps/api/pipelines/mission_builder.py` | `Waypoint` (NED) → `MissionItemPayload` → renderable Python |
| Real MAVSDK template | `apps/api/pipelines/mavsdk_templates.py` | Full runnable mission: `drone.connect` → `core.connection_state` → `telemetry.health` → `action.arm/takeoff` → `mission.upload_mission/start_mission` → `mission.mission_progress` → `action.return_to_launch` |
| GSCE prompt update | `apps/api/pipelines/code_generator.py` | Layer 2 (Skill APIs) and Layer 4 (Examples) rewritten to teach the LLM real MAVSDK with an explicit "NOT ALLOWED" list of legacy skill-API names |
| Safety whitelist | `apps/api/agents/safety_layer.py` | `APPROVED_FUNCTIONS` extended with every real MAVSDK plugin method (`arm`, `upload_mission`, `mission_progress`, `connection_state`, `health`, ...) plus stdlib helpers the template needs |

Tests: `tests/test_mavsdk_codegen.py` — **26 passed**.

---

### Phase 9 — High-Fidelity SITL Environment

Turns the SITL stack into a real **design verification** environment instead of a calm-air smoke test.

| Component | File | Description |
|-----------|------|-------------|
| `SITLEnvironmentSpec` | `apps/api/services/sitl_environment.py` | Wind (N/E + gust + Dryden turbulence), GPS (jitter / multipath / sat count), compass interference, baro drift, telemetry rate, RC signal-loss schedule |
| 9 pre-built scenarios | (same) | `calm_indoor`, `ideal_outdoor`, `light_breeze`, `windy_outdoor`, `storm`, `gps_denied`, `gps_multipath_urban`, `magnetic_interference`, `hot_day_thermal_battery` |
| PX4 param mapping | (same) | Translates spec to `EKF2_GPS_CTRL`, `EKF2_REQ_NSATS`, `EKF2_MAG_NOISE`, `EKF2_BARO_NOISE`, `COM_ARM_WO_GPS`, `SDLOG_PROFILE` (canonical PX4 v1.16 names) |
| Gazebo commands | (same) | Emits `gz topic -t /wind -m gz.msgs.Wind …` commands the orchestrator pipes into the live world |

Tests: `tests/test_sitl_environment.py` — **28 passed**.

---

### Phase 10 — PID Auto-Tune Agent

Closes the control loop in the design pipeline: reads the Phase 11 flight-quality metrics, diagnoses which axis has which issue, and emits typed `PIDGainUpdate` records ready to apply via PX4 `param set`.

| Component | File | Description |
|-----------|------|-------------|
| `PIDGains` | `apps/api/agents/pid_tune_agent.py` | One gain set per axis (P/I/D/FF) with PX4 prefix mapping (`MC_ROLLRATE`/`MC_PITCHRATE`/`MC_YAWRATE`) |
| `diagnose_axis()` | (same) | Three-tier classifier: oscillation (power ≥ 1.0 AND freq ≥ 5 Hz) / lag (RMS ≥ 3°) / saturation (motor peak ≥ 95 %) |
| Adjusters | (same) | `adjust_for_oscillation` (P↓ 15 %, D↑ 20 %), `adjust_for_lag` (P↑ 10 %), `adjust_for_saturation` (all 3 terms ↓ 15 %) — clamped to per-axis envelope |
| Ziegler-Nichols seed | (same) | `fit_step_response()` fits a FOPDT model (dead-time / τ / gain), `ziegler_nichols_seed()` emits initial P/I/D using classic open-loop rules |
| `PIDTuneAgent.tune()` | (same) | Single entry point — runs roll/pitch diagnosis, then overrides with full {roll, pitch, yaw} pullback when motor saturation ≥ 95 % |
| `render_param_script()` | (same) | Produces a runnable PX4 `param set` script with one comment line per update explaining the diagnosis |

Tests: `tests/test_pid_tune.py` — **22 passed**.

---

### Phase 11 — Production-Grade Evaluator

Adds the control-engineering metrics drone designers actually care about: cross-track RMSE, oscillation FFT, saturation headroom, hover drift, RTL closure error. Sits next to the legacy success-rate metric in `MonitorAgent`.

| Component | File | Description |
|-----------|------|-------------|
| `FlightQualityMetrics` | `apps/api/pipelines/flight_quality_solver.py` | Tracking RMSE, altitude RMSE, cross-track max/mean, roll/pitch oscillation freq + power, attitude RMS, motor saturation peak + mean, hover drift, RTH error, per-pillar grades (tracking / stability / overall A-F) |
| `compute_flight_quality()` | (same) | Pure-Python solver (no numpy/scipy), naive DFT for oscillation detection, FOPDT-free FFT, smart fallback when commanded path is missing |
| MonitorAgent integration | `apps/api/agents/monitor_agent.py` | New `compute_flight_quality_metrics` tool registered in `_TOOL_DISPATCH`; aggregates across closed-loop iterations (best tracking, worst stability, worst saturation, worst RTH) |
| Score blending | (same) | Replaces the path-efficiency pillar with flight_quality_score when available; falls back to the legacy weighting when not |

Tests: `tests/test_flight_quality.py` — **23 passed** (includes a regression test for the May-2026 e2e run that previously scored 5 m of drift as Grade B).

---

### Phase 13 — Failure Injection Harness

Validates that the Phase 2.2 safety injections (IP001-IP004) actually fire correctly under synthetic faults. The IP probes are tested with **MTTD / MTTSS budgets** so a SafetyLayer regression that silently breaks an RTL trigger gets caught by CI in 100 ms.

| Component | File | Description |
|-----------|------|-------------|
| `FailureScenario` | `apps/api/services/failure_injector.py` | One synthetic-fault case: inject_at_t_s + expected_reaction + expected_mttd_s + expected_mttss_s |
| 7 scenarios | (same) | `gps_jam_at_15s`, `battery_cell_open_at_10s`, `low_battery_at_20s`, `radio_signal_loss_at_25s`, `motor_3_failure_at_20s`, `compass_offset_at_5s`, `esc_overheat_at_40s` |
| Synthetic streams | (same) | `make_battery_drain_stream`, `make_gps_loss_stream`, `make_connection_loss_stream` — pure functions, no SITL needed |
| Probes | (same) | `battery_monitor_probe` (IP001), `connection_watchdog_probe` (IP002), `gps_loss_probe` (PX4-style ALTCTL fallback) |
| `run_scenario()` | (same) | Computes MTTD + MTTSS from probe output, validates against budget, returns typed `FailureInjectionResult` (Pydantic, JSON-serialisable) |

Tests: `tests/test_failure_injector.py` — **19 passed**.

---

### Phase 14 — Field-Ready Engineering Deliverables

Produces the artefacts a drone engineer takes to procurement and the workshop: BOM, assembly guide, pre-flight checklist, maintenance schedule, and top-level engineering report.

| Component | File | Description |
|-----------|------|-------------|
| `DeliverableBundle` | `apps/api/pipelines/engineering_deliverables.py` | Single input the generator needs — built from the orchestrator's existing state (design tree, Phase 6 components, Phase 11 metrics, Phase 10 PID results, Phase 13 failure passes) |
| `render_bom_csv` / `render_bom_markdown` | (same) | RFC-4180 CSV with totals row + GFM Markdown table with per-line totals |
| `render_assembly_guide` | (same) | 8-step canonical multicopter build order: Frame → Motor mounting → ESC → Flight controller → Power distribution → Radio → Calibration → Propellers (LAST) |
| `render_preflight_checklist` | (same) | 4 sections (Vehicle / Avionics / GPS-radio / Crew-site) with unticked `- [ ]` items — paper-usable |
| `render_maintenance_schedule` | (same) | 4 intervals (every flight / 10 h / 50 h / 200 h-annual) of preventative tasks pulled from FAA Part 107 + manufacturer guidance |
| `render_engineering_report` | (same) | Top-level summary tying every artefact + verdict + paths together |
| `render_all_deliverables()` | (same) | Returns `{filename: contents}` for all 6 files in one shot |

Tests: `tests/test_engineering_deliverables.py` — **25 passed**.

---

## API Reference

### REST Endpoints

```
POST   /api/auth/token                      → issue JWT

POST   /api/projects/                       → create project + start pipeline
GET    /api/projects/                       → list projects
GET    /api/projects/{id}                   → get project
DELETE /api/projects/{id}

GET    /api/projects/{id}/design            → current design
POST   /api/projects/{id}/design/approve    → HITL approve
POST   /api/projects/{id}/design/modify     → HITL modify with feedback

GET    /api/projects/{id}/code              → generated code + safety report
POST   /api/projects/{id}/code/approve      → HITL approve
POST   /api/projects/{id}/code/modify

POST   /api/projects/{id}/simulate/start    → trigger simulation
POST   /api/projects/{id}/simulate/stop
GET    /api/projects/{id}/simulate/results

GET    /api/projects/{id}/monitor/report    → performance report
GET    /api/projects/{id}/monitor/logs

GET    /api/projects/{id}/files/urdf        → download URDF
GET    /api/projects/{id}/files/code        → download Python
GET    /api/projects/{id}/files/report      → download PDF
```

### Socket.io Events

| Direction | Event | Payload |
|-----------|-------|---------|
| Client → Server | `join_project` | `{ project_id }` |
| Client → Server | `approve_design` | `{ project_id, feedback? }` |
| Client → Server | `approve_code` | `{ project_id }` |
| Client → Server | `request_modification` | `{ project_id, phase, feedback }` |
| Client → Server | `stop_simulation` | `{ project_id }` |
| Client → Server | `restart_phase` | `{ project_id, phase }` |
| Server → Client | `phase_changed` | `{ phase, project_id }` |
| Server → Client | `design_ready` | `{ project_id, data }` |
| Server → Client | `code_ready` | `{ project_id, data: { code, iteration } }` |
| Server → Client | `safety_report` | `{ project_id, data }` |
| Server → Client | `simulation_telemetry` | `{ project_id, iteration, point }` |
| Server → Client | `semantic_observation` | `{ project_id, description, severity, ... }` |
| Server → Client | `simulation_complete` | `{ project_id, passed, score, duration_s }` |
| Server → Client | `monitor_update` | `{ project_id, data }` |
| Server → Client | `agent_log` | `{ agent, status, message }` |
| Server → Client | `error` | `{ project_id, phase, message }` |

### JWT Authentication

```bash
# 1. Get token (once per session)
POST /api/auth/token → { access_token, user_id }

# 2. Use in all requests
Authorization: Bearer <token>

# Debug mode: token not required (anonymous user auto-injected)
```

---


---

## Project Structure

```
eljazari/
├── apps/
│   ├── api/                              # FastAPI Backend
│   │   ├── main.py                       # App + Socket.io ASGI mount
│   │   ├── config/
│   │   │   ├── settings.py               # Pydantic Settings v2
│   │   │   └── logging.py                # Structlog configuration
│   │   ├── middleware/
│   │   │   ├── auth.py                   # JWT: AuthUser dependency
│   │   │   └── rate_limit.py             # SlowAPI rate limiting
│   │   ├── ws/
│   │   │   ├── socket_manager.py         # python-socketio AsyncServer
│   │   │   ├── events.py                 # All @sio.on() handlers
│   │   │   └── mavlink_bridge.py         # MAVSDK ↔ Socket.io bridge
│   │   ├── agents/
│   │   │   ├── orchestrator.py           # LangGraph runner + HITL + LangSmith
│   │   │   ├── code_agent.py             # GSCE code generation (real MAVSDK)
│   │   │   ├── test_agent.py             # Closed-loop simulation
│   │   │   ├── design_agent.py           # AircraftVerse RAG → JSON design
│   │   │   ├── monitor_agent.py          # ReActEval + Phase 11 flight quality
│   │   │   ├── performance_monitor.py    # Head+Worker hierarchical analysis
│   │   │   ├── pid_tune_agent.py         # Phase 10 — PID auto-tune
│   │   │   ├── safety_layer.py           # SafeLLMSupervisionLayer (3 pillars)
│   │   │   ├── safety_models.py          # Pydantic safety schemas
│   │   │   ├── design_models.py          # Pydantic design schemas
│   │   │   ├── monitor_models.py         # Pydantic monitor schemas
│   │   │   └── performance_models.py     # Pydantic performance schemas
│   │   ├── graph/
│   │   │   ├── state.py                  # ElJazariState TypedDict (30+ fields)
│   │   │   ├── nodes.py                  # 10 async LangGraph nodes
│   │   │   ├── edges.py                  # 10 conditional routing functions
│   │   │   └── workflow.py               # StateGraph + PostgreSQL checkpointing
│   │   ├── pipelines/
│   │   │   ├── code_generator.py         # GSCE 4-layer builder (real MAVSDK)
│   │   │   ├── mavsdk_templates.py       # Phase 8 — real MAVSDK mission renderer
│   │   │   ├── mission_builder.py        # Phase 8 — Waypoint → MissionItemPayload
│   │   │   ├── flight_quality_solver.py  # Phase 11 — RMSE / FFT / saturation
│   │   │   ├── engineering_deliverables.py # Phase 14 — BOM/assembly/preflight/maintenance
│   │   │   ├── semantic_converter.py     # Telemetry → English NL
│   │   │   └── json_to_urdf.py           # JSON → URDF/SDF
│   │   ├── routers/
│   │   │   ├── auth.py                   # POST /api/auth/token
│   │   │   ├── projects.py               # CRUD + pipeline trigger
│   │   │   ├── design.py                 # Design + HITL
│   │   │   ├── code.py                   # Code + HITL
│   │   │   ├── simulation.py             # Start/stop/results
│   │   │   ├── monitor.py                # Report + logs
│   │   │   ├── files.py                  # URDF/code/PDF download
│   │   │   └── agents.py                 # Pipeline run/approve
│   │   ├── models/                       # SQLAlchemy ORM models (7 tables)
│   │   ├── schemas/                      # Pydantic request/response schemas
│   │   ├── repositories/                 # Async CRUD base + 7 repos
│   │   ├── services/
│   │   │   ├── gazebo_service.py         # Gazebo subprocess manager
│   │   │   ├── px4_service.py            # MAVSDK + simulated fallback
│   │   │   ├── failure_injector.py       # Phase 13 — 7 fault scenarios + probes
│   │   │   ├── sitl_environment.py       # Phase 9 — 9 env scenarios + PX4 params
│   │   │   ├── llm_service.py            # Ollama HTTP wrapper
│   │   │   └── storage_service.py        # File storage
│   │   ├── utils/
│   │   │   ├── coordinates.py            # Phase 8 — NED↔LLA (WGS84)
│   │   │   ├── sitl_runtime.py           # WSL / docker / native / mock resolver
│   │   │   └── visualization_helpers.py  # Chart-data generators
│   │   ├── tests/                        # 717 tests (15 suites)
│   │   └── requirements.txt
│   │
│   └── web/                              # Next.js 14 Frontend
│       ├── app/
│       │   ├── layout.tsx                # Root layout + providers
│       │   ├── page.tsx                  # Hero + RTL prompt input
│       │   ├── providers.tsx             # QueryClient + JWT bootstrap
│       │   ├── globals.css               # Dark navy/green theme
│       │   └── dashboard/
│       │       ├── layout.tsx            # Two-column dashboard
│       │       ├── DashboardHeader.tsx   # Nav + WS indicator
│       │       ├── page.tsx              # Phase overview cards
│       │       ├── design/page.tsx       # 3D viewer + JSON tree + HITL
│       │       ├── code/page.tsx         # Monaco + safety report + HITL
│       │       ├── simulate/page.tsx     # Terminal + map + charts + obs
│       │       ├── monitor/page.tsx      # KPI + radar + recommendations
│       │       ├── viewer/page.tsx       # Standalone 3D viewer
│       │       └── gcs/page.tsx          # WebGCS ground control station
│       ├── components/
│       │   ├── drone-viewer/
│       │   │   ├── DroneViewer.tsx       # Full 3D viewer
│       │   │   ├── FlightPathViewer.tsx  # Flight path + animated drone
│       │   │   ├── TelemetryOverlay.tsx  # Real-time HUD
│       │   │   ├── URDFLoader.tsx        # Custom URDF parser
│       │   │   └── controls.ts           # Types, colours, presets
│       │   ├── gcs/
│       │   │   ├── LeafletMap.tsx        # Interactive satellite map
│       │   │   ├── TelemetryDashboard.tsx # SVG instrument panel
│       │   │   ├── ParameterTuning.tsx   # PID gains panel
│       │   │   ├── MissionControls.tsx   # Flight command buttons
│       │   │   ├── AttitudeIndicator.tsx # Artificial horizon
│       │   │   ├── MapView.tsx           # NED SVG map
│       │   │   └── EventLog.tsx          # Filterable event log + CSV
│       │   ├── pipeline/
│       │   │   └── PipelineTimeline.tsx  # Animated sidebar timeline
│       │   ├── charts/
│       │   │   └── TelemetryCharts.tsx   # Altitude / Speed / Battery
│       │   ├── code/
│       │   │   └── CodeEditor.tsx        # Monaco drone-dark theme
│       │   └── terminal/
│       │       └── SimulationTerminal.tsx # XTerm.js log stream
│       └── lib/
│           ├── api.ts                    # Typed fetch client (20+ endpoints)
│           ├── hooks/
│           │   └── useProject.ts         # TanStack Query hooks
│           └── stores/
│               ├── drone-store.ts        # Zustand + Immer pipeline state
│               ├── pipeline-store.ts     # Re-export shim
│               └── ws-store.ts           # Socket.io client store
│
├── packages/
│   ├── shared-types/src/index.ts         # 30+ shared TypeScript types
│   ├── gsce-framework/                   # GSCE package stub
│   └── urdf-converter/                   # URDF converter stub
│
├── simulation/
│   ├── gazebo/worlds/default.sdf         # Gazebo Harmonic world
│   ├── px4/configs/sitl_params.txt       # PX4 SITL parameters
│   └── docker-compose.sim.yml
│
├── data/
│   ├── gsce-templates/mission_template.yaml
│   ├── aircraftverse/                    # AircraftVerse dataset
│   ├── meshes/                           # STL mesh files
│   └── safety-rules/
│       ├── flight_rules.yaml             # Human-readable: SR/RR/CR/IP rules
│       └── flight_rules.json             # Machine-readable: RAG compliance
│
├── infrastructure/
│   ├── docker-compose.yml               # Production stack + bootstrap/simulation profiles
│   ├── docker-compose.dev.yml           # Dev + hot reload
│   ├── Dockerfile.api / .web / .sim / .celery
│   ├── k8s/                             # Kubernetes manifests (8 services)
│   └── monitoring/                      # Prometheus + Grafana + alerts
│
├── .github/workflows/
│   ├── ci.yml                           # Lint + test + build
│   ├── cd-staging.yml                   # Deploy to staging
│   └── cd-production.yml               # Deploy to production
│
├── .env.example
├── turbo.json
├── package.json
└── README.md
```

---

