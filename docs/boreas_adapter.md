# Boreas adapter contract

## Supported public workflow

The `boreas-public-v1` adapter validates one public Boreas sequence containing `applanix/gps_post_process.csv`, `applanix/lidar_poses.csv`, the three lidar extrinsics, lidar binary frames, and the official `route.html` visualization.
It reads each selected source once, emits deterministic normalized metadata, reports runtime measurements separately, and evaluates the frozen gate in `benchmarks/m0_4_adapter_gate.yaml`.

Download and verify the selected public inputs before inspection.

```console
uv run python scripts/download_public_data.py --tier public-smoke
uv run python scripts/verify_public_data.py --tier public-smoke
```

Run the public adapter workflow from the repository root.

```console
uv run cartosentry inspect-boreas \
  data/public/boreas-2021-09-02-11-42 \
  --road-region benchmarks/road_graphs/toronto_glen_shields_v1.polygon.json \
  --gate benchmarks/m0_4_adapter_gate.yaml \
  --output benchmark-results/boreas-contract.json
```

The command exits with status `0` only when every gate check passes, status `1` when a complete report fails a threshold, and status `2` when input or configuration is invalid.
The output is installed atomically when `--output` is used.
No absolute source path or raw malformed value is included in a successful report or native parser error.

## Time and point layout

Boreas filenames are interpreted as integer microseconds since the Unix UTC epoch and are normalized to signed 64-bit nanoseconds by checked integer multiplication.
`GPSTime` in `gps_post_process.csv` is parsed from the original plain-decimal lexeme directly into signed 64-bit nanoseconds with nearest-nanosecond, half-away-from-zero rounding.
No binary floating-point seconds value is used as the intermediate persisted timestamp.

A lidar filename is the scan midpoint.
Each lidar record is exactly 24 little-endian bytes containing six float32 values in the order `x`, `y`, `z`, `intensity`, `laser_id`, and `time_offset`.
`time_offset` is measured in seconds relative to the scan midpoint.
The adapter preserves the float32 bit patterns for the minimum and maximum observed offsets and materializes each absolute point timestamp using nearest-nanosecond, half-away-from-zero rounding.
All six fields must be finite, laser identifiers must be integers in `[0,127]`, and record timestamps must be nondecreasing within each selected frame.

## Frames and coordinates

Every reported matrix uses the explicit convention `T_target_source`.
The supported calibration identities are `T_applanix_lidar`, `T_camera_lidar`, and `T_radar_lidar`.
Each matrix must have a proper orthonormal rotation, determinant `+1`, and homogeneous final row `[0,0,0,1]`.

The postprocessed trajectory contains paired source time, dataset ENU coordinates, altitude, latitude, and longitude on each row.
Latitude and longitude are source radians and are converted to WGS84 degrees using `degrees = radians * 180 / pi`.
The dataset altitude's vertical datum is not documented, so the adapter labels it `unknown_dataset_altitude` and makes no orthometric-height claim.
The postprocessed trajectory pose is named `T_enu_ref_applanix`, and each selected lidar pose is named `T_enu_ref_lidar` under the report's `T_target_source` convention.
Global WGS84 and ECEF calculations remain float64.
Float32 is qualified only after conversion to a sequence-local Cartesian frame through GeographicLib.

The authoritative timestamped trajectory must remain inside the frozen road-graph acquisition region.
The source `route.html` polyline is used only as a secondary visualization cross-check.
The frozen cross-check limits are `1.0 m` at p95 and `1.5 m` maximum, which allow the official visualization's polyline decimation while remaining far tighter than the road-region scale.
The independent WGS84 to local to WGS84 round-trip limit remains `0.001 m`.

## Qualified public interval

The qualified interval is the exact manifest selection `boreas-public-smoke-clear-v1` plus the separate `boreas-public-smoke-clear-route-v1` artifact.
The route artifact remains separate so the accepted sensor aggregate identity is unchanged.
The normalized result is identical on macOS ARM64 and Linux x86-64 with SHA-256 `5cfa32a0156a928be3f44b133111e7d25fac8596329b294a2d8bb989a7e819de`.

| Contract observation | Measured result |
| --- | ---: |
| Selected lidar frames | 10 |
| Selected lidar points | 2,131,876 |
| Lidar source bytes | 51,165,024 |
| Overlapping trajectory rows | 207 |
| Selected lidar pose matches | 10 of 10 |
| Route polyline points | 879 |
| Route comparison samples | 1,075 |
| Route p95 residual | 0.726239 m |
| Route maximum residual | 1.143946 m |
| Maximum WGS84 and local round-trip error | 0.000000005 m |
| Maximum local coordinate magnitude | 2,129.686822 m |
| Maximum local float32 quantization | 0.000122061 m |
| Maximum global ECEF float32 quantization | 0.249999657 m |
| Maximum point-time rounding error | 0.5 ns |

The ECEF float32 observation is diagnostic evidence for retaining float64 global coordinates.
It is not an accepted global float32 representation.

## Runtime measurements

Runtime measurements are environment-specific and are excluded from the normalized metadata hash.
The Linux result below was measured in the repository's pinned Linux x86-64 container running under host emulation, so it is a compatibility and memory result rather than a native-host performance comparison.

| Environment | One-pass input bytes | Elapsed time | Throughput | Peak RSS |
| --- | ---: | ---: | ---: | ---: |
| macOS ARM64, native | 131,252,881 | 0.336221583 s | 390,376,132 B/s | 36,749,312 B |
| Linux x86-64, pinned container under emulation | 131,252,881 | 0.677962669 s | 193,598,980 B/s | 47,513,600 B |

Both measured peak RSS values pass the provisional `2.5 GiB` budget.
These single-run throughput values establish bounded one-pass behavior only and are not V1 performance claims.

## Limitations

This adapter covers the selected public Boreas trajectory, lidar pose, lidar point, and extrinsic formats only.
It does not qualify camera payloads, radar payloads, the undocumented altitude datum, arbitrary Leaflet applications, or files outside the pinned Boreas public layout.
The accepted road-region containment result is a source-format georeferencing gate, not confident road matching.
Directed road-graph import and map-match observability are qualified separately from this source-format contract.
