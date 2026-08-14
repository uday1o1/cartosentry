# Public source inventory

This inventory records the public evidence used to make CartoSentry implementation decisions.
Every URL below was resolved from the public internet on 2026-08-13.
The inventory establishes source provenance only and does not replace the dataset license records required by M0.2.

## Mapping, sensors, recording, and timestamp behavior

- [DRIVE Labs: How Localization Helps Vehicles Find Their Way](https://developer.nvidia.com/blog/drive-labs-how-localization-helps-vehicles-find-their-way/) supports the HD-map localization and complementary-sensor motivation.
- [Getting to Know Autonomous Vehicles](https://developer.nvidia.com/blog/getting-to-know-autonomous-vehicles/) provides public context for camera, lidar, radar, and GNSS map localization layers.
- [NVIDIA Autonomous Vehicles Safety Report](https://images.nvidia.com/aem-dam/en-zz/Solutions/auto-self-driving-safety-report.pdf) supports the public descriptions of collection, verification, validation, and sensor modalities.
- [DriveWorks Recording Sensor Data](https://developer.nvidia.com/docs/drive/drive-os/6.0.9/public/driveworks-nvsdk/dwx_recording_devguide_group.html) supports synchronized multimodal recording and storage decisions.
- [DriveWorks Basic Recording](https://developer.nvidia.com/docs/drive/drive-os/6.0.9/public/driveworks-nvsdk/dwx_recording_devguide_basic_recording.html) supports recording timestamp and basic sensor-sanity decisions.
- [DriveWorks Sensor Timestamping](https://developer.nvidia.com/docs/drive/drive-os/6.0.5/public/driveworks-nvsdk/sensors_usecase4.html) defines the public raw, host, synchronized, and smoothed timestamp behavior used by the time contract.
- [DriveWorks Time Sensor Sample](https://developer.nvidia.com/docs/drive/drive-os/6.0.10/public/driveworks-nvsdk/nvsdk_dw_html/dwx_time_sensor_sample.html) supports raw and synchronized lidar timestamp comparison.
- [DriveWorks Post-record Checker](https://developer.nvidia.com/docs/drive/drive-os/6.0.7/public/driveworks-nvsdk/dwx_postrecord_checker.html) supports the recording-file and timestamp-delta integrity baseline.
- [How DriveWorks Makes it Easy to Record and Replay Data for AV Development](https://developer.nvidia.com/blog/how-driveworks-makes-it-easy-to-record-and-replay-data-for-av-development/) supports the effects of temporal and spatial offsets and the synchronized replay motivation.

The earlier DriveWorks 4.0 documentation paths redirected to a generic documentation landing page and no longer supported the cited claims directly.
The inventory and build plan therefore use the verified versioned public pages above.
The earlier safety-report artifact path returned HTTP 404 and was replaced by the verified current public report.

## NCore format and adapter decisions

- [NVIDIA NCore repository](https://github.com/NVIDIA/ncore) is the public implementation and source-license authority for the post-V1 NCore adapter.
- [nvidia-ncore on PyPI](https://pypi.org/project/nvidia-ncore/) is the public package, release, and Python-requirement authority.
- [NCore Data Formats](https://nvidia.github.io/ncore/data/formats) defines the public V4 component-store organization.
- [NCore Specification](https://nvidia.github.io/ncore/data/conventions) defines the coordinate-frame and transformation conventions considered by the canonical contracts.
- [NCore Data Loading](https://nvidia.github.io/ncore/tutorial/data_loading.html) documents the public loader and sensor access patterns.
- [NCore Data Sanity Check](https://nvidia.github.io/ncore/tutorial/data_sanity_check) documents public pose, lidar fusion, motion compensation, projection, and timestamp checks.
- [NCore Sequence Metadata](https://nvidia.github.io/ncore/tools/ncore_sequence_meta.html) documents sequence metadata and component checksum extraction.
- [NCore Interactive 3D Viewer](https://nvidia.github.io/ncore/tools/ncore_vis.html) records relevant public visualization prior art.

## Public datasets and native Boreas format

- [Boreas paper](https://arxiv.org/abs/2203.10168) supports the primary public dataset selection and its multi-season sensor, timing, calibration, and trajectory capabilities.
- [Boreas development kit](https://github.com/utiasASRL/pyboreas) is the official public selective-download and file-access implementation reference.
- [Boreas data reference](https://github.com/utiasASRL/pyboreas/blob/master/DATA_REFERENCE.md) defines timestamps, lidar point records, radar metadata, transforms, synchronization, and trajectory fields for the native adapter.
- [Boreas project site](https://www.boreas.utias.utoronto.ca/) is the public dataset landing page and attribution source.
- [KITTI raw data](https://www.cvlibs.net/datasets/kitti/raw_data.php) records an optional post-V1 generalization source.
- [nuScenes](https://www.nuscenes.org/nuscenes) records relevant public multimodal dataset prior art.

M0.2 must record the exact dataset terms, selected object identities, byte counts, hashes, and redistribution decision before these sources can qualify any benchmark input.

## Algorithms, evaluation, and prior art

- [Hidden Markov Map Matching Through Noise and Sparseness](https://www.microsoft.com/en-us/research/publication/hidden-markov-map-matching-noise-sparseness/) is the primary HMM map-matching reference.
- [Parameterized Rural Postman Problem](https://arxiv.org/abs/1308.2599) defines the required directed-arc routing problem family.
- [RoadRunner](https://mapster.csail.mit.edu/roadrunner/roadrunner.pdf) is primary prior art for connectivity-aware road-network inference from trajectories.
- [MAPSTER](https://mapster.csail.mit.edu/) provides broader public map-inference and map-update context.
- [Robust Real-time LiDAR-inertial Initialization](https://arxiv.org/abs/2202.11006) provides public observability evidence relevant to later lidar and IMU alignment work.
- [IN2LAAMA](https://arxiv.org/abs/1905.09517) provides public evidence for per-point lidar timing, motion distortion, and time-shift estimation.
- [bagx](https://github.com/rsasaki0109/bagx) records public prior art for recording readiness, synchronization, anomaly checks, reports, and manifests.
- [rosbag-slam-lint](https://pypi.org/project/rosbag-slam-lint/) records public prior art for lidar and IMU recording checks.

The old direct Microsoft Research PDF rejected automated access, while the official publication page above resolves and links the paper.

## Build and packaging

- [scikit-build-core getting started](https://github.com/scikit-build/scikit-build-core/blob/main/docs/guide/getting_started.md) supports the CMake and pybind11 packaging foundation.
- [scikit-build-core build guide](https://github.com/scikit-build/scikit-build-core/blob/main/docs/guide/build.md) supports wheel and source-distribution decisions.

The current documentation confirms that `scikit-build-core` is the build backend, that CMake and Ninja can be managed by the backend, and that pybind11 modules use the CMake package integration.

## Source maintenance rule

A source must link to the exact public page or artifact that supports the claim.
A redirect to a generic landing page, an access-denied artifact, or a missing artifact does not satisfy that requirement when an exact public replacement exists.
If an authoritative public source changes a technical decision, the build plan and this inventory must receive the smallest evidence-backed correction in the same focused commit.
