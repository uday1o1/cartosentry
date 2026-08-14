# Evaluation charter v0

CartoSentry freezes its evaluation identities, numerical decisions, fault scope, and claim fallbacks before confirmatory implementation work begins.
The machine-readable authority is `benchmarks/split_manifest.yaml`, `benchmarks/numerical_charter.yaml`, `benchmarks/fault_matrix_v1.yaml`, and `benchmarks/fallback_tree.yaml`.

## Partition discipline

Every real sequence inherits the indivisible source-group assignment recorded before the M0.2 data inspection.
Every derivative, including a fault-injected derivative, inherits its clean source group's partition.
The split has development, threshold-calibration, policy-tuning, and final-test partitions.
Ordinary commands can select only the first three partitions.
The final-test identities are recorded in the public split contract for reproducibility, but execution access remains mechanically sealed until an audited unblinding event is created.

Synthetic family identifiers and seeds are expanded mechanically from frozen prefixes, counts, and seed starts.
The zero-padded one-based family index and zero-based seed offset make repeated expansion byte-for-byte deterministic.
The family identifier is also the independent bootstrap cluster identifier.

Validate the complete frozen contract with:

```console
uv run python scripts/evaluation_charter.py validate
```

Select an ordinary partition with:

```console
uv run python scripts/evaluation_charter.py select --partition development
```

Attempting to select `final_test` without an event fails before returning any identities.

## Final-test unblinding

Unblinding is permitted only for a clean, exact Git commit bound to every frozen charter file.
The authorization file must contain exactly `I_AUTHORIZE_FINAL_TEST_UNBLINDING`.
The explicit confirmation argument must be `UNBLIND_FINAL_TEST`.
The event log must be a new path outside the repository so routine development cannot overwrite or commit it.

Prepare a release-binding JSON document with schema version 1, state `BOUND`, a release tag, the exact Git commit, and the complete `file_sha256` mapping emitted by the validation command under the key `frozen_file_sha256`.
Then run:

```console
uv run python scripts/evaluation_charter.py unblind \
  --release-binding /external/RELEASE_BINDING.json \
  --authorization-file /external/AUTHORIZATION.txt \
  --event-log /external/UNBLINDING_EVENT.json \
  --confirmation UNBLIND_FINAL_TEST
```

The command records a unique event identifier, UTC time, exact commit, release tag, charter hash, frozen file hashes, and selected final identities.
The event log is created atomically and an existing event is never replaced.

## Statistical decisions

Intervals use a 10,000-replicate clustered percentile bootstrap with seed `2026081401`.
The bootstrap samples whole source groups, synthetic families, scenario-graph families, independent drives, or complete performance runs according to the domain.
Event intervals are half-open and are matched one-to-one at interval IoU of at least 0.5 with frozen deterministic tie breakers.
Confirmatory claims require the support counts in the numerical charter and apply Holm correction inside the three predeclared metric families.
Insufficient support produces a descriptive result, never a passing confirmatory result.

Each numerical gate has a stable key, operator, value, unit, decision bound, responsible metric, and rationale.
Performance evidence uses one warmup and five measured Release runs on a bound native Linux x86-64 host.
Apple Silicon and emulated Linux runs are development evidence and cannot satisfy a blocking performance claim.

## Fault and claim scope

The V1 fault identifier is `cartosentry-v1-core`.
Only its six exact trajectory and lidar operators are accepted.
Every other operator, including the listed follow-on examples, is rejected before an identifier can be derived.
Fault identifiers bind the matrix, operator, case, source family, and source content SHA-256 through canonical JSON.

The fallback tree freezes every narrower claim that may replace a primary claim when support or an empirical gate fails.
Selecting a narrower branch changes the stated population and evidence scope, but never changes a numerical threshold after observing final-test results.

## Changes from the starting plan

No starting numerical value was changed.
The existing final public Boreas sequence is currently excluded because its pinned public post-processed trajectory is unavailable, so it cannot support a confirmatory real-data V1 claim.
This eligibility finding was recorded during M0.2 without moving the sequence or source group.
The pre-M0.2 partition assignment and its exact file hash remain unchanged.
