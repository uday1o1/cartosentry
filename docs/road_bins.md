# Directed road bins and spatial evidence

CartoSentry converts confident moving road matches into fixed longitudinal bins on directed arcs.
The M5.4 implementation records usable distance and duration, independent traversals, speed distribution, yaw excitation, modality evidence, and affected findings without allowing ambiguous or off-map matches to count as coverage.

## Native implementation boundary

Bin construction, path-segment splitting, independent-pass segmentation, modality evidence joins, coverage aggregation, and finding localization run in a C++20 batch kernel.
The pybind11 boundary releases the Python global interpreter lock while the kernel executes.
Python authenticates the graph, decoder, binning profile, evidence intervals, and findings, then validates native references and composes the identity-bound portable coverage ledger.
The ledger and qualification report declare `C++20_NATIVE_BATCH_V1` as their algorithm backend.

## Frozen profile

`profiles/road_binning_v1.yaml` is the self-hashed M5.4 aggregation charter.
It binds the exact graph-import profile, map-decoder profile, and numerical charter used by the implementation.
The profile freezes a 20 m nominal bin length, a 300 s minimum same-sequence gap for a new independent traversal, input budgets, and six-decimal distance rounding.
Changing any authority or parameter invalidates the profile identity.

## Bin geometry

Each directed arc is partitioned from offset zero using half-open 20 m longitudinal intervals.
A point exactly on an internal boundary enters the following bin, while a movement ending exactly on that boundary contributes no zero-length coverage to the following bin.
The final bin ends at the authenticated arc length and retains its true length when the arc is not an exact multiple of 20 m.
The stable `road_bin_id` binds road-graph identity, directed arc identity, and longitudinal bin index.

The kernel materializes every bin in the supplied graph, including bins with zero usable coverage.
This preserves unknown or failed spatial scope for later tri-state readiness evaluation instead of silently dropping unsupported roads.

## Coverage eligibility and traversal identity

Coverage is derived only from consecutive decoded points that are confident, moving, on the same directed arc, strictly time ordered, and advancing along that arc.
Ambiguous, stationary, off-map, cross-arc, and against-arc point pairs contribute zero usable distance.
Cross-arc motion is represented by the eligible same-arc evidence on either side rather than inventing unobserved offsets through a junction.

Movement is split at every crossed bin boundary.
Time and endpoint speed are interpolated by along-arc distance within each decoded point pair.
Yaw excitation uses the wrapped heading difference and is allocated by the same distance fraction.

Independent traversal identity includes sequence identity, source-group identity, directed arc, arc direction, and a deterministic traversal ordinal.
Same-sequence pieces on one directed arc remain one traversal until the frozen time gap is exceeded.
Adjacent or overlapping analysis windows therefore cannot inflate the independent traversal count, and exact duplicate movement pieces are deduplicated.
Different sequence identities are always independent.

## Modality evidence joins

Each modality evidence interval has a stable identity, sequence identity, canonical typed time interval, usability state, timestamp-support state, source artifact hash, and transformation lineage.
Only intervals in the same sequence and exact epoch and clock domain may join a road traversal.
Incomparable clocks fail closed instead of being joined by their numeric timestamp values.

Valid modality duration is the union of temporal overlap between usable evidence and the portion of a traversal inside one bin.
Timestamp-supported duration is tracked separately.
Lidar evidence may additionally contribute proportional point support and a duration-weighted overlap-support measurement.
Non-lidar evidence cannot carry lidar-only fields.

## Finding localization

An unlocalized normalized `Finding` is paired with its sequence identity before aggregation.
The native kernel joins the finding's half-open source interval to eligible bin traversal intervals in the same sequence.
Python validates each returned bin reference and emits a fully revalidated `Finding` whose `road_bin_ids` contain the canonical affected bins.
Finding identity remains stable because the canonical finding identifier already binds detector, rule, source interval, streams, and evidence rather than derived spatial annotations.

An interval that overlaps no eligible confident movement remains unlocalized with an empty bin set.
This is an explicit unsupported result and is not forced onto nearby road geometry.

## Public qualification

`benchmarks/m5_4_road_bins_gate.yaml` freezes all profile and fixture authorities, the synthetic population, exact coverage requirements, spatial F1 threshold, bootstrap unit, bootstrap seed, replicate count, and minimum support.
The confirmatory population contains 36 injected finding intervals across 12 independent synthetic families.
Every event crosses the same independently specified two-bin affected interval, while sequence and evidence identities remain distinct.
Forward and reverse directed controls verify exact 15 m, 20 m, and 5 m coverage pieces across boundaries.
An adjacent-window control verifies that one physical pass remains one independent traversal.

Run the gate with:

```console
uv run cartosentry qualify-road-bins \
  --output output/m5-4-road-bins.json
```

The verified M5.4 run materialized 218 directed bins, including 26 true final partial bins.
Exact synthetic coverage had zero mismatches, and the adjacent-window traversal-inflation count was zero.
Affected-bin localization produced 72 true-positive bins, zero false positives, and zero false negatives.
Spatial affected-bin F1 was 1.000000 with a one-sided cluster-bootstrap lower 95 percent bound of 1.000000 against the frozen 0.90 gate.
These results apply only to the frozen synthetic qualification population and are not public-route coverage or fault-localization claims.

## Current limitations

M5.4 does not yet evaluate readiness policy or produce recollection routes.
Those consumers are later milestones and must preserve the zero-coverage and empty-localization cases as unknown or failed support according to their own frozen profiles.
The interpolation model assumes motion between two same-arc decoded points is distributed by along-arc distance.
Sparse observations that cross an arc transition do not receive invented within-arc timing evidence.
The current traversal deduplication removes exact repeated movement pieces but does not attempt to infer that partially overlapping recordings from different sequence identities are the same physical pass.
Manual public-route adjudication remains a later checkpoint.
