# Propulsion curve evidence

A thrust curve belongs to the combination it was measured with: one motor, one propeller, one voltage. The optional local catalog import has been verified with **25 published tests** of that kind, from T-MOTOR's own test tables:

- motors: MN3508 (KV380/580/700), MN3110 (KV470/700/780) and MN4014 (KV330/400);
- propellers: P12×4 to P17×5.8;
- voltages: 11.1, 14.8 and 22.2 V.

They are generated locally in `.local/components/propulsion_tests.json`, not distributed in the public repository. See [Local component data](LOCAL_COMPONENT_DATA.md) and [Sourced tests](#sourced-tests).

**Generated-aircraft SITL flies documented combinations only.**
- Selection gives each candidate the curve published for its exact motor, propeller and battery voltage. Whenever any feasible candidate is documented, it chooses among those alone.
- The flight model refuses anything else.
- The six FPV motors' curves stay unsourced. They remain usable for preliminary sizing, clearly marked, but never become a flight model.

Every performance number is still a **preliminary estimate**: a manufacturer table is not a calibration. No declaration was invented or backfilled.

## Data contract

An optional `motor.thrust_curve_evidence` record identifies one test condition:

| Field | Required content |
| --- | --- |
| `motor_part_number` | Exact catalog motor ID, including its KV variant |
| `propeller_part_number` | Exact catalog propeller ID, including diameter, pitch and blade variant |
| `test_voltage_v` | Voltage under which the curve was recorded |
| `source_kind` | `manufacturer_test` or `bench_test` |
| `source_reference` | Specific archived test report, table/page, dataset or bench-run reference |
| `source_sha256` | SHA-256 of the actual source artifact, lowercase hexadecimal |
| `source_date` | Date of that source in ISO format |
| `curve_sha256` | Canonical fingerprint calculated by `thrust_curve_sha256(points)` |

The curve fingerprint hashes parsed point values in ascending throttle order. JSON whitespace and point order do not change it. Changing current, thrust, power, voltage, RPM or throttle changes it. Duplicate throttle values, non-increasing thrust, fewer than two points and non-finite values are rejected by the solver.

Preserve source artifacts and redistribution permissions separately. This implementation **does not retrieve the artifact, verify its hash against a file, authenticate its origin, or certify its measurements**. `source_sha256` is a declared identifier, and `source_independently_verified` is always false. Do not claim a source was measured simply because its schema parses.

## Calculation modes

```python
from pipelines.performance_solver import PerformanceSolver

# Existing design exploration: explicit preliminary scope and warnings.
report = PerformanceSolver().solve(selected_components)

# Refuses missing or inconsistent source declarations.
report = PerformanceSolver().solve(
    selected_components,
    require_matching_evidence=True,
)
```

The assessment is `missing`, `mismatch`, or `matched_declaration`. Matching requires exact motor and propeller IDs, unchanged curve fingerprint, matching test and point voltages, matching selected battery nominal voltage, and curve RPM within the selected propeller rating. The absolute voltage tolerance is 0.05 V for rounded entries; it is **not** a validated operating envelope. There is no voltage scaling or extrapolation in the evidence matcher.

Default component selection still uses the preliminary calculation mode. Its legacy `is_flyable` property means numerical feasibility under that calculation, not validated hardware or measured flight readiness. Consumers must read `estimate_scope` and `propulsion_evidence`. The strict option is available to callers but is not presented as a certification gate.

Evidence travels in serialized components, performance reports and engineering exports. Since resolved model identity includes its components, changing the source declaration changes the model fingerprint and invalidates an older linked export.

## Sourced tests

[`apps/api/scripts/extract_propulsion_tests.py`](../apps/api/scripts/extract_propulsion_tests.py) fetches T-MOTOR's product pages and writes:

| File | Content |
| --- | --- |
| `.local/components/evidence/tmotor_mn3508.json`, `tmotor_mn3110.json`, `tmotor_mn4014.json` | Every «Test Data» row and specification row of the page, cell text verbatim (merged cells repeated on each row) |
| `.local/components/evidence/tmotor_p_series_propellers.json` | The specification table of each P-series propeller page |
| `.local/components/propulsion_tests.json` | One test per (catalog motor, catalog propeller, voltage). Each declares the SHA-256 of its extract; excluded tables are listed with the reason |

- `--write-catalog` builds the motor and propeller entries from the same extracts. No catalog value is typed by hand.
- `--check` re-fetches the pages and reports whether the published rows changed.
- Source hashes are calculated from the locally written extract bytes. Local imports are excluded from Git and Docker build contexts.

`tests/test_propulsion_evidence.py` checks in CI:
- every test and every motor's default curve equals its extract rows, point by point;
- the hashes match the extract files;
- motor weight and continuous current equal the specification rows;
- propeller mass and RPM rating equal their pages.

That checks the transcription, not the measurement.

What was **not** taken from a source, and is stated as such:
- `max_burst_current_a` repeats the 180 s continuous rating, because T-MOTOR publishes no burst figure.
- A propeller's `max_rpm_safe` is the upper end of its published «Optimum RPM» range, because no maximum is published. Curves above it are refused.
- The propeller hub size is the catalog's shaft-fit heuristic (4 mm for the MN motors).
- The existing P17×5.8 entry keeps its 5 mm hub, so it pairs only with the (unsourced) MN5208.

Two things the extraction found and reported instead of changing:
- **A publisher typo.** MN4014 KV400 with the 17×5.8 at 22.2 V lists 2.17 A, 48.17 W and 64.56 g/W at 85% throttle, between 16.7 A and 25.7 A. The whole combination is excluded and the reason recorded.
- **Old catalog values.** The P13×4.4 weighs 14.2 g on its page (the catalog had 21 g). The P17×5.8 weighs 26.5 g (35 g), and its optimum RPM ends at 6000 (the catalog claimed 7500).

Tests on the 9×3, 10×3.3 and 11×3.7 propellers stay in the extracts but are not catalog tests, because those propellers have no specification page to source their mass from.

The source metadata does not grant redistribution permission. The public package contains the importer and validation code, not the extracts or their derived catalog records. Keep local imports private unless permission is obtained.

What the source does **not** state, and so nothing here claims:
- the ESC, or what "throttle" commands;
- whether the voltage was held by a supply or sagged;
- air density and rig geometry;
- uncertainty and sample count;
- reaction torque;
- a revision date.

The flight model therefore fits `T = kω²` to the published points, and it warns when:
- hover falls below the lowest published point, where the curve is extrapolated;
- the fit deviates from the points. It deviates up to 23% for the MN3110 KV700 + P12×4.

### Measured curves start at 50% throttle

Manufacturer tables often begin at 50% throttle. The solver interpolates linearly and clamps below the first point, so a light aircraft would have been reported at that point's throttle, current and power as though measured.
- Preliminary mode now warns `Outside the propulsion curve`.
- `require_matching_evidence=True` refuses the hover point outright.

The same warning appears for the unsourced curves whenever hover falls below their first point.

### Motors that stay unsourced (survey of 2026-09-25)

| Catalog motor | What the publisher shows | Result |
| --- | --- | --- |
| T-MOTOR MN5208 KV340 (`tmotor_p17_58`, 6S) | Store page test table with T-MOTOR 17×5.8CF, including torque, **at 24 V** | Voltage mismatch with 22.2 V nominal; no scaling is allowed, so unsourced. The published torque would be useful for a future yaw model |
| T-MOTOR F80 PRO KV1900 | T-Hobby page, test data only as images: **5055 tri-blade at ~23.9 V** (6S, per-point voltage) | The catalog pairs it with 5.1-inch propellers at 14.8 V; mismatch. The catalog's `store.tmotor.com` URL returns 404 |
| iFlight XING2 2207 1855KV | Product page states a 24 V test input; no propeller-specific table found | Mismatch with 14.8 V; unsourced |
| EMAX RS2306 2400KV | No manufacturer table found; third-party bench data exists (MiniQuadTestBench) at other voltages, with unclear redistribution rights | Unsourced |
| T-MOTOR Air 2207, Happymodel EX1404 | Not found in this pass | Unsourced |

## Remaining work before flight dynamics

- Acquire test data for the remaining combinations, and ask T-MOTOR for redistribution permission. Independent bench data is needed to check even the sourced curve.
- Cover battery voltage variation and the operating region needed for hover and maneuvers; nominal voltage matching alone is insufficient.
- Collect reaction torque, rotational response, air density and test setup details before fitting dynamic coefficients. Electrical power is not a measurement of shaft torque.
- Fit and check a dynamic model with documented residuals and domain limits.
- Sensors, PX4 actuator mapping, runtime model-identity checks and generated-model SITL flights now exist ([FLIGHT_MODEL_SITL.md](FLIGHT_MODEL_SITL.md)). They fly catalog estimates, so they do not replace any item above.

A matched declaration neither enables nor upgrades generated-aircraft SITL. The flight bundle's scope stays `sitl_catalog_estimate` either way. Synthetic declarations used in unit tests are explicitly test fixtures, not catalog evidence or hardware calibration.
