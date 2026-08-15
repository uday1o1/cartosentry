# CartoSentry

CartoSentry implementation is paused at the verified M5.6 boundary recorded in `BUILD_PLAN.md`.
The verified public workflow and evidence-backed project description will be completed at the portfolio release-candidate milestone.

The current foundation can be installed and checked with the following commands.

```console
uv sync --frozen
uv run python -c "import cartosentry; print(cartosentry.native_self_check())"
uv run pytest
```

After materializing the manifest-pinned public development sample, the streaming LiDAR integrity qualification is available through:

```console
uv run cartosentry qualify-lidar-integrity --public-data-root data/public
```

The deterministic analytic motion-compensated LiDAR alignment qualification is available through:

```console
uv run cartosentry qualify-lidar-alignment
```

The manifest-pinned OpenStreetMap extract can be imported and qualified as a deterministic directed graph with:

```console
uv run cartosentry import-road-graph \
  data/public/road_graphs/toronto-glen-shields-v1.osm \
  --output output/toronto-glen-shields-directed-graph.json
uv run cartosentry qualify-road-graph \
  --public-data-root data/public \
  --output output/m5-1-road-graph.json
```

The [directed road-graph import contract](docs/road_graph_import.md) documents conservative access handling, directed topology, graph identity, spatial indexing, source-derived local coordinates, and OpenStreetMap attribution.

One graph-local observation can be projected onto directed road candidates and the explicit off-map state with:

```console
uv run cartosentry score-road-candidates \
  output/toronto-glen-shields-directed-graph.json \
  --x-m 418.30337 \
  --y-m -1043.484102 \
  --time-seconds 1630597338.276238200 \
  --speed-mps 2.0025078746077942 \
  --heading-rad -0.04396327434802761 \
  --source-observation-id observation-sha256-b2dbd538c82505c394199acd9f80c0c87e376f231b92fdcefa9216880442069d \
  --output output/road-candidates.json
```

The [road-candidate scoring contract](docs/road_matching.md) documents the frozen model charter, uncertainty handling, directed emissions and transitions, impossible-transition semantics, and current limitations.

The deterministic offline decoder and its complete synthetic topology gate can be exercised without public data with:

```console
uv run cartosentry qualify-road-matching \
  --output output/m5-3-road-matching.json
```

The command covers unambiguous directed paths, deliberate ambiguity, off-map behavior across 12 distinct missing-edge topology fixtures, stationary suppression, and cluster-bootstrap acceptance against a separately frozen truth artifact.

Directed road bins, independent-pass identity, modality evidence joins, and fault localization can be qualified without public data with:

```console
uv run cartosentry qualify-road-bins \
  --output output/m5-4-road-bins.json
```

The [directed road-bin contract](docs/road_bins.md) documents fixed arc-length bins, true final partial-bin lengths, confidence-based coverage eligibility, adjacent-window traversal merging, modality support, and the frozen spatial-localization gate.

Review-only repeated-trajectory topology disagreement hypotheses can be qualified without public data with:

```console
uv run cartosentry qualify-topology-hypotheses \
  --output output/m5-5-topology-hypotheses.json
```

The [topology-hypothesis contract](docs/topology_hypotheses.md) documents high-quality off-map selection, direction-aware clustering, robust corridor fitting, graph-endpoint comparison, the frozen supported synthetic gates, and the mandatory not-ground-truth label.
CartoSentry never edits the source road graph automatically.

The current versioned artifact contracts, deterministic identifiers, portable export rules, and validation commands are documented in `docs/artifact_schemas.md`.
Representative portable artifacts and their JSON Schemas are committed under `schemas`.

See `BUILD_PLAN.md` for the authoritative scope and acceptance gates.
