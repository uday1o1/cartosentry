# Data and license record

## Scope

CartoSentry uses public external data only through exact, noninteractive retrieval rules recorded in `benchmarks/data_manifest.yaml`.
No Boreas payload, OpenStreetMap extract, license snapshot, generated thumbnail, model artifact, or other third-party raw data is tracked in Git.
The repository ships object identities, sizes, hashes, attribution, retrieval code, and bounded acquisition inputs instead.
Downloaded material belongs under the ignored `data/` tree or another caller-selected output directory.

## Partition assignment

`benchmarks/source_groups.yaml` was created before any selected sensor payload was decoded.
The initial assignment used only route and weather labels published by the official Boreas download page.
The exact official route and weather metadata bundle is 37,545 bytes with SHA-256 `f83b313107b15ca52ecc0f869645572888a34de3d08c965cf03b9f74064f3ca4`.
All clips, conversions, injected derivatives, reports, and cached artifacts must inherit their source group's partition.
An existing sequence or derivative may never move to another partition after inspection.

The selected 2021-01-19, 2021-08-05, and 2021-09-02 sequences are repeated traversals of the Glen Shields route family and therefore remain together in the development partition.
The 2021-08-05 sequence is reserved for a declared development case study and is not used for threshold or parameter selection before that run.
This structure supports only within-corridor temporal and weather comparisons.
It does not support an unseen-route or unseen-corridor generalization claim.

Official metadata identified 2021-01-26 as a snowing benchmark candidate and 2021-09-09 as an alternate-route benchmark candidate.
Anonymous S3 object enumeration showed that neither candidate publishes the postprocessed trajectory required by the V1 trajectory and map-matching contract.
Both candidates remain recorded in their originally assigned source groups with an explicit exclusion status.
They are not silently replaced, treated as passing data, or used for detector development.

## Boreas

