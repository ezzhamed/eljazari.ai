# Verified End-to-End Run — Real LLM + Real WSL SITL

**Date:** 2026-06-17
**Goal:** Produce a pipeline run that *genuinely* uses the local LLM (Ollama) for
design + code generation **and** a *real* PX4/Gazebo Software-in-the-Loop flight
inside WSL — not the rule-based / mock-telemetry fallbacks — and document it with
hard evidence from the database and SITL artifacts.

> **Headline:** project `0677916b-e1b8-4167-aef8-92631335ef52` ran the whole
> pipeline with a RAG-grounded LLM design, an LLM-generated MAVSDK mission, and a
> **completed autonomous flight in real WSL SITL** (`mission_completed=True`,
> 168 real telemetry points, drone climbed to the 12 m target, flew the 30 m
> square, and landed).

---

## 1. What "genuine" means here, and how it was verified

Two independent failure modes had to be ruled out:

1. **LLM not actually used** — the design agent silently falls back to
   `rule_based` when Ollama is unreachable, and the code agent falls back to a
   static template. The telltale is the `generation_method` and the per-phase
   latency (rule-based design is instant; an LLM design takes ~80–90 s).
2. **WSL SITL not actually used** — `PX4Service` falls back to
   `simulated_fallback` (synthetic telemetry) when the WSL runtime probe fails.
   The telltale is `telemetry_source` and telemetry that sits at the canonical
   PX4 home coordinates with a real arm/EKF sequence.

### Evidence (run `0677916b`)

| Claim | Evidence | Source |
| --- | --- | --- |
| LLM design (RAG) | `design.llm_complete`; 2 `POST /api/embed → 200`; **0** "RAG disabled"/"RAG retrieval error" ⇒ `generation_method = rag_llm` | backend log |
| LLM code | `Rewrote LLM output to real MAVSDK` (qwen2.5-coder:7b → Phase-8.5 rewriter) | backend log |
| Real WSL SITL | `run_simulation.complete … mission_completed=True telemetry_points=168 telemetry_source=real_wsl_sitl` | backend log + `simulations` row |
| Real flight | alt 0 → 12.2 m (target 12 m), north span 30.4 m (the 30 m leg), RTH closure 0.05 m, 99.85 s, `armed=True` | `simulations.raw_telemetry` (168 pts) |
| Monitor + deliverables | `generate_monitor_report.complete score=89.5`; `deliverables.written count=6` | backend log + storage dir |

### Altitude profile (sampled from the 168 real telemetry points)

```
0.0 → 0.4 → 4.1 → 11.0 → 12.2 → 12.2 → 12.5 → 12.1 → 12.3 → 12.2 → 12.8 → 12.2 → 12.2 → 12.4 → 9.8 → 4.6 → 2.0   (metres)
```

A real climb to the 12 m target, a cruise segment at ~12.2 m while flying the
square, and a descent for landing.

---

## 2. Environment used

| Component | Detail |
| --- | --- |
| LLM runtime | Ollama (kept alive by the desktop service) at `http://127.0.0.1:11434` |
| Design model | `gemma3:4b` |
| Code model | `qwen2.5-coder:7b` |
| Eval model | `llama3.2` (tag aliased to `llama3.2:3b`) |
| Embeddings | `nomic-embed-text` (768-dim) — **new**, see fix #1 |
| Database | PostgreSQL on `127.0.0.1:5433` |
| SITL | WSL Ubuntu, PX4-Autopilot SITL + Gazebo Sim 8.11.0, MAVSDK venv |
| Backend launch | `uvicorn main:socket_app --port 8010` (from `apps/api`) |

No SITL env flags are needed: the default runtime is `auto`, which picks WSL on
Windows, and the WSL exec probe retries while a cold distro boots, so a transient
timeout no longer drops the run to mock mode. (The earlier write-up used
`DRONEPROMPT_FORCE_SITL` / `DRONEPROMPT_SKIP_WSL_EXEC_PROBE`; those are no longer
required — see §4.)

---

## 3. Fixes that made this possible

All four are real defects found during the run; each was confirmed by the run
advancing one stage further.

### Fix #1 — RAG embeddings were silently disabled (`llm_only`, not `rag_llm`)
`OllamaEmbeddings` was pointed at `settings.ollama_default_model` (a **chat**
model), which has no embeddings endpoint → Ollama returned HTTP 501 →
ChromaDB init failed → RAG disabled. Added a dedicated
`ollama_embed_model = "nomic-embed-text"` setting and used it. Result:
`/api/embed → 200`, RAG retrieval engages, design becomes `rag_llm`.
*Files:* `apps/api/config/settings.py`, `apps/api/agents/design_agent.py`.

