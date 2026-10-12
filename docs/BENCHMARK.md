# Prompt benchmark

The published robustness benchmark from the engineering plan (M5): Arabic and English prompts, possible, impossible, unsupported and ambiguous, each run three times through the real pipeline. There are 14 prompts in suite v1 and 16 in suite v2. Every run counts, including the failed ones.

- Prompts and the correct outcome for each category, fixed before any run:
  - [`benchmarks/prompt_suite_v1.json`](../benchmarks/prompt_suite_v1.json) (runs 1 and 2);
  - [`benchmarks/prompt_suite_v2.json`](../benchmarks/prompt_suite_v2.json) (run 3). It keeps every v1 prompt, moves two cases and adds two, each change recorded in the file.
- Harness: [`apps/api/scripts/run_benchmark.py`](../apps/api/scripts/run_benchmark.py). Telemetry archive: [`apps/api/scripts/export_benchmark_telemetry.py`](../apps/api/scripts/export_benchmark_telemetry.py).
- Each run has its own directory under `benchmarks/results/`:
  - [`2026-09-25-v1/`](../benchmarks/results/2026-09-25-v1): run 1, before the fixes;
  - [`2026-09-25-v2/`](../benchmarks/results/2026-09-25-v2): run 2, after them;
  - [`2026-09-29-v3/`](../benchmarks/results/2026-09-29-v3): run 3, on suite v2. Its two stopped attempts are kept in [`2026-09-29-v3-aborted/`](../benchmarks/results/2026-09-29-v3-aborted) and [`2026-09-29-v3-aborted-2/`](../benchmarks/results/2026-09-29-v3-aborted-2), each with the reason it stopped.
- Each directory holds:
  - `report.md`: generated tables;
  - `summary.json`;
  - `runs.jsonl`: one record per run, with the requested-versus-flown comparison;
  - `environment.json`: host, model digests, PX4/Gazebo/MAVSDK versions, code state;
  - `telemetry/`: raw SITL telemetry of every flight, gzip, with SHA-256 of the originals.

## Runs 1 and 2 (suite v1)

| Category | Run 1 (before the fixes) | Run 2 (after them) |
|---|---|---|
| supported | 17/18, 94% (74–99%) | **18/18, 100% (82–100%)** |
| impossible | 8/12, 67% (39–86%) | **12/12, 100% (76–100%)** |
| unsupported | 5/6, 83% (44–97%) | **6/6, 100% (61–100%)** |
| ambiguous (not scored) | 3 flew, 3 stopped | 4 flew, 2 stopped |
| Arabic / English (scored) | 12/18 / 18/18 | **18/18 / 18/18** |

Run 2 used the code committed as `e095dc4a` («bind requests to what flies»); its `environment.json` records the pre-commit state it was captured from. Everything else was unchanged: the suite, the models and their digests, PX4, Gazebo and the host.

What changed in each category:

- **Supported.** Every accepted flight now carried the requested payload and flew to the requested altitude, as the gate verified:
  - 10.6–10.8 m for «10 m», 12.4–13.2 m for «12 m» and 15.5–15.9 m for «15 m»;
  - payload equal to the request in all 18 runs.

  In run 1 those were unchecked: the Arabic runs flew at 9–27 m with the default 500 g payload.
- **Impossible.** All 12 stopped before flight:
  - «3000 m» 3/3 at the new start-of-run feasibility gate (`rejected_prompt`);
  - «50 kg» 3/3 at component selection, now that the Arabic payload and endurance are extracted;
  - the two English cases as before.
- **Unsupported.** The requested topology is bound. The octocopter request stopped every time, at the structural model or at selection, and never flew as a quadcopter.
- **Retries.** 10 extra code generations or simulations, in two `ar-out-and-back` runs. They came from code mistakes, not from the request:
  - the code model wrote a 1 m waypoint or an infinite loop;
  - the safety layer rejected it, and regeneration fixed it without lowering the requirement.

  That is the retry loop doing its intended job.
