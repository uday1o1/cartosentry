# Bounded analysis scheduler

CartoSentry runs native analysis work through a byte-budgeted C++20 scheduler.
The scheduler accounts for queued and active work against one resident-byte budget, so producers apply backpressure before admitting work that would exceed the configured bound.

## Work and fairness contract

Every work unit has an immutable unit identifier, stage identifier, modality, estimated resident-byte cost, and stop-token-aware function.
Separate FIFO queues hold metadata, IMU, trajectory, lidar, camera, and radar work.
Workers select queues with stable round-robin modality fairness while preserving FIFO order inside each modality.
This prevents a sustained stream of tiny IMU work from starving less frequent, larger lidar work.

Normal mode admits work concurrently and blocks producers when the next unit would exceed the byte budget.
Deterministic mode uses one worker and begins execution only after input closes, producing the same fair execution order for the same submitted batch.
The complete deterministic batch must fit the configured resident-byte budget, and submission rejects a batch that would exceed it instead of deadlocking.

## Failures, cancellation, and metrics

Expected task failures and caught worker exceptions become structured values with unit and stage identities.
An error in one work unit does not terminate unrelated work.
Cancellation closes admission, drains queued units as cancelled outcomes, and sends cooperative stop requests to active work.
Artifact code must publish a complete pointer only after every scheduled unit succeeds.

Snapshots report queue depth, queued bytes, active units, active bytes, resident bytes, peaks, completion states, backpressure duration, modality completions, and per-stage totals.
Resident bytes include both queued and active estimates and return to zero after a completed or cancelled run.

## Frozen qualification

The repository-owned `benchmarks/scheduler_stress.yaml` suite mixes 1,800 64-byte IMU units with 200 4,096-byte lidar units under a 32,768-byte budget and four workers.
The public qualification command runs the mixed stress, repeats a deterministic trace, forces real producer backpressure, isolates checked and thrown worker failures, and cancels two active plus two queued units.

```bash
cartosentry qualify-scheduler scheduler-evidence \
  --suite benchmarks/scheduler_stress.yaml
```

The command refuses an existing output directory.
Its cancellation attempt retains only clearly named partial files and never creates `completion.json`.
Linux CI runs the native suite once with AddressSanitizer plus UndefinedBehaviorSanitizer and again in a separate ThreadSanitizer process.
