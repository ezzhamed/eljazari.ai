# Run 3, aborted after 2 of 48 runs (2026-09-29)

Suite `prompt_suite_v2`, code `8d7becf9`. Kept because every run counts.

| Run | Outcome |
| --- | --- |
| en-hover-camera #1 | accepted (401 s) |
| en-hover-camera #2 | blocked_simulation: no published propulsion test for the selected EMAX RS2306S 2400KV |

Why it stopped: the design LLM chose a small frame for «a small quadcopter». Component selection searched only frames within 30% of the design's wheelbase. The only quad frame that fits any documented motor and propeller combination (Holybro X500 V2, 500 mm, 12-inch propellers) was outside that window. So selection could offer only unsourced FPV motors, which generated-aircraft SITL refuses. Run #1 flew because the LLM happened to choose a larger frame.

The same supported request therefore passed or failed on the LLM's size estimate. Running the other 46 runs would have measured a known defect. The fix and the full run 3 are in [docs/BENCHMARK.md](../../../docs/BENCHMARK.md).