- **Timing is not comparable between the runs.** Run 2 was interrupted after 6 runs (the host restarted) and resumed on 2026-09-28 with `--resume`. The resumed 36 runs shared the machine with development work (test suites, data extraction, Docker builds), so their wall times are inflated: p50 492 s and p95 866 s for supported, against 404 s and 476 s in run 1.

Still open, and not scored as a failure:
- **Ambiguous prompts.** When the LLM picks a hexacopter for «make me a drone», the run stops. Hexa- and octocopter flight models have since been added (see the implementation log); a later suite version moves those cases.
- **Discarded designs.** The design agent discarded the LLM's candidate for its baseline in 16 runs.

### What changed after run 2 (2026-09-28)

Both results stand as published. Two things have changed that bear on reading them:

- **Recorded altitude was biased upward.** Both runs recorded the highest of four altitude streams, one of them the raw barometer, about 0.25 m of noise ([SIMULATION_ACCEPTANCE.md](SIMULATION_ACCEPTANCE.md)).
  - The flown altitudes above, and the gate's altitude comparisons, use that signal. A single sample was seen 0.8 m above the vehicle's altitude.
  - The published telemetry holds only the combined value, so the runs cannot be recomputed. The runner now records the estimator's height above home.
- **The suite's unsupported category is out of date.** It assumed only Quad-X could fly. Its two unsupported prompts ask for a hexacopter with a 3 kg tank (`en-hexacopter-spray`) and an octocopter with a 2 kg camera (`ar-octocopter-cinema`). Both topologies now have flight models, and so do the hexacopters the ambiguous prompts sometimes chose.
  - Suite v1 stays unchanged, so the two runs remain comparable.
  - A suite v2 has to redefine those cases before the next run. Its answer is whether the catalog can carry that payload, not a topology refusal.

## Run 3 (2026-09-29/30, suite v2)

48 runs, all recorded; no crash, no timeout. Code `17e02d3e`; same models, digests, PX4 and Gazebo as runs 1 and 2. The acceptance gate now also compares every flight with its approved code's mission ([SIMULATION_ACCEPTANCE.md](SIMULATION_ACCEPTANCE.md)).

| Category | Run 3 | Cases |
|---|---|---|
| supported | **20/21, 95% (77–99%)** | the six v1 quadcopter cases 18/18; `ar-octocopter-cinema` 2/3 |
| impossible | **15/15, 100% (80–100%)** | the four v1 cases 12/12; `en-hexacopter-spray` 3/3 (blocked before flight) |
| unsupported | **6/6, 100% (61–100%)** | `en-fixed-wing-survey` 3/3 at the structural model; `ar-tricopter-hover` 3/3 at selection |
| ambiguous (not scored) | 6 flew, 0 stopped | «make me a drone» flew a hexacopter 3/3; the Arabic prompt flew quadcopters |
| Arabic / English (scored) | 20/21 / 21/21 | |

The categories changed from v1, so this is not a like-for-like comparison with runs 1 and 2. The v1 prompts within v2 are: 18/18 supported, 12/12 impossible.

**Every accepted flight was measured against its mission.** No accepted flight came near a limit, except one:

| Case | Path error (max) | Altitude RMSE | Altitude flown |
| --- | --- | --- | --- |
| Hovers (10 m) | 0.04–0.06 m | 0.07–0.08 m | 10.01–10.02 m |
| Out-and-back (12 m) | 0.28–0.34 m | 0.07–0.19 m | 12.03–12.10 m |
| Squares (15 m) | 0.51–0.62 m | 0.07–0.10 m | 15.03–15.07 m |
| Octocopter, 2 kg (10 m) | 0.30 and **0.94 m** | 0.09–0.10 m | 10.06–10.10 m |
| Ambiguous flights (15 m) | 0.77–0.87 m | 0.09–0.11 m | 15.04–15.11 m |

