# eljazari.ai — Research Foundation


The architecture borrows ideas from the papers below. **The "Reported Metric"
column is the result claimed by each source paper — not a measurement of this
implementation.** This project adapts the *techniques*; it does not reproduce
or benchmark those numbers.

| Paper | Technique we adapted | Reported Metric (from the paper) |
|-------|----------------------|----------------------------------|
| AGENT (arxiv:2504.08981) | Design-from-text + RAG pattern | 12s eval vs 120s simulation |
| GSCE (arxiv:2502.12531) | 4-layer prompt framework (Guidelines/Skill APIs/Constraints/Examples) | 7.6% → **90.9%** success |
| Taking Flight (arxiv:2506.07509) | PX4+ROS2+Ollama local-model stack | 100% valid commands (local models) |
| Closed-Loop UAV (arxiv:2507.01930) | Semantic telemetry → LLM evaluation loop | 85% success, 98.5% completeness |
| Hitachi ReActEval (arxiv:2510.00259) | Head + Worker ReActEval monitoring | Scalable fleet management |
| AircraftVerse (NeurIPS 2023) | Design dataset schema | 27,714 UAV designs (processed subset bundled in `data/aircraftverse/designs.json`) |

---

