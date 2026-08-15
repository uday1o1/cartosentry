# Repeated-trajectory topology hypotheses

CartoSentry can group repeated, high-quality off-map trajectory intervals and compare their fitted corridor with a directed road graph.
Every output is a review hypothesis.
No output is ground truth, a road-legality claim, or authorization to edit a source map.

## Public qualification workflow

Run the frozen supported synthetic benchmark from the repository root:

```console
uv run cartosentry qualify-topology-hypotheses \
  --output output/m5-5-topology-hypotheses.json
```

The command authenticates the topology-hypothesis profile, the M5.5 gate, the road-bin profile authority, and the numerical charter before it generates any evidence.
It exits with status `0` only when every M5.5 gate passes.
It exits with status `1` when the evidence is valid but a measured gate fails.
It exits with status `2` when an input authority or execution contract is invalid.

## Selection contract

An input interval declares whether it is off map, whether its positioning support is observable, whether its travel direction is confident, whether it is stationary, and its bounded positioning-quality score.
The frozen profile excludes intervals that are on map, unobservable, directionally uncertain, stationary, below the positioning-quality threshold, or shorter than the minimum supported length.
Selection accounting is exhaustive, so every input interval is either selected once or assigned one rejection reason.
Adjacent windows may share one traversal identity.
Such windows can contribute geometry evidence without increasing the independent-traversal count.

All points must use the topology graph view's declared local coordinate frame.
The Python boundary rejects frame mismatches before native execution.
The native boundary independently rejects nonfinite points, duplicate identities, invalid graph endpoints, and work beyond the frozen interval, point, graph, pairwise-comparison, and cluster budgets.

## Direction-aware clustering

The native C++20 implementation resamples every selected interval at the frozen number of normalized arc-length positions.
Intervals are compatible only when their travel headings, start endpoints, end endpoints, and mean corresponding-point distance all satisfy the frozen bounds.
Reverse-direction trajectories therefore do not silently support a forward-direction cluster.

Selected intervals are sorted by immutable interval identity before deterministic complete-link clustering.
Complete-link membership requires a new interval to be compatible with every existing member, which prevents single-link chaining across distinct corridors.
Only clusters with the frozen minimum number of independent traversal identities may produce a hypothesis.

## Robust corridor and graph comparison

Each corridor point is the coordinate-wise median of the corresponding resampled member points.
This median fit limits the influence of isolated trajectory outliers without hiding the supporting interval identities.

The fitted start and end are compared with graph nodes inside the frozen snap radius.
A supported directed corridor between two snapped nodes with no corresponding directed graph arc can produce `POSSIBLE_MISSING_CONNECTION`.
A supported corridor whose corresponding directed graph arc exceeds the frozen mean geometry-distance threshold can produce `POSSIBLE_GEOMETRY_DISAGREEMENT`.
If a matching graph arc agrees with the corridor, the cluster remains review silent.
Parallel roads remain separate when their graph nodes and arcs remain separate.

The comparison is intentionally local to a directed endpoint pair.
It does not infer legal access, evaluate conditional restrictions, prove that a missing edge exists, or search for a preferred map edit.

## Artifact semantics

The derived `cartosentry.topology-hypothesis-report.v1` artifact records the authenticated profile identity, selected interval identities, exhaustive rejection counts, deterministic clusters, and review hypotheses.
Every hypothesis contains the literal result label `REVIEW_HYPOTHESIS_NOT_GROUND_TRUTH`.
Every hypothesis also sets `review_required` to true, `automatic_map_edit_permitted` to false, and `ground_truth_status` to `NOT_GROUND_TRUTH`.

Stable report, cluster, interval, graph-view, and hypothesis identifiers use canonical JSON and full SHA-256 digests.
The graph view can be derived from the portable directed-road graph with `make_topology_graph_view`.
Synthetic or independently generated graph views use `make_topology_graph_view_from_primitives` and remain bound to their declared source graph identity.

## Frozen benchmark

The M5.5 benchmark contains 12 independent synthetic families.
Each family contains five independent traversals for missing-connection, perturbed-geometry, altered-connection, parallel-road control, and unchanged control scenarios.
The supported positive population therefore contains 36 expected hypotheses across three mutation kinds.
The control exposure is frozen at 48 unchanged synthetic kilometers.

Precision and recall use one-sided 95 percent family-cluster bootstrap lower bounds.
Endpoint localization uses a one-sided 95 percent family-cluster bootstrap upper bound on median error measured in 20 m road-bin lengths.
The unchanged-distance metric uses an exact one-sided Poisson upper bound, including the zero-event case.

Passing this benchmark supports only the frozen synthetic mutation population.
It does not establish real-world map-change accuracy, unseen-corridor generalization, or suitability for automatic source-map modification.

## Current limitations

The fitted corridor uses two-dimensional local geometry and does not reason about elevation, lanes, construction status, permits, or time-dependent access.
The comparison considers direct directed arcs between snapped endpoints rather than choosing a legal multi-arc route.
The benchmark varies deterministic family geometry and measurement offsets but does not represent every GNSS failure mode or map convention.
Human review must examine the source graph, supporting trajectories, map provenance, and local context before acting on a hypothesis.
