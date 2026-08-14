# Algorithm observability qualification

## Scope

Milestone 0.5 qualifies the numerical assumptions needed for lidar motion compensation, directed road matching, and required-arc routing.
The checked workflow combines synthetic controls with the pinned Boreas development clip and pinned OpenStreetMap extract.
It does not establish detector recall, final-test accuracy, or production throughput.

Run the public workflow after materializing the pinned development inputs.

```console
cartosentry qualify-observability \
  data/public/boreas-2021-09-02-11-42 \
  --road-graph data/public/road_graphs/toronto-glen-shields-v1.osm \
  --gate benchmarks/m0_5_observability_gate.yaml \
  --output benchmark-results/m0_5_observability.json
```

The command returns exit code `0` only when every frozen gate passes, exit code `1` for a completed failed qualification, and exit code `2` for invalid or unavailable inputs.
The JSON output is written atomically and excludes local source paths.
Semantic float values are canonicalized to `10^-9` before hashing so sub-nanometer platform reduction differences do not change artifact identity.

## Motion and alignment model

Trajectory rows define `T_enu_ref_applanix` in float64.
The lidar extrinsic defines `T_applanix_lidar`.
Each sampled lidar point is transformed through `T_enu_ref_lidar = T_enu_ref_applanix * T_applanix_lidar` at the point's float32 relative time from the scan midpoint.
Translations use linear interpolation and rotations use quaternion spherical interpolation.
Point times outside trajectory support and interpolation gaps greater than `100 ms` are rejected.

The public multi-frame diagnostic reports symmetric nearest-neighbor residuals between adjacent motion-compensated frames after deterministic range, height, and near-ego filtering.
Injected-defect separation is a sensitivity measurement, not a root-cause detector result.
It measures the mean coordinate effect on the same sampled points and requires at least `0.05 m` only when both motion and structure are observable.
This avoids interpreting a small nearest-neighbor change on repeated road surfaces as proof that a known perturbation had no geometric effect.

The synthetic suite uses one shared trajectory, point-time, and static-landmark model.
Turning and accelerating motion are expected to expose both injected defects.
Static and sparse-structure controls must remain `NOT_OBSERVABLE` even when an injection changes coordinates.
Constant-speed straight motion is retained as a `WEAK` control because some uniform time shifts are gauge-like under that motion.

## Directed graph and candidates

The importer uses the locked libosmium `2.23.1` source and streams OSM XML nodes and ways.
The `m0.5-directed-candidate-v1` profile includes motor-vehicle road classes from motorway through service roads.
It preserves the source way identifier, source segment index, and generated direction on every arc.
It honors explicit forward, reverse, and bidirectional `oneway` values and roundabout direction.
It excludes private or denied motor-vehicle access, areas, unsupported road classes, and ways with conditional restrictions that this spike cannot resolve safely.

Candidate emission uses lateral distance and directed heading error.
Heading is evaluated only for observations above the frozen `1 m/s` moving threshold.
The frozen candidate radius is `30 m`, the confident lateral bound is `8 m`, and the confident heading bound is `0.7853981633974483 rad`.
The spike records candidate confidence only and does not claim temporal HMM route decoding, which belongs to Milestone 5.

Public distance coverage uses `endpoint-half-distance-v1`.
Each sampled observation owns half of each adjacent moving segment, so a segment with one confident endpoint contributes half its length.
This is a fixed distance-weighted observation coverage measure and does not require both endpoints to be confident before either endpoint's support is counted.

## Measured development evidence

The macOS ARM64 and Linux x86-64 qualifications produced identical normalized hash `e8361b332d88b40f0a7902d35b93a5e0663b5fdf4350631dfb86014c815816be`.

| Evidence | Observed result | Frozen requirement |
| --- | ---: | ---: |
| Lidar frames | 10 | At least 10 |
| Sampled structured points | 15,585 | At least 5,000 |
| Motion speed range | 5.504 to 6.090 m/s | Minimum at least 1 m/s |
| Heading change | 0.122224 rad | Observable turn |
| Clean adjacent-frame residual | 0.834844 m | Diagnostic only |
| `100 ms` point-time injection effect | 0.594712 m | At least 0.05 m when observable |
| `1 m` trajectory injection effect | 0.301636 m | At least 0.05 m when observable |
| Imported directed arcs | 5,458 | At least 100 |
| Confident moving-distance coverage | 90.614 percent | At least 85 percent |
| Confident lateral residual p95 | 6.168762 m | Descriptive development evidence |
| Tiny route exact and brute-force costs | 8.0 and 8.0 | Exact equality |
| Independent tiny-route validation | Passed | Required |

The turning and accelerating synthetic scenarios separated both perturbations.
The static and sparse-structure controls remained `NOT_OBSERVABLE` and could not pass defect separation.
All 20 frozen checks passed on the public development workflow.

## Limitations

The public measurements cover one development source group and cannot support a generalization claim.
The adjacent-frame residual includes real scene change and nearest-neighbor ambiguity and is not an absolute registration-accuracy estimate.
The known-injection coordinate effect proves numerical sensitivity under the recorded support but does not identify a naturally occurring defect or distinguish time, pose, and extrinsic causes.
The map stage measures confident directed candidates rather than final sequence decoding or directed-arc accuracy.
The tiny route result retires exact-solver and validator feasibility only and does not qualify the later production heuristic.
