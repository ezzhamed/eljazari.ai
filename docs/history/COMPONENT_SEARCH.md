# Component combination search

Historical changes: 21 and 29 September 2026.

The selector previously tried only three combinations, one per price tier. It now enumerates catalog combinations passing voltage, battery-cell, ESC current/channel, approximate shaft fit, propeller/frame clearance, wheelbase and PX4/MAVSDK compatibility filters. The performance solver evaluates the requested payload and duration and rejects infeasible or critically warned combinations.

Up to three distinct alternatives are selected: best score, lowest known cost and longest estimated duration. Missing prices are not treated as free. Ties are deterministic regardless of catalog ordering. The default search limit is 50,000 combinations; reaching it reports an incomplete search rather than declaring a partial search complete or claiming no solution exists.

A historical local example evaluated 828 combinations, with 256 passing the then-current filters, in approximately 0.59 seconds. Its estimated mass was 1,458.2 g with a 100 g payload. The corresponding structural URDF mass agreed. Its predicted endurance was an estimate, not measured flight performance, and its propulsion pairing lacked measurement provenance.

## Frame expansion

On 29 September, a benchmark showed that a small frame proposed by the design model could exclude every frame supporting sourced propulsion. Search now tries the proposed size first, then other frames with the same motor count if needed to find a sourced combination. Expansion is reported in the result and agent log.

An explicit total-mass limit applies to both searches. Expansion cannot override the user's mass requirement. A request that permits only an unsourced lightweight combination must remain blocked from accepted SITL flight.

Historical validation included 69 selection, engineering-integrity and structural-model tests. These numbers describe that development session, not the latest release checks. Manufacturer test data is now imported locally; see [local component data](../LOCAL_COMPONENT_DATA.md).
