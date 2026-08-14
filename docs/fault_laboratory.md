# Deterministic fault laboratory

CartoSentry's V1 fault laboratory creates controlled derivatives of canonical synthetic fixtures without changing the source artifact.
Every allowed operator and case comes from the frozen `cartosentry-v1-core` matrix in `benchmarks/fault_matrix_v1.yaml`.
Namespace similarity is not sufficient, and follow-on IMU, camera, radar, cross-modal, and topology operators are rejected by the V1 registry.

## Provenance contract

Injection accepts canonical clean fixture bytes, an exact matrix case, a deterministic target-selection seed, and the SHA-256 of clean-source truth frozen before injection.
The source is validated and the complete target range is checked before a mutable copy is made or an output directory is created.
The source fixture identifier, source byte hash, source family and group, inherited partition, clean-truth hash, operator version, typed parameters, target streams and fields, source interval, resulting artifact hash, expected detector capabilities, and expected affected road bins are recorded in the fault manifest.

Each semantic change is represented by an absolute JSON Pointer, a change kind, and hashes of the source and derived values.
Removal operators record the source value hash and no derived value hash.
Because both source and derivative use one canonical sorted-key serialization, independently replaying the request verifies both the semantic attribution and the exact derivative bytes.
The derivative intentionally retains the clean source schema tag and fixture identifier as provenance and may fail clean-fixture validation because the injected fault violates that contract.

The fault identifier follows the frozen matrix contract exactly.
It hashes matrix ID, operator ID, case ID, source family ID, and source identity SHA-256.
The seed remains a required manifest field but is not silently added to that frozen identity contract.

## Supported V1 operators through trajectory integrity

The registry implements the nine operators currently frozen in the `cartosentry-v1-core` matrix:

- `trajectory.timestamp_discontinuity`
- `trajectory.position_jump`
- `trajectory.position_freeze`
- `trajectory.position_bias`
- `trajectory.position_drift`
- `lidar.point_time_shift`
- `lidar.ring_loss`
- `lidar.azimuth_sector_loss`
- `lidar.calibration_perturbation`

Timestamp discontinuity creates one exact consecutive delta and applies its time step through the remaining support, so it does not manufacture an inverse exit regression.
Position jump applies a bounded constant translation whose entry and exit are both ground-truth discontinuities.
Position freeze holds the selected translation while retaining time and paired source-velocity observations.
Position bias translates the complete trajectory support, which makes it deliberately unobservable without an independent position reference.
Position drift applies a linearly increasing translation from the selected start through the remaining trajectory support.
The translation reaches `terminal_translation_m` after `duration_s` and continues at that rate, which avoids an unintended exit discontinuity.
Position bias applies one of three frozen seed-scaled translations within the case severity while retaining a whole-support constant offset.
Point-time shift changes only relative point times.
Ring and sector loss remove only selected lidar points.
Calibration perturbation changes only the named `T_rig_lidar` matrix.

Every trajectory and timestamp family has below-threshold, near-threshold, and detectable cases whose labels refer to the measured detector quantity.
The synthetic generator exposes a 32-column lidar control for the frozen 5-degree sector case.
Its default remains the compact 16-column fixture set.

## Inject and verify

The clean-source truth argument is a file, not a caller-supplied hash, so the public command binds the manifest to an artifact that exists before injection.

```console
uv run cartosentry inject-synthetic-fault \
  tests/fixtures/synthetic/v1/fixtures/sensor-map-dev-001.json \
  output/fault-001 \
  --operator lidar.ring_loss \
  --case ring-loss-1-short \
  --seed 42 \
  --clean-source-truth clean-truth.json
```

The output path must not already exist.
The command publishes `derivative.json` and `manifest.json` together through a staged directory rename.

Reapply the recorded operator and compare the exact bytes and manifest:

```console
uv run cartosentry verify-synthetic-fault \
  tests/fixtures/synthetic/v1/fixtures/sensor-map-dev-001.json \
  output/fault-001
```

Fault derivatives are local benchmark data and are not committed or redistributed.

Run the complete deterministic representative-operator qualification with:

```console
uv run python scripts/qualify_fault_laboratory.py
```
