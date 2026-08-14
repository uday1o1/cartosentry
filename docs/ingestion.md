# Immutable ingestion

CartoSentry's first ingestion pass creates a portable sequence manifest and a streaming frame index without modifying the source recording.

The production Boreas path is:

```bash
uv run cartosentry index-boreas \
  data/public/boreas-2021-09-02-11-42 \
  run-input/boreas-clear-index \
  --split-manifest benchmarks/split_manifest.yaml \
  --budget benchmarks/ingestion_budget.yaml
```

The command exits with status `0` only when the immutable output directory is published.

A structural rejection exits with status `1`, returns machine-readable findings, and leaves no output directory or completion pointer.

Invalid command arguments, an existing destination, or an output destination inside the source recording exit with status `2`.

## Published contract

One successful directory contains exactly these files.

| File | Purpose |
| --- | --- |
| `sequence-manifest.json` | Canonical `cartosentry.sequence-manifest.v1` source, adapter, sensor, calibration, timestamp, coordinate, and partition identity. |
| `frame-index.jsonl` | Incrementally written `cartosentry.frame-index-entry.v1` records with portable source locators, frame identities, explicit times, parse state, and lightweight payload counts. |
| `frame-index-summary.json` | Source timestamp ranges, duplicate counts, structural findings, frozen budget identity, and the manifest and index hashes. |
| `completion.json` | The small completion pointer that binds the three semantic artifacts by SHA-256. |

The implementation writes into a unique sibling attempt directory, flushes and synchronizes every artifact, validates the portable manifest, computes all artifact hashes, and publishes the directory with one same-filesystem rename.

Temporary uniqueness state is not published.

Local absolute paths, host identity, file modification times, device identifiers, and inode identifiers are excluded from every portable artifact.

The scanner compares those local snapshot fields before hashing and after parsing only to detect a recording that changes during the pass.

## Bounded-memory behavior

The frozen `m2.2-v1` budget in `benchmarks/ingestion_budget.yaml` limits both source hash chunks and writer-owned index batches to `1,048,576` bytes.

Source files are hashed sequentially with SHA-256.

The sequence source identity is SHA-256 over the concatenated selected source bytes in ascending portable source-key order.

Trajectory and lidar-pose rows update range summaries one record at a time and are not retained or copied into the frame index.

Frame entries are serialized in bounded batches.

The retained index-batch measurement includes the CPython 3.12 list and byte-object allocations owned by the writer rather than only the payload lengths.

Non-adjacent duplicate timestamps are detected with a disk-backed uniqueness table configured with a bounded cache.

Source ordering is not treated as timestamp ordering.

When a source iterator is reordered, each index record retains its timestamp and source ordinal, the range summary marks the order as nonmonotonic, and an informational `SOURCE_ORDER_REORDERED` finding is emitted.

Duplicate timestamps remain blocking because they make a stream identity ambiguous.

## Structural findings

The scanner distinguishes missing required sources, duplicate source keys, duplicate canonical timestamps, corrupt record layouts, read failures, parse failures, and recordings that change during indexing.

Finding text contains portable source keys and record positions rather than absolute local paths or raw malformed values.

The frozen finding cap converts an excessive adversarial finding stream into one blocking `FINDING_LIMIT_EXCEEDED` result instead of allowing unbounded memory growth.

## Measured public-smoke result

The verified clear Boreas public-smoke selection produced the following result on both mandatory correctness platforms.

| Metric | Observed |
| --- | ---: |
| Selected source files | 15 |
| Selected source bytes | 131,213,082 |
| Trajectory samples summarized | 214,719 |
| Lidar-pose samples summarized | 9,967 |
| Lidar frames indexed | 10 |
| Duplicate timestamps | 0 |
| Structural findings | 0 |
| Frozen retained index-batch budget | 1,048,576 bytes |
| Maximum retained index batch | 10,434 bytes |

The macOS ARM64 and pinned Linux x86-64 runs produced byte-identical copies of all four published files.

The selected source byte count excludes unsupported camera calibration payloads and the route visualization because those bytes do not contribute to the V1 trajectory and lidar adapter output.

The public-smoke sequence manifest SHA-256 is `4c8a52f0970719e9f563c5245b15280a1df00f1e54cc90650e532c381e204bb2`.

The public-smoke frame-index SHA-256 is `5c16219abff9611369b315fc3319186aaf192e41ae07890763216fc6797d6c15`.

The public-smoke frame-index summary SHA-256 is `0ffbdc16a81385fee6998da751b1446166296ab99ba6133e63a5ca00118821b5`.

The public-smoke completion pointer SHA-256 is `e64fab7aa1ce4c69a996e343091e35814f5204ff1fb3778f7fb056e6bc6d8f69`.

These hashes are qualified as correctness and portability evidence only.

They are not full-pipeline throughput claims.
