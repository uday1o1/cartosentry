# CartoSentry

CartoSentry is under active implementation.
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

The current versioned artifact contracts, deterministic identifiers, portable export rules, and validation commands are documented in `docs/artifact_schemas.md`.
Representative portable artifacts and their JSON Schemas are committed under `schemas`.

See `BUILD_PLAN.md` for the authoritative scope and acceptance gates.
