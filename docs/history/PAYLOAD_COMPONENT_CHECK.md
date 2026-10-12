# Payload and component consistency

Historical test: 21 September 2026.

A saved design specified a 100 g camera but the design-repair path installed a default 500 g payload. The earlier parser accepted `100g camera` but missed `100 gram camera`.

Explicit payload extraction now accepts g/gram/grams and kg/kilogram/kilograms before or after camera, payload or FPV terms. It converts kilograms to grams and applies the explicit mass to candidate and repair designs before performance calculation. It does not retrospectively rewrite saved projects. Missing installed mass no longer causes the solver to count only half the declared payload. Diagnostics record the evaluated payload.

Conflicting explicit weights or requests without a weight do not manufacture a mass. Ambiguous language still requires careful handling.

After the fix, the historical design used 100 g, but the limited selector still rejected its candidate sets: predicted cruise times of 2.8 and 2.2 minutes missed the five-minute requirement. No noncompliant set was saved as feasible. This established failure of those candidates, not impossibility across the whole catalog. [Expanded component search](COMPONENT_SEARCH.md) addressed that limitation later.

The extended test run initially had 117 passes and one obsolete half-payload expectation. After replacing that expectation with a full-payload mass/endurance regression, all 45 affected tests passed. These test groups overlap; no complete backend run or flight occurred in that session.
