# Trajectory integrity

CartoSentry evaluates the postprocessed reference trajectory that the active adapter supplies.
It does not claim that a detector finding proves a particular sensor or processing root cause.

## Frozen profile

`profiles/trajectory_integrity_v1.yaml` is the self-hashed M3.2 detector authority.
It pins the M3.1 continuous-trajectory gate, structural and physical thresholds, rule priority, event-consolidation behavior, resource ceilings, exact source-group sets, and qualification workloads.
The loader rejects duplicate JSON keys, unsupported numeric constants, excessive nesting, unknown fields, an altered self-hash, an unknown M3.1 hash, missing rules, reordered rule priority, and files larger than 64 KiB.

Timestamp rules require zero duplicates and regressions and allow at most a 100 ms consecutive gap.
Coordinate continuity requires one time domain, one named target and source frame pair, and consistent availability of geographic support.
The physical rules retain their explicit units in every event measurement.
Floating-point threshold comparisons use the profile's frozen relative and absolute tolerances of `1e-12`, so an analytic value at the boundary is not rejected because of derivative roundoff.

## Structural pass before interpolation

The detector scans raw source order before constructing a continuous trajectory.
Duplicate timestamps, regressions, oversized gaps, and coordinate-continuity failures therefore remain reportable even when interpolation is impossible.
Duplicates, regressions, and coordinate mismatches make content rules `NOT_APPLICABLE` rather than allowing a constructor failure to hide the structural evidence.
An oversized positive gap remains a structural event and divides continuous-trajectory support, so no derivative window crosses it.

## Position and motion evidence

Position-jump residual compares each observed displacement with the trapezoidal displacement predicted by the paired source-velocity field.
Opposite entry and exit steps with compatible magnitude are paired into the complete bounded translated interval.
Only adjacent step candidates separated by at most the frozen three-second pairing ceiling can form one bounded step, which keeps pairing linear in the raw-failure count and prevents distant discontinuities from being joined.
This pairing preserves the injected interval rather than reporting two isolated edges.

Frozen position requires observed speed at or below 0.1 m/s while the paired source-speed field is at least 1.0 m/s.
The condition must persist for at least 500 ms after half-open interval consolidation.
A truly stationary sequence has zero source speed and cannot satisfy the freeze rule.

Velocity residual compares displacement-derived velocity with the paired source-velocity field as self-consistency evidence.
Speed, acceleration, jerk, and yaw rate use the gap-aware robust local polynomial and heading treatment from the M3.1 continuous trajectory.
All rule support records state whether the necessary evidence was observable, absent, weak, or not applicable.

## Bias-compatible residuals

A whole-support constant translation preserves every internal position increment.
Without an independent position reference, the detector marks the reference-residual rule `NOT_OBSERVABLE` and does not report that bias as detected.
When hash-bound independent position evidence is present, the residual rule can establish disagreement.
Its compatible causes include position bias, local-world-origin disagreement, and coordinate-frame interpretation error, but each cause remains explicitly unconfirmed.

The detector API accepts one typed reference-evidence object that binds the reference-source hash, provenance kind, independence basis, unit, exact canonical time identity, named frame pair, positions, and its own canonical hash.
The detector rejects self-derived evidence, a mismatched sample count, a time mismatch, a frame mismatch, a nonfinite position, or an invalid evidence hash.
Fault manifests, expected intervals, operator identifiers, and injected observability labels are not detector inputs.

## Events and localization

Raw rule failures are consolidated deterministically with the frozen 500 ms adjacency and two-clear-window hysteresis contract.
Each event has a content-derived identifier, one half-open source interval, sample bounds, a priority-selected primary rule, all triggered rules, exact measured values, threshold keys, threshold operators, severity, observability, and unconfirmed compatible causes.
Event boundaries use the strongest direct localization rule rather than the wider support halo of a robust derivative.
Paired position steps, freeze runs, reference residual runs, and sustained velocity residuals therefore retain their natural fault interval while related derivative evidence remains attached.

