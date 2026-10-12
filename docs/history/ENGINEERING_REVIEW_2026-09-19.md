# Engineering review — 19 September 2026

English historical summary. The original review covered the working tree near `09175ceb`, including local changes, rather than that commit alone. Its findings describe that date and must not be read as the current release status.

The project had a useful FastAPI/Next.js foundation, LangGraph orchestration, human review, retrieval, component selection, MAVSDK generation and PX4/Gazebo integration. The central gap was one verified aircraft identity connecting components, mass, geometry, flight dynamics, simulation, the bill of materials and the final report. Flying a mission on a reference x500 did not validate the generated aircraft.

| Finding | Required change |
| --- | --- |
| F01: simulation could use a reference aircraft | Spawn and attest the generated aircraft and its fingerprint |
| F02: structural SDF was not a flight model | Generate rotors, inertia, sensors, dynamics and compatible PX4 settings |
| F03: selected components and deliverables diverged | Derive outputs from one resolved specification |
| F04: infeasible component sets could pass | Reject incompatible and underperforming combinations |
| F05: thrust curves lacked pairing provenance | Bind each curve to motor, propeller, voltage and source |
| F06: ESC quantities and payload/duration were inconsistent | Align counts, installed mass and performance constraints |
| F07: acceptance did not combine deterministic requirements | Gate identity, telemetry, isolation and mission conformance together |
| F08: generated code inherited service privileges | Require approval identity and isolated mission execution |
| F09: durability and worker ownership were incomplete | Repair checkpoint lifecycle and cross-worker run ownership |
| F10: Docker and WSL simulation paths differed | Use the same generated-aircraft and acceptance contracts |
| F11: CI did not prove the full claim | Require tests and real SITL evidence for the release commit |

The review recommended an explicitly limited research release after the blockers were addressed. It did not endorse manufacturing readiness, airworthiness or a claim of being the world's first such system. Existing flight reports were prior evidence, not flights repeated during that review.

The subsequent work is summarized in [Implementation progress](IMPLEMENTATION_PROGRESS.md). Current implementation details and limits are in [Flight models](../FLIGHT_MODEL_SITL.md), [Mission sandbox](../MISSION_SANDBOX.md), [Acceptance](../SIMULATION_ACCEPTANCE.md), [Multiworker operation](../MULTIWORKER.md) and [Release gate](../RELEASE_GATE.md).
