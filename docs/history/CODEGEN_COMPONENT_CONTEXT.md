# Code generation from resolved components

Historical change: 21 September 2026.

The workflow previously passed the preliminary design into code generation after selecting real components; a legacy generation path discarded the design context entirely. Both paths were corrected.

When selected components exist, the generator rebuilds their structural specification and verifies it against the stored specification and fingerprint. Missing specifications or changed components/payload stop generation and clear stale code and configuration from the node result. The original design tree is not changed.

The generator receives catalog battery capacity and mass, the selected frame, motor/propeller/controller identities, installed payload, part numbers, total mass, model fingerprint and the model's validation scope. An explicit installed payload of zero is preserved. Legacy states without selected components remain marked `unverified_design`.

Historical validation: 125 focused tests passed; a later group of 8 workflow-binding tests also passed and overlaps that group. A real resolved specification confirmed a 6,000 mAh battery, 100 g payload and 1,458.2 g total mass. That check did not call an LLM. Ruff and diff checks passed; the full backend suite was not rerun in that session.

Correct context does not prove that generated code obeys every constraint. Static checks, approval identity and mission-specific simulation acceptance remain necessary. The change did not run a mission or delete historical database code versions.
