# Directed road-graph import

CartoSentry M5.1 converts one manifest-pinned OpenStreetMap XML extract into a portable directed multigraph for later trajectory matching.
The graph represents traversal permitted by the frozen import profile, not an assertion that a road is currently legal, safe, open, or suitable for any vehicle.

## Frozen profile

`profiles/graph_import_v1.yaml` is self-hashed and binds the public object identity, OpenStreetMap attribution, included highway classes, access interpretation, directionality rules, turn-restriction support, geometry normalization, spatial index, and input budgets.
The public command rejects any XML payload whose full SHA-256 differs from the manifest authority.
The parser also rejects external entities, document type declarations, duplicate identifiers, duplicate tags, missing node references, invalid coordinates, unsupported XML roots, and inputs beyond the frozen byte or element budgets.

The profile includes the main motor-vehicle road classes from motorway through service roads.
Generic access keys are evaluated from the most specific supported key to the broadest supported key.
Explicitly denied, private, limited, unknown, construction, ferry, and conditional cases are excluded conservatively.
This conservative interpretation avoids silently treating unevaluated conditions as permitted traversal.

Forward, reverse `oneway=-1`, bidirectional, roundabout, circular-junction, motorway, and directional-access semantics are represented explicitly.
Each retained traversal direction becomes a separate arc with its own stable identity.

## Topology and geometry

Ways split at endpoints, shared source nodes, and supported turn-restriction via nodes.
Intermediate source shape nodes remain in every arc geometry.
Parallel carriageways, ramps, bridges, and tunnels remain distinct when their OpenStreetMap ways and nodes are distinct.
A geometric crossing does not create a topological connection by itself.

The graph local frame is derived mechanically from the exact center of the extract bounds.
Its axes are east, north, and up, and its zero altitude is declared as WGS84 ellipsoidal zero.
Source coordinates are retained as integer WGS84 degrees scaled by `10^7`.
Local coordinates and lengths are rounded to six decimal places before graph identity is computed.
Road geometry is explicitly flattened to local z zero because the OpenStreetMap extract does not provide an authoritative ellipsoidal height and the V1 matcher is two-dimensional.
No manual translation or rotation is accepted by the importer.

The public Boreas trajectory supplies WGS84 horizontal coordinates but does not establish an ellipsoidal altitude.
Matching observations therefore preserve the original coordinate and raw source record while declaring `ZERO_ELLIPSOID_FOR_HORIZONTAL_MATCHING` for the derived local horizontal position.
This policy is suitable for two-dimensional road projection and does not claim a source-derived height.

## Restrictions and spatial lookup

Simple `no_*` and `only_*` relations with one via node produce forbidden directed-arc transitions.
Conditional restrictions, `except` clauses, via-way relations, and unknown restriction values produce explicit `UNKNOWN_RESTRICTION` transition evidence rather than being ignored or treated as allowed.
Malformed member sets and relations whose declared ways do not connect at the via node are recorded as malformed or disconnected unknown evidence and can never be reported as applied.
The importer does not claim complete road-law evaluation.

The immutable Shapely `STRtree` indexes arc geometries in ascending arc-identity order.
Radius queries apply an exact geometry-distance filter after the bounding-box query and return candidates in ascending arc-identity order.
Crossing grade-separated arcs can both be spatial candidates while remaining topologically disconnected.

## Portable identity and attribution

The graph identity is SHA-256 over the complete canonical portable graph content except the identity field itself.
It binds the profile, source hash, source declaration, local frame, topology nodes, directed arcs, source tags, restriction evidence, and statistics.
Portable JSON retains the exact OpenStreetMap attribution, snapshot, license URL, source object key, and derivative-database classification.
The independent hand-authored test fixture is identified separately as a project test fixture and does not falsely claim OpenStreetMap provenance.

## Commands

Import the verified public extract into a portable graph artifact with:

```console
uv run cartosentry import-road-graph \
  data/public/road_graphs/toronto-glen-shields-v1.osm \
  --output output/toronto-glen-shields-directed-graph.json
```

Run the complete M5.1 qualification with:

```console
uv run cartosentry qualify-road-graph \
  --public-data-root data/public \
  --output output/m5-1-road-graph.json
```

The qualification authenticates the profile, gate, data manifest, public graph, public trajectory, and independent topology fixture.
It then checks every required fixture topology, exact graph identities and counts, portable attribution, deterministic spatial lookup, and the frozen real-observation provenance selection.

## Current measured development evidence

The pinned public extract imports 5,127 source nodes, 1,194 ways, eight restriction relations, 933 topology nodes, and 2,398 directed arcs.
It applies five supported simple restrictions and records three unsupported restrictions as unknown.
The independent fixture imports 26 directed arcs and passes the forward one-way, reverse one-way, divided-road, ramp, roundabout, parallel-road, grade-separation, restriction, and spatial-order checks.
The frozen 64-observation public selection preserves source provenance for every observation and measures a maximum horizontal local-to-WGS84 round-trip error below `0.000001 m` against the `0.001 m` engineering gate.
The portable public graph and complete qualification report are byte-identical between the pinned Python 3.12.13 and Shapely 2.1.2 environments on macOS ARM64 and the immutable Linux x86-64 qualification image.

These are deterministic development engineering results, not final map-matching accuracy, coverage, routing, legal-access, or performance claims.
HMM scoring, path decoding, ambiguity handling, and directed road bins belong to later milestones.