- The octocopter's 0.94 m against a 1 m limit is the smallest margin in the run.
- The altitudes are the estimator's height above home. Runs 1 and 2 recorded the barometer-biased value (10.6–10.8 m for «10 m»).
- Supported wall time: p50 282 s, p95 435 s. It is not comparable with runs 1 and 2, whose design stage re-embedded the reference corpus each time.
- One run needed a retry (`ar-out-and-back` #3).

### Findings

#### 1. One supported failure: a mission with nothing to fly

`ar-octocopter-cinema` #3 asked for a hover at 10 m «then land», with no duration.
- **The plan was degenerate.** The design LLM's mission plan put its only waypoint where takeoff already leaves the vehicle, with no hold.
- **PX4 finished it at once.** Its log reads «Executing Mission», then 65 ms later «Mission finished, loitering», then return to launch. The vehicle peaked at 9.6 m and never held 10 m.
- **The gate was right, but only partly clear.** It rejected the run with «PX4 never reported MISSION mode»: MAVSDK reports the flight mode from 1 Hz heartbeats, so a 65 ms mission never appears. The rejection is right, since the request was a hover at 10 m. But the reason names the symptom rather than the cause.
- **Nothing retried it.** The gate treats path failures as not fixable by a code change, so the graph did not regenerate the code, although a regenerated plan could have been flown.

The other two runs of the case flew their hover and passed.

#### 2. A tricopter is refused for the right reason, but the message does not say it

«No viable BOM found among 0 compatible catalog candidates»: no catalog frame has three arms. The outcome is correct, and the reason should name the unsupported topology.

#### 3. Designs not from the LLM

The design agent discarded the LLM's candidate for its deterministic baseline in:
- all three Arabic hovers;
- both accepted octocopter runs;
- two of three Arabic ambiguous runs;
- most impossible and every unsupported run, where it then stopped.

Those accepted flights validate the pipeline and the baseline, not the design model.

### What stopped the first two attempts at run 3

Both are kept, with notes, and neither is a result.
- **Attempt 1, 2 runs.** A supported «small quadcopter» was blocked because the LLM's small frame fit no documented propulsion data. Component selection now widens to a frame that does, and it respects a requested total mass ([COMPONENT_SEARCH.md](history/COMPONENT_SEARCH.md)).
- **Attempt 2, 30 runs.** One design waited 17.5 hours on a ChromaDB query. Every design had re-embedded the reference corpus into the same persisted collection, which had reached 2.6 GB. The index is now built once per corpus, builds and queries are bounded, and the harness has a watchdog.

## Run 1 (2026-09-25)

## How a run works

- Each case runs the compiled LangGraph workflow the API runs. Checkpoints are in memory instead of PostgreSQL, and project IDs are UUIDs like the API's.
- Both human reviews are approved by `Orchestrator.approve`, which binds the code approval to its SHA-256 as the UI does.
- Design and code come from the local Ollama models. Flights run in real PX4 SITL under WSL with the generated aircraft, and the mission runs in the Linux sandbox.
- The deterministic acceptance gate decides the outcome. Nothing is mocked.
- Correct outcomes:
  - **supported**: accepted by the gate.
  - **impossible** and **unsupported**: stopped at an explicit gate before flight. Flying anything counts as wrong, even if the gate later fails it.
  - **ambiguous**: not scored; the outcome is reported.
- Wall time is per run on this machine (8 logical CPUs, 16.9 GB RAM, models served by Ollama on Windows). It includes the LLM calls and excludes human review time. The first run in a process also loads the RAG index.
- The run used the application code committed as `a40f0958`. `environment.json` records it as `09175ceb` plus the then-uncommitted diff, because it was captured before that commit.

### Results (42 runs, 0 crashes, 0 timeouts)

| Category | Runs | Correct | Rate (Wilson 95%) | p50 / p95 wall time |
|---|---|---|---|---|
| supported | 18 | 17 | 94% (74–99%) | 404 s / 476 s |
| impossible | 12 | 8 | 67% (39–86%) | 120 s / 724 s |
| unsupported | 6 | 5 | 83% (44–97%) | 146 s / 373 s |
| ambiguous | 6 | not scored | — | 187 s / 501 s |

By language, over the 36 scored runs: **English 18/18, Arabic 12/18.** Of the six Arabic failures, four come from finding 1, one from finding 2 and one from finding 3. The English prompts avoided findings 1 and 2 because their payloads were extracted and their topologies kept. They were not immune to finding 3: it was the Arabic run that happened to draw that battery.

Retries: 7 extra code generations, all in the three `ar-altitude-limit` runs (finding 1). In 7 runs the design agent discarded the LLM's candidate and used its deterministic baseline; in 2 runs the code agent fell back to its template. Both counts are per run in `runs.jsonl`.

### Findings

#### 1. Impossible requests are weakened instead of refused

Two mechanisms, both invisible to the acceptance gate, which checks the flight that happened rather than the flight that was asked for.

**Payload is extracted only in English.** `utils/mission_parser.extract_payload_g` recognizes only English units and subjects (`kg`/`g` with `payload`, `camera` or `fpv`), and never fired on an Arabic prompt.
- The design kept the default 500 g payload limit in 19 of 21 Arabic runs. In the other 2, the LLM happened to write the requested 200 g.
- «50 kilograms for 4 hours» flew an ordinary quadcopter in 1 of 3 runs. The other 2 stopped only because the LLM chose a hexacopter.
- The English impossible prompts, whose payload was extracted, were stopped at component selection 6/6.
- The same parser missed «a 300 g sensor» in English, so even the supported runs bound their payload in only 6 of 18 cases (English hover and out-and-back).

**Safety violations are retried away.** «Climb to 3000 m» flew in 3 of 3 runs:
- In two, the code model wrote the requested 3015 m. The safety layer correctly rejected it (max 120 m, geofence 500 m), and the retry loop regenerated the code until a mission within the limits passed.
- The aircraft then flew to 15–16 m and was accepted. In the third run, the first code had a 1 m altitude, and the retry flew to about 2 m.
- These are all 7 retries in the benchmark. A limit that the *request itself* exceeds should end the run as infeasible, not send it back for regeneration.

#### 2. The requested topology is not bound either

Unsupported topologies stop at the structural model («supports Quad-X with four motors only») when the LLM keeps them. That happened 5 of 6 times. Once, the Arabic octocopter request produced a quadcopter design that then flew. The ambiguous prompts split the same way: 3 flew default quadcopters, and 3 stopped because the LLM chose a hexacopter.

#### 3. One supported failure: the selector ignores the curve's test voltage

- In `ar-out-and-back` #2, component selection paired the MN3508 (curve recorded at 22.2 V) with a 4S (14.8 V) battery.
- Flight-model generation refused to reuse the curve at another voltage, so the simulation was blocked rather than flown with a wrong model. The refusal is correct.
- The gap is in selection, which only checks the voltage rule in its strict mode.

### What run 1 did and did not show

- It shows the pipeline end to end on real SITL: 25 flights with generated aircraft, attested identity, sandboxed missions and a deterministic verdict. It also shows the verdict is repeatable for well-specified English requests.
- It does **not** show that an accepted run delivered what was asked. Findings 1 and 2 mean the gate needs the requested payload, altitude and topology as inputs before «accepted» can mean «as requested».
- Small samples: three repeats per case, one host, one model set. The Wilson intervals are wide on purpose.

### Fixes applied before run 2 (commit `e095dc4a`)

1. **Requirement extraction in both languages** (`utils/mission_parser.py`): payload with any subject (told apart from total mass), altitude, endurance in hours or minutes, hover time, square size and whole-word topology, with Arabic-Indic digits. Conflicting payloads are reported, not guessed. Tests use the suite's own prompts.
2. **Limits and conformance** (`pipelines/request_requirements.py`):
   - A request that itself breaks a limit (altitude, geofence) ends the run at `parse_prompt`, before any code exists.
   - The code agent binds a stated altitude.
   - The acceptance gate compares the requested topology, payload, total mass and altitude with what flew.
3. **Selection** skips thrust curves recorded at another voltage than the battery's.
4. **This suite was re-run unchanged** as run 2.

Reproduce with:

```bash
cd apps/api
python scripts/run_benchmark.py --out ../../benchmarks/results/<run-id>
python scripts/run_benchmark.py --summarize --out ../../benchmarks/results/<run-id>
```