The [Boreas project](https://www.boreas.utias.utoronto.ca/) is the primary real-data source.
The [official pyboreas repository](https://github.com/utiasASRL/pyboreas) documents the public file formats and selective anonymous S3 download workflow.
The exact documentation revision audited for this milestone is commit `e968198cd564ccfca5ad256624c80e0e584e7150`.
The official `DATA_LICENSE.md` at that revision states that Boreas and Boreas Road Trip data are licensed under [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/).
The exact terms snapshot is 315 bytes with SHA-256 `92368d96fe04291eaa6e56c22faabd9860d34f1d4ce1ecad0f0b3ba9e953a52d`.

CartoSentry's intended nonredistributed benchmark use is compatible with those terms when attribution is retained.
The project nevertheless uses a download-script-only distribution policy for Boreas sensor payloads.
Reports and documentation must attribute Burnett et al., "Boreas: A Multi-Season Autonomous Driving Dataset," IJRR 2023, and the University of Toronto Institute for Aerospace Studies.
Generated images or other derived dataset material are not cleared for public distribution merely because the source can be downloaded anonymously.

The public-smoke selection contains two development sequences.
Sequence `boreas-2021-09-02-11-42` supplies a clear-weather turn, postprocessed GPS trajectory, lidar poses, calibration, and ten consecutive lidar frames.
Sequence `boreas-2021-01-19-15-08` supplies a snow control from the same route family with the same modalities.
The manifest pins all 34 Boreas object keys individually with exact byte counts and SHA-256 hashes.

### Actual format checks

The selected `gps_post_process.csv` files expose paired GPS time, projected easting and northing, altitude, latitude, longitude, orientation, velocity, and quality fields.
The selected `lidar_poses.csv` files expose lidar timestamps and postprocessed pose, velocity, and angular-rate fields.
Latitude and longitude are stored in radians and must be converted to degrees for WGS84 interchange.
The clear route converts to an observed bounding box from latitude `43.781789450` to `43.801318111` degrees and longitude `-79.482963867` to `-79.464085207` degrees.
The ten selected lidar files are nonempty actual sensor payloads rather than mocked success fixtures.
Detailed per-point timing and coordinate qualification remains the M0.4 gate and is not claimed by this spike.

### Measured storage rates

The storage rates below are input sizing measurements and are not runtime performance results.
They are calculated from exact manifest bytes and timestamp support in the selected development files.

| Sequence and stream | Bytes | Timestamp support | Measured bytes per second |
| --- | ---: | ---: | ---: |
| 2021-09-02 postprocessed GPS | 77,453,207 | 1,073.592929 s | 72,143.924 |
| 2021-09-02 lidar poses | 2,593,799 | 1,033.520020 s | 2,509.675 |
| 2021-09-02 lidar payload | 51,165,024 | 1.036987 s | 49,340,079 |
| 2021-01-19 postprocessed GPS | 70,980,942 | 985.137182 s | 72,051.835 |
| 2021-01-19 lidar poses | 2,367,177 | 944.953186 s | 2,505.073 |
| 2021-01-19 lidar payload | 50,640,744 | 1.037568 s | 48,807,152 |

The lidar support interval adds one observed frame period to the span between the first and last selected frame timestamps.
This prevents a ten-frame sample from being divided by only nine inter-frame intervals.
The clear and snow lidar samples measure approximately `47.054 MiB/s` and `46.546 MiB/s`, respectively.

## OpenStreetMap

The road-graph source is a bounded historical [OpenStreetMap](https://www.openstreetmap.org/) extract around the Glen Shields development route.
The rectangle is recorded in `benchmarks/road_graphs/toronto_glen_shields_v1.polygon.json` and has SHA-256 `ad617dc1e59522aaf0076e53b7ddd465a13002b5bed9cab2b3a15f6e81a2ffdd`.
The Overpass query is fixed to OSM base timestamp `2026-08-14T00:47:44Z` and has SHA-256 `69e6b1765d7f1f9517cbc4c7365f9caca63d4e67ee1e51c554ce07b1feaa3690`.
The retrieved XML is 1,414,633 bytes with SHA-256 `4218be930d42d4b1fcfcf811f366a8837e4b220ffa7fdbd6415820e3a0527600`.
The query requests highway ways, turn-restriction relations, and their referenced elements, so runtime behavior does not depend on a tile server.
Overpass reports the server's current replication base in the response even when a historical query selects an earlier snapshot.
The downloader requires that replication base to be at least the requested snapshot, normalizes only that one metadata timestamp to the requested value, and then verifies the exact pinned size and hash.
Any difference in graph elements, attributes, ordering, or other response bytes is rejected.

OpenStreetMap data is made available under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
The audited canonical license page is 51,176 bytes with SHA-256 `b8d8aebb21bf405f93f6e21bbf0d8f0a749844f9812ec58692f189add6402b47`.
The downloaded XML and normalized routable graph are treated as an ODbL database and derivative database.
Public redistribution must preserve OpenStreetMap attribution, source date, the ODbL notice, and applicable share-alike obligations.
Route GeoJSON or GPX that reproduces material OSM geometry, identifiers, or attributes is conservatively classified as a derivative database.
Static maps, screenshots, and video frames are produced works and must visibly attribute OpenStreetMap and identify the ODbL source.
Metrics that contain no recoverable OSM geometry, identifiers, or substantial attributes may be classified as independent project artifacts only after a release audit records that basis.

## Storage budgets

The byte caps are enforced by the manifest validator and are separate from runtime memory budgets.

| Tier | Current pinned bytes | Hard acquisition cap | Intended use |
| --- | ---: | ---: | --- |
| `public-smoke` | 256,618,712 | 512 MiB | Local real-format and integration smoke checks. |
| `public-full` | 343,289,489 | 64 GiB | Longer correctness windows, full trajectories, and the reserved development case study outside Git. |
| `gpu-perf` | 0, to be frozen before M6 | 16 GiB | One lidar-heavy target workload sized from the measured clear rate. |

At the measured clear lidar rate, a 300-second lidar workload is approximately 13.79 GiB before small trajectory and calibration overhead.
The final `gpu-perf` object identities, interval, and hashes must be frozen before optional GPU performance qualification begins.
An empty `gpu-perf` selection is not performance evidence and cannot pass a hardware gate.

## Reproduction

The manifest and partition contract can be checked without downloading payloads.

```console
python3 scripts/verify_public_data.py --manifest-only
```

The public smoke tier can be downloaded and then verified without a browser.

```console
python3 scripts/download_public_data.py --tier public-smoke
python3 scripts/verify_public_data.py --tier public-smoke
```

The downloader writes through a process-unique partial file, synchronizes it, checks its exact size and SHA-256 identity, and only then atomically installs it.
An existing correct object is verified and reused.
An existing incorrect object is never overwritten automatically.
An unverified remote response is quarantined under a dot-prefixed rejected filename and is never accepted as benchmark input.

## Kill and pivot decision

The exact Boreas terms support the intended attributed, nonredistributed benchmark use.
The selected development sequences provide the required V1 trajectory, calibration, lidar points, frame timestamps, and per-point or per-azimuth time field documented by the source format.
The two initial benchmark candidates without public postprocessed trajectories were excluded rather than weakening the modality contract.
The project may proceed with real development evidence and synthetic untouched final-test families, but it may not claim a real unseen-corridor final evaluation from this selection.
