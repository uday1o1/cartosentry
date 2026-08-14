# Road-candidate scoring

CartoSentry M5.2 generates directed road candidates and scores individual emissions and transitions without forcing every observation onto a road.
Path decoding, ambiguity classification, interval output, and accuracy qualification belong to later milestones.

## Frozen parameter charter

`profiles/map_matching_v1.yaml` is a self-hashed model charter.
It binds the M5.1 graph-import profile and the numerical-charter authority used by the project.
Every search bound, uncertainty multiplier, emission scale, low-speed threshold, off-map score, transition penalty, physical limit, search budget, and rounding rule used by this model is an explicit charter key.
Changing any value invalidates the profile identity.

The charter is intentionally separate from `benchmarks/numerical_charter.yaml` because that benchmark authority was frozen before earlier milestone acceptance.
This preserves the prior acceptance evidence while freezing the M5.2 model before its own acceptance.

## Observation contract

A road-match observation contains graph-local horizontal position, exact typed time, speed, optional heading, and optional horizontal uncertainty.
Its deterministic identity binds every matching feature and any upstream normalized-observation identity.
The matching identity therefore changes when heading, speed, time, position, frame, uncertainty, or upstream provenance changes.

Position uncertainty affects search radius and lateral variance only when its basis is explicitly declared `DECLARED_TRUSTWORTHY`.
Supplying uncertainty without that declaration fails closed.
The public CLI treats an explicitly supplied `--uncertainty-m` value as the caller's declaration that the value is trustworthy.

## Candidate and emission model

The spatial search uses the immutable directed-arc index from M5.1 and an uncertainty-aware radius clamped to the frozen minimum and maximum.
Each on-road candidate records graph identity, directed arc, projected position, lateral distance, directed tangent heading, along-arc offset, search radius, and decomposed emission evidence.
Candidates retain direction, so opposite traversals of one geometric road receive different heading evidence.

The lateral term is a zero-mean Gaussian log likelihood in meters.
Trustworthy horizontal uncertainty increases the lateral standard deviation in quadrature up to the frozen maximum.
The heading term is a wrapped angular Gaussian in radians.
Heading is disabled at or below the frozen low-speed threshold and linearly reaches full weight at the frozen full-weight speed.

An explicit off-map state is always generated.
It can win the emission comparison when nearby road geometry is not plausible, and it is the only candidate when no road is within the bounded search radius.

## Directed transition model

The transition scorer compares graph-valid directed distance with observed horizontal displacement and elapsed time.
It applies chartered path-discrepancy, observed-speed-support, turn-count, and U-turn penalties.
Off-map entry, exit, and stay transitions have separate explicit scores.

The route search carries the incoming directed arc as part of its state so turn restrictions are evaluated at each transition.
One-way violations, absent directed paths, forbidden turns, unresolved restrictions, nonpositive elapsed time, over-budget search, and implausible absolute speed are rejected explicitly.
Rejected transitions expose negative infinity through the runtime `score` property so a decoder cannot select them.
Portable JSON records `possible: false`, a rejection reason, and `total_log_likelihood: null` instead of serializing a nonportable infinity value.

## Public command

First import the pinned road graph as described in `docs/road_graph_import.md`.
Then score one graph-local observation with:

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

The command validates graph identity and profile identity before emitting deterministic JSON.
The output reports all bounded on-road candidates, the unconditional off-map candidate, decomposed emission features, and the emission-only winner.

Score the transition between two real source-derived development observations with:

```console
uv run cartosentry score-road-transition \
  output/toronto-glen-shields-directed-graph.json \
  --from-x-m 418.30337 \
  --from-y-m -1043.484102 \
  --from-time-seconds 1630597338.276238200 \
  --from-speed-mps 2.0025078746077942 \
  --from-heading-rad -0.04396327434802761 \
  --from-source-observation-id observation-sha256-b2dbd538c82505c394199acd9f80c0c87e376f231b92fdcefa9216880442069d \
  --to-x-m 434.905085 \
  --to-y-m -1042.823634 \
  --to-time-seconds 1630597343.276253000 \
  --to-speed-mps 3.9688012723273056 \
  --to-heading-rad 0.09753551155083215 \
  --to-source-observation-id observation-sha256-a6c4b6b81561d4f494db7a9903e87c9b15ea904004332ff6a3f93441b5621551 \
  --output output/road-transition.json
```

The transition command selects each emission winner by default.
Use `--from-candidate` or `--to-candidate` with a reported directed arc ID or `OFF_MAP` to audit a specific candidate pair.
The report preserves both observations, both selected candidates, every transition component, and the explicit impossible-transition representation.

## Current limitations

M5.2 scores one observation or one candidate transition at a time and does not claim a matched route.
It does not yet use Viterbi decoding, beam pruning, path-separation confidence, stationary coverage suppression, or manual public-route adjudication.
The graph and projection remain two-dimensional under the authenticated flattened-height policy from M5.1.
The scorer enforces only the access and restriction semantics represented by the pinned graph-import profile and does not claim legal, safe, or current road access.
