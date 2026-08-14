# Deterministic synthetic fixtures

CartoSentry's V1 synthetic generator provides compact, analytic trajectory and spinning-lidar inputs for development and regression testing.
It does not model a complete vehicle sensor suite.

## Scope

The generator emits a directed road graph, an analytic rig trajectory, a checked lidar extrinsic, a spinning-lidar stream with exact per-point relative times, and known static cylinder landmarks.
The eight frozen development families cover straight motion, a constant-radius turn, stop and start motion, parallel roads, a ramp, an overpass, an off-map connection, and a stationary low-excitation control.
The scenario argument is the geometry and motion-excitation control, while the seed controls only deterministic landmark placement.
Camera, IMU, radar, cross-modal, and topology-specific synthetic measurements are outside this V1 generator.

All transforms are named `T_target_source` and use right-handed rig axes with x forward, y left, and z up.
Trajectory and lidar timestamps use signed integer nanoseconds tagged with the `SENSOR_BOOT` epoch and a fixed synthetic clock identifier.
Each lidar point records an exact integer offset from its scan midpoint and has a trajectory pose at the same firing time.

## Generate and qualify

Generate the frozen fixture set into a new directory:

```console
uv run cartosentry generate-synthetic-fixtures output/synthetic
```

Qualify its deterministic bytes, scenario coverage, time alignment, and geometry against the frozen numerical charter:

```console
uv run cartosentry qualify-synthetic-fixtures output/synthetic
```

Repository maintainers regenerate or check the committed test inputs with:

```console
uv run python scripts/generate_synthetic_fixtures.py
uv run python scripts/generate_synthetic_fixtures.py --check
```

Each fixture records generator version `1.0.0`, its frozen synthetic family identifier, its seed, analytic truth, and a content-derived fixture identifier.
The fixture-set manifest binds every file by SHA-256 and also binds the frozen split manifest by SHA-256.

## Determinism and limits

The generator uses a specified SplitMix64 implementation instead of a runtime-specific random-number service.
Persisted analytic floating-point values are rounded to 12 decimal places, which suppresses platform-specific last-bit differences while remaining well inside the frozen `1e-9 m` transform gate.
Fixture JSON uses sorted keys and fixed separators, so a fixed generator version, scenario, family, and seed produce identical bytes.

These fixtures establish implementation correctness and regression coverage, not real-world sensor validity.
They contain ideal static geometry, no environmental dynamics, no material response, no occlusion model beyond first analytic ray intersection, and no measurement noise.
