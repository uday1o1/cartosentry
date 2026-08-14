# Run state and crash recovery

CartoSentry persists each analysis run in a local SQLite database and publishes stage artifacts through immutable attempt directories.
The database uses foreign-key enforcement, write-ahead logging, full synchronous durability, a busy timeout, and explicit immediate write transactions.
An exclusive advisory lock prevents concurrent writers from resuming or forcing the same run directory.
SQLite owns state transitions, while content hashes reconcile the transaction boundary between SQLite and the filesystem.

## Stage lifecycle

Every registered workflow is a dependency-ordered directed acyclic graph.
Stages use the persisted `PENDING`, `RUNNING`, `COMPLETE`, `FAILED_RETRYABLE`, `FAILED_FINAL`, `INVALIDATED`, and `SKIPPED_NOT_APPLICABLE` states defined by the run artifact contract.
A stage can enter `RUNNING` only when every declared upstream stage is `COMPLETE` and its upstream artifact hashes are available.

Each stage cache key binds the workflow and stage identities, every source hash, relevant upstream artifact hashes, relevant configuration hashes, the algorithm version, and the numerical backend.
A report-theme hash is relevant only to report assembly, while a detector-threshold hash is relevant to detection and reaches policy and report stages through upstream artifact identities.
Persisted run inputs and stage topology are immutable for one run directory.

## Artifact commit protocol

A stage creates a unique attempt directory and records its attempt number in SQLite before invoking stage code.
It validates every output against its registered stage schema, writes and flushes the exact output set, writes a hash-bound attempt manifest, and flushes the attempt directory.
It then atomically renames the attempt into an immutable cache directory on the same filesystem.
An atomic completion pointer selects one fully verified attempt, and a final SQLite transaction records the same artifact identities.

Published attempts are never overwritten or silently deleted.
Repeated forced execution for one cache key must produce the same artifact hashes, or reconciliation reports a final integrity failure.
Incomplete attempt directories remain under `.attempts` for later inspection and explicit cleanup.

## Resume and reconciliation

Resume first runs SQLite `quick_check` and then visits stages in dependency order.
A valid filesystem publish that precedes the SQLite completion transaction is adopted by full hash.
A missing or stale completion pointer is rebuilt from a valid immutable attempt manifest and verified artifact bytes.
A database record that claims completion without its immutable artifacts becomes `FAILED_FINAL` instead of being silently recomputed.
A `RUNNING` attempt without a published immutable directory becomes `FAILED_RETRYABLE` and is executed as a new unique attempt.

Valid complete stages are skipped.
`--force-stage` invalidates only the selected stage and its dependent closure and records a minimum new attempt number so a crash cannot re-adopt an older cached result.
Use `--dry-run` to display the exact closure without changing the run.

```bash
cartosentry resume-run RUN_ROOT --force-stage detect --dry-run
cartosentry resume-run RUN_ROOT --force-stage detect
```

## Frozen interruption qualification

The repository-owned qualification starts real worker processes and terminates them after each database and filesystem commit boundary.
Every terminated run is resumed and compared with an uninterrupted semantic artifact hash.
The suite also verifies orphan adoption, stale-pointer repair, stable missing-artifact failure, dry-run immutability, and exact dependency-closure invalidation.

```bash
cartosentry qualify-run-recovery recovery-evidence
```

The output directory is new and contains the uninterrupted run, one run per injected boundary, reconciliation cases, and `qualification.json`.
