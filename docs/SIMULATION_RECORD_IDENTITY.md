# Simulation record identity

The graph's collection node owns simulation-row creation. After committing a row, it returns its UUID as `simulation_record_id`. Evaluation and reporting deltas carry this ID and the same one-based `simulation_iteration`. Simulation completion events use the committed ID when available.

Persistence handlers resolve the exact ID and check both project ownership and iteration before updating evaluations, supplementing flight logs or attaching monitor reports. They never infer the target from the project's newest simulation. An absent, malformed, deleted, foreign-project or wrong-iteration ID causes the write to be skipped with a log message. Old checkpoints without this identity must not be used to update a guessed row.

The event handler no longer creates a second simulation row or overwrites a result with `completed` when collection finishes. The graph initially records a completed mission as `pending` evaluation, with no success score; known crashes or mission failures are recorded as failures. Semantic acceptance sets the eventual evaluation state.

Before initial persistence, the latest stored code text must exactly equal the code that was executed. A mismatch is skipped rather than attaching telemetry to a different revision. This is a conservative check, not a complete code-version identity system.

Every new attempt clears the previous record ID. A database/commit failure leaves the ID absent and produces a warning. Runtime event handling does not fabricate a replacement row. A generated design blocked from flight also loses any prior flight identity when evaluated or reported.

## Validation and remaining work

Tests use mocked repositories/transactions to cover late events, wrong projects, wrong iterations, missing/deleted IDs, commit failures, code mismatch, iteration numbering, and sequential replay of log supplementation. They are not a live PostgreSQL concurrency or crash-recovery test.

The `pipeline_logs_written` marker avoids repeated sequential log supplementation. Concurrent replay still requires locking/idempotency guarantees. A process crash between database commit and checkpoint persistence may leave an orphaned row; durable checkpoint lifecycle, a stable execution ID and transactional event handling remain necessary.

The existing PostgreSQL checkpointer setup and application lifecycle have not been repaired in this phase. Multi-worker coordination, code/design approval identity, run-bound download authorization, and the separate manual simulation endpoint also require follow-up. This change does not establish restart-safe execution or physical flight readiness.
