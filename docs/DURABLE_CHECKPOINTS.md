# Durable checkpoints

`CHECKPOINT_BACKEND` selects graph state storage:

| Value | Behavior |
| --- | --- |
| `auto` (default) | PostgreSQL in production; volatile memory in development |
| `postgres` | PostgreSQL in either environment; errors never fall back to memory |
| `memory` | Development only; rejected by production settings |

The development default is unchanged. Installing the dependency does not activate persistent storage in an existing development configuration. For a durable deployment, set `CHECKPOINT_BACKEND=postgres` and use the supported Linux/WSL execution environment with the application's PostgreSQL credentials configured.

## Connection ownership

`get_workflow_async()` is now an asynchronous context manager. Use `async with`; do not await it or retain the returned graph after the context exits. This follows the context-managed connection lifetime of [LangGraph's PostgreSQL saver](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres).

The orchestrator opens a graph/connection for each run, resume, approval, rejection or state/history read. The connection stays open for the entire operation and closes on completion, failure, cancellation or explicit stream closure. PostgreSQL graphs are not cached across worker threads or event loops. The development memory graph remains shared in-process.

At API startup, PostgreSQL mode opens a short-lived connection and runs checkpoint setup/migrations. Failure stops startup. Each later operation also fails explicitly if storage is unavailable. Applications embedding the orchestrator directly must initialize the schema with `async with get_workflow_async(setup=True)` before first use.

This change uses one connection per operation rather than a shared connection pool. Each suspended review closes its operation's connection; resuming opens a new connection to the same persisted thread ID. Connection pooling and distributed execution coordination remain separate work.

## Windows limitation

Psycopg async connections do not support the default Windows Proactor event loop. The existing Windows simulation paths need asynchronous subprocess support, so changing the entire application to a Selector loop is not a safe general fix. Run the durable application under Linux/WSL. Direct native Windows PostgreSQL mode may fail at startup and does not silently fall back to memory.

The checkpoint smoke test uses a Selector loop in its Windows child processes because it runs no subprocesses from those loops and launches no simulator. Passing that test does not establish support for the full native Windows pipeline.

## Verification

From `apps/api`, after installing requirements and configuring a local PostgreSQL database:

```bash
python scripts/check_checkpoint_restart.py
```

This opt-in test permits only an explicitly local database host. It creates a uniquely named schema, runs three separate processes and removes that schema afterward:

1. Write graph state and suspend before a review node.
2. In a fresh process, recover the review state, approve and resume it.
3. In another fresh process, verify completion, approval and the retained simulation record ID.

It uses the production checkpoint factory and orchestrator with a tiny deterministic graph. It does not run the design agent, LLM, Gazebo or aircraft. Existing application tables are not used as test storage.

Verified locally with PostgreSQL, Python 3.13, LangGraph 1.1.8, checkpoint 4.0.2, checkpoint-postgres 3.0.4, psycopg/binary 3.3.6 and psycopg-pool 3.3.2. The repository's full pinned deployment environment still needs its own CI verification. Test-only packages were installed in an ignored local directory, without changing the global Python installation.

## Remaining release requirements

- End-to-end restart of the complete API/pipeline in its deployment environment.
- Coordination of concurrent approvals and multiple workers.
- Idempotency for side effects across crashes between a database commit and a graph checkpoint.
- Explicit cancellation and shutdown of worker threads.
- Separate migration privileges, connection limits and operational recovery checks.

Durable graph state is not proof of exactly-once execution or flight validation.
