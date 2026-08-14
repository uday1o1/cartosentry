# Canonical time and geometry contracts

CartoSentry uses strict immutable Python models at persistence boundaries and checked C++20 value types for runtime arithmetic and geometry.
Native rigid composition, inversion, and point application use the locked Sophus 1.0.0 and Eigen 3.4.0 implementation.
The persisted models reject unknown fields and implicit type coercion.
Their JSON Schema is available through each model's `model_json_schema()` method.

## Time

`TimePoint.value_ns` is the only canonical numeric timestamp.
It is a signed 64-bit integer in the declared epoch and clock.
Every persisted time point also carries a reference and a `RawTime` value that preserves one exact source representation.
The raw source epoch and reference must match the canonical tags.
Decimal seconds remain decimal strings and are converted directly with nearest-nanosecond, half-away-from-zero rounding.
Binary floating-point seconds are never used as persisted timestamps.

The supported epochs are `UNIX_UTC`, `GPS`, `SENSOR_BOOT`, `HOST_MONOTONIC`, and `UNKNOWN`.
The supported references are the exposure, scan, sample, point, azimuth, and explicit unknown values frozen in the build plan.
An unknown epoch or reference must be written explicitly and cannot be omitted.

Time arithmetic checks signed 64-bit overflow.
Differences and intervals reject time points whose epochs or clocks differ.
A nonzero measurement interval is represented as the half-open interval `[capture_start, capture_end)` with a strictly later end.
An instantaneous measurement uses `sensor_time` and leaves its capture interval absent.

`CorrectedTime` retains its original `TimePoint` and records the target epoch and clock, correction identity and hash, pivot, offset, rate, uncertainty, and applicability interval.
There is no corrected-common epoch.
The correction model does not rewrite the original timestamp domain.

```python
from cartosentry.contracts import TimeEpoch, TimePoint, TimeReference

point = TimePoint.from_decimal_seconds(
    "1630597311.041161",
    source_key="applanix/gps_post_process.csv",
    field="GPSTime",
    epoch=TimeEpoch.UNIX_UTC,
    clock_id="boreas-applanix",
    reference=TimeReference.SAMPLE,
)
assert point.value_ns == 1_630_597_311_041_161_000
assert point.raw.decimal_lexeme == "1630597311.041161"
```

## Frames and rigid transforms

`NamedFrame` records a stable frame identifier, right-handedness, and the positive meaning of each axis.
The canonical rig factory records x forward, y left, and z up.

`RigidTransform` always means `T_target_source`.
It applies the column-vector equation `p_target = T_target_source * p_source`.
Composition applies only when the outer source frame equals the inner target frame and follows `T_c_a = T_c_b * T_b_a`.
Inversion swaps the named source and target frames.
Interpolation requires identical frame names and a fraction in the closed interval from zero to one.
Extrapolation is rejected.

Rotations persist quaternions in `w, x, y, z` order.
Validated construction normalizes recoverable input whose norm deviation is at most `1e-6`.
Equivalent positive and negative quaternions are reduced to one deterministic persisted sign.
Zero, nonfinite, and larger-deviation quaternions are rejected.
Matrix construction rejects reflections and matrices outside the frozen `1e-9` proper-rotation tolerance.

```python
from cartosentry.contracts import RigidTransform, UnitQuaternion

world_from_rig = RigidTransform(
    target_frame="world",
    source_frame="rig",
    translation_m=(10.0, 0.0, 0.0),
    rotation=UnitQuaternion(w=1.0, x=0.0, y=0.0, z=0.0),
)
point_world = world_from_rig.apply((1.0, 2.0, 3.0))
assert point_world == (11.0, 2.0, 3.0)
```

## Global and local coordinates

`GlobalCoordinate` stores WGS84 latitude and longitude in angular degrees using float64.
Altitude is optional and its vertical datum is explicit.
The `UNKNOWN_VERTICAL_DATUM` value preserves source altitude without claiming it is ellipsoidal height.

`LocalOrigin` requires a WGS84 ellipsoidal altitude because a three-dimensional local tangent plane cannot be reconstructed safely without one.
`LocalCoordinate` stores float64 metric x, y, and z in its named local frame.
Dense float32 kernels may consume only coordinates that have already had such a local origin subtracted.

The analytic contract suite exercises Toronto-area points and large global translations.
It enforces at most `0.001 m` horizontal WGS84 round-trip error and at most `1e-9 m` rigid-transform point round-trip error in both debug and release builds.
