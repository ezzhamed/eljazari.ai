# Run 3, second attempt, stopped after 30 of 48 runs (2026-09-29)

Suite `prompt_suite_v2`, code `d1f99961`. Kept because every run counts. It is not a result: runs 17–30 shared the machine with development work.

| Run | Outcome | Wall time |
| --- | --- | --- |
| en-hover-camera #1 | accepted | 349 s |
| en-hover-camera #2 | accepted | 392 s |
| en-hover-camera #3 | accepted | 362 s |
| en-square-inspection #1 | accepted | 502 s |
| en-square-inspection #2 | accepted | 491 s |
| en-square-inspection #3 | accepted | 376 s |
| en-out-and-back #1 | accepted | 374 s |
| en-out-and-back #2 | accepted | 366 s |
| en-out-and-back #3 | accepted | 384 s |
| ar-hover-camera #1 | accepted | 339 s |
| ar-hover-camera #2 | accepted | 358 s |
| ar-hover-camera #3 | accepted | 372 s |
| ar-square-farm #1 | accepted | 423 s |
| ar-square-farm #2 | accepted | 415 s |
| ar-square-farm #3 | accepted | 405 s |
| ar-out-and-back #1 | timeout | 63107 s |
| ar-out-and-back #2 | accepted | 762 s |
| ar-out-and-back #3 | failed_simulation | 503 s |
| en-mass-paradox #1 | rejected_components | 257 s |
| en-mass-paradox #2 | rejected_components | 263 s |
| en-mass-paradox #3 | rejected_components | 230 s |
| en-endurance #1 | rejected_components | 271 s |
| en-endurance #2 | rejected_components | 170 s |
| en-endurance #3 | rejected_components | 527 s |
| ar-heavy-lift #1 | rejected_components | 169 s |
| ar-heavy-lift #2 | rejected_components | 165 s |
| ar-heavy-lift #3 | rejected_components | 175 s |
| ar-altitude-limit #1 | rejected_prompt | 0 s |
| ar-altitude-limit #2 | rejected_prompt | 0 s |
| ar-altitude-limit #3 | rejected_prompt | 0 s |

28 of 30 recorded runs had the correct outcome.

## What happened

**Run 16 (`ar-out-and-back` #1): 17.5 hours, one query.** Design generation waited that long on a single ChromaDB similarity query, and the harness's asyncio timeout could only take effect once it returned. The cause was the AircraftVerse index:
- Every design embedded the 5,000-document corpus again into a collection with a fixed name.
- In persistent mode, which the local `.env` selects, the collection kept every copy across runs and restarts. It reached 2.6 GB in this worktree and 6 GB in the main checkout.

**Runs 17–30 are not clean measurements.** They ran after that query returned, while the fix was being written, test suites ran, and the same ChromaDB store was being examined. The benchmark was then stopped.

**What changed:**
- The index is now built once per process and corpus, and builds and queries are bounded in time.
- The harness has a watchdog that records a stuck run as a timeout and resumes.

Run 3 was restarted from the beginning rather than resumed. All 48 runs then share one code version. The fix also changes design timing: the first design of the restarted run took 96 s against 116 s before it, most of that stage being LLM time. So wall times before and after it would not be comparable. The fix and the full run 3 are in [docs/BENCHMARK.md](../../../docs/BENCHMARK.md).
