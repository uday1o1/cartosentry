# LiDAR integrity analysis

CartoSentry M4.1 analyzes point records and scan coverage in one pass without retaining point payloads.
The detector validates finite coordinates, range, intensity, declared ring identity, relative point time, point ordering, frame cadence, capture duration, and observed point-time span.
It reports fixed-bin range and intensity quantiles, total and finite point counts, per-ring counts, per-azimuth counts, the maximum circular blank-sector span, cadence bounds, capture-duration bounds, and observed point-time-span bounds.

## Frozen sensor profiles

The authenticated profile in `profiles/lidar_integrity_v1.yaml` contains separate contracts for the analytic four-ring spinning LiDAR and the 128-ring Boreas LiDAR.
Range and intensity limits are sensor-model properties rather than generic limits.
Ring identifiers in the selected model are the supported ring set, so a model element outside that set is invalid while absence is evaluated only for supported elements.
The current Boreas source does not declare an ego or expected-occlusion azimuth mask, so the profile does not silently invent one.
Sources that provide such a mask require a new frozen profile revision before it can affect sector evidence.

The synthetic fixture schema does not contain a measured intensity channel.
The adapter therefore supplies the documented normalized control value `1.0` for synthetic qualification only.
No public-data intensity value is replaced or synthesized.

Coverage warnings require two consecutive affected frames.
This persistence rule prevents a single sparse scene view from becoming a ring, sector, or density finding.
Density reduction compares each frame with the fixed absolute minimum and with the prior running maximum using the frozen ratio, while retaining constant memory.
This signal is compatible with packet loss or partial scans, but it is also compatible with scene sparsity and weather effects.

## Deterministic fault qualification

The M4.1 engineering supplement in `benchmarks/m4_1_lidar_gate.yaml` freezes seven operator families and 23 severity cases before qualification.
It covers complete-scan loss, supported-ring loss, azimuth-sector loss, spatially uniform or sector-biased density reduction, nonfinite values, range scaling, and point-time shift, reversal, or clamp faults.
Every derivative records the clean source hash, deterministic derived hash, changed-record count, affected half-open frame interval, typed parameters, severity, and expected rule.

This supplement does not replace or mutate the already accepted M3 representative matrix.
M11.1 merges the proven M4.1 cases into the sole complete release matrix before release evaluation and unblinding.

The synthetic gate runs every case on all eight development source groups and all twelve threshold-calibration source groups.
Below-threshold controls must remain below their operator's primary rule, detectable cases must emit the declared rule, and detected boundaries must be within one frame of an injected interval boundary.
Clean critical or blocking findings must remain zero.

## Public bounded-memory smoke

The public smoke path verifies the first ten clear-weather Boreas LiDAR objects against `benchmarks/data_manifest.yaml` before decoding them.
It then streams the production `BoreasAdapter` point iterator through the detector while Python allocation tracing is active.
The gate requires both observed traced peak memory and the detector's declared retained-state upper bound to remain at or below 64 MiB.
The report is development-only evidence and cannot support confirmatory, final-test, or release claims.

Run the complete workflow after materializing the pinned public-smoke data:

```console
uv run cartosentry qualify-lidar-integrity \
  --public-data-root data/public \
  --output output/m4-1-lidar-integrity.json
```

The command exits with status `0` only when both synthetic partitions and the public bounded-memory gate pass.
Malformed authority files, moved partitions, missing public objects, hash mismatches, decoder errors, and memory-limit failures are errors rather than passing results.

## Interpretation limits

Ring, sector, and density findings describe observed coverage loss under the selected sensor profile.
They do not uniquely identify a hardware failure.
Snow, rain, dust, glass, sparse open space, moving objects, and scene occlusion can change return density and azimuth support.
The public clear-weather smoke measures execution and memory behavior, not recall, weather robustness, or a false-positive rate.
Motion compensation and multi-frame alignment are reported separately by the authenticated M4.2 workflow documented in `docs/lidar_motion_alignment.md` and must not be inferred from this structural report.