## Qualification

Run the complete user-facing workflow from the repository root.

```console
uv run cartosentry qualify-trajectory-integrity \
  --output output/trajectory-integrity-qualification.json
```

The command derives exactly eight development and 12 threshold-calibration source groups from `benchmarks/split_manifest.yaml`.
It exercises every timestamp, jump, freeze, bias, and drift case at below-threshold, near-threshold, and detectable severity with three deterministic target seeds per source group.
Every operator and severity stratum therefore has 24 distinct injected events on development and 36 on calibration.
The fault matrix freezes whether each case expects a finding or no finding at its boundary semantics.
Assigned scenarios provide clean controls, including the stationary false-freeze control, while a frozen constant-speed straight workload provides observable fault injection for every source group.
The same public qualification path also gates maximum speed, acceleration, jerk, yaw rate, coordinate continuity, timestamp regression, and event hysteresis with explicit boundary and detectable controls.

The clean false-critical workload contains 900 seconds per source group at a frozen 100 ms sample period.
This provides two clean sensor-hours on development and three clean sensor-hours on calibration without splitting any source-group bootstrap cluster.
The profile pins the exact M3.1 gate, split, fault matrix, numerical charter, and ordered source-group identities before the qualifier reads any case.
The report records those authenticated file hashes plus the immutable profile and charter identities.
It uses the charter's 10,000-replicate clustered bootstrap and frozen seed.
When every clean cluster has zero false-critical events, the clustered bootstrap has a zero upper bound and cannot express the plan's rule that zero observed failures do not imply zero risk.
The predeclared qualification fallback therefore reports the exact one-sided 95 percent Poisson exposure upper bound for that zero-event rate while retaining the raw clustered-bootstrap value separately.

Development is labeled `DESCRIPTIVE_ONLY`, and threshold calibration is labeled `CALIBRATION_ONLY`.
The frozen synthetic corpus is an exhaustive engineering and calibration checkpoint, so its acceptance uses the exact frozen case outcomes and engineering point gates.
Bootstrap intervals, interval-degeneracy flags, and confirmatory support are reported, but every confirmatory pass field is null.
Holm inference is explicitly not applied because this checkpoint forbids a confirmatory claim.
This result is not a final-test, public-data, or portfolio release claim.

## Threshold change procedure

The M3.5 checkpoint must use the exact immutable M3.2 profile and may not change a threshold, comparison tolerance, event-consolidation rule, support rule, or workload after development or public-review results are visible.
A proposed pre-unblinding threshold change must be justified only with the frozen `threshold_calibration` partition.
The change must create a new profile version and immutable hash, update every authority that pins the profile, record the rationale and expected risk in the aggregate charter revision history, and rerun all M3 gates before any development benchmark is reviewed again.
Development clips, public-review clips, policy-tuning inputs, and final-test inputs may not be used to select or revise trajectory thresholds.
Any threshold change after final-test unblinding invalidates the candidate and requires a new untouched final partition under a new charter.

## M3.5 temporal checkpoint

Download and verify the ordinary public-full inputs, then run the public checkpoint.

```console
uv run python scripts/download_public_data.py \
  --tier public-full \
  --output-root data/public

uv run cartosentry qualify-temporal-checkpoint \
  --public-data-root data/public \
  --output output/temporal-checkpoint.json
```

`benchmarks/m3_5_temporal_checkpoint.yaml` freezes all authority hashes and selects the start, middle, and end 4,096-sample clips from both the clear and snow development sequences.
The command reruns every M3.1 gate and the complete M3.2 development and calibration-guard qualification before reading public clips.
Each public clip passes through the production Boreas adapter and the frozen detector.
The machine report retains every finding and creates an unresolved review record for any `CRITICAL` or `BLOCKING_ANALYSIS` event.
Warnings remain visible but are not reclassified as false critical findings.
This development checkpoint is descriptive and does not make a confirmatory, final-test, or release claim.
