# CartoSentry Build Plan

Status: implementation is paused by user request at the verified M5.6 pre-production-qualification boundary recorded in the final handoff section.

## Document status

This document is the implementation contract for CartoSentry.
It is intentionally more specific than a product brief and must be updated when verified evidence changes a technical decision.
Implementation agents must treat mandatory gates as requirements rather than aspirations.
No implementation agent may silently narrow, replace, or declare a gate passed without the evidence required here.
Every substantial milestone and meaningful submilestone must end in a focused commit after its acceptance checks pass.
Every verified commit must be pushed to the current remote branch before later work begins.

## Product identity

**Name:** CartoSentry.

**Repository name:** `cartosentry`.

**One-line description:** CartoSentry determines whether a supported vehicle recording is ready for an HD-map pipeline, localizes every deficiency to affected directed road segments, and computes a graph-valid recollection drive that repairs rejected coverage.

**Primary user question:** Can this drive build a map, which parts are unsafe to use, and what is the smallest drive required to repair the coverage?

**Primary user:** A mapping, robotics, or autonomous-systems data engineer who validates vehicle recordings before expensive map creation or localization experiments.

**Secondary user:** A collection-operations engineer who must decide which route should be driven again and why.

**Core value:** CartoSentry turns mapping-input integrity from a late manual discovery into an evidence-backed, road-localized, reproducible acceptance decision.

## North-star demonstration

The final demonstration must begin with a real public vehicle sequence and a deterministic V1 trajectory or lidar defect.
The V1 injected defect must exercise the supported trajectory or lidar contract, such as a lidar point-time shift, dropped lidar azimuth sector, or trajectory timestamp discontinuity.
CartoSentry must detect the defect, explain the observation and observability conditions, identify the affected time interval, map the interval to directed road bins, reject only the unsupported map coverage, and generate a verified recollection route.
The demonstration must compare the recollection route with a deterministic greedy baseline.
Any downstream registration or odometry improvement demonstration belongs to a separately qualified follow-on claim.
The final public claim must be no stronger than the frozen benchmark evidence.

## Why this project exists

HD-map creation depends on spatially and temporally coherent recordings from sensors that have different capture models, rates, coordinate frames, timestamp sources, failure modes, and storage costs.
A recording may contain a valid video and apparently plausible trajectory while still being unsafe for mapping because of a timestamp-domain mistake, calibration mismatch, GNSS discontinuity, lidar ring loss, stale camera frames, radar azimuth corruption, or incomplete directed road coverage.
These defects may be discovered only after expensive downstream processing or manual inspection.
Generic recording validators usually focus on file presence, topic rates, or format correctness.
Mapping teams need an additional decision layer that connects sensor evidence to road-level map coverage and to an actionable recollection plan.

## Public evidence and product grounding

The target Systems Software Engineer, Autonomous Systems Mapping role defines the primary role-alignment rubric for this portfolio project.
That rubric emphasizes data collection, data analysis, map creation, map consumption, GPS and IMU trajectory properties, lidar and camera integrity, efficient storage and upload, visualization, graph theory, statistics, computational geometry, robotics sensors, C++, Python, Linux, operating systems, and networking.

