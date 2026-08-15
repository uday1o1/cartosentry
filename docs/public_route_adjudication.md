# Public route adjudication

## Scope

This protocol governs the Milestone 5.6 manual review of the pinned Boreas development route.
It measures whether the selected public route has enough independently reviewable directed-road support for a development demonstration.
It does not establish confirmatory public map-matching accuracy, unseen-route generalization, or a final-test result.

The review uses only sequence `boreas-2021-09-02-11-42` from source group `boreas-glen-shields-family-v1` in the immutable `development` partition.
The sealed `final_test` partition and every final-test output are forbidden inputs.
The sequence, graph, source hashes, profiles, sample rule, labels, and numerical gate are frozen in `benchmarks/m5_6_public_road_matching_gate.yaml`.

## Blind review packet

Generate the packet from the pinned public objects with the public CLI.

```console
cartosentry prepare-public-road-review \
  --public-data-root data/public \
  --output benchmark-results/m5_6_public_route_review_packet.json
```

The packet samples every 200th source-order trajectory row and the final row.
It includes moving observations, endpoint-half-distance weights, source record identities, WGS84 positions, travel headings, and geometrically plausible directed-arc options.
Candidate options are sorted by stable directed-arc identity rather than by model score.
The packet omits emission scores, transition scores, production decoder choices, runner-up path evidence, confidence verdicts, expected labels, aggregate outcomes, and final-test identities.

The review packet is derived from Boreas and OpenStreetMap data and remains under the ignored `benchmark-results/` tree.
It must not be committed or published as a cleared portfolio artifact.

## Evidence allowed during review

The reviewer may inspect the blind packet, the pinned OpenStreetMap extract, the source route visualization, and the source trajectory fields.
The reviewer must consider lateral proximity, travel direction, named-road continuity, intersection topology, one-way direction, divided carriageways, ramps, and grade separation.
The reviewer may inspect preceding and following blind observations to resolve continuity.
The reviewer must not inspect the production decoder output or any final-test input, intermediate artifact, summary, or result.

## Decision labels

Each moving observation receives exactly one of the following labels.

- `DIRECTED_ARC` requires one exact graph-valid directed arc supported by geometry, direction, and local topology.
- `AMBIGUOUS` records two or more plausible directed arcs that the allowed evidence cannot distinguish.
- `OFF_MAP` records confident travel outside the represented road graph.
- `GRAPH_DATA_LIMITATION` records missing, stale, excluded, or insufficient graph data that prevents an exact directed-arc decision.
- `UNRESOLVED` records any other case with insufficient evidence.

Only `DIRECTED_ARC` carries an expected directed-arc identity.
Every other label must keep that field absent and therefore cannot be coerced into a road agreement.
A reviewer must choose `UNRESOLVED` whenever the evidence does not justify a stronger label.

## Review procedure

1. Verify the public inputs and generate the blind packet from the committed frozen protocol.
2. Record the packet SHA-256 and the full protocol-freeze commit before inspecting the packet.
3. Review observations in source order without running or inspecting the production decoder.
4. Record one decision for every moving observation and preserve every ambiguous, off-map, graph-limited, or unresolved decision without an expected arc.
5. Record the review start and completion timestamps, reviewer role, allowed evidence, forbidden evidence, and attestations.
6. Validate the completed adjudication file before running the production qualification.
7. Run the public qualification once the decisions are complete.

The committed adjudication is an ODbL derivative database with OpenStreetMap attribution and incorporates annotations over Boreas data attributed under CC BY 4.0.
It contains source record identities and directed-arc identities but no raw trajectory coordinates or road geometry.

## Qualification

Run the checked public path after the adjudication file exists.

```console
cartosentry qualify-public-road-matching \
  --public-data-root data/public \
  --adjudication benchmarks/m5_6_public_route_adjudication.yaml \
  --output benchmark-results/m5_6_public_road_matching.json
```

The denominator is moving trajectory distance between consecutive selected observations whose endpoint speeds both meet the frozen moving threshold.
The `endpoint-half-distance-v1` method assigns half of each eligible segment to each endpoint.
The confident numerator includes an endpoint half only when its manual label is `DIRECTED_ARC` and the frozen production decoder returns the same directed arc with confident, nonstationary support.
Manual ambiguity, off-map, graph limitation, and unresolved labels never contribute to confident road coverage, even if the decoder emits a road candidate.

Acceptance requires at least `0.85` confident moving-distance coverage.
The report separately records manually adjudicated directed-arc agreement, ambiguous distance, off-map distance, graph-limited distance, unresolved distance, and all counts.
If coverage is below `0.85`, the route or graph may change only through the Milestone 0 pivot procedure.
The confidence rule and gate must not be weakened to make the route appear complete.

## Attribution and limitations

The reviewed trajectory is from Burnett et al., "Boreas: A Multi-Season Autonomous Driving Dataset," IJRR 2023, provided by the University of Toronto Institute for Aerospace Studies under CC BY 4.0.
The road graph contains information from OpenStreetMap, available under the Open Database License 1.0.
The result is a single development-route case study from one source group and is not independent support for the later twelve-group confirmatory public claim.
