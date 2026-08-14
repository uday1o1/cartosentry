# Decision 0002: Stage NCore as a post-V1 adapter

- Status: accepted
- Date: 2026-08-13
- Scope: M0.3b NCore follow-on feasibility

## Context

CartoSentry V1 uses a native Boreas adapter and must not acquire NCore as a runtime or build dependency.
The post-V1 plan needs evidence that the current public NCore V4 package can expose the canonical sensor information on both mandatory host platforms.
This spike therefore installed the current public wheel only in disposable environments and used a generated sample containing no third-party bytes.

The spike used the primary [NVIDIA NCore documentation](https://nvidia.github.io/ncore/), [NVIDIA source repository](https://github.com/NVIDIA/ncore), and [PyPI project](https://pypi.org/project/nvidia-ncore/19.5.1/).

## Tested release and license

The tested release is `nvidia-ncore` 19.5.1, published as the pure Python wheel `nvidia_ncore-19.5.1-py3-none-any.whl`.
The wheel is 115,543 bytes and has SHA-256 `a753f81470ba1b35567cbca26794a7f9ceefe04ec306b962a52dd18dc988fe29`.
The matching public source is tag `v19.5.1` at commit `cbd8c16f3487186e8848e707eece0045ece3a058`.
The wheel metadata and tagged source both identify the license as Apache-2.0.
PyPI publishes no source distribution for this release.

The wheel declares Python 3.8 or newer and the direct dependencies `numpy`, `dataclasses_json>=0.2.12`, `pillow`, `zarr>=2.12.0,<3.0.0`, `cbor2`, `scipy`, `torch`, `typing_extensions`, and `universal_pathlib`.
Only the NCore wheel is content-pinned for this feasibility result.
Its dependencies were intentionally resolved fresh to observe the current default install rather than create a V1 lock.

## Platform evidence

The repository-owned `tools/ncore_v4_probe.py` generates a deterministic directory-store V4 sequence with poses, static sensor extrinsics, camera intrinsics and image bytes, lidar intrinsics and per-ray values, and radar per-detection values.
It finalizes the component groups, reloads them through `SequenceLoaderV4`, and prints the observed public compatibility surface as JSON.
The generated sample contains no external data and its temporary store is deleted when the probe exits.

On macOS 26.5.2 ARM64 with Python 3.12.13, uv 0.11.23 installed 28 resolved packages in the disposable environment and the probe passed.
On the pinned Linux x86-64 Python 3.12.13 container, pip installed 47 resolved packages and the same probe passed.
The Linux default install and probe took 593.1 seconds because the mandatory PyTorch 2.13 dependency expanded to CUDA 13, cuDNN, NCCL, NVSHMEM, Triton, and related packages despite the CPU-only workload.
Both platforms resolved NCore 19.5.1, NumPy 2.5.2, Pillow 12.3.0, SciPy 1.18.0, Torch 2.13.0, and Zarr 2.18.7.

## Capability result

### Python and dependencies

Python 3.12.13 imports and executes the package on both required platforms.
The follow-on lock must apply an upper compatibility bound because the package declares only Python 3.8 or newer.
The default dependency graph resolves on both platforms.
The follow-on lock must pin the complete graph and investigate a supported CPU-only Torch path because format-only use currently pulls Torch.

### Platform

The same generated V4 observations load on macOS ARM64 and Linux x86-64.
Follow-on qualification must cover transitive platform wheels, not only the `py3-none-any` NCore wheel tag.

### Timestamps

Frame intervals and lidar and radar per-ray timestamps round-trip as unsigned microseconds.
The adapter must convert them with overflow checks to signed nanoseconds and attach epoch, clock identifier, reference, and raw-field provenance.

### Calibration and pose

Named-frame extrinsics and typed camera and structured-lidar intrinsics round-trip.
The adapter must supply calibration identity, validity, uncertainty, and typed radar-model capability records.
Named 4x4 dynamic poses and their microsecond samples interpolate through the pose graph.
The adapter must preserve source lineage, uncertainty and quality fields, and explicit global-coordinate semantics.

### Camera

Encoded images, start and end time, extrinsics, and pinhole intrinsics round-trip.
The adapter must add source-object lineage and explicit clock semantics at normalization.

### Lidar

Direction, per-ray time, model element, multiple-return shape, distance, intensity, frame interval, extrinsics, and a structured model are available.
The adapter must add source-object lineage and explicit clock semantics at normalization.

### Radar

Direction, per-detection time, distance, frame interval, and extrinsics round-trip.
The adapter must define stable typed velocity, RCS, status, and radar-model mappings instead of treating generic arrays as canonical fields.

### Writer API

The public V4 writer and reader classes create and reload the sample.
The adapter must isolate the current need to import `HalfClosedInterval` from `ncore.impl` because that required writer type is not exported by `ncore.data`.

## Decision

The post-V1 NCore track is `feasible` on macOS ARM64 and Linux x86-64, with adapter work required for canonical semantics and reproducible dependency control.
NCore remains absent from CartoSentry V1 dependencies, the uv lock, the native dependency lock, normal tests, and shipped artifacts.
M9.1 must refresh this evidence, select and lock a supported release and complete dependency graph, and preserve every gap above as an explicit adapter capability until implemented.

## Verification

The isolated environment must install the wheel by its exact URL and SHA-256, then run:

```bash
python tools/ncore_v4_probe.py
```

The checked machine-readable record is `benchmarks/ncore_feasibility.yaml`.
The ordinary V1 regression check is:

```bash
uv run pytest tests/m0/test_ncore_feasibility.py
```

## Revisit conditions

Revisit this decision if the public wheel, dependency graph, supported Python range, typed V4 contracts, license, or mandatory platform matrix changes.
Revisit it before any NCore object crosses the stable CartoSentry ABI or any NCore dependency enters a release lock.
