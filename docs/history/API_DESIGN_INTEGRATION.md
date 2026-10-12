# API design integration in WSL

Historical test: 20–21 September 2026. This is an English summary of the earlier development note, not a new validation run.

A project requesting a quadcopter with a 100 g camera, five-minute target duration and 10 m altitude was created through `POST /api/projects/`. The test used the real API, local development authentication, Ollama on Windows and PostgreSQL in WSL. The design could be retrieved through the API, and the `human_review_design` checkpoint survived a server restart. The test did not approve the design or fly it.

The run exposed an incompatible existing Chroma index and missing diagnostic fields in the workflow state. A separate index and persisted generation diagnostics made failures and repairs observable. A later read from an independent PostgreSQL process confirmed `design_generation.method=rag_llm_repaired`, one repair and matching stored diagnostics. Model response time was 28.8 seconds, excluding model loading and indexing.

The first generated battery specification was implausible: 4S, 34,000 mAh and 480 g implied roughly 503 Wh and 1,048 Wh/kg. A saved payload limit also did not establish an installed payload mass. Successful API persistence therefore did not establish engineering feasibility. Catalog selection and the resolved aircraft specification must determine actual mass and performance; see [payload handling](PAYLOAD_COMPONENT_CHECK.md) and [component search](COMPONENT_SEARCH.md).