### Fix #2 — Pre-flight bailed on the first (cold-EKF) health sample
`preflight_check` read only the **first** `telemetry.health()` sample and raised
"vehicle not armable" immediately. In SITL the EKF needs ~15–30 s to converge, so
the first sample is never armable — PX4 reaches "Ready for takeoff!" seconds
later, but pre-flight had already given up. Rewrote it to **poll until armable**
(gated on `is_global_position_ok and is_home_position_ok`, the real arming gate),
up to a 60 s timeout. *File:* `apps/api/pipelines/mavsdk_templates.py`.

### Fix #3 — `int(FixType.FIX_3D)` raised `TypeError` (every mission failed here)
The GPS check did `int(gps.fix_type) >= int(FixType.FIX_3D)`, but MAVSDK's
`FixType` enum is **not** int-convertible — `int()` raised
`TypeError: ... not 'FixType'`. This pre-existing bug failed **every** mission's
pre-flight. Replaced with a name-based comparison (`"3D"/"DGPS"/"RTK" in
fix.name`). *Files:* `apps/api/pipelines/mavsdk_templates.py`,
`apps/api/pipelines/code_generator.py`.

### Fix #4 — Geofence upload corrupted the mission protocol (`PROTOCOL_ERROR`)
The optional geofence was uploaded **before** the mission. It fails on this
MAVSDK version (`Polygon.FenceType` missing), and because geofence and mission
share the MAVLink mission protocol, the half-open transaction made the
subsequent `upload_mission()` raise `PROTOCOL_ERROR`. Reordered to upload the
mission **first** (with a `clear_mission()` first), then attempt the optional
geofence — so a geofence failure can no longer block the mission.
*File:* `apps/api/pipelines/mavsdk_templates.py`.

Tests: `tests/test_mavsdk_codegen.py` + `tests/test_code_post_processor.py`
(51 tests) pass, including the safety-layer static check on the generated
template.

---

## 4. Caveats — status after the follow-up fixes

- **No env flags needed (fixed).** The committed default is `auto`; the WSL exec
  probe now retries with a longer timeout so a cold distro no longer false-
  negatives to mock. A genuine WSL run starts with a bare
  `uvicorn main:socket_app` — no `DRONEPROMPT_FORCE_SITL` /
  `DRONEPROMPT_SKIP_WSL_EXEC_PROBE`. Verified: `auto` resolves to
  `mode=wsl, is_real=True` and a flag-free run produced `mission_completed=True`,
  166 real points.
- **Flight-quality scorer (fixed).** A completed survey flight was graded C
  (66/100) because the *hover-drift* pillar measured the gross position spread of
  a mission that is *supposed* to traverse (≈1500 m/min). Hover drift is now
  measured only over genuine station-keeping windows and dropped from the score
  when the aircraft never hovers — the same flight now grades **B (84)**.
  Tracking RMSE is reported as **"not evaluated"** (honest) instead of a
  misleading `0.00 m` when the commanded path can't be reconstructed.
- **AircraftVerse dataset (improved).** `AIRCRAFTVERSE_PATH` is configurable and
  `scripts/ingest_aircraftverse.py` converts a real-corpus export into the
  project schema (tested: maps `num_rotors`/`arm_length`/`cells`… and skips
  unmappable records). The full 27,714-design corpus still must be obtained
  separately under its own licence; the bundled default remains the 200-entry
  synthetic sample.
- **Geofence now enforced via PX4 params (fixed).** The MAVLink polygon upload
  (`drone.geofence.upload_geofence`) returns a generic `Error` on this PX4 + gz
  SITL build and, run before the mission, corrupts the shared mission protocol
  (verified: mission then fails). It is replaced with PX4's built-in parameter
  geofence — `GF_ACTION=3` (RTL on breach), `GF_MAX_HOR_DIST` (radius),
  `GF_MAX_VER_DIST` (ceiling) — set via `drone.param.*`, which the commander
  enforces directly with no mission-protocol upload. Verified: a run with an
  active geofence (`radius_m=200`) set the params with **no `geofence.skip`** and
  `mission_completed=True` (167 real points). `set_param_int/float` were added to
  the safety-layer allow-list.
- **One SITL iteration.** The run completed on iteration 0; the iterate-on-failure
  loop was not exercised here.

---

## 5. Reproduce

```bash
# 1. Ollama up with the models (incl. the embedder)
ollama pull nomic-embed-text          # 768-dim embeddings for RAG

# 2. Postgres on 5433 and WSL PX4/Gazebo present (see README)

# 3. Backend — no SITL flags needed (default runtime is auto → WSL on Windows)
cd apps/api
python -m uvicorn main:socket_app --port 8010

# 4. Submit a prompt and approve the two HITL gates once each
#    (verify_run.py drives this gate-aware single-approval flow).
```

Confirm genuineness in the `simulations` row: `status='completed'`,
`telemetry_source` ⇒ `real_wsl_sitl`, `raw_telemetry` length ≈ 168 with a real
altitude climb; and in the backend log: `design.llm_complete` with
`/api/embed → 200` (no "RAG disabled") for the LLM/RAG side.
