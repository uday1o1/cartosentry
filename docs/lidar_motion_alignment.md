# Motion-compensated LiDAR alignment

CartoSentry M4.2 checks whether adjacent spinning-LiDAR frames remain geometrically consistent after every retained point is transformed at its own measurement time.
The implementation targets the trajectory-local world frame and never substitutes one reference-time pose for all points in a scan.

## Processing contract

The analyzer evaluates the authenticated continuous trajectory at the frame reference time and at every retained point time.
It composes the named `T_world_rig` and `T_rig_lidar` transforms, then places the point in `trajectory_local_world`.
Any unsupported frame-reference or point-time query makes the entire crossing frame `UNKNOWN_TRAJECTORY` and removes its voxel payload from alignment scoring.
The report retains representative source offsets for unsupported point queries.

Declared dynamic points are excluded before trajectory evaluation.
The frozen geometric near-ego mask excludes points within a 2.5 m horizontal radius and 3.0 m absolute LiDAR-frame height.
The mask order and dimensions are deterministic and authenticated by `profiles/lidar_alignment_v1.yaml`.

Each processed frame is aggregated into 1 m local-world voxels with point count and first and second coordinate moments.
Adjacent frames report occupied-voxel counts, shared-voxel count, occupancy Jaccard overlap, and the mean combined root-sum coordinate spread of shared voxels as surface thickness.
Only the previous voxel frame remains resident while frame and pair evidence accumulate under explicit frozen bounds.

## Observability and interpretation

An adjacent pair is observable only when both frames have sufficient retained points, occupied voxels, shared voxels, and either translation or rotation excitation.
Sparse structure and stationary ego motion return `UNKNOWN_OBSERVABILITY` rather than an alignment pass.
Unsupported trajectory support returns `UNKNOWN_TRAJECTORY` rather than an alignment pass.

A supported pair passes when occupancy Jaccard overlap is at least 0.75 and mean shared surface thickness is at most 0.8 m.
An alignment failure remains compatible with trajectory error, point-time error, extrinsic calibration error, dynamic scene content, and insufficient static overlap.
The report does not assign a unique cause without independent discriminating evidence.

## Frozen analytic qualification

The authenticated gate in `benchmarks/m4_2_alignment_gate.yaml` binds the alignment profile, M3.1 trajectory gate, M4.1 LiDAR gate, split manifest, numerical charter, and representative V1 fault matrix by SHA-256.
The analytic fixture uses six frames, 68 fixed static-world targets per frame, 17 exact point times per frame, and a turning trajectory with changing angular rate.
Changing angular rate is required because a constant time shift under constant-twist motion can be absorbed as one rigid transform and does not provide the intended timing observability.

The development and threshold-calibration partitions run frozen below-threshold, near-threshold, and detectable point-time, trajectory, and extrinsic perturbations.
The 0.75 overlap cutoff was selected before acceptance from the threshold-calibration control floor of 0.7662 while the detectable perturbations retained lower-overlap failing pairs.
The clean input hash binds every generated point, expected-world coordinate, reference pose, velocity, timestamp, extrinsic, and generator version.
Each derivative hash binds the actual mutated frames, trajectory samples, extrinsic, and case provenance, while typed truth records changed-field counts and measured deltas.
The qualification requires complete non-null analytic truth coverage and never substitutes zero error for absent truth.
The unsupported-gap control freezes frame 2 as unknown, pairs `(1, 2)` and `(2, 3)` as unknown, and all other adjacent pairs as supported passes.
The qualification also verifies per-point trajectory evaluation, sparse and stationary observability, dynamic and near-ego masks, and the one-frame voxel-retention bound.

Run the complete public workflow with:

```console
uv run cartosentry qualify-lidar-alignment \
  --output output/m4-2-lidar-alignment.json
```

The command exits with status `0` only when every frozen development and threshold-calibration gate passes.
Authority substitution, moved source groups, malformed inputs, unsupported gaps reported as passes, or fault-control mismatches are errors or failed qualification rather than passing evidence.

## Limits

The current M4.2 evidence is deterministic synthetic analytic qualification only.
It does not establish public-dataset accuracy, target-hardware performance, weather robustness, dynamic-object classification quality, or fleet false-positive rates.
The optional nearest-neighbor residual described by the project plan is not enabled because the frozen occupancy and surface-thickness metrics already satisfy the selected V1 gate.
Camera-to-LiDAR consistency, radar integrity, and release claims remain separate later milestones.