NVIDIA publicly describes localization in an HD map as a centimeter-scale pose problem and identifies camera, radar, lidar, GNSS, inertial, and vehicle sensors as complementary modalities.
The public source is [DRIVE Labs: How Localization Helps Vehicles Find Their Way](https://developer.nvidia.com/blog/drive-labs-how-localization-helps-vehicles-find-their-way/).

NVIDIA publicly describes DRIVE AGX Hyperion as supporting data collection and ingestion, verification, validation, and multiple sensor modalities.
The public source is the [NVIDIA Autonomous Vehicles Safety Report](https://images.nvidia.com/aem-dam/en-zz/Solutions/auto-self-driving-safety-report.pdf).

DriveWorks documentation distinguishes host, raw sensor, synchronized, and smoothed timestamp behavior.
The public source is [DriveWorks Sensor Timestamping](https://developer.nvidia.com/docs/drive/drive-os/6.0.5/public/driveworks-nvsdk/sensors_usecase4.html).

DriveWorks documentation describes synchronized recording and replay as critical for sensor-fusion development.
The public source is [How DriveWorks Makes it Easy to Record and Replay Data for AV Development](https://developer.nvidia.com/blog/how-driveworks-makes-it-easy-to-record-and-replay-data-for-av-development/).

The public DriveWorks post-record checker analyzes required recording files and per-sensor timestamp deltas.
The public source is [DriveWorks Post-record Checker](https://developer.nvidia.com/docs/drive/drive-os/6.0.7/public/driveworks-nvsdk/dwx_postrecord_checker.html).

NVIDIA NCore provides a current public canonical representation for multimodal autonomous-vehicle and robotics recordings.
The public source is the [NVIDIA NCore repository](https://github.com/NVIDIA/ncore).

NCore V4 separates pose, calibration, camera, lidar, radar, and other data into independently managed component stores.
The public source is [NCore Data Formats](https://nvidia.github.io/ncore/data/formats).

NCore publishes explicit coordinate-frame and transformation conventions, including local world coordinates and `SE(3)` transforms.
The public source is [NCore Specification](https://nvidia.github.io/ncore/data/conventions).

The NCore data-sanity workflow uses multi-frame lidar alignment and lidar-to-camera projection to investigate pose, calibration, and timestamp consistency.
The public source is [NCore Data Sanity Check](https://nvidia.github.io/ncore/tutorial/data_sanity_check).

Only public sources may appear in source code, documentation, benchmark reports, issues, screenshots, commits, or other repository artifacts.
No private company names, internal project identifiers, ticket numbers, internal links, excerpts, architecture details, or personnel information may enter the repository.

## Standalone product workflow

### Inspect a recording

```bash
cartosentry inspect \
  --sequence /data/boreas/boreas-example \
  --adapter boreas \
  --road-graph data/road_graphs/toronto-example.osm.pbf \
  --profile profiles/hdmap-lidar-v1.yaml \
  --output runs/example
```

The command must produce the following artifacts.

- `run.json` records input identities, profile identity, tool versions, stages, environment facts, and artifact hashes.
- `verdict.json` records the sequence-level and road-bin-level `PASS`, `FAIL`, or `UNKNOWN` decisions.
- `findings.jsonl` records normalized findings and evidence lineage.
- `metrics.parquet` records time-window and spatial-bin measurements.
- `accepted_intervals.json` records sensor intervals that meet the selected profile.
- `excluded_intervals.json` records rejected or unsupported intervals.
- `road_readiness.geojson` records verdicts for directed road bins.
- `report.html` or the local report application provides the human-review interface.
- `logs/` contains structured execution logs without raw data or secrets.

The process exit code must be `0` when the analysis completed, even if the mapping verdict is `FAIL` or `UNKNOWN`.
The process exit code must be nonzero only when CartoSentry itself cannot complete the requested analysis.
Automation must read the verdict artifact rather than confusing product rejection with process failure.

### Explain a finding

```bash
cartosentry explain \
  --run runs/example \
  --finding finding-001 \
  --format text
```

The explanation must name the rule, observation, threshold, observability result, source interval, affected road bins, evidence references, and remediation.
The text output must be reproducible from the machine-readable finding.

### Plan recollection

```bash
cartosentry plan-recapture \
  --run runs/example \
  --depot data/depots/example.geojson \
  --budget-seconds 5400 \
  --output runs/example/recapture
```

The command must produce a route that is valid under the pinned graph-import profile, a required-arc coverage proof, unreachable requirements, a cost summary, and a comparison with the greedy baseline.
The route is an offline collection plan and must not be presented as a safety-certified turn-by-turn navigation product.

### Publish accepted data

```bash
cartosentry publish \
  --run runs/example \
  --destination s3://mapping-staging/example \
  --resume
```

Publication is a later milestone and must remain optional.
The default publication payload must contain manifests, findings, metrics, and accepted interval references rather than copied raw sensor data.
Copying raw data must require an explicit `--include-raw` option and a profile that permits it.

### Serve the report

```bash
cartosentry serve \
  --run runs/example \
  --host 127.0.0.1 \
  --port 8765
```

The report server must bind only to loopback by default.
V1 must reject every non-loopback bind request.
Non-loopback serving is outside V1 and cannot ship without a separate authenticated threat model and qualification gate.

## Claims ladder

CartoSentry must earn claims in this order.

1. The engine deterministically parses supported recordings and detects schema, transport, and timestamp faults.
2. The V1 engine detects supported trajectory and lidar faults when their required evidence is observable.
3. The engine localizes findings to time intervals and directed road bins.
4. The readiness policy converts evidence into auditable tri-state decisions without score compensation.
5. The route planner covers every reachable rejected road requirement and improves on a frozen greedy baseline.
6. A follow-on study may claim that CartoSentry filtering improves a frozen downstream probe over accepting all corrupted data.
7. The CPU pipeline satisfies the frozen throughput and memory charter.
8. The optional CUDA path may claim acceleration only after numerical parity and end-to-end speed gates pass.

No README, resume bullet, demo narration, or benchmark headline may skip a lower claim and assert a higher claim.

## Mandatory V1 scope

The following work is mandatory for the first portfolio-complete release.

- A native Boreas adapter must read the selected public corpus without converting it through ROS.
- A deterministic synthetic trajectory and spinning-lidar fixture generator must create small test recordings that the project is permitted to redistribute.
- A deterministic fault laboratory must inject schema-preserving faults with exact ground truth.
- A C++20 streaming engine must validate the supported Boreas timestamp, trajectory, calibration, and lidar contracts.
- A directed road-graph importer and HMM map matcher must associate usable trajectory intervals with road bins.
- A versioned readiness engine must produce `PASS`, `FAIL`, and `UNKNOWN` decisions with evidence lineage.
- A static local report must support overview, map, timeline, lidar evidence, finding, policy, and recollection inspection.
- The static report may be served only through a loopback-only helper.
- A recollection planner must produce and independently validate a route against the pinned graph-import profile.
- A frozen benchmark must evaluate supported detection, localization, readiness, routing, throughput, and memory.
- The supported CPU product must run on macOS ARM64 and Linux x86-64.
- Documentation must enable a fresh user to reproduce the synthetic demo without private data or credentials.

## Staged follow-on claim tracks

The following tracks begin only after the portfolio V1 release tag exists.
They are not required for V1 and may not delay its qualification.

- The camera and radar track adds within-stream detectors, carefully gated cross-modal evidence, and their profiles.
- The topology track adds repeated off-map disagreement hypotheses without editing the source graph.
- The NCore track adds a production NCore V4 adapter after its M0 feasibility record is refreshed.
- The publication track adds immutable bundles and resumable S3-compatible upload against local MinIO.
- The downstream track evaluates registration or odometry utility with a separately frozen protocol.
- A Linux release wheel and source distribution remain part of V1 packaging, not a follow-on feature.

## Optional scope

The following work is optional and must not delay the CPU product.

- CUDA acceleration may optimize lidar voxelization, per-ring statistics, or occupancy aggregation.
- A KITTI adapter may provide a second-dataset generalization experiment.
- An MCAP or ROS 2 adapter may be added after the native and NCore adapters are complete.
- A multi-vehicle recollection optimizer may be explored only after the single-vehicle solver is proven.
- Topology-disagreement hypotheses may be expanded beyond missing-connection and geometry-disagreement review suggestions.

## Explicit non-goals

CartoSentry is not an HD-map builder.
CartoSentry is not a SLAM system.
CartoSentry is not a perception model-training project.
CartoSentry is not a live vehicle-control component.
CartoSentry is not a safety-certified product.
CartoSentry is not a replacement for physical sensor calibration.
CartoSentry is not a generic ROS bag browser.
CartoSentry is not a cloud-scale fleet data lake.
CartoSentry does not automatically mutate an authoritative road graph.
CartoSentry does not infer that a detector failure identifies the unique root cause unless the evidence contract supports that claim.
CartoSentry does not publish public raw dataset extracts.
CartoSentry does not require NVIDIA DRIVE hardware, a private SDK, or private datasets.

## Prior art and defensible novelty

### DriveWorks post-record checking

Public DriveWorks tooling checks required recording files and timestamp distributions.
CartoSentry must not claim to invent post-record checking.
CartoSentry extends the workflow with cross-modal observability, road-localized mapping readiness, deterministic fault evaluation, and recollection planning.

### NVIDIA NCore sanity checking

NCore provides a canonical multimodal format and a published manual or notebook-oriented sanity workflow.
CartoSentry must reuse NCore as an input adapter rather than recreating its storage specification.
CartoSentry adds an automated, versioned, evidence-backed mapping acceptance and repair decision.

### Generic recording validators

Public tools such as [`bagx`](https://github.com/rsasaki0109/bagx) and [`rosbag-slam-lint`](https://pypi.org/project/rosbag-slam-lint/) inspect topic availability, rates, gaps, synchronization, and workflow readiness.
CartoSentry must not market itself as the first recording readiness checker.
Its distinction is a sensor-format-independent map-creation gate tied to directed road coverage and optimized recollection.

### Map inference research

RoadRunner and related systems infer road topology from GPS trajectories.
CartoSentry must treat repeated off-map motion as a review hypothesis rather than claiming general automatic map construction.
Its trajectory work supports integrity, coverage, disagreement localization, and recollection.

### Novelty statement allowed after V1

The public-safe V1 novelty statement is that CartoSentry integrates trajectory and lidar recording integrity, directed road-bin mapping readiness, deterministic corruption evaluation, and graph-optimized recollection into one reproducible workflow.
Camera, radar, IMU alignment, and cross-sensor checks are separately qualified follow-on tracks.
The repository must say that this is an integration and product contribution rather than an unsupported claim that no similar internal or commercial system exists.

## Dataset and licensing contract

### Primary public dataset

The primary public dataset is [Boreas](https://arxiv.org/abs/2203.10168).
Boreas includes a 128-beam lidar, a scanning radar, a camera, GNSS and IMU information, calibration, accurate postprocessed poses, per-point or per-azimuth timing, repeated routes, and varied weather.
The official development kit is [utiasASRL/pyboreas](https://github.com/utiasASRL/pyboreas).
The official development kit supports selective anonymous S3 downloads.

### Dataset selection policy

M0 must select public sequences by official metadata and verify every selected object key before it is frozen.
The corpus must include at least one clear-weather sequence, one precipitation or snow sequence, one repeated-route pair, and one route or time window not used for detector development.
Frame-level random splitting is forbidden because adjacent sensor frames are strongly correlated.
Before any data inspection or fault generation, `benchmarks/source_groups.yaml` must assign every real sequence, repeated-route family, and synthetic scenario family to an immutable `source_group_id` and partition.
Every window, clip, converted format, injected derivative, repeated seed, report, and cached artifact must inherit the partition of its source group.
Moving a source group or derivative after any member has been inspected is forbidden.
Development, threshold-calibration, policy-tuning, and final-test partitions must have disjoint source groups and fault-seed families.
No source sequence may be copied into another partition through a new clip boundary, adapter, conversion, or injected fault.
An unseen-corridor generalization claim requires all sequences from the same physical corridor or route family to remain in one partition.
If repeated traversals of one corridor span partitions for a weather or temporal study, the claim must be limited to within-corridor temporal or weather generalization and must not say unseen route or corridor.
Weather and route slices must be reported separately when support permits them.

### Corpus tiers

The `synthetic-ci` tier contains only generated fixtures committed to the repository.
The identifier is retained as a stable suite name and is executed through repository-owned local commands.
The `public-smoke` tier contains short selectively downloaded real clips and is used for local integration checks.
The `public-full` tier contains longer real windows and full pose trajectories and is used for frozen evaluation outside Git.
The `gpu-perf` tier contains the frozen lidar-heavy workload used only for optional CUDA and Linux performance qualification.

### Distribution rule

No Boreas, NCore demo, KITTI, OpenStreetMap extract, or other third-party raw data may be committed unless its exact license explicitly allows repository redistribution and the licensing audit records that decision.
The safe default is to ship download scripts, exact public object identifiers, expected hashes, and attribution.
Generated thumbnails must be treated as derived dataset material and must not be published until the license review permits them.

### `data_manifest.yaml`

The repository must contain `benchmarks/data_manifest.yaml` with one entry per external artifact.
Every entry must include the following fields.

```yaml
id: boreas-public-smoke-clear-v1
source_name: Boreas
source_url: https://www.boreas.utias.utoronto.ca/
source_object_keys: []
retrieved_at_utc: null
license_url: null
terms_snapshot_sha256: null
redistribution: download_script_only
attribution: null
content_sha256: []
expected_bytes: null
partition: development
source_group_id: boreas-route-family-example
weather_tags: []
route_tags: []
purpose: adapter-and-detector-smoke
```

M0 must replace every `null` required for a frozen artifact.
Local manifest validation must fail if a frozen benchmark entry lacks its source, terms, attribution, partition, source group, object identity, or expected hash.
It must also fail when an artifact partition differs from its source-group partition or when one source group appears in more than one partition.

### Road graph data

OpenStreetMap road extracts must be pinned by source URL, retrieval timestamp, bounding polygon hash, license attribution, and content hash.
The report must include OpenStreetMap attribution when OSM data is visible.
The benchmark must never depend on a live tile server or a changing remote graph query.

The license manifest must classify each OSM-derived artifact before it is distributed.
The downloaded extract and normalized routable graph are databases or derivative databases and must retain ODbL notices, source date, attribution, and applicable share-alike obligations.
`road_readiness.geojson` and route GeoJSON or GPX that reproduce material OSM geometry, identifiers, or attributes must be treated conservatively as derivative databases for distribution.
Rendered static maps, screenshots, and video frames are produced works and must display OpenStreetMap attribution and identify the ODbL data source.
Metrics or findings that contain no recoverable OSM geometry, identifiers, or substantial extracted attributes may be classified as independent project artifacts only after the license record documents that basis.
The release audit must prevent a generated artifact from inheriting only the project source-code license when its OSM classification requires additional terms.

### Dataset license kill gate

If the exact public dataset terms cannot be located or do not permit the intended benchmark use, M0 must select a different public dataset or limit the project to locally downloaded nonredistributed research use.
The implementation must not proceed on an assumption that public accessibility equals unrestricted reuse.

## Technology stack and dependency responsibilities

### Languages

C++20 is mandatory for ingestion, numerical algorithms, detector kernels, map matching, readiness aggregation, routing, and performance-critical runtime behavior.
Python 3.12 is mandatory for CLI orchestration, dataset tooling, benchmark composition, profile validation, report assembly, packaging coordination, and evaluation statistics.
TypeScript is permitted only for the small local report client and must not duplicate domain logic from C++ or Python.
CUDA C++ is optional and must compile only behind `CARTOSENTRY_ENABLE_CUDA`.

### Build and packaging

CMake is the authoritative C++ build system.
CMake presets must define developer, release, sanitizer, coverage, and optional CUDA builds.
Ninja is the preferred generator for documented local and clean-environment commands.
`scikit-build-core` is the Python build backend for the CMake extension.
`pybind11` exposes narrow batch-oriented C++ interfaces to Python.
`uv` owns the Python lock, virtual environment, and command execution.
The initial dependency lock must be generated during M0 from current compatible releases and committed.
Dependency upgrades require their own reviewed commit and must not occur opportunistically during a feature milestone.

Current scikit-build-core documentation supports a `pyproject.toml` build backend, CMake discovery, pybind11 integration, editable installs, wheels, and source distributions.
The build configuration must not redundantly list CMake, Ninja, setuptools, or wheel in `build-system.requires` when scikit-build-core manages them.
The implementation source for this decision is [scikit-build-core getting started](https://github.com/scikit-build/scikit-build-core/blob/main/docs/guide/getting_started.md).

### C++ dependencies

Eigen owns fixed and dynamic linear algebra and `SE(3)` matrix operations.
Sophus owns checked `SO(3)` and `SE(3)` exponential, logarithm, and interpolation primitives if M0 confirms clean compatibility with the selected Eigen and compiler versions.
If Sophus compatibility fails, a small reviewed internal `SE(3)` wrapper may be implemented with Eigen, but both paths must not coexist.
GeographicLib owns WGS84 geodesy and local Cartesian conversion.
OpenCV owns camera decode, image statistics, image gradients, distance transforms, and evidence rendering primitives.
Apache Arrow and Parquet own columnar metrics exchange and persisted metrics.
SQLite owns the local run-state database.
`nlohmann/json` owns C++ JSON parsing and serialization.
`yaml-cpp` owns native profile parsing only after the Python schema validator has accepted the profile.
`spdlog` owns structured local logging through a project wrapper.
`fmt` owns formatting through the logging and error layers.
`CLI11` owns the small native diagnostic executables but not the public Python CLI.
`nanoflann` owns local nearest-neighbor indices for point and polyline calculations.
`libosmium` with its required Protozero and compression dependencies owns streaming OpenStreetMap PBF or XML parsing and source-element provenance.
No second OSM parser may coexist after the M0 toolchain spike.
Google OR-Tools may own the budgeted prize-collection formulation if M8 shows that it improves the deterministic heuristic without weakening validation.
The exact small-case state-space solver must remain project-owned so its correctness argument and limits are reviewable.
`libcurl` owns optional S3-compatible HTTP transport only if the MinIO interoperability spike validates the required multipart behavior.
`aws-sdk-cpp` must not be added unless libcurl plus SigV4 cannot meet the frozen publication contract.
`zstd` owns optional accepted-data shard compression.
Catch2 owns C++ unit and integration tests.
RapidCheck owns C++ property tests if its selected release works with the compiler matrix.
LibFuzzer owns native parser fuzz targets on Clang.

PCL, ROS, and a full SLAM framework are intentionally excluded from mandatory V1.
Their dependency surface and abstractions are not required for the selected algorithms.

### Python dependencies

Typer owns the public CLI argument model and help output.
Pydantic v2 owns Python configuration, schema, and artifact validation.
PyYAML may load configuration text only before Pydantic validation and may not define policy semantics.
NumPy owns array exchange and benchmark calculations.
PyArrow owns Parquet assembly and Python-side metric reads.
Pandas is optional for offline analysis notebooks and must not be required by the production CLI.
SciPy owns statistical tests and bootstrap helpers in the evaluation package, not runtime detector truth.
FastAPI and Uvicorn are not V1 dependencies and may enter only a separately approved stateful-report follow-on.
Jinja2 owns static report templates.
Plotly may own small embedded plots, but large sensor data must not be serialized into HTML.
Shapely owns report-side geometry preparation only, while C++ remains authoritative for readiness and routing geometry.
Boto3 may own benchmark download and optional publication orchestration if the MinIO spike shows it is simpler and more reliable than native C++ transport.
The product must select one upload implementation in M10 and delete the unselected spike.
pytest owns Python tests.
Hypothesis owns schema, CLI, and fault-lab property tests.
Ruff owns Python formatting and linting.
mypy owns strict type checking for production Python packages.
Bandit is not a substitute for threat-model review and may be used only as an additional static check.

### Public-format adapters

The Boreas adapter must be implemented against the public documented file formats and pinned fixtures.
The NCore adapter must use the public `nvidia-ncore` Python API and normalize data without exposing NCore objects across the C++ ABI.
The NCore dependency version must be pinned in the lockfile after M9.1 verifies macOS and Linux support.

### Version policy

`pyproject.toml`, `uv.lock`, `CMakePresets.json`, and the chosen C++ dependency lock mechanism must define the exact reproducible development environment.
Human-readable documentation must state supported compiler, Python, CMake, CUDA, driver, and operating-system ranges after qualification.
The plan deliberately does not invent exact dependency release numbers before the compatibility spike has built them together.
M0 must resolve and record those versions before feature work begins.

## Repository layout

```text
cartosentry/
  BUILD_PLAN.md
  README.md
  LICENSE
  SECURITY.md
  CONTRIBUTING.md
  CITATION.cff
  pyproject.toml
  uv.lock
  CMakeLists.txt
  CMakePresets.json
  cmake/
    CartoSentryWarnings.cmake
    CartoSentrySanitizers.cmake
    CartoSentryDependencies.cmake
  cpp/
    include/cartosentry/
      contract/
      evidence/
      geometry/
      ingest/
      map/
      readiness/
      routing/
      runtime/
      sensors/
      time/
      trajectory/
    src/
    bindings/
    tools/
    tests/
    fuzz/
    cuda/
  python/
    cartosentry/
      adapters/
      benchmarks/
      cli/
      config/
      datasets/
      faultlab/
      packaging/
      report/
      upload/
  web/
    src/
    tests/
  schemas/
    sequence_manifest.schema.json
    finding.schema.json
    readiness_profile.schema.json
    run.schema.json
    recapture_plan.schema.json
    bundle.schema.json
  profiles/
    structural-preflight-v1.yaml
    hdmap-lidar-v1.yaml
    follow_on/
      semantic-camera.yaml
      radar-redundancy.yaml
  benchmarks/
    data_manifest.yaml
    split_manifest.yaml
    numerical_charter.yaml
    fault_matrix_v1.yaml
    follow_on/
      fault_matrices/
    scenarios/
    baselines/
  scripts/
    bootstrap.sh
    download_public_data.py
    verify_public_data.py
    run_demo.sh
    run_benchmarks.py
    gpu/
      sync_gpu_host.sh
      provision_verified_data.sh
      qualify_gpu_host.sh
  docs/
    architecture.md
    contracts.md
    benchmark_methodology.md
    data_and_licenses.md
    threat_model.md
    supported_profiles.md
    gpu_qualification.md
    decisions/
  tests/
    e2e/
    golden/
```

Only create files when their milestone begins.
Do not prefill empty abstraction directories merely to match this tree.

## System architecture

```text
Source recording
      |
      v
Adapter and immutable source manifest
      |
      v
Streaming index and structural preflight
      |
      +--------------------+
      |                    |
      v                    v
Trajectory and time       Sensor modality checks
analysis                  and cross-modal checks
      |                    |
      +----------+---------+
                 |
                 v
        Normalized evidence ledger
                 |
      +----------+-----------+
      |                      |
      v                      v
HMM road matching       Readiness policy
and spatial bins        evaluation
      |                      |
      +----------+-----------+
                 |
                 v
     Road-localized verdict and report
                 |
                 v
       Recollection route optimizer
                 |
                 v
      Validated route and optional bundle
```

Adapters must expose immutable views of source data.
Detectors must emit measurements and evidence rather than directly deciding the sequence verdict.
The policy engine must be the only component allowed to convert normalized evidence into readiness states.
The route optimizer must consume failed or unsupported road requirements rather than raw detector output.
The route validator must be independent of the optimizer.

## Canonical domain contracts

### Stable identifiers

Identifiers must be deterministic and stable across resumed runs.
A `sequence_id` derives from the normalized source manifest identity rather than a local path.
A `stream_id` is unique within a sequence and must include modality and sensor identity.
A `frame_id` combines stream identity with the source frame key and capture interval.
A `finding_id` derives from detector identity, rule identity, source interval, stream set, and evidence fingerprint.
A `road_bin_id` combines road-graph identity, directed arc identity, and longitudinal bin index.
A `run_id` derives from sequence identity, road graph identity, profile identity, engine version, and relevant configuration hashes.

### Time representation

Every normalized time value must use the tagged `TimePoint` object.
`TimePoint.value_ns` is a signed 64-bit integer count of nanoseconds in the declared epoch and is the only canonical numeric timestamp representation.
Durations use signed 64-bit integer nanoseconds.
Floating-point seconds and unit-encoded field names such as `timestamp_us` are forbidden in canonical schemas.

Every `TimePoint` must contain `value_ns`, `epoch`, `clock_id`, `reference`, and `raw`.
`epoch` must be one of `UNIX_UTC`, `GPS`, `SENSOR_BOOT`, `HOST_MONOTONIC`, or `UNKNOWN`.
`reference` must be one of `EXPOSURE_START`, `EXPOSURE_MIDPOINT`, `EXPOSURE_END`, `SCAN_START`, `SCAN_MIDPOINT`, `SCAN_END`, `SAMPLE`, `PER_POINT`, `PER_AZIMUTH`, or `UNKNOWN`.
`raw` must preserve the source key, field name, original unit, source epoch, source reference, numeric encoding, and exact source representation as a decimal lexeme, integer value, or encoded byte string.
Conversion to `value_ns` must use checked arithmetic, a declared rounding rule, and a recorded maximum conversion error.
The raw representation, rather than `value_ns`, owns lossless source round trip.

Clock correction must use a separate `CorrectedTime` object that contains the original `TimePoint`, corrected `value_ns`, target epoch, target clock identifier, correction-model identifier and hash, pivot, offset, rate term, uncertainty, and applicability interval.
A correction must never overwrite the original clock identity or masquerade as a timestamp domain.
There is no `CORRECTED_COMMON` epoch or clock domain.
Every join must declare whether it uses original or corrected time and must reject incomparable epochs or clocks without an applicable correction model.
Monotonic process time must never be serialized as Unix time.
Missing source time must remain absent rather than being copied from another field.

Every sensor frame may contain `capture_start`, `capture_end`, `sensor_time`, `host_receive_time`, and `corrected_sensor_time`, with each populated field using the tagged object required above.
Lidar point times and radar azimuth times must remain relative or absolute exactly as identified by the adapter contract.
An adapter that materializes an absolute point or azimuth time must preserve the relative raw value and record the frame reference used in the conversion.

### Boreas temporal and geodetic mapping

Boreas camera, lidar, and radar filenames are integer microseconds since the Unix UTC epoch.
The camera filename is an exposure midpoint, the lidar filename is a scan midpoint, and the radar filename is the timestamp of the documented middle azimuth.
Because V1 does not have a documented camera exposure duration, a camera filename becomes a point-valued `EXPOSURE_MIDPOINT`, not an invented exposure interval.
Boreas lidar point time is a stored floating-point relative-second offset from the lidar filename time.
The adapter must preserve the source float bits, convert the relative offset to integer nanoseconds with the frozen rounding rule, and materialize the absolute per-point time from the scan midpoint only through recorded provenance.
Boreas radar embeds an unsigned 64-bit Unix UTC microsecond timestamp for each azimuth.
The adapter must preserve those values as `PER_AZIMUTH` time and cross-check the documented middle azimuth against the frame filename.
`gps_post_process.csv` and the follow-on `imu_raw.csv` use a decimal `GPSTime` field in Unix UTC seconds.
The adapter must parse the original decimal lexeme directly into integer nanoseconds rather than first converting through binary floating point.
The trajectory row timestamp and raw IMU row timestamp use the `SAMPLE` reference.

The Boreas trajectory fields `latitude` and `longitude` are radians and must be converted to WGS84 angular degrees with the source radians retained.
The `easting`, `northing`, and `altitude` fields belong to the dataset ENU reference.
The public data reference does not establish the vertical datum of `altitude` strongly enough for an ellipsoidal-height claim.
The adapter must therefore persist vertical datum as `UNKNOWN_VERTICAL_DATUM`, avoid converting altitude to WGS84 ellipsoidal height, and use horizontal position only for the V1 road match.
M0 must verify every mapping above against pinned public bytes and the public data reference before the adapter contract freezes.

### Frame interval convention

A measurement with a nonzero exposure, scan, or integration period occupies the half-open interval `[capture_start, capture_end)` and requires `capture_end.value_ns > capture_start.value_ns` in the same epoch and clock.
An instantaneous measurement persists its point time in `sensor_time` and leaves the capture interval absent unless the source documents a nonzero support interval.
An index may derive the synthetic one-nanosecond query interval beginning at `sensor_time`, but that derivative must be tagged as index metadata and never serialized as source truth.
Overlap joins must define the reference timepoint selected for each modality.
The selected reference timepoint must be recorded in the detector configuration.

### Coordinate and `SE(3)` convention

All coordinate frames are right handed.
The canonical vehicle rig frame uses `x` forward, `y` left, and `z` up.
The canonical local world frame is metric and anchored at the first structurally valid trajectory pose.
The global geographic frame is WGS84 latitude, longitude, and ellipsoidal altitude when altitude is available.
The Boreas adapter must read `latitude` and `longitude` from `applanix/gps_post_process.csv`, whose documented values are radians, and normalize them to WGS84 degrees with the conversion recorded in timestamp and coordinate provenance.
Road matching must use those timestamped geographic observations or a transform derived from those same paired geographic and ENU rows.
The visualization-only `route.html` polyline may be used as a cross-check but must not become an undocumented geographic anchor or replace timestamped source rows.

`T_target_source` means a homogeneous transform that maps a point expressed in `source` coordinates into `target` coordinates.
The only allowed point equation is `p_target = T_target_source * p_source` using homogeneous column vectors.
The only allowed chain equation is `T_c_a = T_c_b * T_b_a`.
Every composition must follow that equation and must be covered by named-frame unit tests that fail if source and target are reversed.
Transforms must be stored as a translation vector and unit quaternion or as a checked 4 by 4 matrix with declared serialization order.
Quaternions must use the persisted order `w, x, y, z`.
Quaternions must be normalized during validated construction and rejected when their norm is not recoverable.
Rotation matrices must be checked for orthonormality and positive determinant within frozen tolerances.

Global positions and transforms must use `float64`.
GPU and dense point kernels may use `float32` only after subtracting a local origin.
Tests must prove that global-to-local-to-global round trips satisfy the numerical charter.

### Uncertainty and observability

An uncertainty is not interchangeable with a detector confidence.
Measurement uncertainty must carry units and derivation.
Detector confidence must identify its calibration source or remain absent.
Observability must be one of `OBSERVABLE`, `WEAK`, `NOT_OBSERVABLE`, or `NOT_APPLICABLE`.
Weak or missing evidence may produce a measurement but may not produce a mandatory pass.

### Verdict states

The only readiness states are `PASS`, `FAIL`, and `UNKNOWN`.
`PASS` means all mandatory requirements have observable passing evidence.
`FAIL` means at least one mandatory requirement has observable failing evidence.
`UNKNOWN` means no mandatory requirement failed but at least one mandatory requirement lacks observable evidence.
An optional score may rank bins for review but must never override this logic.

### Severity

Finding severity must be one of `INFO`, `WARNING`, `CRITICAL`, or `BLOCKING_ANALYSIS`.
`CRITICAL` means the selected profile may reject affected coverage.
`BLOCKING_ANALYSIS` means CartoSentry could not compute the requested analysis for the affected scope.
Severity and readiness effect are separate fields because a profile may treat the same finding differently.

### Evidence reference

Every evidence reference must contain a source artifact hash, source interval, optional frame identifiers, derived artifact hash, detector version, and transformation lineage.
Evidence thumbnails and plots must be reproducible from the reference and must not become independent untraceable truth.

### Sequence manifest

The normalized sequence manifest must contain source identity, adapter identity and version, sensor descriptors, source files with hashes, calibration identities, timestamp metadata, coordinate metadata, and declared gaps.
Source local paths may appear only in the local run manifest and must be replaced with portable source keys in exported artifacts.

### Finding schema

Each finding must include the following logical fields.

```json
{
  "finding_id": "finding-001",
  "detector_id": "imu_pose_time_alignment",
  "detector_version": "1.0.0",
  "rule_id": "maximum_observable_offset",
  "severity": "CRITICAL",
  "observability": "OBSERVABLE",
  "streams": ["imu:applanix", "trajectory:reference"],
  "interval": {
    "start": {
      "value_ns": 0,
      "epoch": "UNIX_UTC",
      "clock_id": "synthetic-common",
      "reference": "SAMPLE",
      "raw": {
        "source_key": "synthetic",
        "field": "interval_start",
        "unit": "ns",
        "epoch": "UNIX_UTC",
        "reference": "SAMPLE",
        "encoding": "signed_integer",
        "integer_value": "0"
      }
    },
    "end": {
      "value_ns": 1000000000,
      "epoch": "UNIX_UTC",
      "clock_id": "synthetic-common",
      "reference": "SAMPLE",
      "raw": {
        "source_key": "synthetic",
        "field": "interval_end",
        "unit": "ns",
        "epoch": "UNIX_UTC",
        "reference": "SAMPLE",
        "encoding": "signed_integer",
        "integer_value": "1000000000"
      }
    }
  },
  "measurement": {
    "name": "estimated_offset_ns",
    "value": 42000000,
    "unit": "ns"
  },
  "threshold": {
    "operator": "abs_le",
    "value": 15000000,
    "unit": "ns",
    "charter_key": "time_alignment.maximum_offset_ns"
  },
  "road_bin_ids": [],
  "evidence": [],
  "remediation": "Recollect the affected directed road bins after synchronizing the IMU clock."
}
```

The schema must distinguish a detector observation from a root-cause hypothesis.
The field `hypotheses` may list possible causes with supporting and contradicting evidence.
The UI must not render a hypothesis as a confirmed cause.

## Run state and resume semantics

### Stage state machine

Every stage must use the following persisted states.

```text
PENDING
RUNNING
COMPLETE
FAILED_RETRYABLE
FAILED_FINAL
INVALIDATED
SKIPPED_NOT_APPLICABLE
```

A stage may transition from `PENDING` to `RUNNING` only after all required upstream artifact hashes are present.
A stage becomes `COMPLETE` only after its output artifacts validate against schema and expected hashes are committed atomically.
A process crash while `RUNNING` must be interpreted as an incomplete attempt on resume.
Incomplete temporary outputs must never be treated as complete.

### Artifact commit protocol

Each stage must write into a unique attempt directory.
The stage must flush files, validate schemas, compute hashes, and write an attempt manifest.
The stage must then atomically publish a small completion pointer or rename the attempt directory on the same filesystem.
The run database transaction that records completion must include the published artifact identities.
The implementation must define recovery behavior for a filesystem publish that succeeds before the database commit and for a database commit that cannot observe the published artifact.
Recovery must reconcile artifacts by hash rather than silently recomputing or deleting them.

### Cache keys

Every stage cache key must include all source hashes, relevant upstream artifact hashes, relevant configuration hashes, detector or algorithm versions, and numerical backend identity.
Unrelated report theme changes must not invalidate sensor detection.
Detector threshold changes must invalidate policy evaluation and any downstream artifacts whose meaning changes.
Adapter changes must invalidate every artifact derived from the normalized source manifest.

### Resumption requirements

`--resume` must skip valid complete stages.
`--resume` must retry retryable failures and incomplete attempts.
`--force-stage` must invalidate the selected stage and all dependent stages after showing the affected scope.
No command may silently reuse artifacts produced by a different profile or source hash.
The clean run and interrupted then resumed run must produce identical semantic artifacts.
Run timestamps and attempt logs may differ and must be excluded from semantic equality checks.

### Cancellation and cleanup

SIGINT and SIGTERM must request cooperative cancellation.
Workers must stop accepting new work, finish or abandon current atomic units safely, and release file descriptors and memory.
Source recordings must never be modified.
Temporary attempt directories older than a configurable retention period may be listed by `cartosentry doctor` but must not be deleted without an explicit cleanup command.

## Ingestion architecture

### Adapter interface

Every adapter must implement a common read-only contract.
The contract must expose sequence metadata, sensor descriptors, calibration, pose samples, frame intervals, frame payload handles, per-point or per-azimuth timing when available, and source provenance.
The contract must support sequential iteration without materializing the full sequence.
Random access may be added through a sidecar index but must not be required for the first scan.
Adapters must not normalize away information that a detector might need.

### Source manifest pass

The first pass must enumerate expected files and directories, validate naming and basic schema, record byte sizes, compute or schedule content hashes, and identify source timestamp ranges.
The pass must not decode every camera frame or load every point cloud.
The output is the immutable normalized sequence manifest.

### Streaming frame index

The frame index must map each stream and frame identity to source key, byte range or file path, capture interval, parse status, and optional lightweight statistics.
The index must be written incrementally in bounded batches.
The implementation must tolerate source file order that differs from timestamp order.
It must report duplicate source keys and duplicate timestamps explicitly.

### Work scheduler

The work scheduler must use bounded queues measured in estimated bytes rather than only task counts.
Large lidar frames must not starve small IMU or metadata tasks indefinitely.
The scheduler must expose per-stage queue depth, active bytes, completed units, failed units, and backpressure time.
Worker errors must be value objects returned to the orchestrator rather than uncaught exceptions that terminate unrelated work.
The scheduler must make task ordering deterministic when deterministic mode is enabled for tests and benchmarks.

### Source access

Local files are the only mandatory V1 source transport.
Remote object access is not required for analysis because the public downloader can materialize selected inputs first.
The adapter may use buffered reads or memory mapping after benchmarks establish the better behavior for each file type.
The implementation must not memory map an unbounded collection of large files simultaneously.
Every open file, decoded frame, and point buffer must have explicit lifetime ownership.

### Integrity hashing

SHA-256 is the portable source and artifact identity hash.
A faster noncryptographic checksum may be used internally for block-level corruption scans but cannot replace SHA-256 in exported lineage.
Content hashing must stream through bounded buffers.
Hash results may be reused only when source size, stable file identity, and modification metadata match the recorded scan and the user has not requested full revalidation.
Every release-candidate source object, benchmark object, graph extract, normalized manifest, fault derivative, schema, profile, charter, split manifest, fallback tree, binary, wheel, source distribution, report payload, screenshot source, and benchmark result must have a full-file SHA-256 digest.
Chunk or sampled hashes may accelerate development checks but can never qualify release evidence.

### Structural detector outputs

The ingestion layer may emit structural findings but must not decide map readiness.
Examples include missing required source file, unreadable frame, duplicate timestamp, unexpected payload size, nonfinite point field, and inconsistent calibration reference.

## Detector framework

### Detector contract

Every detector must declare the following metadata.

- The detector identifier and semantic version must be stable.
- The required modalities and fields must be explicit.
- The supported epochs, clock identifiers, references, and correction models must be explicit.
- The required calibration and pose information must be explicit.
- The minimum observation duration and motion excitation must be explicit.
- The emitted measurements, units, and uncertainty methods must be explicit.
- The observability predicate must be explicit.
- The failure rules must reference frozen charter keys rather than hidden constants.
- The computational cost and memory budget must be declared.
- The detector must name deterministic evidence selectors.

Detectors must be pure with respect to source recordings.
Detector output must depend only on declared inputs, configuration, and engine version.
Randomized algorithms must accept and record a seed.

### Windowing contract

Detectors must operate on named time-window policies such as fixed duration, fixed distance, frame neighborhood, or full sequence.
Windows are half open.
Overlapping windows must declare stride separately from duration.
Measurements near missing data must record effective support rather than pretending the nominal window is complete.
Window aggregation must avoid counting multiple overlapping failures as independent events.

### Event consolidation

Raw failing windows must be consolidated into events using a documented temporal adjacency and hysteresis rule.
The consolidation rule must be frozen before the holdout benchmark.
Event start and end errors must be evaluated against injected ground truth.

### Baselines and adaptive thresholds

Hard physical or schema limits may be global.
Scene-dependent content metrics must use a frozen reference or robust within-sequence baseline.
Adaptive baselines must use only past or explicitly designated calibration data and must not learn from the final holdout.
A detector that cannot distinguish weather, scene content, and sensor failure must report degradation evidence or `UNKNOWN` rather than a definitive hardware-failure claim.

## Structural and transport integrity detectors

The structural suite must include the following checks.

- Required sequence metadata and calibration files must exist.
- Every source file must match its declared or inferred format.
- Payload sizes and shapes must satisfy adapter contracts.
- Camera images must decode completely.
- Lidar point records must have a valid stride and finite required fields.
- Radar polar images and embedded metadata must have valid dimensions and encodings.
- Pose and IMU rows must have the expected column count and parseable numeric values.
- Stream timestamps must not regress unless the adapter declares a source epoch transition.
- Duplicate source frames and timestamps must be reported.
- Gaps must be measured against robust expected cadence and profile rules.
- Abrupt cadence changes must be localized.
- Unexpected stream termination must be reported separately from a full recording end.
- Calibration identifiers must match the streams that reference them.
- Content hashes must match pinned benchmark expectations when available.

The suite must distinguish `MISSING`, `CORRUPT`, `UNSUPPORTED`, and `NOT_REQUESTED` data.
Unsupported data must not be described as corrupt.

## V1 trajectory analysis and follow-on IMU analysis

### Reference trajectory

V1 may use the postprocessed Boreas trajectory as the reference path because the project is a map-input validator rather than a navigation estimator.
The report must call it a postprocessed reference trajectory and must not imply that CartoSentry reconstructed it from raw GNSS.
The Boreas postprocessed trajectory is derived from an Applanix solution that uses inertial data.
Comparing `imu_raw.csv` with that trajectory is therefore a self-consistency study, not an independent accuracy measurement.
Raw IMU support belongs to the follow-on camera, radar, and cross-modal track rather than portfolio V1.
Any claimed time-offset estimation accuracy must come from injected ground truth or a separately documented reference that is independent of both compared signals.

### Trajectory interpolator

Translation interpolation must use a continuous local-world representation.
Hermite interpolation may use source velocity when it is reliable.
Orientation interpolation must use quaternion slerp or a checked Lie-group interpolation.
The interpolator must reject or mark gaps longer than the frozen support threshold.
Extrapolation outside the source support interval is forbidden by default.

### Robust derivatives

Velocity, acceleration, jerk, heading, yaw rate, and curvature estimates must use a documented smoothing window and robust outlier handling.
Heading must be unwrapped before differentiation.
Stationary intervals must not generate arbitrary heading changes from positional noise.
The derivative implementation must be verified against analytic synthetic trajectories including straight motion, constant-radius turns, stop and start, and timestamp gaps.

### Trajectory integrity checks

The following checks are mandatory.

- Position jumps must compare displacement with elapsed time and local speed support.
- Frozen position must be distinguished from a truly stationary vehicle by source velocity evidence in V1 and may add IMU evidence only in the follow-on track.
- Implausible velocity, acceleration, jerk, and yaw-rate changes must reference profile-specific physical thresholds.
- Nonmonotonic or duplicate pose timestamps must be reported.
- Long interpolation gaps must create unsupported intervals.
- WGS84 and local-world conversion must be checked for continuity and numerical stability.
- Global position uncertainty or quality fields must be retained when the source supplies them.

### Follow-on IMU integrity checks

The following checks are mandatory.

- Nonfinite accelerometer or gyroscope fields must fail structural integrity.
- Repeated identical samples must be tested against quantization-aware stuck-axis rules.
- Saturation must use source limits when available and a declared fallback otherwise.
- Gravity magnitude during observable stationary intervals must be compared with local gravity tolerance.
- Bias and noise statistics must be estimated only on intervals that satisfy their stationarity or motion assumptions.
- Rate gaps, bursts, and timestamp regressions must be detected.
- Axis conventions and the transform into the rig frame must be tested with signed synthetic rotations.

### Follow-on IMU-to-trajectory time alignment

The primary time-alignment signal is rig-frame yaw rate from the reference trajectory compared with the transformed gyroscope vertical-axis signal.
The first stage must resample both signals onto a common grid without extrapolation.
The second stage must compute normalized cross-correlation over a bounded lag interval.
The third stage must refine the best lag with continuous local optimization around the discrete peak.
The estimator must report peak correlation, peak-to-secondary separation, effective bandwidth, angular excitation, support duration, lag estimate, and uncertainty.
The sign convention must be established with synthetic positive and negative turns.

The result is `OBSERVABLE` only when support, excitation, correlation, and peak separation satisfy frozen gates.
Low-excitation straight driving must return `NOT_OBSERVABLE` rather than a zero-offset pass.
Multiple inconsistent high-quality windows must produce a drift or instability finding rather than an averaged global offset.
On Boreas, this detector may report only observable agreement or disagreement between related Applanix products.
It must not report real-world offset accuracy, sensitivity, or specificity from that comparison.
Offset-error gates must be evaluated only on injected synthetic truth or an independently sourced timing reference.

### Follow-on optional GNSS and IMU fusion probe

A full error-state Kalman filter is not mandatory for V1.
It may be added only if the public data exposes the required raw GNSS observations and covariance semantics.
The implementation must not build an estimator around postprocessed truth while marketing it as independent GNSS and IMU fusion.

## Lidar integrity analysis

### Parse and physical validity

Every lidar point record must validate coordinates, range, intensity, ring or model-element identity when present, and point time when present.
The detector must count invalid records and retain representative source offsets.
Impossible range checks must use the selected sensor profile rather than an undocumented generic limit.

### Coverage statistics

The detector must compute point count, finite-return ratio, range quantiles, intensity quantiles, per-ring counts, per-azimuth counts, blank-sector spans, and scan duration.
Statistics must be calculated in bounded streaming batches.
The expected per-ring or per-azimuth pattern must be derived from the declared sensor model or a frozen clean calibration partition.

### Ring and sector loss

A missing-ring detector must distinguish a consistently absent unsupported ring from a ring that disappears during a recording.
An azimuth-sector detector must consider expected occlusion or ego masks when the source supplies them.
Injected ring and sector losses must be evaluated across several severities and durations.

### Motion compensation

Each lidar point must be transformed using its measurement time when per-point times are available.
The target frame must be the lidar frame at the documented frame reference time or the local world frame.
The detector must not apply one pose to a spinning scan and call the result motion compensated.
Frames that cross unsupported trajectory gaps must be marked unsupported.

### Multi-frame alignment

The engine must aggregate selected static-scene point evidence into a local voxel representation.
It must measure voxel occupancy consistency, local surface thickness, and optional nearest-neighbor residuals across adjacent motion-compensated frames.
Dynamic objects and near-ego regions must be suppressed with deterministic geometric masks where possible.
The detector must report observability based on scene structure, ego motion, and valid point support.
It may identify alignment degradation but must not uniquely assign the cause to trajectory, time, or extrinsic calibration without discriminating evidence.

### Weather and scene limitations

Snow, rain, dust, glass, sparse open space, and highly dynamic scenes can alter lidar statistics.
The detector must not call every density change a broken sensor.
Weather-tagged holdout slices must quantify false critical findings.
When a selected mapping profile genuinely disallows precipitation-degraded lidar, the policy rule must express that product limitation explicitly.

## Follow-on camera integrity analysis

### Decode and stream checks

The camera detector must validate decode completion, image dimensions, channel format, frame cadence, capture interval, and content finiteness after decode.
Decode failures must include exact source evidence without embedding the corrupt raw payload in logs.

### Frozen and duplicate frames

Exact byte equality is sufficient for exact duplicates but not for freeze detection.
Freeze detection must use a deterministic perceptual representation and account for genuinely stationary scenes.
Vehicle or lidar motion evidence must be used when deciding whether repeated imagery is suspicious.

### Blur and exposure

Blur measurement may use gradient energy or variance of Laplacian after a frozen resize and color conversion.
Exposure measurement must report dark, saturated, and usable pixel fractions.
Scene-content metrics require an observable reference and must default to degradation evidence rather than a hardware-root-cause claim.
Thresholds must be calibrated on development and calibration partitions only.

### Camera-to-lidar projection consistency

The engine must motion compensate lidar points to the camera exposure reference time before projection.
It must apply the declared lidar-to-camera extrinsic and camera model.
The selected geometric lidar features and image-edge representation must be deterministic.
The metric may use edge-distance residual, normalized mutual information, or another M0-validated measure.
M0 must compare at least two candidate metrics on clean and perturbed calibration or timing fixtures and retain only the better-observed metric.

The detector must separate static and moving-vehicle test intervals.
A static-scene projection failure is stronger evidence of camera model or extrinsic inconsistency.
A moving-scene failure with otherwise good multi-frame lidar alignment is evidence compatible with timing inconsistency.
Low-texture, darkness, precipitation, or insufficient projected structure must produce weak or not-observable output.

## Follow-on radar integrity analysis

### Boreas radar contract

The Boreas adapter must preserve raw polar scan dimensions, per-azimuth timestamp metadata, encoder values, range resolution, and documented range offset.
The adapter must test byte ordering with official format examples and synthetic fixtures.

### Radar checks

The mandatory radar checks are polar schema validity, azimuth count, timestamp monotonicity, encoder progression, repeated azimuths, missing sectors, scan cadence, blank-sector persistence, and robust range-energy statistics.
The detector must avoid a fixed global energy threshold that rejects quiet scenes.
It must compare within-sequence behavior and frozen clean references.

### Radar-to-lidar consistency

A radar-to-lidar BEV consistency check belongs only to the later modality track.
If retained, it must use motion-compensated common-time projections and document the very different sensing physics.
It may detect large extrinsic or time perturbations but must not expect pointwise correspondence.

## Follow-on cross-modal reasoning

Cross-modal detectors must emit separate evidence rather than a single opaque fusion score.
The evidence graph must preserve which observations support or contradict each root-cause hypothesis.

For example, a camera-to-lidar projection failure may support time-offset and extrinsic-error hypotheses.
Successful lidar multi-frame alignment may contradict a large trajectory error hypothesis.
A failure only while moving may support a timing hypothesis more strongly than an extrinsic hypothesis.
A failure while static may support a camera model or extrinsic hypothesis more strongly.

The first release may expose this reasoning as deterministic rules.
It must not use a language model or black-box learned classifier to decide readiness.

## HMM road matching

### Road graph

The road graph must be directed.
Nodes represent topological junction positions.
Arcs represent graph-valid directed traversal under one pinned `GraphImportProfile`, with geometry, length, source identifier, directionality, access interpretation, and supported turn attributes.
Parallel roads, ramps, bridges, and tunnels must remain distinct where the source graph distinguishes them.
The importer must retain enough provenance to map a directed arc back to its OpenStreetMap elements.
V1 must not call a route road-legal, drivable, or safe.
V1 supports only restrictions explicitly represented and implemented by the pinned import profile.
At minimum, that profile must define highway-class inclusion, directional access, generic vehicle access tags, one-way semantics including reverse one-way, supported turn restrictions, handling of conditional access, construction and private roads, ferry links, and unknown tag values.
Conditional restrictions that depend on date, time, vehicle dimensions, weight, weather, permits, or local law must cause the affected arc or transition to be excluded or marked `UNKNOWN_RESTRICTION` unless the profile fully evaluates that condition from declared planning inputs.
If full conditional restriction evaluation is not implemented, the planner must conservatively exclude those elements and report resulting unreachable requirements.
The graph identity must include the import-profile hash.

### Candidate generation

Candidate road positions must be generated within an uncertainty-aware radius around each trajectory observation.
The search radius must have minimum and maximum bounds in the numerical charter.
Candidates must include projected position, lateral distance, tangent heading, directed arc, along-arc offset, and emission features.
An explicit off-map candidate must always be available.

### Emission model

The baseline emission log likelihood must combine lateral distance and heading agreement.
Position uncertainty may scale lateral-distance variance when the source supplies trustworthy uncertainty.
Heading agreement must be downweighted or disabled below a minimum vehicle speed.
The off-map emission must be calibrated to prevent both forced matching and gratuitous off-map paths.

### Transition model

The transition score must compare observed displacement and elapsed time with graph-profile-valid directed distance between candidates.
Transitions forbidden by the pinned graph-import profile must have negative infinite score.
Transition penalties may include path-length discrepancy, implausible speed, U-turn behavior, and turn count.
The model must not use future observations outside the decoded sequence window when operating in an explicitly causal mode.
The offline default may use full-window Viterbi decoding and must label itself offline.

### Decoder

Viterbi decoding with deterministic beam pruning is the mandatory baseline.
Beam width and pruning rules must be frozen before holdout evaluation.
The decoder must retain the best and runner-up path evidence required for ambiguity assessment.
Forward-backward marginal computation is optional if it fits the performance budget.

### Match outputs

Each matched interval must include directed arc, along-arc offsets, posterior or path-separation evidence, off-map state, and source trajectory support.
Low-confidence intervals must not silently count as covered road bins.
Stationary periods must not generate repeated road coverage.

### Map-matching validation

Synthetic road graphs must cover parallel roads, overpass and underpass, ramp merge, divided road, one-way loop, roundabout, U-turn, missing edge, sparse observations, GPS noise, and stopped vehicle.
Tiny cases must have hand-authored expected directed paths.
Map matching on the public route must be manually reviewed on a frozen sample and compared with geometric and topological consistency checks.

The baseline algorithm is grounded in [Hidden Markov Map Matching Through Noise and Sparseness](https://www.microsoft.com/en-us/research/publication/hidden-markov-map-matching-noise-sparseness/).

## Road bins and trajectory mining

### Directed road bins

Each directed arc must be divided into fixed longitudinal bins using arc-length parameterization.
The final partial bin must retain its true length.
Coverage must record entry and exit offset, direction, usable duration, usable distance, and evidence by modality.
Crossing a bin by a low-confidence map match must not count as mandatory coverage.

### Independent traversals

Independent traversals must be separated by sequence identity or a frozen minimum time gap.
Adjacent windows from one pass must not inflate traversal count.
Traversal direction must be part of the identity.

### Coverage features

V1 per-bin features must include usable trajectory distance, number of independent traversals, speed distribution, yaw excitation, valid sensor duration, lidar point and overlap support, timestamp support, and critical findings.
Camera quality, radar support, and cross-modal time-alignment support are later-track features.
Profile logic must decide which features are mandatory.

### Follow-on repeated off-map trajectory mining

Only high-quality trajectory intervals with observable positioning support may enter off-map clustering.
Candidate intervals must be direction aware.
Clustering may use resampled polyline distance, Fréchet distance, heading, and endpoint proximity.
A topology hypothesis requires a frozen minimum number of independent traversals.
A robust fitted corridor may be compared with the source road graph.
Outputs are review hypotheses such as possible missing connection, geometry disagreement, or map-match ambiguity.
CartoSentry must never automatically edit the source road graph.

### Follow-on synthetic topology benchmark

The topology benchmark must remove an edge, perturb a road geometry, add a parallel road, and alter a connection in generated graphs.
It must generate noisy repeated trajectories through both changed and unchanged regions.
Metrics must include hypothesis precision, hypothesis recall, endpoint localization error, geometry corridor error, and false hypotheses per unchanged kilometer.

RoadRunner is prior art for using trajectory connectivity to protect topology in noisy and overlapping-road settings.
The public source is [RoadRunner: Improving the Precision of Road Network Inference from GPS Trajectories](https://mapster.csail.mit.edu/roadrunner/roadrunner.pdf).

## Readiness policy engine

### Policy separation

Detectors generate evidence.
Profiles declare mapping requirements.
The policy engine evaluates profiles against evidence.
No detector may contain hidden profile-specific sequence acceptance logic.

### Profile schema

Every profile must include an identifier, semantic version, supported adapter capabilities, required modalities, required detectors, window and spatial aggregation rules, mandatory requirements, optional review features, and charter references.
Profiles must not embed executable Python or arbitrary expressions.
The allowed predicate language must be small, typed, unit aware, and testable.

### V1 and follow-on profiles

`structural-preflight-v1` validates that supported data can be parsed and indexed.
`hdmap-lidar-v1` requires trajectory, timing, lidar integrity, and map coverage evidence.
The later modality track may add `semantic-camera-v1` for camera quality and camera-to-lidar consistency.
The later modality track may add `radar-redundancy-v1` for radar evidence without pretending radar is a pointwise lidar substitute.

### Tri-state evaluation

A mandatory observable failure makes the affected scope `FAIL`.
If no mandatory requirement fails but at least one lacks observable support, the affected scope is `UNKNOWN`.
Only complete observable success makes the affected scope `PASS`.
Sequence-level state must be derived from the requested spatial scope and may report partial accepted coverage rather than collapsing all information into one label.

### No compensation rule

A weighted score may prioritize manual review.
A score must never convert a mandatory `FAIL` or `UNKNOWN` into `PASS`.
Mutation tests must demonstrate that every mandatory predicate independently affects the verdict.

### Evidence completeness

Every evaluated requirement must produce an evaluation record even when evidence is missing.
The record must include policy path, evidence queries, selected observations, value, units, threshold, observability, state, and explanation template inputs.

### Freeze rule

The profile schema may evolve during development.
All detector thresholds, event-consolidation parameters, readiness rules, and evaluation slices must be frozen before the final-test partition is run.
A change after unblinding invalidates the final result and requires a newly versioned untouched test partition or an explicitly labeled exploratory result.

## Recollection planning

### Problem definition

Failed or unknown directed road bins become `RecaptureRequirement` records after profile-specific remediation logic.
A requirement contains directed arc, start and end offsets, required modality, traversal direction, minimum continuous observation distance, sensor warm-up distance, priority, and reason.
Only requirements selected by the user or profile enter planning.

### Graph preparation

The route graph must respect directed arcs and every restriction represented by the pinned graph-import profile.
The planner must snap the depot to a validated graph position.
Required partial bins must be expanded to traversable arc segments without losing proof of the original required interval.
Unreachable requirements must be identified before optimization.

### Exact validation solver

Tiny scenario fixtures use Dijkstra or A-star over the expanded state `(graph_node, incoming_arc, requirement_automata_state)` after every required partial segment has been split into explicit directed arcs.
Each `RecaptureRequirement` owns a deterministic automaton with states for sensor warm-up accumulation, entry into the required interval in the required direction, contiguous valid observation distance, satisfaction, and reset after any invalidating gap, reversal, disallowed transition, or profile-specific interruption.
The automata state must retain bounded progress distance rather than only one covered bit.
Traversing an arc applies its length, direction, restriction status, turn transition, and observation eligibility to every requirement automaton, including requirements encountered on connector paths.
The incoming arc is part of state because turn restrictions and turn costs depend on the predecessor transition.
The initial state is `(depot, NO_INCOMING_ARC, all_automata_reset)` and the closed-route goal is `(depot, any_valid_incoming_arc, all_reachable_requirements_satisfied)`.
Nonnegative traversal and turn costs make this finite state-space search exact, and predecessor reconstruction emits the complete source-graph traversal.
The implementation must declare a strict maximum required-arc count and estimated state-memory limit before allocating the exact search.
Larger inputs must use the production heuristic rather than silently exhausting memory.
Exhaustive route enumeration on very small graphs independently checks the state-space solver.

### Production heuristic

The scalable baseline must compute shortest-path closure between required components, connect components deterministically, balance directed degree, construct an Eulerian traversal where possible, and apply local route improvements.
Turn costs and U-turn penalties must be included when the graph supplies them.
The heuristic must expose a lower bound or exact small-case comparison rather than claiming optimality generally.

### Budgeted planning

If every requirement cannot fit within the declared time or distance budget, the planner must solve a deterministic prioritized partial-coverage problem.
It must not silently drop requirements.
The result must list covered, deferred, and unreachable requirements with their value and cost.
Each reachable requirement has a frozen nonnegative integer priority weight.
The primary objective is to maximize the sum of weights of satisfied requirement automata subject to the declared route-cost budget and depot constraints.
The first tie-breaker minimizes total route cost.
The second tie-breaker minimizes deadhead cost.
The final tie-breaker is lexicographic order of the canonical arc-identifier sequence.
Unreachable requirements are excluded from the optimization denominator but must appear separately in output.
Deferred reachable requirements retain their weight and a machine-readable reason.

### Independent validator

The validator must reconstruct the route on the source graph.
It must prove continuity, graph-profile-valid direction and transitions, depot start and return, declared cost, budget compliance, and requirement-automaton satisfaction.
The validator must not reuse optimizer-internal coverage flags.
A plan failing validation must never be exported as accepted.

### Route artifacts

The planner must emit `recapture_plan.json`, `route.geojson`, optional `route.gpx`, `coverage_proof.json`, and `route_comparison.json`.
The report must show why each detour exists.

The graph problem is grounded in the directed rural postman formulation described in [Parameterized Rural Postman Problem](https://arxiv.org/abs/1308.2599).

## Accepted-data packaging and upload

This entire section is a post-V1 publication-track contract.

### Bundle manifest

The accepted-data bundle must be an immutable manifest over source intervals and derived artifacts.
It must identify the source sequence hash, selected profile, accepted intervals, excluded intervals, required calibration, derived metric artifacts, and optional raw-data shards.
The bundle must never rewrite the original recording in place.

### Sharding

Raw publication, when explicitly enabled, must use bounded content-addressed shards.
Shard boundaries must not split a source record without a reconstruction contract.
Each shard must have uncompressed and compressed hashes, logical contents, source lineage, and byte count.
The target shard size must be a frozen configuration rather than a hidden constant.

### Multipart upload

M10 must compare a Python Boto3 implementation with a native libcurl plus SigV4 implementation only through a bounded spike.
The selected implementation must support multipart upload, retries with backoff and jitter, idempotent resume, per-part state, final object verification, cancellation, and a local MinIO target.
The unselected implementation and its dependencies must be removed.
Every remote object key must be content addressed by the full SHA-256 digest of the exact uploaded bytes plus a nonsemantic media-type suffix.
Creation must use a conditional no-clobber request equivalent to `If-None-Match: *` whenever the target supports it.
If the object already exists, the client must verify full byte count and full SHA-256 identity before treating it as complete.
An existing object with the same key and different bytes is a terminal integrity failure and must never be overwritten.
Multipart resume state must bind destination, upload identifier, object key, exact total byte count, part size, ordered part numbers, each part byte range, each part SHA-256, remote part identifier, source file full hash, and selected transport version.
Resume must list remote parts and compare every bound field before uploading another byte.
A mismatch must abandon that upload identifier and start a new content-addressed upload without overwriting a complete object.
ETag is transport metadata and must never substitute for SHA-256 identity.

### Publication visibility

Consumers must not discover an accepted bundle until every required object is uploaded and the final manifest is committed.
The manifest commit is the publication point.
An interrupted upload may leave unreachable multipart parts but must not expose a complete bundle.
Cleanup of abandoned remote uploads must be an explicit administrative command.
The final manifest key must itself be content addressed and created through conditional no-clobber semantics.
An optional human-readable release pointer may be updated only through an explicit compare-and-swap operation against its previously observed version and is never the source of artifact identity.

### Upload fault tests

Tests must interrupt before the first part, after arbitrary parts, before completion, after remote completion but before local state commit, and during manifest publication.
Resumption must produce the same final object hashes as an uninterrupted upload.
The implementation must detect a remote object with the correct key but wrong contents.

## Report and user interface

### Product surface

The report must be usable by a reviewer who did not run the analysis.
The initial page must answer whether the requested scope passed, failed, or remains unknown.
It must show accepted and rejected road distance, critical findings, missing evidence, and recollection cost.

### Required views

The overview view shows sequence identity, profile, state, coverage summary, and reproducibility facts.
The map view colors directed road bins by readiness and supports filtering by modality and finding.
The timeline view shows stream availability, findings, and accepted or excluded intervals.
The V1 sensor evidence view shows lidar BEV or sampled 3D evidence and trajectory plots at a selected time.
Camera and radar evidence views belong to the later modality track.
The policy view shows every mandatory requirement and why it passed, failed, or remained unknown.
The recollection view shows required arcs, the planned route, deferred requirements, and greedy comparison.

### Data loading

The UI must load bounded downsampled evidence artifacts rather than raw full-resolution sensor streams.
Large point clouds must be downsampled deterministically with the method and seed recorded.
The browser must never receive local absolute source paths.

### Report architecture decision

V1 uses one self-contained static report with bounded, content-addressed assets.
The loopback helper may serve those immutable files for browser compatibility but must expose no API, mutation, file-browsing, upload, or non-loopback capability.
FastAPI and Uvicorn are excluded from V1 unless a later threat-model decision authorizes a stateful follow-on service.
The V1 report must support a fully offline synthetic demo.

### User-visible testing

End-to-end browser tests must inspect actual rendered state, not only API responses.
Golden screenshots must cover pass, fail, unknown, mixed road coverage, and recollection views.
Accessibility checks must verify keyboard navigation, color-independent verdict labels, contrast, and descriptive text for critical graphics.
The UI must make hypotheses visually distinct from confirmed findings.

## Security and data-handling model

### Protected assets

The protected assets are raw vehicle recordings, precise location traces, local file paths, access credentials, signed upload requests, report tokens, unpublished derived evidence, and benchmark integrity.
The repository itself must contain no private company data or internal documentation.

### Trust boundaries

Input recordings and road graphs are untrusted files.
Adapter parsers must assume malformed lengths, dimensions, timestamps, and numeric fields.
The report browser is less trusted than the local analysis process.
The upload destination is an external boundary even when tests use local MinIO.
Downloaded public data must be verified before use.

### Parser safety

Every binary parser must perform checked arithmetic before allocation or pointer movement.
Array dimensions and record counts must have explicit maximums.
Invalid UTF-8, path traversal, symlinks, sparse files, decompression bombs, and deeply nested metadata must be handled safely.
Archive extraction must reject absolute paths and parent traversal.
Fuzz targets must cover all custom binary and manifest parsers.

### Filesystem safety

All outputs must remain under the resolved run directory.
The implementation must reject output paths that escape through symlinks.
Source data must be opened read only.
No cleanup command may accept a filesystem root, home directory, unresolved environment variable, or unvalidated glob as a recursive target.

### Report security

The V1 report helper binds only to loopback and rejects wildcard, LAN, public, proxy-derived, or hostname-resolved non-loopback addresses.
It must send a restrictive Content Security Policy, `X-Content-Type-Options`, and a no-referrer policy.
The client must not load third-party scripts, fonts, tiles, or analytics.
State-changing report endpoints are forbidden in V1.
Any later non-loopback server requires a separate authenticated threat model, authorization decision, origin and host validation, cross-site request protections, transport-security contract, and qualification before that capability ships.

### Credential handling

Credentials must be obtained through standard environment or SDK provider chains and must never be written into run artifacts.
Logs must redact access keys, bearer tokens, signed query parameters, session tokens, and credential-bearing URLs.
Test credentials must be obviously fake and scoped only to local MinIO.

### Location privacy

Exported public demo artifacts must use public-dataset locations and attribution.
The report must have a portable export mode that strips local absolute paths and machine identifiers.
An optional location-redaction feature is out of scope for V1 and must not be implied.

### Dependency and supply-chain checks

Repository-owned local verification must check Python lock consistency, scan Python and system dependency advisories, and build from the committed lock.
Release artifacts must include an SBOM and checksums.
Third-party notices must be generated from the actual selected dependency set and reviewed before release.

## Deterministic fault laboratory

### Purpose

The fault laboratory provides known ground truth that real public recordings do not provide for sensor corruption.
It must alter supported source representations while preserving every unrelated field and recording exact provenance.
It must support both fully synthetic sequences and copied local public clips without redistributing the latter.
The source-group partition must be fixed before the first fault target, interval, operator, or severity is chosen.
Every fault derivative inherits its immutable source-group partition.

### Named V1 fault matrix

`benchmarks/fault_matrix_v1.yaml` with matrix identifier `cartosentry-v1-core` is the only release-blocking fault matrix for portfolio V1.
It is limited to the V1 structural, timestamp, trajectory, calibration, and lidar operators named below.
The allowed V1 structural and timestamp operators are `drop_frame`, `drop_burst`, `duplicate_frame`, `reorder_frames`, `constant_time_offset`, `linear_clock_drift`, `timestamp_reset`, `truncate_file`, and `flip_bytes`.
The allowed V1 trajectory operators are `position_jump`, `position_freeze`, `position_bias`, and `position_drift`.
The allowed V1 calibration operators are `remove_calibration`, `replace_calibration_id`, and `lidar_extrinsic_perturb`.
The allowed V1 lidar operators are `lidar_scan_drop`, `lidar_ring_drop`, `lidar_sector_drop`, `lidar_nonfinite`, `lidar_range_scale`, `lidar_density_reduce`, and `lidar_point_time_corrupt`.
No other operator may appear in `cartosentry-v1-core` or block the V1 release.
IMU, camera, radar, cross-modal, and topology fault families must live under `benchmarks/follow_on/fault_matrices/` with separate charters, partitions, and release tags.

### Pre-injection truth

Before injection, the pipeline must freeze a clean-source truth artifact that records structural validity, trajectory support, confident HMM path, directed-bin traversal intervals, ambiguous or off-map exclusions, and profile readiness for every candidate target interval.
Synthetic fixtures must derive clean bin truth from the analytic route independently of the production HMM.
Public clips may use only predeclared, manually adjudicated, confidently matched clean bins for spatial fault evaluation.
An injected fault may not redefine its expected bins by running the faulted trajectory through the same matcher being evaluated.
The fault manifest must reference the clean-source truth hash and compute expected affected bins from the frozen pre-injection mapping plus the operator interval.
Pre-existing failed, unknown, ambiguous, or off-map bins must be excluded from clean-bin false-positive and unaffected-distance denominators.

### Fault manifest

Each fault event must record operator identifier, semantic version, seed, source identity, source group, inherited partition, clean-source truth hash, target streams, target fields, source interval, severity parameters, resulting artifact hashes, expected detector capabilities, and expected affected road bins.
The manifest must distinguish an injected observable fault from an injected fault that intentionally lacks sufficient evidence.

### V1 structural and timestamp faults

- `drop_frame` removes one selected frame and updates only the container metadata necessary to keep the source parseable when the test requires schema preservation.
- `drop_burst` removes a contiguous frame interval.
- `duplicate_frame` duplicates content and optionally timestamp according to the selected variant.
- `reorder_frames` changes source order without changing capture time.
- `constant_time_offset` applies a declared offset to a selected source clock while preserving the original raw value and recording the injected correction separately.
- `linear_clock_drift` applies a deterministic affine time distortion around a declared pivot.
- `timestamp_reset` introduces a discontinuity to a declared earlier time.
- `truncate_file` removes a declared number of trailing bytes.
- `flip_bytes` corrupts declared byte ranges using a seeded operator.
- `remove_calibration` removes or invalidates a calibration reference.
- `replace_calibration_id` links a stream to a mismatched calibration object.

### V1 trajectory faults

- `position_jump` applies a step translation over a declared interval.
- `position_freeze` repeats position while retaining selected time fields.
- `position_bias` applies a constant local translation.
- `position_drift` applies a ramp or smooth drift.

### Follow-on IMU faults

- `imu_axis_stuck` holds one accelerometer or gyroscope axis constant.
- `imu_saturation` clamps one or more axes.
- `imu_bias` adds a constant or temperature-like slow component.
- `imu_noise_burst` adds seeded band-limited noise.
- `imu_time_shift` shifts IMU time without changing samples.

### V1 lidar and calibration faults

- `lidar_scan_drop` removes complete scans.
- `lidar_ring_drop` removes selected model elements or rings.
- `lidar_sector_drop` removes points in selected azimuth sectors.
- `lidar_nonfinite` injects NaN or infinity into selected fields.
- `lidar_range_scale` scales valid ranges while preserving direction.
- `lidar_density_reduce` samples points using a seeded spatially uniform or sector-biased rule.
- `lidar_extrinsic_perturb` changes the referenced rigid transform.
- `lidar_point_time_corrupt` shifts, reverses, or clamps per-point time.

### Follow-on camera faults

- `camera_freeze` repeats decoded frame content across a declared moving interval.
- `camera_corrupt` creates a controlled undecodable payload or partial decode failure.
- `camera_blur` applies a declared kernel to selected frames.
- `camera_underexpose` applies a declared intensity transformation.
- `camera_saturate` clips declared channels or regions.
- `camera_time_shift` changes capture time.
- `camera_extrinsic_perturb` changes the referenced transform.

### Follow-on radar faults

- `radar_azimuth_drop` removes selected polar rows or marks them empty while preserving the declared variant contract.
- `radar_azimuth_repeat` repeats encoder and return data.
- `radar_blank_sector` zeroes a declared sector.
- `radar_range_offset` shifts return bins with explicit boundary behavior.
- `radar_time_shift` changes scan or azimuth time.

### Fault interaction policy

Single-fault evaluation is mandatory before composite faults.
V1 composite faults must be limited to a frozen small matrix whose members are all allowed by `cartosentry-v1-core`.
Follow-on composite faults must remain in their own track-specific matrices.
The benchmark must not create every Cartesian product.
Fault severity levels must be selected before final evaluation and must include below-threshold, near-threshold, and clearly detectable cases.

### Golden fixture generator

The V1 synthetic generator must produce only a directed road graph, continuous trajectory, rig and lidar calibration, spinning lidar, and known static landmarks.
V1 trajectory and lidar samples must be generated from one shared time and frame model so that clean alignment is known exactly.
The generator must support straight roads, turns, stops, parallel roads, ramps, overpasses, and off-map connections.
It must expose geometric structure and motion excitation controls so V1 observability behavior can be tested.
IMU, camera, radar, cross-modal, and topology-specific synthetic generation belongs only to the corresponding follow-on track.

## Evaluation methodology

### Partitioning

The `development` partition is used for implementation and detector debugging.
The `threshold_calibration` partition is used to choose frozen thresholds and event-consolidation rules.
The `policy_tuning` partition is used to assemble readiness profiles after detector thresholds are frozen.
The `final_test` partition is used once for the reported result.
The `final_test` partition must use source groups and synthetic scenario and fault-seed families disjoint from every earlier partition.
All derivatives inherit source-group membership as defined in the dataset contract.

Before unblinding, `benchmarks/fallback_tree.yaml` must freeze a finite ordered tree of primary and narrower claims.
Every branch must declare its exact population, supported fault family and severity, metric, statistical bound, gate, multiplicity handling, and claim wording.
The fallback tree may omit a failed primary claim in favor of a predeclared narrower claim, but it may not invent a new slice, threshold, severity boundary, metric, or claim after results are visible.

The one-shot release evaluation must run from one clean, fully committed release-candidate commit with no working-tree changes.
The unblinding record must bind the full commit hash, full source-group manifest hash, full split hash, charter hash, profile hashes, fallback-tree hash, dependency lock hashes, and execution environment.
No final-test frame, sequence summary, intermediate artifact, or result may be inspected before that binding is recorded.
The exact release-candidate commit may execute the final test only once.
A transient infrastructure failure that occurs before any metric or decoded final data becomes visible may resume only from content-verified stage checkpoints of the same commit and configuration.
An implementation defect, detector failure, or visible partial result invalidates reuse of that final partition for any fixed code or tuned rule.
A corrected implementation requires a genuinely untouched replacement final partition, or its result must be labeled exploratory and excluded from release claims.

### Units of analysis

Detector event metrics use injected events as the unit.
False-critical rate uses clean sensor-hours as the unit.
Spatial localization uses directed road bins as the unit.
Readiness metrics use profile requirements and road bins as units.
Routing metrics use complete scenario graphs as the unit.
Downstream utility uses independent sequences or drives as the unit.
Performance uses complete frozen workloads as the unit.

Frames from one sequence must not be treated as independent samples in confidence intervals.
The resampling cluster is `source_group_id` for real and injected sensor metrics, synthetic scenario family for generated sensor and map-matching metrics, scenario-graph family for routing, independent drive for downstream utility, and complete measured run for performance.
Every observation from a selected cluster must remain together during resampling.
The charter must define the denominator, eligibility rule, cluster identifier, minimum cluster count, minimum event or distance support, and missing-data treatment for every confirmatory metric.
No confirmatory claim may be made when its frozen minimum support is not met.

### Confirmatory support and decision bounds

V1 starting support is at least `12` independent source or scenario groups and at least `30` eligible injected events for each claimed fault-family and severity stratum.
V1 clean-harm support is at least `12` independent clean groups and at least `3` eligible clean sensor-hours after pre-existing failed, unknown, ambiguous, and off-map intervals are excluded.
The public HMM claim requires at least `12` independently adjudicated source groups and reports matched distance as well as group count.
The routing comparison requires at least `30` scenario graphs from at least `12` independently generated graph families.
The follow-on downstream claim requires at least `8` independent source groups with all comparison arms available.
M0 may increase these supports, but may lower them only by removing the associated confirmatory claim.

Every confirmatory estimate must report its point estimate, eligible support, cluster count, and two-sided `95 percent` confidence interval.
Benefit gates such as recall, F1, map-match accuracy, route improvement, throughput, and downstream recovery use the one-sided `95 percent` lower confidence bound.
Harm gates such as false-critical rate, false pass, unaffected-distance discard, graph-profile-invalid traversal, discontinuity, peak memory, and failure rate use the one-sided `95 percent` upper confidence bound.
The clustered bootstrap must resample whole clusters with replacement for `10000` frozen replicates and a frozen seed.
If fewer than `12` eligible clusters remain, the result is descriptive and cannot pass a confirmatory gate.

A zero observed failure is not a zero failure-rate estimate.
For a predeclared independent cluster-level binary failure endpoint with zero failures in `n` clusters, the one-sided `95 percent` Clopper-Pearson upper bound is `1 - 0.05^(1/n)`.
For nonzero failures, use the exact one-sided Clopper-Pearson bound on the independent cluster-level endpoint.
For correlated event counts or rates, use the clustered bootstrap upper bound instead of treating events as independent Bernoulli trials.
The mandatory generated false-pass gate requires zero observed failed scenario groups, at least `30` independent scenario families, and a one-sided upper bound no greater than `0.10`.

All confirmatory metric families and any Holm adjustment or simultaneous interval method must be listed in the frozen charter.
Unadjusted additional slices must be labeled exploratory.

### Detector metrics

Event precision and recall must use an interval-overlap matching rule frozen in the charter.
Interval intersection over union must be reported.
Start-time and end-time absolute errors must be reported.
Severity-stratified recall must be reported.
False critical findings per clean sensor-hour must be reported.
Observability accuracy must be reported on synthetic observable and nonobservable cases.

### Spatial metrics

Affected-bin precision, recall, and F1 must compare finding road bins with injected-event ground truth.
Along-route boundary error in meters must be reported.
Off-map and ambiguous intervals must be evaluated separately from matched road intervals.

### Readiness metrics

Policy mutation tests must delete or invert one mandatory evidence item and verify the expected verdict transition.
The benchmark must report mandatory-requirement false pass, false fail, and false unknown rates on generated truth.
Real-data readiness results without independent truth must be reported as case studies rather than classification accuracy.

### Map-matching metrics

Synthetic cases must report directed arc accuracy, path edit distance, off-map precision and recall, and ambiguity detection.
The public route review set must report manually adjudicated directed arc agreement and the unresolved fraction.
Manual adjudication instructions must be frozen before review.

### Follow-on topology-hypothesis metrics

The synthetic topology suite must report hypothesis precision, recall, endpoint error, fitted-corridor error, and false hypotheses per unchanged kilometer.
The public dataset may provide qualitative examples but must not be used to claim ground-truth topology correction without authoritative labels.

### Route metrics

Tiny graphs must compare route cost with the exact optimum.
Every plan must report required reachable coverage, graph-profile-invalid traversals, continuity errors, budget status, route length, route duration, deadhead distance, and solver time.
Generated nontrivial cases must compare the production heuristic with a deterministic greedy nearest-requirement baseline.
The benchmark must report median and per-scenario improvement rather than only the best example.

### Downstream usefulness probe

This section defines a follow-on claim track and cannot block portfolio V1.
The downstream probe must be frozen before final evaluation.
It may use a pinned public lidar registration or odometry implementation as an external evaluator.
The probe is not part of CartoSentry and must be identified as an evaluation dependency.

The comparison arms are accept all, oracle fault removal, CartoSentry filtering, and random removal of the same usable distance or frame count.
Random removal must use multiple recorded seeds.
Metrics may include relative-pose translation error, rotation error, registration failure rate, map thickness, or localization success according to the selected probe.
The final claim requires a practically meaningful improvement and must not rely only on a p-value.

One primary sequence-level loss metric with lower values meaning better performance must be frozen before unblinding.
If the natural metric is higher-is-better, the protocol must freeze a monotone loss transformation before evaluation.
For each eligible sequence, define `L_all`, `L_filter`, and `L_oracle` for accept-all, CartoSentry filtering, and oracle removal.
The recovery fraction is `(L_all - L_filter) / (L_all - L_oracle)`.
A negative recovery fraction means filtering harmed the downstream result and values above one remain uncapped in machine-readable output.
If `L_all - L_oracle` is nonpositive or no greater than the frozen practical-resolution epsilon, that sequence is not informative for recovery and must be excluded with a recorded reason before aggregation.
The minimum informative-sequence support must still be met after exclusions.
Evaluator crashes and nonfinite outputs must receive the predeclared worst loss rather than disappear through complete-case filtering.

Unaffected usable-distance discard is the clean, pre-injection eligible distance removed by CartoSentry divided by all clean, pre-injection eligible distance.
Its denominator must exclude pre-existing failed, unknown, ambiguous, off-map, and unsupported bins according to the frozen clean-source truth artifact.
The recovery claim uses its one-sided lower bound, while unaffected-distance discard and evaluator failure use their one-sided upper bounds.

### Statistical method

Bootstrap confidence intervals must follow the cluster, support, and bound-direction contract above.
Frame-level and event-level resampling that breaks source groups is forbidden.
The bootstrap seed, `10000` replicate count, confidence level, estimator, missing-cluster behavior, and fallback for degenerate resamples must be frozen.
Multiple detector slices must be labeled exploratory unless they belong to a predeclared confirmatory family with its frozen multiplicity procedure.

### Performance method

Performance runs must record CPU model, core count, memory, storage type, operating system, compiler, build flags, Python version, dependency lock hash, input hash, and thermal or power notes where available.
Warmup and measured repetitions must be frozen.
Report wall time, CPU time, peak RSS, bytes read, frames or seconds processed, queue backpressure, and output bytes.
Performance claims must use release builds with assertions configured according to the shipped profile.

## Numerical acceptance charter

`benchmarks/numerical_charter.yaml` must own every numerical gate.
The following values are starting values for M0 calibration and are not final until the charter is frozen.
Unless a deterministic exhaustive gate explicitly says otherwise, a minimum benefit threshold applies to the one-sided lower confidence bound and a maximum harm threshold applies to the one-sided upper confidence bound.
Passing point estimates without the required support and bound is insufficient.

### Coordinate and transform starting values

- WGS84 to local-world to WGS84 horizontal round-trip error must be at most `0.001 m` on synthetic points within the supported region.
- `SE(3)` composition and inverse point round-trip error must be at most `1e-9 m` in float64 synthetic tests.
- Rotation orthonormality Frobenius error must be at most `1e-9` after validated construction.
- Quaternion norm deviation before normalization may be at most `1e-6`, while larger deviations must be rejected.
- CPU and optional GPU local point outputs must agree within `1e-4 m` unless a detector-specific tighter tolerance is frozen.

### Time starting values

- Persisted timestamp conversion must round trip exactly at integer precision.
- The common resampling grid for the IMU and trajectory alignment spike starts at `200 Hz` or the highest supported common rate not exceeding `200 Hz`.
- The alignment lag search starts at `[-500 ms, 500 ms]`.
- An injected observable offset of at least `30 ms` and at most `300 ms` must have median absolute estimation error no greater than `10 ms`.
- At least `95 percent` of explicitly low-excitation synthetic windows must be classified `NOT_OBSERVABLE` rather than passed.
- A passing observable time offset starts with an absolute maximum of `15 ms`, subject to M0 evidence.

### Structural detector starting values

- Structural corruption and timestamp-fault event recall must be at least `0.95` at supported severities.
- Structural corruption and timestamp-fault event precision must be at least `0.95` on the frozen fault corpus.
- A parser must detect every injected truncation that removes bytes from a required record.
- Parser fuzz targets must complete the frozen local qualification duration with no crash, sanitizer finding, timeout amplification, or unbounded allocation.

### Content detector starting values

- Supported V1 lidar and trajectory fault recall must have a one-sided lower bound of at least `0.85` at predeclared detectable severities.
- Camera, radar, and cross-modal thresholds belong to their follow-on charter and cannot block V1.
- False `CRITICAL` findings must not exceed `1.0` per clean sensor-hour on the frozen clean corpus.
- Event boundary median absolute error must be at most one detector stride.
- Spatial affected-bin F1 must be at least `0.90` on matched injected faults.

### Map matching starting values

- Directed-arc accuracy must be at least `0.95` on the synthetic suite.
- Off-map state F1 must be at least `0.90` on synthetic missing-road cases.
- Every hand-authored tiny graph path must match exactly unless the case is deliberately ambiguous.
- At least `85 percent` of selected moving public trajectory distance must have an adjudicable confident match for the road-readiness demonstration.
- If public confident coverage is lower, M0 must change the route or road graph rather than lowering the gate without evidence.

### Topology starting values

- These gates belong to the topology follow-on and cannot block V1.
- Synthetic topology-hypothesis precision must be at least `0.90`.
- Synthetic topology-hypothesis recall must be at least `0.80` for predeclared supported mutations.
- Median endpoint localization error must be at most one road-bin length.
- False hypotheses must be no more than `0.1` per unchanged synthetic kilometer.

### Readiness starting values

- Mandatory policy mutation tests must have `100 percent` expected verdict transitions.
- Generated truth must have zero false pass for mandatory critical faults.
- Missing mandatory evidence must produce `UNKNOWN` in `100 percent` of schema-driven unit cases.
- Every verdict requirement must have an evidence or missing-evidence record.

### Routing starting values

- The route validator must cover `100 percent` of required reachable arcs in accepted unbudgeted plans.
- The route validator must report zero graph-profile-invalid directions or transitions and zero discontinuities.
- The exact solver must match brute-force optimal cost on every tiny fixture.
- The production heuristic must be no worse than greedy on every frozen standard scenario.
- The production heuristic must reduce route distance by at least `10 percent` at the median across the generated nontrivial suite.
- A budgeted plan must account for `100 percent` of requirements as covered, deferred, or unreachable.

### Performance starting values

- Structural preflight must process at least `3.0` times recording real time on the frozen Linux CPU host.
- Full `hdmap-lidar-v1` analysis must process at least `1.0` times recording real time on the frozen Linux CPU host.
- Peak RSS starts with a limit of `2.5 GiB` for the selected public-full workload.
- Peak RSS growth from a one-minute to a ten-minute logically repeated workload must be less than `15 percent` after steady-state buffers are allocated.
- Interrupted and resumed semantic artifacts must match uninterrupted semantic artifact hashes exactly.

### Downstream utility starting values

- These gates belong to the downstream follow-on and cannot block V1.
- The one-sided lower bound of recovery fraction must be at least `0.50` on the frozen primary metric.
- The one-sided upper bound of unaffected usable-distance discard must be at most `0.10` on the injected-fault corpus.
- The one-sided lower bound of paired improvement over equal-size random removal must be greater than `0`.

### Optional CUDA starting values

- CUDA and CPU detector outputs must pass the detector-specific parity tolerances.
- The optional GPU backend must achieve at least `1.5` times end-to-end speedup for the selected lidar-heavy stages on the frozen GPU workload.
- The speed comparison must include transfers and synchronization.
- If the gate fails, CUDA remains experimental and no acceleration claim may appear in primary portfolio materials.

### Freeze procedure

M0 converts the starting values into immutable charter version `v0` after the feasibility and calibration spikes.
A pre-unblinding revision must create a new version instead of rewriting a previously recorded charter.
Every revision must record rationale, affected detectors, data partition, expected risk, predecessor hash, and new hash.
The release-candidate charter and profiles must receive a final freeze before the final-test partition is unblinded.
No threshold, rule, slice, or metric may change after that unblinding without invalidating the result under the declared failure policy.
Every benchmark report must include the exact charter version and hash it evaluated.

## Clean developer commands

The exact commands may be adjusted during M0 if the selected tools require it, but the final repository must preserve this small command surface.

```bash
uv sync --frozen --all-extras --dev
cmake --preset dev
cmake --build --preset dev -j 4
ctest --preset dev --output-on-failure
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy python/cartosentry
```

The full local verification command must be one wrapper with no hidden remote dependency.

```bash
uv run python scripts/run_benchmarks.py --suite synthetic-ci
```

The public smoke command must verify local public-data hashes before execution.

```bash
uv run python scripts/verify_public_data.py --tier public-smoke
uv run python scripts/run_benchmarks.py --suite public-smoke
```

The synthetic user demo must be one command.

```bash
./scripts/run_demo.sh
```

The release build must produce a wheel and source distribution through the configured PEP 517 backend.

```bash
uv build --wheel --sdist
```

## Local verification design

### Foundational verification

The repository-owned local gate must run formatting, linting, type checking, schema validation, C++ warnings-as-errors, C++ unit tests, Python unit tests, synthetic adapter integration tests, synthetic end-to-end tests, and policy mutation tests.
Documented clean-environment qualification must cover macOS ARM64 and Linux x86-64 when the applicable milestone requires both environments.
Compiler coverage must include Apple Clang and a supported Linux Clang or GCC.

### Sanitizer verification

Linux Clang qualification must run AddressSanitizer and UndefinedBehaviorSanitizer.
ThreadSanitizer must run the deterministic scheduler and concurrency tests in a separate job because it cannot be combined with AddressSanitizer.
Sanitizer jobs must use synthetic fixtures and require no external data.

### Fuzz verification

The local gate runs a short fixed fuzz smoke corpus.
The extended suite runs longer fuzz targets for every custom binary parser and schema boundary when explicitly invoked.
Crashing inputs must become minimized regression fixtures only if their contents are safe to commit.

### Public-data verification

Routine local verification must not download multi-gigabyte public data.
An explicitly invoked public-data qualification may use a cache keyed by verified public object hashes.
Public-data qualification must fail closed if hashes or terms manifest entries do not match.

### Benchmark verification

Correctness benchmark results are blocking after their milestone freezes them.
Performance regressions initially produce reports because shared runners are noisy.
A dedicated or self-hosted frozen host is required before performance becomes a blocking release gate.

### Release verification

Release verification must build the documented wheel and source distribution in clean environments, run installation smoke tests, create checksums and an SBOM, and verify that no private paths, source data, tokens, or internal strings appear in the artifacts.
It must recompute every full-file SHA-256 in the transitive release evidence manifest from bytes rather than trusting development metadata or modification time.
The evidence manifest must bind the full Git commit hash, complete dependency-lock hashes, complete data and graph hashes, charter and fallback-tree hashes, artifact hashes, toolchain identity, and environment record.
Publishing to a package index or public release requires explicit user authorization and is not implied by this plan.

## Local and GPU hardware handoff

### Local-first rule

Every V1 component that does not require CUDA must be implemented, tested, benchmarked, and documented locally before any optional GPU qualification.
GPU absence must never be used to postpone V1 CPU correctness, schemas, the Boreas adapter, fault injection, policy logic, routing, or static-report work.

### GPU optionality

The default build uses `CARTOSENTRY_ENABLE_CUDA=OFF`.
The optional build must compile only CUDA-specific translation units when enabled.
CPU and CUDA backends must implement the same batch contract and produce detector-parity artifacts.

### Repository-owned scripts

`scripts/gpu/sync_gpu_host.sh` must synchronize only repository-controlled source and lock files to the configured remote workspace.
It must exclude local raw datasets, run outputs, credentials, `.git` internals not required by the chosen strategy, and unrelated files.
The script must support dry run, print exact included and excluded roots, and verify the remote target is not empty, root, home, or a broad shared directory.

`scripts/gpu/qualify_gpu_host.sh` must perform remote prerequisites, build, unit tests, parity tests, frozen GPU benchmark, Nsight profiling, artifact hashing, and result retrieval in one resumable workflow.
The script must not install or modify global drivers, CUDA toolkits, system packages, or shared machine configuration.
Missing prerequisites must produce one consolidated report.

`scripts/gpu/provision_verified_data.sh` must materialize the exact frozen remote workload without assuming that local raw data was synchronized with source code.
Its preferred mode downloads public objects directly on the remote host from the URLs and object keys in `data_manifest.yaml`.
Its fallback mode transfers explicit locally verified files through a bounded resumable transport after the user authorizes that transfer.
Both modes must verify expected full byte count and full SHA-256 before atomically publishing each object into a content-addressed remote cache.
The script must reject unmanifested data, wrong hashes, mutable destination aliases, insufficient space, and any destination that resolves to root, home, or a broad shared directory.
Raw public data must not be copied back in result retrieval.

### Remote qualification phases

Phase 1 records GPU model, driver, CUDA toolkit, compiler, CPU, memory, storage, and repository commit.
Phase 2 provisions and verifies the exact manifest-bound workload in the remote content-addressed cache.
Phase 3 configures the release CUDA build.
Phase 4 runs CPU and GPU correctness and parity tests.
Phase 5 runs the frozen lidar-heavy performance benchmark with warmup and repetitions.
Phase 6 runs bounded Nsight Systems profiling and selected Nsight Compute kernels if the tools exist.
Phase 7 packages machine-readable results, logs, profiles, and hashes.
Phase 8 retrieves and verifies every non-raw result artifact locally.

### Deferred hardware status

Before remote qualification, GPU-only gates must be marked `DEFERRED_HARDWARE`.
They must not be marked passed based on compilation, emulation, or CPU results.
The primary V1 release remains valid without the optional GPU backend.

### Resume behavior

Remote phases must have completion manifests and hashes.
A rerun must skip valid phases and restart only invalid or failed phases.
The local result must identify the exact commit and charter hash tested remotely.

## Documentation requirements

`README.md` must remain user-facing and concise.
It must lead with the problem, the one-command synthetic demo, a real report screenshot, verified claims, and limitations.
It must not read like an implementation diary.

`docs/architecture.md` must explain component boundaries and data flow.
`docs/contracts.md` must define timestamp, frame, transform, identifier, evidence, and verdict contracts.
`docs/benchmark_methodology.md` must define partitions, metrics, freeze policy, hardware, and statistics.
`docs/data_and_licenses.md` must document external data acquisition, attribution, terms, and redistribution decisions.
`docs/threat_model.md` must document assets, boundaries, threats, mitigations, and residual risk.
`docs/supported_profiles.md` must document what each profile can and cannot establish.
`docs/gpu_qualification.md` must document optional GPU prerequisites, commands, evidence, and fallback.

Architecture decisions that materially constrain later work must use short decision records under `docs/decisions/`.
Decision records are required for the `SE(3)` library, upload implementation, report architecture, downstream probe, and optional CUDA kernel selection.

## Milestone and commit discipline

Each submilestone below ends with the named focused commit only after its acceptance checks pass.
If implementation requires a smaller logically complete commit, it may be added without combining unrelated work.
Do not use a milestone commit to hide failing tests or unfinished code.
After every verified commit, push the current branch to `origin` and confirm that the remote contains the commit before continuing.
If a push fails, resolve it and retry instead of accumulating unpushed commits.
Any submilestone or milestone labeled `Follow-on` is a staged specification, not part of the first execution pass.
The implementation agent must skip it until M13 produces the qualified portfolio V1 release tag.
After V1, each follow-on track receives its own charter, untouched final partition, claim decision, and release tag.
The V1 execution order is M0 through M10 with every follow-on skipped, M11.1, all development-only CPU profiling and optimization, M13.1 through M13.3, immutable evaluation tag creation, M11.2, M11.4, M11.5, M11.6, and M13.4.
All implementation, CPU optimization, documentation and benchmark-summary templates, packaging, installed-artifact checks, and public-safe audit must be complete before the immutable V1 evaluation tag is created and pushed from an exact clean commit.
M11.2, M11.4, and M11.5 must check out and execute that exact tag without modifying the source tree or creating a source commit.
Their machine-readable results, logs, tables, figures, and environment records must be stored outside the repository as content-addressed release evidence whose manifest binds the full tag commit hash and every artifact SHA-256 digest.
M11.6 may verify and render only those bound artifacts and may not change source, profiles, templates, thresholds, workloads, code, or documentation.
M13.4 may attach that external content-addressed manifest and its artifacts to the exact immutable tag but may not create a different evaluated source commit.
No optimization, repair, threshold change, workload change, documentation-source update, or result-driven profile change is allowed after any final result is visible.
A performance failure must fail the affected claim or select only the first passing predeclared fallback profile whose metrics were produced by the same one-shot execution.
Any source change after unblinding requires a new immutable candidate tag and a genuinely untouched final partition before another release claim can be evaluated.

## Milestone 0: Retire feasibility risks and freeze the first contracts

### M0.1: Repository and public-source inventory

Create only the initial repository metadata, this plan, a public-source inventory, and a decision-log template.
Record the exact public sources that support timestamp, format, dataset, coordinate, map-matching, and routing decisions.
Add a repository string-scan rule that fails on configured private-domain patterns and local absolute paths.

Acceptance requires that no internal link, internal project name, private excerpt, or local personal context appears in tracked files.
Acceptance requires that all source claims in the plan resolve to public URLs.

Focused commit: `docs: add CartoSentry implementation plan`.

### M0.2: Dataset and license spike

Create `benchmarks/data_manifest.yaml` and `docs/data_and_licenses.md`.
Resolve official Boreas terms, object identities, attribution, expected sizes, and download behavior.
Select candidate clear-weather, precipitation or snow, repeated-route, and held-out drives through official metadata.
Download only the smallest metadata and sensor windows needed for the spike.
Measure actual V1 trajectory and lidar storage per second.
Verify that scripts can fetch selected objects without manual browser steps.
Pin an OpenStreetMap extract and attribution for the candidate route.

Acceptance requires every selected artifact to have a public source, terms reference, retrieval rule, partition, and SHA-256 identity.
Acceptance requires that no third-party raw data is tracked.
Acceptance requires a documented storage budget for `public-smoke`, `public-full`, and `gpu-perf`.

Kill or pivot if exact data terms cannot support the intended nonredistributed benchmark use.
Kill or pivot if the selected corpus cannot provide the required modality and time fields.

Focused commit: `docs: lock public data provenance`.

### M0.3: Toolchain compatibility spike

Create the minimal `pyproject.toml`, CMake project, CMake presets, dependency lock, C++ library, pybind11 module, and Python import smoke test.
Resolve exact compatible versions for Python, scikit-build-core, pybind11, CMake, compilers, Eigen, selected `SE(3)` implementation, GeographicLib, OpenCV, Arrow, SQLite, JSON, YAML, logging, and test dependencies.
Build an editable install and a wheel on macOS ARM64 and Linux x86-64.
Record the versions and supported ranges.
Delete all spike-only code that is not the selected foundation.

Acceptance requires `uv sync --frozen`, native CMake build, CTest, Python import, editable install, wheel build, and clean-wheel installation to pass.
Acceptance requires that the Python wheel can call one checked C++ function.
Acceptance requires one `SE(3)` implementation to be selected and the alternative removed.

Kill or pivot if the chosen core dependency set cannot build reproducibly on both mandatory platforms.

Focused commit: `build: establish portable C++ Python toolchain`.

### M0.3b: NCore follow-on feasibility spike

Without adding NCore to the V1 runtime dependency lock, inspect and install the current public `nvidia-ncore` wheel in isolated disposable environments on macOS ARM64 and Linux x86-64.
Load a permitted public or generated V4 sample and record Python, dependency, platform, timestamp, calibration, pose, camera, lidar, and radar capability gaps against the canonical contracts.
Record the tested wheel full hash, release, public source, license, and result in a decision record.
Delete the disposable environments and keep NCore out of V1 build and test paths.

Acceptance requires a clear feasible, blocked, or platform-limited decision for the post-V1 NCore track.
Failure or incompatibility must not block V1.

Focused commit: `spike: record NCore follow-on feasibility`.

### M0.4: Real-format adapter spike

Implement disposable or minimal production readers for one short Boreas interval covering the V1 trajectory, calibration, and lidar contract.
Print source frame identities, exact time fields, dimensions, calibration identities, and coordinate conventions.
Measure one-pass throughput and peak RSS.
Verify lidar per-point timing on actual bytes.
Verify that global coordinates remain stable in float64 and local coordinates are well conditioned.
Verify paired timestamp, ENU, latitude, and longitude fields on actual trajectory rows and record the radians-to-degrees conversion.
Cross-check the resulting WGS84 path against the source `route.html` polyline without using that visualization file as primary truth.

Acceptance requires that the same pinned clip yields identical normalized metadata on macOS and Linux.
Acceptance requires peak RSS below the provisional `2.5 GiB` budget.
Acceptance requires parser errors to identify the source key without leaking raw data.
Acceptance requires the normalized WGS84 trajectory to overlap the pinned road-graph region and the route cross-check residual to meet a frozen georeferencing tolerance.

Kill or pivot if the documented format and actual public bytes conflict in a way that cannot be resolved from authoritative sources.

Focused commit: `spike: validate Boreas sensor contracts`.

### M0.5: Algorithm observability spikes

Prototype trajectory interpolation, lidar point-time handling, lidar motion compensation, and lidar multi-frame alignment.
Use synthetic clean and perturbed data plus the development public clip.
Measure observability on straight, turning, static, sparse-structure, and moving intervals.
Prototype directed OSM graph import and HMM candidate generation on the selected route.
Measure confident map-match coverage without changing thresholds to fit isolated examples.
Prototype a tiny directed required-arc route solver and independent validator.

Acceptance requires injected point-time and trajectory perturbations to separate from clean development fixtures under observable motion and structure.
Acceptance requires at least `85 percent` candidate public moving distance to be adjudicable for confident road matching.
Acceptance requires the tiny route solver to match brute force.

Pivot the public route or graph extract if confident road coverage is below the gate.

Focused commit: `spike: retire mapping observability risks`.

### M0.6: Freeze evaluation charter `v0`

Validate the source-group assignments already frozen before M0.2 data inspection without moving any group, and create `benchmarks/split_manifest.yaml`, `benchmarks/numerical_charter.yaml`, `benchmarks/fault_matrix_v1.yaml`, and `benchmarks/fallback_tree.yaml`.
Set the fault-matrix identifier to `cartosentry-v1-core` and reject every operator outside its enumerated V1 allowlist.
Assign every real sequence and synthetic seed family to development, threshold calibration, policy tuning, or final test.
Select the event-overlap rule, bootstrap unit, bootstrap seed, bootstrap replicates, warmup, repetitions, performance hosts, and first profile thresholds.
Document every change from the starting values in this plan.
Record charter version `v0` and its immutable hash.

Acceptance requires that every derivative inherits its source-group partition and that final-test identities are mechanically inaccessible to ordinary development commands.
Acceptance requires every numerical gate to have a named charter key, units, rationale, and responsible metric.
Acceptance requires an explicit unblinding command that records the event.
Acceptance requires every allowed narrower release claim to exist in the frozen fallback tree before unblinding.

Focused commit: `test: freeze evaluation charter v0`.

## Milestone 1: Establish canonical contracts and generated truth

### M1.1: Time, frame, and transform contracts

Implement validated time, duration, frame-interval, named-frame, quaternion, rigid-transform, global-coordinate, and local-coordinate types.
Implement serialization and schema validation.
Add analytic tests for time conversion, transform composition, inversion, interpolation, and coordinate round trips.
Add tests for invalid quaternions, reflection matrices, large global coordinates, missing time epochs or references, incomparable clocks, and unsupported extrapolation.

Acceptance requires every numerical contract gate to pass in debug and release builds.
Acceptance requires no untyped floating-point seconds in persisted schemas.
Acceptance requires all transform tests to name source and target frames.

Focused commit: `feat: define time and frame contracts`.

### M1.2: Artifact schemas and identifiers

Implement JSON Schemas and Pydantic models for the sequence manifest, run, finding, readiness profile, recapture plan, and accepted-data bundle.
Implement deterministic identifiers and portable export redaction.
Generate representative valid and invalid examples.
Add cross-language round-trip tests between C++ JSON and Python Pydantic models.

Acceptance requires schema round trips to preserve semantic content.
Acceptance requires unknown required fields, wrong units, invalid enum values, and path leakage to fail validation.
Acceptance requires deterministic identifiers to remain stable across local roots.

Focused commit: `feat: add versioned artifact schemas`.

### M1.3: Synthetic trajectory and lidar generator

Implement the V1 synthetic world, directed graph, analytic trajectory, rig, spinning lidar, and known static landmarks.
Generate compact clean fixtures for straight, turn, stop and start, parallel road, ramp, overpass, and off-map connection scenarios.
Record generator version and seed in every fixture.
Commit only small fixtures required by tests.

Acceptance requires generated measurements to agree with the analytic world within the charter.
Acceptance requires clean trajectory-to-lidar alignment and per-point time to be known exactly.
Acceptance requires generated fixtures to be byte deterministic for a fixed version and seed.

Focused commit: `test: generate deterministic trajectory lidar fixtures`.

### M1.4: Fault-manifest foundation

Implement the fault-operator registry, typed parameters, immutable source-to-derived provenance, and fault manifest.
Add structural time, position jump, lidar point-time shift, lidar ring loss, lidar sector loss, and lidar calibration perturbation as representative V1 operators.
Add property tests that unrelated streams and fields remain unchanged.

Acceptance requires every injected byte or field change to be attributable to a recorded operator.
Acceptance requires repeated execution with the same seed to produce identical hashes.
Acceptance requires an invalid operator range to fail before modifying output.
Acceptance requires every generated V1 fixture and derivative to use only an operator allowed by `cartosentry-v1-core`.

Focused commit: `test: establish deterministic fault laboratory`.

## Milestone 2: Build resumable bounded-memory ingestion

### M2.1: Boreas normalized adapter

Implement production parsing for the selected Boreas trajectory, lidar, and required calibration formats.
Expose sequential frame and sample iterators through the canonical adapter contract.
Preserve source timestamp and coordinate provenance.
Add golden tests from permitted tiny or synthetic equivalents and local public-data integration tests.

Acceptance requires all selected public-smoke streams to normalize successfully.
Acceptance requires actual lidar point-time checks.
Acceptance requires unsupported or missing optional fields to remain explicit.

Focused commit: `feat: add Boreas mapping adapter`.

### M2.2: Manifest scanner and frame index

Implement source enumeration, streaming SHA-256, structural metadata scan, frame index, duplicate detection, and source-range summaries.
Persist the normalized sequence manifest and index atomically.
Support timestamp order that differs from filesystem order.

Acceptance requires deterministic manifests across mandatory platforms.
Acceptance requires corrupted, duplicate, missing, and reordered synthetic sources to produce expected structural findings.
Acceptance requires index memory to remain within its byte budget.

Focused commit: `feat: index immutable sensor recordings`.

### M2.3: Bounded scheduler

Implement the byte-budgeted worker scheduler, modality fairness, deterministic mode, cancellation, metrics, and structured worker errors.
Add stress tests with mixed tiny IMU tasks and large lidar tasks.
Run AddressSanitizer, UndefinedBehaviorSanitizer, and ThreadSanitizer suites.

Acceptance requires no deadlock, race report, leaked task, unbounded queue, or starvation in the frozen stress suite.
Acceptance requires cooperative cancellation to leave no complete pointer for incomplete artifacts.

Focused commit: `feat: add bounded analysis scheduler`.

### M2.4: Run database and atomic stages

Implement the SQLite run database, stage state machine, cache keys, atomic attempt publication, recovery reconciliation, invalidation, and resume commands.
Inject process termination at every artifact-commit boundary.
Compare uninterrupted and resumed semantic hashes.

Acceptance requires exact semantic equality across every interruption test.
Acceptance requires stale complete records with missing artifacts and orphaned valid artifacts to reconcile deterministically.
Acceptance requires `--force-stage` to invalidate only the dependency closure.

Focused commit: `feat: make analysis stages resumable`.

### M2.5: Parser fuzz and hardening pass

Add LibFuzzer targets for every custom binary parser and manifest boundary.
Seed fuzz corpora with clean, truncated, oversized, malformed, duplicate, and endian-swapped fixtures.
Add checked arithmetic and allocation limits exposed by fuzzing.

Acceptance requires the frozen local and nightly fuzz durations to pass with sanitizers.
Acceptance requires every discovered crash to have a minimized regression test.

Focused commit: `test: harden sensor parsers with fuzzing`.

## Milestone 3: Implement V1 trajectory integrity and stage follow-on IMU work

### M3.1: Continuous reference trajectory

Implement reference trajectory loading, local-world anchoring, interpolation, gap handling, robust derivatives, heading unwrap, and stationary classification.
Validate against analytic generated paths.

Acceptance requires derivative errors to satisfy the charter across straight, turn, stop, and gap fixtures.
Acceptance requires no interpolation or extrapolation across unsupported intervals.

Focused commit: `feat: interpolate reference trajectories`.

### M3.2: Trajectory integrity detectors

Implement timestamp, position jump, freeze, bias-compatible residual, velocity, acceleration, jerk, yaw-rate, and continuity detectors.
Add corresponding fault operators at severity levels below, near, and above gates.
Implement observability and event consolidation.

Acceptance requires structural and supported content detector gates on development and calibration partitions.
Acceptance requires stationary synthetic sequences not to be falsely classified as frozen-position failures.

Focused commit: `feat: detect trajectory integrity faults`.

### M3.3: Follow-on IMU integrity detectors

Implement rate, gap, nonfinite, repeated value, stuck axis, saturation, stationary gravity, bias evidence, and noise evidence.
Transform IMU axes into the rig frame through checked calibration.
Add signed-axis fixtures and injected faults.

Acceptance requires exact structural detection and supported content thresholds.
Acceptance requires missing sensor-limit metadata to produce an explicit fallback provenance record.

Focused commit: `feat: detect IMU integrity faults`.

### M3.4: Follow-on IMU and trajectory self-consistency

Implement resampling, normalized cross-correlation, peak refinement, uncertainty, support metrics, excitation, peak separation, multi-window drift detection, and observability classification.
Add offset, drift, straight-driving, stop, and inconsistent-window fixtures.

Acceptance requires the offset-error and nonobservability gates to pass.
Acceptance requires positive and negative time shifts to retain the correct sign.
Acceptance requires a low-excitation window never to satisfy the mandatory pass rule.

Focused commit: `feat: estimate observable sensor time offset`.

### M3.5: V1 trajectory and timestamp benchmark checkpoint

Run the complete frozen V1 trajectory and timestamp development benchmark.
Generate a machine-readable report and review false critical findings on public development clips.
Change thresholds only through the documented calibration procedure.

Acceptance requires no unresolved blocking detector defect and all frozen M3 gates to pass.

Focused commit: `test: qualify temporal integrity engine`.

## Milestone 4: Complete V1 lidar integrity and stage later modalities

### M4.1: Lidar structural and coverage detectors

Implement finite field, range, intensity, ring, azimuth, point-time, frame cadence, scan duration, count, quantile, and sector statistics.
Implement ring loss, sector loss, density reduction, nonfinite, scan loss, range scale, and point-time fault operators.

Acceptance requires exact structural fault detection and supported coverage-fault gates.
Acceptance requires bounded memory on the public-smoke workload.

Focused commit: `feat: analyze lidar stream integrity`.

### M4.2: Lidar motion compensation and alignment

Implement per-point-time motion compensation, local voxel aggregation, dynamic and near-ego masks, multi-frame overlap, surface thickness, and observability.
Add analytic moving-lidar fixtures and time, trajectory, and extrinsic perturbations.

Acceptance requires clean synthetic alignment within tolerance.
Acceptance requires supported perturbations to separate from clean calibration data at frozen severities.
Acceptance requires unsupported trajectory gaps to produce `UNKNOWN` support rather than a pass.

Focused commit: `feat: validate motion-compensated lidar alignment`.

### M4.3: Follow-on camera stream detectors

Implement decode, dimensions, cadence, exact duplicate, perceptual freeze, blur, darkness, saturation, and evidence extraction.
Use motion evidence to distinguish suspicious freeze from a stationary scene.
Implement the complete camera fault family.

Acceptance requires clean stationary fixtures not to fail freeze detection.
Acceptance requires supported moving freeze, decode, blur, and exposure faults to meet gates.
Acceptance requires weather or darkness limitations to appear in observability or profile support.

Focused commit: `feat: analyze camera stream integrity`.

### M4.4: Follow-on camera-to-lidar consistency

Implement the M0-selected projection metric, static and moving interval classification, deterministic feature selection, motion compensation, observability, and evidence rendering.
Add time and extrinsic perturbation tests.

Acceptance requires the selected metric to meet the frozen clean-versus-perturbation gate.
Acceptance requires low-texture and insufficient-projection fixtures to return weak or not observable.
Acceptance requires the output to state hypotheses rather than an unsupported unique cause.

Focused commit: `feat: measure camera lidar consistency`.

### M4.5: Follow-on radar integrity

Implement polar metadata, azimuth timestamp, encoder progression, cadence, repeated or missing azimuth, blank sector, and robust energy-distribution checks.
Implement the complete radar fault family.
Retain radar-to-lidar consistency only if the M0 decision says it is supported.

Acceptance requires supported radar structural and content gates.
Acceptance requires quiet clean scenes not to fail a fixed global energy assumption.

Focused commit: `feat: analyze radar stream integrity`.

### M4.6: Follow-on cross-modal evidence graph

Implement typed hypotheses, supporting evidence, contradicting evidence, and deterministic explanation inputs.
Cover timing, trajectory, extrinsic, camera-model, and source-corruption hypotheses without claiming exhaustive diagnosis.
Add scenarios with deliberately ambiguous causes.

Acceptance requires the engine to preserve ambiguity.
Acceptance requires every rendered hypothesis to trace to machine-readable observations.

Focused commit: `feat: connect cross-modal evidence`.

### M4.7: Follow-on modality benchmark checkpoint

Run the frozen development and threshold-calibration fault matrix.
Audit false critical findings by weather and scene type.
Freeze detector thresholds and event-consolidation parameters before policy tuning.

Acceptance requires the structural, content, observability, spatial-support, memory, and false-critical gates to pass or a profile capability to be explicitly removed.

Focused commit: `test: qualify multimodal integrity engine`.

## Milestone 5: Build directed road matching and spatial evidence

### M5.1: Directed road-graph importer

Implement pinned OpenStreetMap extract ingestion, node and directed-arc construction, geometry normalization, one-way handling, source provenance, spatial index, and graph identity.
Retain parallel roads and grade-separated topology where represented.
Add tiny hand-authored graph fixtures independently of OSM.

Acceptance requires deterministic graph identity and geometry across platforms.
Acceptance requires one-way, divided road, ramp, roundabout, parallel road, and grade-separated fixtures to import correctly.
Acceptance requires OSM attribution to survive portable report export.
Acceptance requires every public trajectory observation used for matching to carry declared WGS84 or source-derived local-world provenance, with no manually tuned translation or rotation.

Focused commit: `feat: import directed road graphs`.

### M5.2: HMM candidate and scoring model

Implement uncertainty-aware candidate search, directed projection, lateral and heading emission terms, off-map state, graph-distance transition, speed support, and impossible-transition rejection.
Make every model parameter a charter key.
Add unit tests for emission and transition signs, units, and edge cases.

Acceptance requires no forced match when only the off-map state is plausible.
Acceptance requires low-speed heading to be disabled or downweighted as specified.
Acceptance requires impossible directed transitions to remain impossible.

Focused commit: `feat: score road match candidates`.

### M5.3: Viterbi decoder and ambiguity

Implement deterministic beam-pruned Viterbi decoding, runner-up retention, confidence or path-separation evidence, stationary handling, and interval outputs.
Add the complete synthetic graph scenario suite.

Acceptance requires exact expected paths on hand-authored unambiguous fixtures.
Acceptance requires deliberate ambiguous fixtures to be labeled ambiguous.
Acceptance requires the synthetic directed-arc and off-map gates to pass.

Focused commit: `feat: match trajectories to road topology`.

### M5.4: Directed road bins

Implement arc-length binning, traversal segmentation, partial-bin handling, independent-pass identity, modality evidence joins, and affected-finding localization.
Add boundary, reverse-direction, short arc, repeated pass, ambiguous match, and off-map tests.

Acceptance requires exact expected bin coverage on synthetic paths.
Acceptance requires adjacent windows from one pass not to inflate traversal count.
Acceptance requires injected fault localization to satisfy the spatial-bin gate.

Focused commit: `feat: localize evidence to directed road bins`.

### M5.5: Follow-on repeated-trajectory disagreement hypotheses

Implement high-quality off-map interval selection, direction-aware resampling, clustering, robust corridor fitting, graph-endpoint comparison, and review-only hypotheses.
Add synthetic missing connection, perturbed geometry, parallel road, and unchanged controls.

Acceptance requires topology precision, recall, endpoint, and false-hypothesis gates on the supported synthetic mutation set.
Acceptance requires all public results to be labeled hypotheses without ground-truth claims.

Focused commit: `feat: surface repeated topology disagreements`.

### M5.6: Public route adjudication checkpoint

Create frozen review instructions and manually adjudicate the selected public route sample without viewing final-test outcomes.
Record directed-arc agreement, ambiguity, off-map intervals, and graph-data limitations.
Change the candidate public route only through the M0 pivot procedure.

Acceptance requires the confident public coverage gate.
Acceptance requires unresolved samples to remain unresolved rather than being forced into the expected road.

Focused commit: `test: qualify public road matching`.

## Milestone 6: Implement auditable map-readiness decisions

### M6.1: Typed profile language

Implement the profile JSON Schema, Pydantic model, unit-aware typed predicates, capability checks, and charter references.
Reject arbitrary code, unknown units, unsupported detector references, and cyclic derived requirements.
Add profile lint output.

Acceptance requires every shipped profile to validate.
Acceptance requires malformed and dangerous expressions to fail before analysis.
Acceptance requires profile evaluation to be deterministic across platforms.

Focused commit: `feat: define map readiness profiles`.

### M6.2: Tri-state policy evaluator

Implement requirement queries, aggregation, observability handling, `PASS`, `FAIL`, `UNKNOWN`, spatial-scope evaluation, sequence summary, and missing-evidence records.
Ensure that mandatory failures and unknowns cannot be compensated by optional scores.

Acceptance requires every policy truth-table test and no-compensation mutation test to pass.
Acceptance requires zero generated false pass for mandatory critical faults.
Acceptance requires every requirement to have a complete evaluation record.

Focused commit: `feat: evaluate tri-state mapping readiness`.

### M6.3: Required production profiles

Implement and document `structural-preflight-v1` and `hdmap-lidar-v1` using frozen V1 detector capabilities.
Run profile-specific policy tuning without altering detector thresholds.
Remove requirements whose evidence did not survive V1 lidar qualification.
Stage `semantic-camera-v1` and `radar-redundancy-v1` only in the later modality track.

Acceptance requires every profile to state required inputs, observable conditions, supported claims, and limitations.
Acceptance requires the final profile thresholds to be frozen before final test.

Focused commit: `feat: ship mapping acceptance profiles`.

### M6.4: Explanation renderer

Implement deterministic text and JSON explanations from requirement evaluations and findings.
Include measured value, unit, threshold, observability, source support, road scope, and remediation.
Keep hypotheses visually and semantically separate from findings.

Acceptance requires golden explanations for pass, fail, unknown, conflicting evidence, and unsupported modality cases.
Acceptance requires explanation text to contain no hidden inference absent from the artifact.

Focused commit: `feat: explain readiness evidence`.

### M6.5: Readiness benchmark checkpoint

Run generated truth, mutation, profile, and public development case-study suites.
Freeze readiness profiles and their charter hash.

Acceptance requires the readiness gates to pass with no unresolved mandatory policy ambiguity.

Focused commit: `test: qualify readiness policy engine`.

## Milestone 7: Build the reviewer-facing product

### M7.1: Qualify the static report architecture

Implement bounded static HTML with content-addressed local assets and test it using the largest V1 public-smoke evidence payload.
Measure load time, memory, point evidence size, offline behavior, and testability.
Record the evidence budget and reject stateful server dependencies from V1.

Acceptance requires the selected report to load fully offline and avoid third-party runtime resources.
Acceptance requires no local source path in browser payloads.

Focused commit: `docs: select report architecture`.

### M7.2: Overview, map, and timeline

Implement sequence and profile summary, tri-state overview, directed road-bin map, modality filters, finding filters, timeline, and accepted or excluded interval display.
Use color-independent labels and keyboard-accessible controls.

Acceptance requires actual-browser end-to-end tests for pass, fail, unknown, and mixed coverage.
Acceptance requires a reviewer to reach every critical finding from the initial page.

Focused commit: `feat: visualize road readiness`.

### M7.3: Sensor and evidence review

Implement bounded lidar BEV or sampled 3D evidence, trajectory plots, detector measurements, and source lineage at a selected time.
Use deterministic downsampling and record evidence hashes.

Acceptance requires evidence views to reproduce the machine-readable finding.
Acceptance requires browser memory and load time to stay within the UI charter selected in M7.1.

Focused commit: `feat: inspect trajectory lidar evidence`.

### M7.4: Policy and hypothesis review

Implement the mandatory-requirement tree, threshold details, observability, missing evidence, support and contradiction graph, and remediation text.

Acceptance requires hypotheses never to render as confirmed causes.
Acceptance requires unit and threshold displays to match the machine-readable artifact exactly.

Focused commit: `feat: inspect readiness decisions`.

### M7.5: Portable export and user-visible QA

Implement portable report export with local path and machine-identity removal.
Add screenshots, accessibility audit, Content Security Policy tests, no-network tests, and corrupted-artifact UI behavior.

Acceptance requires a portable synthetic report to open on another clean machine without source data.
Acceptance requires no outbound request during the browser test.
Acceptance requires all golden user-visible states to be reviewed.

Focused commit: `test: qualify offline review experience`.

## Milestone 8: Build and verify the recollection planner

### M8.1: Recapture requirement generation

Implement profile-specific conversion from failed or unknown bins into directed recapture requirements with warm-up, continuous distance, modality, priority, and rationale.
Add deduplication and partial-arc merging with proof preservation.

Acceptance requires exact requirements for hand-authored readiness fixtures.
Acceptance requires unsupported and unreachable cases to remain explicit.

Focused commit: `feat: derive recapture requirements`.

### M8.2: Exact small-case solver

Implement the exact `(graph_node, incoming_arc, requirement_automata_state)` shortest-path solver with state-memory preflight and predecessor reconstruction.
Implement exhaustive traversal enumeration as an independent oracle for very small graphs.
Model graph-profile direction and transitions, depot return, warm-up, minimum contiguous observation distance, required partial arcs, reset conditions, incidental coverage on connector paths, nonnegative turn costs, and disconnected requirements.
Add infeasible and disconnected scenarios.

Acceptance requires exact cost equality with brute force on every tiny fixture.
Acceptance requires infeasibility to produce a structured result instead of a partial invalid route.

Focused commit: `feat: solve exact small recapture routes`.

### M8.3: Scalable deterministic heuristic

Implement component connection, shortest-path closure, directed balancing, Eulerian traversal, warm-up insertion, turn costs, and local improvement.
Record lower-bound and baseline comparisons.

Acceptance requires full reachable requirement coverage and no graph-profile-invalid traversal.
Acceptance requires no standard scenario to be worse than greedy and the median improvement gate to pass.

Focused commit: `feat: plan scalable recollection routes`.

### M8.4: Budgeted planning

Implement the frozen lexicographic objective that first maximizes satisfied integer priority weight under the distance or time budget, then minimizes route cost, then deadhead cost, then canonical arc sequence.
Account for every requirement as covered, deferred, or unreachable.
Expose marginal cost and priority decisions.

Acceptance requires budget compliance and complete accounting in every scenario.
Acceptance requires deterministic tie breaking.

Focused commit: `feat: plan budgeted recollection`.

### M8.5: Independent validator and exports

Implement route reconstruction, graph-profile direction and transition validation, requirement-automaton replay, continuity, cost recomputation, budget check, coverage proof, GeoJSON, and GPX export.
Add deliberate optimizer-output corruption tests.

Acceptance requires the validator to reject every mutated invalid route.
Acceptance requires accepted exports to reconstruct to the same verified graph path.

Focused commit: `feat: verify recapture plans`.

### M8.6: Recollection UI and benchmark

Add required arcs, route, deferred requirements, cost decomposition, and greedy comparison to the report.
Run the frozen routing suite.

Acceptance requires all routing gates and actual-browser route review tests to pass.

Focused commit: `test: qualify recollection planning`.

## Follow-on Milestone 9: Add current public NVIDIA-format support

### M9.1: NCore capability and version lock

Refresh the M0 feasibility record and pin the current public `nvidia-ncore` release that passes the follow-on platform matrix.
Create a capability map between NCore V4 pose, camera, lidar, radar, timestamp, calibration, and generic component APIs and the CartoSentry adapter contract.
Record unsupported fields without inventing data.

Acceptance requires a public NCore sample or permitted generated V4 sequence to load on macOS and Linux.
Acceptance requires exact dependency and source documentation in the lock and decision record.

Focused commit: `build: lock public NCore compatibility`.

### M9.2: NCore V4 adapter

Implement the Python NCore adapter and batch normalization boundary into the C++ engine.
Preserve per-frame and per-ray timing, pose graph, calibration, and sensor identity.
Avoid exposing NCore implementation objects across the stable C++ ABI.

Acceptance requires normalized contract tests to pass for supported camera, lidar, radar, pose, and calibration data.
Acceptance requires absent NCore capabilities to become explicit adapter capability records.

Focused commit: `feat: ingest NCore V4 sequences`.

### M9.3: Cross-adapter equivalence

Generate or convert one semantically equivalent synthetic sequence through both native and NCore paths.
Compare canonical measurements, findings, road matches, and readiness within frozen tolerances.

Acceptance requires semantic equivalence for every supported common field.
Acceptance requires differences caused by format capability to be documented rather than hidden.

Focused commit: `test: verify adapter semantic equivalence`.

## Follow-on Milestone 10: Package and resume accepted-data publication

### M10.1: Immutable accepted-data bundle

Implement bundle schema, accepted and excluded interval references, calibration dependencies, evidence artifacts, content identities, portable path handling, and optional raw inclusion.
Add validation that every reference resolves and every excluded interval has a reason.

Acceptance requires deterministic bundle identity.
Acceptance requires default bundles to contain no copied raw data.
Acceptance requires source recordings to remain byte unchanged.

Focused commit: `feat: package accepted mapping evidence`.

### M10.2: Content-addressed raw shards

Implement optional bounded shards, zstd compression, logical contents, hashes, and reconstruction tests.
Test cancellation and disk-full behavior through fault injection.

Acceptance requires exact reconstruction of selected source intervals.
Acceptance requires no published complete shard after an interrupted write.

Focused commit: `feat: shard optional accepted sensor data`.

### M10.3: Upload implementation spike

Compare Python Boto3 and native libcurl plus SigV4 against local MinIO for required multipart, resume, checksum, and cancellation behavior.
Select the smaller robust implementation and delete the alternative.
Record the decision.

Acceptance requires one selected dependency path and no duplicated upload stack.

Focused commit: `docs: select resumable upload stack`.

### M10.4: Resumable multipart publication

Implement multipart state, retries, backoff with jitter, idempotent resume, remote verification, manifest publication point, cancellation, and explicit cleanup.
Add every upload interruption and wrong-content scenario.

Acceptance requires identical final hashes across uninterrupted and every resumed test.
Acceptance requires no consumer-visible manifest before all required objects verify.
Acceptance requires logs to contain no credential or signed-query material.

Focused commit: `feat: publish accepted bundles safely`.

## Milestone 11: Run frozen end-to-end evaluation

### M11.1: Complete fault matrix

Finish only the V1 structural, timestamp, trajectory, calibration, and lidar operators, severities, observable cases, and nonobservable controls enumerated by the named `cartosentry-v1-core` matrix.
Verify partition and seed isolation.
Do not generate or evaluate IMU, camera, radar, cross-modal, topology, publication, or downstream follow-on faults in this milestone.

Acceptance requires complete machine-readable coverage of matrix identifier `cartosentry-v1-core` in `benchmarks/fault_matrix_v1.yaml` and zero operators outside its allowlist.

Focused commit: `test: complete sensor fault corpus`.

### M11.2: Detector and readiness evaluation

Check out the immutable evaluation tag and verify a clean source tree before reading any final-test input.
Verify that detector thresholds, event consolidation, profiles, evaluation slices, split identities, and the release-candidate charter have their final pre-unblinding freeze.
Run the explicit audited unblinding command and then run the frozen final-test partition once.
Compute V1 event, boundary, false-critical, observability, spatial, map-matching, and readiness metrics with the frozen clustered confidence intervals.
Require at least `12` independently adjudicated source groups for any confirmatory public HMM claim.
Do not tune after results are visible.

Acceptance requires every mandatory detector and readiness gate to pass.
Acceptance requires the unblinding record, split hash, charter version, charter hash, profile hashes, exact commit, and execution timestamp in the machine-readable result.
Acceptance requires the full immutable evaluation tag and commit hash to match the source tree used by every process.
If a gate fails, publish only the first passing ancestor or descendant claim allowed by the predeclared fallback tree from that same one-shot result.
Do not rerun, repair, retune, or reuse that final partition after any result becomes visible.

This milestone creates no source commit.
Its output is external content-addressed evidence bound to the immutable evaluation tag.

### Follow-on M11.3: Downstream utility study

Freeze and run accept-all, oracle removal, CartoSentry filtering, and equal-removal random controls.
Record external evaluator version, container or environment, configuration, and input hashes.
Compute practically meaningful effects and sequence-level uncertainty.

Acceptance requires the downstream utility gates.
If the gate fails, remove the downstream improvement claim while retaining the detector and routing product if their gates pass.

Focused commit: `test: measure downstream mapping utility`.

### M11.4: Routing evaluation

Check out the immutable evaluation tag and run exact, heuristic, budgeted, invalid-route, and greedy-comparison final suites against at least `12` independent graph families.

Acceptance requires every routing gate.

This milestone creates no source commit.
Its output is external content-addressed evidence bound to the immutable evaluation tag.

### M11.5: CPU systems evaluation

Check out the immutable evaluation tag and run release-build throughput, memory, byte-read, queue, resume, and cancellation benchmarks on the frozen Linux CPU host.
Repeat the duration-scaling memory test.
Run the macOS public-smoke performance check without making cross-hardware comparisons.

Acceptance requires CPU throughput, RSS, scaling, and resume gates.
If the primary profile misses a performance gate, fail that performance claim or select only the first passing predeclared fallback profile measured in the same one-shot execution.
Do not optimize, change workloads, alter semantics, or rerun the final partition after a result is visible.
Any source change requires a new candidate tag and genuinely untouched final partition.

This milestone creates no source commit.
Its output is external content-addressed evidence bound to the immutable evaluation tag.

### M11.6: External evidence verification

Regenerate every table and figure from machine-readable results into the external content-addressed evidence area.
Verify split hashes, charter hash, source hashes, environment, bootstrap unit, and claim wording.
Check that no public claim exceeds the result.

Acceptance requires one clean command to reproduce the synthetic report and documented commands for the public benchmark.

This milestone creates no source commit and may not modify the tagged source tree.

## Follow-on Milestone 12: Optional CUDA acceleration and Linux GPU qualification

### M12.1: Profile before selecting kernels

Use release CPU profiling to identify whether lidar voxelization, histogram aggregation, or another supported stage dominates the frozen workload.
Select at most two CUDA targets with clear batch contracts.
Record the decision and predicted transfer cost.

Acceptance requires measured CPU evidence rather than a GPU feature chosen for appearance.

Focused commit: `docs: select optional CUDA kernels`.

### M12.2: CUDA backend

Implement the selected CUDA kernels, device-memory ownership, bounded batching, error propagation, stream lifecycle, and CPU fallback.
Avoid GPU allocation per frame.
Keep policy, schemas, routing, and explanations on the CPU.

Acceptance requires CUDA unit tests to compile and run on the target host.
Acceptance requires no device leak or asynchronous error hidden past a stage boundary.

Focused commit: `feat: accelerate lidar evidence with CUDA`.

### M12.3: CPU and GPU parity

Run clean, edge, malformed, injected-fault, and long-duration parity cases.
Compare detector measurements, events, affected bins, and final readiness rather than only individual kernel arrays.

Acceptance requires all parity tolerances and identical readiness states.

Focused commit: `test: verify CUDA detector parity`.

### M12.4: Automated remote qualification

Implement the repository-owned sync and qualification scripts.
Run prerequisites, release build, tests, parity, benchmark, Nsight Systems, selected Nsight Compute, artifact packaging, retrieval, and hash verification on the configured Linux GPU workstation.

Acceptance requires the exact commit and charter hash in the result.
Acceptance requires the end-to-end speed gate including transfers.
Acceptance requires a consolidated failure report if prerequisites or phases fail.

Focused commit: `perf: qualify optional CUDA backend`.

### M12.5: Claim decision

If the speed and parity gates pass, document the supported GPU configuration and measured improvement.
If either gate fails, mark the backend experimental or remove it from the primary release.
Do not weaken parity tolerance or workload composition after seeing the result without invalidating the claim.

Focused commit: `docs: record GPU qualification outcome`.

## Milestone 13: Portfolio release candidate

### M13.1: One-command fresh-machine demo

Verify bootstrap, synthetic V1 demo, static report generation, route planning, and portable export in a clean environment.
The demo must require no private data, external service, account, or GPU.
Run development-only CPU qualification rehearsals on the frozen workloads and complete every required profile-guided optimization before the immutable evaluation tag is created.
Freeze the primary performance profile, every predeclared fallback profile, and the exact one-shot performance command after the final rehearsal.

Acceptance requires an independent fresh environment to reproduce documented hashes and screenshots within declared nondeterministic fields.
Acceptance requires the primary and fallback performance workloads to execute in one command without reading final-test results.

Focused commit: `test: verify fresh-machine demo`.

### M13.2: User-facing documentation

Write the README, architecture, contracts, profiles, benchmark methodology, data and licenses, threat model, GPU qualification, contribution guide, security policy, and citation metadata.
Create a concise architecture figure, a real report screenshot from development-only public-safe data, and a synthetic demo recording.
Create an unpopulated benchmark-summary template whose stable fields, captions, claim slots, and result-ingestion command are complete before final evaluation.
The template must reject results that do not bind the immutable tag, source-group manifest, split, charter, profiles, fallback tree, dependency locks, and full artifact hashes.
Populate the benchmark summary only after M11.2, M11.4, and M11.5 by rendering into the external content-addressed evidence area without a source commit.

Acceptance requires every command to be re-run from the documentation.
Acceptance requires no internal, personal, local-path, session-note, or unsupported market language.
Acceptance requires all tracked documentation to be complete without final numeric values and the benchmark template to render from a synthetic schema fixture.

Focused commit: `docs: complete public portfolio documentation`.

### M13.3: Release artifact audit

Build wheel and source distribution.
Install each in a clean environment.
Generate SBOM, dependency notices, and checksums.
Scan Git history, tracked files, built artifacts, screenshots, and logs for secrets, private paths, internal strings, third-party raw data, and license omissions.
Run the final pre-tag source, packaging, documentation-template, license, and public-safety audit.

Acceptance requires a clean audit and passing installed-package smoke test.
Acceptance requires all tracked V1 documentation and claim templates to be final before the immutable evaluation tag is created.

Focused commit: `build: prepare portfolio release candidate`.

### M13.4: Final evidence package

Render the reproducible V1 benchmark report from the pre-tag template and collect the demo script, short demo video, architecture diagram, finding example, road-readiness example, recollection comparison, and CPU profile outside the tagged source tree.
Draft resume bullets only from verified numbers.
Create a content-addressed evidence manifest containing the immutable tag commit and full SHA-256 digest of every result, report, table, figure, log, environment record, and portfolio artifact.

Acceptance requires every number in the README, resume draft, and video to trace to a machine-readable artifact and exact commit.
The evidence package must be attached by full content hash to the exact immutable evaluation tag and must not imply that a later source commit was evaluated.

This milestone creates no source commit.
Publishing or attaching the external evidence is an explicit release action against the immutable evaluation tag.

## Kill and pivot gates

### Dataset gate

Pivot datasets if exact terms, access, modality coverage, or timestamp semantics do not support the benchmark.
Do not solve access problems by committing third-party raw data.

### Public road-graph gate

Pivot the selected route or graph source if less than `85 percent` of moving public trajectory distance can be adjudicated with confident directed-road matching.
Do not lower confidence until forced matches make the visualization look complete.

### Follow-on IMU time-alignment gate

If no real development interval contains sufficient angular excitation, retain exact timestamp structural checks and synthetic offset evaluation but remove the public real-offset claim.

### Follow-on camera-to-lidar gate

If neither candidate metric separates supported clean and perturbed cases under observable conditions, keep deterministic projection evidence for human review and remove it as a mandatory automated policy gate.

### Follow-on radar cross-modal gate

If radar-to-lidar consistency is not robust on compact data, retain radar structural and within-stream integrity checks and remove the cross-modal claim.

### Follow-on topology gate

If synthetic topology hypotheses cannot meet the precision gate, remove automated hypothesis generation from the topology follow-on and retain V1 off-map interval visualization.
Do not present noisy trajectory clustering as reliable map correction.

### Follow-on downstream utility gate

If CartoSentry filtering does not materially improve the frozen downstream probe, remove the downstream improvement claim.
The product may still ship as a validated acceptance, evidence, and recollection tool if its direct gates pass.

### Route gate

If the scalable heuristic cannot beat or equal greedy on every standard scenario, keep exact small-case routing and narrow the supported road-graph size instead of shipping a worse heuristic.

### Performance gate

If bounded memory fails, stop feature work and fix ownership, queue limits, and artifact batching before continuing.
Before the immutable evaluation tag, profile and optimize or predeclare a narrower fallback profile if development-only full CPU rehearsals miss real time.
After final results are visible, a CPU performance failure must fail the affected claim or select only a passing predeclared fallback profile measured in the same one-shot execution.

### CUDA gate

If optional CUDA fails parity, it must not ship.
If it passes parity but misses the speed gate, it may remain experimental but cannot support a performance claim.

### Follow-on upload gate

If neither upload implementation provides reliable resume and publication atomicity against MinIO, defer remote publication and ship local immutable bundles only.

## Risk register and mitigations

### Real data lacks fault ground truth

The deterministic fault laboratory provides event ground truth.
Real data is used for clean behavior, realism, case studies, and downstream effects rather than pretending every sensor defect is labeled.

### Public data may already contain degradation

The project must not call every public clip clean.
Clean controls are generated truth plus manually adjudicated public intervals with uncertainty.

### Weather can resemble sensor failure

Scene-dependent detectors must report observability and degradation evidence.
Weather slices and false-critical rates protect against a detector tuned only to clear conditions.

### Postprocessed poses can leak future information

CartoSentry is explicitly an offline recording validator.
It must not describe the reference trajectory as a deployable real-time estimate.
Any causal mode must have a separate contract and benchmark.

### OSM may be stale or geometrically coarse

The HMM includes an off-map state and ambiguity.
Road-readiness claims are restricted to confidently matched bins.
Topology outputs remain review hypotheses.

### Cross-modal metrics can be scene dependent

Every metric requires an observability predicate.
Low-texture, low-structure, static, dynamic, darkness, and precipitation cases are explicit controls.

### Scope could become a full AV stack

The project excludes SLAM, object detection, control, live vehicle integration, and general HD-map construction.
The downstream evaluator remains an external frozen probe.

### Dependency surface could overwhelm the project

PCL, ROS, full web mapping stacks, and duplicate upload libraries are excluded.
Every spike must select one path and delete the alternative.

### Numerical disagreement across platforms

Persisted inputs and policies are deterministic.
Metrics use frozen tolerances where exact floating equality is inappropriate.
Semantic verdict equality is stricter than low-level iteration-order equality.

### Report could leak paths or precise private locations

Portable export strips local paths and machine identifiers.
Only public-dataset locations are allowed in portfolio artifacts.

### Resume logic could reuse stale evidence

Cache keys include source, algorithm, relevant configuration, and upstream artifact hashes.
Fault-injected crash tests exercise every commit boundary.

### Optimizer could claim invalid coverage

The independent validator reconstructs coverage from the exported graph path.
An invalid plan is never accepted or exported as complete.

## Portfolio artifacts

The final repository must provide the following evidence.

- A concise README with the problem, one-command demo, architecture, verified results, and limitations.
- A public-safe screenshot showing mixed road-bin readiness.
- A finding explanation that traces a subtle temporal or calibration defect to exact evidence.
- A synchronized sensor review view.
- A recollection route with independent coverage proof and greedy comparison.
- A deterministic synthetic fault benchmark report.
- A real Boreas case study with exact public source attribution.
- A downstream registration or odometry utility study.
- A bounded-memory and throughput profile.
- A resume-interruption correctness report.
- An optional CUDA parity and performance report only if gates pass.
- A two-to-four-minute demo video reproducible from a tagged commit.
- An architecture document suitable for a systems interview.

## Interview narratives the implementation must support

### C++ systems narrative

Explain bounded queues, byte budgets, lifetime ownership, parser hardening, atomic stage publication, cancellation, and deterministic resume.

### Algorithms narrative

Explain HMM candidate, emission, transition, off-map, ambiguity, and directed Viterbi decisions.
Explain why recollection is a directed rural postman style problem and why the production solver does not claim general optimality.

### Robotics geometry narrative

Explain tagged time, spinning-sensor measurement time, correction provenance, motion compensation, named `SE(3)` transforms, and local versus global precision.

### Statistics narrative

Explain robust cadence estimates, event consolidation, threshold calibration, sequence-level bootstrap, weather slices, false critical rate, and why frame-level confidence intervals would be misleading.

### Product narrative

Explain why a mapping engineer needs road-localized acceptance and a recollection action instead of another sensor dashboard.

### NVIDIA-role narrative

Explain how the project independently demonstrates sensor data analysis, trajectory analysis, graph algorithms, computational geometry, C++, Python, Linux behavior, networking, storage, visualization, and careful benchmarking without copying private products.

## Definition of portfolio complete

CartoSentry is portfolio complete when the narrowed mandatory V1 scope passes its gates and the V1 release tag exists.
The synthetic demo must run on a clean machine without private data or a GPU.
The public-data benchmark must be reproducible from verified download scripts and hashes.
The tri-state policy must have zero false pass in mandatory generated mutation cases.
The route validator must prove complete reachable coverage for accepted plans.
The CPU product must meet its bounded-memory and throughput gates.
Every public number must trace to a machine-readable artifact and commit.
The repository and built artifacts must pass the public-safe and licensing audit.
Camera, radar, cross-modal, topology, NCore, upload, downstream, and CUDA follow-ons are not required for this definition and must not appear as completed V1 claims.

## Authoritative public sources

### NVIDIA mapping, sensors, and recording

- [DRIVE Labs: How Localization Helps Vehicles Find Their Way](https://developer.nvidia.com/blog/drive-labs-how-localization-helps-vehicles-find-their-way/) explains HD-map localization, pose accuracy, and multimodal localization.
- [Getting to Know Autonomous Vehicles](https://developer.nvidia.com/blog/getting-to-know-autonomous-vehicles/) describes NVIDIA DRIVE Map and camera, lidar, radar, and GNSS localization layers.
- [NVIDIA Autonomous Vehicles Safety Report](https://images.nvidia.com/aem-dam/en-zz/Solutions/auto-self-driving-safety-report.pdf) describes data collection, sensor modalities, verification, validation, data quality metrics, and replay.
- [DriveWorks Recording Sensor Data](https://developer.nvidia.com/docs/drive/drive-os/6.0.9/public/driveworks-nvsdk/dwx_recording_devguide_group.html) describes multimodal synchronized recording and storage considerations.
- [DriveWorks Basic Recording](https://developer.nvidia.com/docs/drive/drive-os/6.0.9/public/driveworks-nvsdk/dwx_recording_devguide_basic_recording.html) describes recording, timestamps, and basic sensor-data sanity checks.
- [DriveWorks Sensor Timestamping](https://developer.nvidia.com/docs/drive/drive-os/6.0.5/public/driveworks-nvsdk/sensors_usecase4.html) defines host, raw, synchronized, and smoothed timestamp behavior.
- [DriveWorks Time Sensor Sample](https://developer.nvidia.com/docs/drive/drive-os/6.0.10/public/driveworks-nvsdk/nvsdk_dw_html/dwx_time_sensor_sample.html) demonstrates raw and synchronized lidar timestamp comparison.
- [DriveWorks Post-record Checker](https://developer.nvidia.com/docs/drive/drive-os/6.0.7/public/driveworks-nvsdk/dwx_postrecord_checker.html) documents recording-file and timestamp-delta integrity checking.
- [How DriveWorks Makes it Easy to Record and Replay Data for AV Development](https://developer.nvidia.com/blog/how-driveworks-makes-it-easy-to-record-and-replay-data-for-av-development/) explains the impact of temporal and spatial offsets on downstream AV software.

### NVIDIA NCore

- [NVIDIA NCore repository](https://github.com/NVIDIA/ncore) is the public implementation and licensing source.
- [nvidia-ncore on PyPI](https://pypi.org/project/nvidia-ncore/) is the authoritative public package, release, and Python-requirement record.
- [NCore Data Formats](https://nvidia.github.io/ncore/data/formats) documents V4 component stores and camera, lidar, radar, pose, and calibration data.
- [NCore Specification](https://nvidia.github.io/ncore/data/conventions) documents transforms, coordinate frames, and local world precision.
- [NCore Data Loading](https://nvidia.github.io/ncore/tutorial/data_loading.html) documents public loader and sensor access patterns.
- [NCore Data Sanity Check](https://nvidia.github.io/ncore/tutorial/data_sanity_check) documents pose, lidar fusion, motion compensation, camera projection, and timestamp sanity workflows.
- [NCore Sequence Metadata](https://nvidia.github.io/ncore/tools/ncore_sequence_meta.html) documents sequence metadata and component checksum extraction.
- [NCore Interactive 3D Viewer](https://nvidia.github.io/ncore/tools/ncore_vis.html) provides relevant public visualization prior art.

### Public datasets

- [Boreas paper](https://arxiv.org/abs/2203.10168) documents the multi-season sensor suite, repeated routes, timing, calibration, and ground-truth poses.
- [Boreas development kit](https://github.com/utiasASRL/pyboreas) documents public selective download and file access.
- [Boreas data reference](https://github.com/utiasASRL/pyboreas/blob/master/DATA_REFERENCE.md) documents timestamps, point records, radar metadata, transforms, synchronization, and pose fields.
- [KITTI raw data](https://www.cvlibs.net/datasets/kitti/raw_data.php) is a possible optional generalization source for camera, lidar, and GPS or IMU data.
- [nuScenes](https://www.nuscenes.org/nuscenes) is relevant public multimodal prior art but is not mandatory for V1.

### Algorithms and evaluation

- [bagx](https://github.com/rsasaki0109/bagx) is public prior art for ROS recording readiness, sensor synchronization, anomaly checks, report generation, and benchmark manifests.
- [rosbag-slam-lint](https://pypi.org/project/rosbag-slam-lint/) is public prior art for metadata-level lidar and IMU recording checks.
- [Hidden Markov Map Matching Through Noise and Sparseness](https://www.microsoft.com/en-us/research/publication/hidden-markov-map-matching-noise-sparseness/) is the primary HMM map-matching reference.
- [RoadRunner](https://mapster.csail.mit.edu/roadrunner/roadrunner.pdf) is primary prior art for connectivity-aware road-network inference from trajectories.
- [MAPSTER](https://mapster.csail.mit.edu/) provides the broader map-inference and map-update context.
- [Parameterized Rural Postman Problem](https://arxiv.org/abs/1308.2599) defines the directed required-arc routing problem family.
- [Robust Real-time LiDAR-inertial Initialization](https://arxiv.org/abs/2202.11006) provides relevant public evidence for lidar and IMU temporal and extrinsic observability.
- [IN2LAAMA](https://arxiv.org/abs/1905.09517) provides relevant public evidence for per-point lidar timing, motion distortion, and time-shift estimation.

### Build system

- [scikit-build-core getting started](https://github.com/scikit-build/scikit-build-core/blob/main/docs/guide/getting_started.md) documents current CMake and pybind11 Python packaging.
- [scikit-build-core build guide](https://github.com/scikit-build/scikit-build-core/blob/main/docs/guide/build.md) documents source-distribution and wheel builds.

## Final implementation instruction

The implementation agent must begin at M0 and move in order.
It must not jump directly to the dashboard, CUDA, or a demo video.
It must keep the numerical charter, data manifest, and milestone status current as evidence changes.
It must treat failed gates as engineering information and narrow claims rather than hiding failures.
It must push every verified focused commit before continuing.
It must stop only for a genuine user-owned blocker such as unavailable legal terms, unavailable required public data, inaccessible required hardware for an optional qualification, or authorization for an external publication action.

## Implementation status and resume handoff

This handoff records the safe M5.6 pre-production-qualification boundary paused by user request on 2026-08-14.
All accepted milestone gates through M5.5 remain unchanged.
M5.6 has progressed through a frozen blind adjudication, but the production qualification has not run and the milestone is not accepted.
Commit `cc3b60e81e6e541ce5352b1e14ed13c989335b2f` is the authoritative pre-adjudication protocol snapshot.

### Status vocabulary

`ACCEPTED` means the complete milestone gate passed locally, the focused implementation and tests are committed, and the commit is intended to be present on `origin/main`.
`IN_PROGRESS` means an ordered milestone work package is implemented and verified, but the complete milestone acceptance gate has not passed.
`PENDING_SEQUENCE` means the milestone has not started because an earlier sequential gate is still incomplete.
`PAUSED_BY_USER_REQUEST` means active work intentionally stopped at a verified boundary without changing the evidence-based state of the current milestone.
`DEFERRED_FOLLOW_ON` means the plan assigns the work to the post-portfolio follow-on sequence.
`DEFERRED_HARDWARE` means the optional GPU gate requires the consolidated remote-hardware workflow and has not been claimed from CPU or emulated evidence.
`BLOCKED` means an external prerequisite prevents otherwise authorized work from continuing.

### Accepted implementation

The following milestones are `ACCEPTED`: M0.1, M0.2, M0.3, M0.3b, M0.4, M0.5, M0.6, M1.1, M1.2, M1.3, M1.4, M2.1, M2.2, M2.3, M2.4, M2.5, M3.1, M3.2, M3.5, M4.1, M4.2, M5.1, M5.2, M5.3, M5.4, and M5.5.
M3.3, M3.4, M4.3, M4.4, M4.5, M4.6, and M4.7 remain `DEFERRED_FOLLOW_ON` and are not implied by the accepted list.
M5.5 is a follow-on gate that has passed locally, but that early acceptance does not promote it into the portfolio V1 completion claim or change the remaining sequential resume point at M5.6.

M5.5 implements bounded high-quality off-map interval selection, deterministic direction-aware normalized arc-length resampling, deterministic complete-link clustering, independent-traversal counting, coordinate-wise median corridor fitting, directed graph-endpoint comparison, and review-only missing-connection or geometry-disagreement hypotheses in native C++20.
The Python boundary adds authenticated profiles and graph views, stable interval, cluster, hypothesis, and report identities, exhaustive selection accounting, strict native-output validation, and the public `qualify-topology-hypotheses` workflow.
Every surfaced result is labeled `REVIEW_HYPOTHESIS_NOT_GROUND_TRUTH`, requires human review, declares that it is not ground truth, and forbids automatic map editing.

The frozen M5.5 profile file SHA-256 is `79202f14439bcc60cb985d903790f4243ff4c308088e6107f5763c5ed3a78084`.
The frozen M5.5 gate file SHA-256 is `def97492800a5084e1ae3ed1f23da09473115a535b16cc0768139b3e6cb8139e`.
The deterministic accepted qualification report SHA-256 is `45c6bfd0b00310dd378ecc19b9aa451c6c5844ae252920f0934338611010eb03` from both the editable-tree CLI and an isolated wheel installation.

The supported synthetic population contains 12 independent families, five independent traversals per scenario, 36 expected positive hypotheses, and 48 unchanged synthetic kilometers.
Observed precision was 1.0 with a one-sided family-cluster bootstrap 95 percent lower bound of 1.0.
Observed recall was 1.0 with a one-sided family-cluster bootstrap 95 percent lower bound of 1.0.
Observed median endpoint error was 0 road-bin lengths with a one-sided family-cluster bootstrap 95 percent upper bound of 0.
Observed false hypotheses per unchanged kilometer was 0 with an exact one-sided Poisson 95 percent upper bound of `0.0624110890323748`, below the frozen `0.1` gate.
These results support only the frozen synthetic missing-connection, perturbed-geometry, and altered-connection population with parallel-road and unchanged controls.
They do not establish real-world map-change accuracy or ground truth.

### Verification evidence at the pause boundary

The current full Python suite passed with 358 tests and 43 subtests.
The focused M5.6 suite passed all seven tests, including exact adjudication loading, tamper rejection, forced-unresolved rejection, packet determinism, packet redaction, and public CLI exposure.
Ruff lint passed.
Ruff format verification passed for 117 files.
Mypy strict verification passed for 54 configured source files.
The adjudication contains 869 unique source-ordered decisions and its embedded canonical immutable SHA-256 recomputes exactly as `47cc45d667e84e33cdb91bea264301e6c42a56687b471c04f810deac8e77d773`.
The adjudication file SHA-256 is `2f18f50fd8d66f5ce41804f7ef1e5a461a59344fd6ce20e8109470f18828ea33`.
The M5.6 changes after the protocol freeze do not modify native code.
The previously accepted M5.5 boundary remains supported by developer, optimized release, AddressSanitizer plus UndefinedBehaviorSanitizer, and ThreadSanitizer native suites that each passed all 55 tests, a successful source distribution and platform wheel build, and byte-identical editable-tree and isolated-wheel M5.5 qualification reports.

The exact current-boundary verification commands were:

```console
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest -q
.venv/bin/pytest -q tests/public_road_matching_qualification_test.py
.venv/bin/python -c 'from pathlib import Path; from cartosentry.public_road_matching_qualification import load_public_route_adjudication; print(load_public_route_adjudication(Path("benchmarks/m5_6_public_route_adjudication.yaml"))[0].immutable_sha256)'
git diff --check
```

### Deferred and blocked status

M5.6 is the next incomplete sequential milestone and is `IN_PROGRESS` at its completed-blind-adjudication, pre-production-qualification boundary.
The repository is `PAUSED_BY_USER_REQUEST` at that boundary.
M6.1 through M8.6, M11.1, M11.2, M11.4 through M11.6, and M13.1 through M13.4 are `PENDING_SEQUENCE` until M5.6 is accepted.
M9.1 through M10.4 and M11.3 remain `DEFERRED_FOLLOW_ON` under the milestone-order contract.
M12.1 through M12.5 remain `DEFERRED_HARDWARE` until the repository-owned consolidated GPU workflow is implemented and run on an authorized NVIDIA GPU host.
No milestone is `BLOCKED` at this boundary.
The M5.6 blind review instructions, self-authenticated gate, deterministic packet generator, redacted candidate batch boundary, strict adjudication schema, production qualification boundary, public CLI commands, and focused tests are implemented.
The regenerated development packet contains 1,075 selected source records, 869 moving review observations, and 7,912.507802064 meters of moving support.
Its canonical immutable SHA-256 is `ae84815296972a5cda2cfe9368206c037d1cf7a5f95605982376a5d802c1f44a`.
The packet contains no production decoder output or final-test material and remains ignored derived data.
The blind review started at `2026-08-15T01:46:44Z` and completed at `2026-08-15T01:49:55Z` against the frozen packet and protocol commit.
The implementation owner reviewed the route-level candidate continuity and all uncertain observations without viewing production decoder output or accessing final-test material.
The committed adjudication records 815 `DIRECTED_ARC` decisions over 7,473.997737213 meters, 48 `AMBIGUOUS` decisions over 401.747813115 meters, six `GRAPH_DATA_LIMITATION` decisions over 36.762251735 meters, and zero `OFF_MAP` or `UNRESOLVED` decisions.
Every non-directed decision carries no expected directed arc.
The production decoder has not run against the completed adjudication, no public coverage result has been observed, and M5.6 is not accepted.
No missing credential, dataset-license acceptance, unavailable mandatory public data, or destructive-action authorization is currently preventing local continuation.
No tag, release, deployment, package publication, or repository-visibility change has been created.

### Precise resume procedure

Resume on the existing private `main` branch with:

```console
git fetch origin
git switch main
git pull --ff-only origin main
git status --short
test "$(git rev-parse HEAD)" = "$(git ls-remote origin refs/heads/main | cut -f1)"
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
uv run cmake --build --preset developer -j2
uv run ctest --preset developer --output-on-failure
uv run cartosentry prepare-public-road-review --public-data-root data/public --output benchmark-results/m5_6_public_route_review_packet.json
jq -r .packet_immutable_sha256 benchmark-results/m5_6_public_route_review_packet.json
uv run python -c 'from pathlib import Path; from cartosentry.public_road_matching_qualification import load_public_route_adjudication; print(load_public_route_adjudication(Path("benchmarks/m5_6_public_route_adjudication.yaml"))[0].immutable_sha256)'
uv run cartosentry qualify-public-road-matching --public-data-root data/public --adjudication benchmarks/m5_6_public_route_adjudication.yaml --output benchmark-results/m5_6_public_road_matching.json
jq '{coverage: .metrics.confident_moving_distance_fraction, threshold: 0.85, accepted: .accepted, gates: .gates}' benchmark-results/m5_6_public_road_matching.json
```

The regenerated review packet must report canonical immutable SHA-256 `ae84815296972a5cda2cfe9368206c037d1cf7a5f95605982376a5d802c1f44a` before qualification resumes.
The loaded adjudication must report canonical immutable SHA-256 `47cc45d667e84e33cdb91bea264301e6c42a56687b471c04f810deac8e77d773` before qualification.
Run the production qualification once, preserve the generated report, and compare every observed metric with the frozen M5.6 gate without forcing or relabeling any non-directed decision.
If the confident moving-distance fraction is below `0.85`, do not accept M5.6 and use only the M0 route or graph pivot procedure.
If every gate passes, add the final public-path and clean-install evidence, update the measured documentation, and accept M5.6.
The required focused M5.6 commit remains `test: qualify public road matching`.
