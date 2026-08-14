# Reference trajectory

CartoSentry V1 uses the Boreas postprocessed trajectory as its reference path.
This input is an Applanix solution derived with inertial data, so CartoSentry calls it a postprocessed reference trajectory and does not claim to reconstruct a trajectory from raw GNSS.

## Local-world anchor

The loader accepts sequential pose samples from the production Boreas adapter and analytic synthetic fixtures through one immutable sample contract.
All samples must use one time epoch, clock, time reference, target frame, and rig frame, and timestamps must be strictly increasing.
The first source pose defines an explicit trajectory-local origin while retaining the original source-world frame, translation, and optional WGS84 observation as anchor provenance.
This subtraction keeps trajectory calculations in a continuous, numerically stable local-world representation without discarding the source mapping.

## Support and interpolation

The M3.1 support ceiling is 100 ms.
Consecutive samples separated by more than that ceiling belong to separate support intervals.
Queries inside such a gap return `UNSUPPORTED_GAP`, and queries outside the first and last source timestamps return `OUTSIDE_SOURCE_SUPPORT`.
There is no extrapolation switch.
Translation is linearly interpolated in the continuous trajectory-local world, and orientation uses the checked quaternion interpolation in the native geometry core.

## Derivatives and stationary intervals

Velocity, acceleration, and jerk come from a nine-sample cubic local polynomial centered as closely as source support permits.
The fit uses float64 least squares with an explicit relative condition cutoff and rejects a rank-deficient design matrix.
Huber iteratively reweighted least squares limits the influence of isolated position outliers.
The window does not cross an unsupported source gap.

Heading comes from the pose quaternion rather than the positional tangent.
The heading sequence is unwrapped before the same robust local fit estimates yaw rate.
Curvature is yaw rate divided by speed only when the interval is moving and speed support exceeds the stationary threshold.

Source velocity controls stationary classification when it is available from the postprocessed trajectory.
Otherwise, the analytic and fallback path uses the robust local polynomial velocity estimate followed by a nine-sample temporal median for stationary classification.
A candidate run must remain below 0.05 m/s for at least 500 ms before it is stationary.
Heading is held through a stationary run within one supported segment, and an entirely stationary segment uses the median observed unwrapped heading.
This prevents positional or orientation noise from manufacturing heading changes while the rig is stopped.

## M3.1 qualification contract

The frozen pre-unblinding numerical charter did not contain the derivative and interpolation keys referenced by the M3.1 milestone gate.
Changing that charter after its freeze would weaken its role as the confirmatory evaluation authority.
The smallest correction is the separate immutable deterministic contract in `benchmarks/m3_1_trajectory_gate.yaml`.
The loader pins that contract's SHA-256 identity and rejects duplicate keys, unsafe files, excessive nesting, nonstandard numeric constants, and files larger than 64 KiB.
This supplement defines only M3.1 implementation acceptance and does not change confirmatory population, split, or final-test claims.

The public qualification covers straight motion, a constant-radius turn, stop and start, a stationary path, and a long timestamp gap.
It measures interpolation position and orientation error, velocity, acceleration, jerk, heading, yaw rate, curvature, stationary and moving classification, a seeded isolated-position outlier control, and unsupported query behavior.
Every supported source pair contributes interpolation evidence, including endpoint-adjacent pairs and transition neighborhoods.
The 250 ms analytic transition exclusion applies only to derivative measurements around the deliberately non-differentiable acceleration boundaries in the generated turn and stop-start paths.

Run the deterministic workflow from the repository root.

```console
uv run cartosentry qualify-reference-trajectory
```

The JSON report includes the authenticated gate hash, every observed value, each comparison operator and threshold, per-scenario evidence, and the support classification for every gap and extrapolation probe.
