# Directed road matching

CartoSentry generates directed road candidates and scores emissions and transitions without forcing every observation onto a road.
M5.3 adds deterministic offline path decoding, explicit ambiguity evidence, stationary suppression, matched intervals, and a frozen synthetic qualification gate.

## Native implementation boundary

Candidate projection, emission scoring, directed transition search, transition scoring, beam-pruned Viterbi decoding, deterministic tie ranking, stationary classification, and interval construction run in C++20 batch kernels.
The narrow pybind11 boundary releases the Python global interpreter lock while those kernels execute.
Python authenticates profiles and portable inputs, composes batch payloads, validates returned evidence, constructs identity-bound artifacts, runs qualification statistics, and writes reports.
Public reports identify this implementation as `C++20_NATIVE_BATCH_V1` so stored evidence cannot silently imply a different algorithm backend.
Native decimal quantization uses nearest, ties-to-even rounding with an eight-ULP half-tie guard so declared precision is stable across the supported architectures.

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

## Offline decoder

`profiles/map_decoder_v1.yaml` is the self-hashed M5.3 decoder charter.
It binds the exact M5.2 matching-profile file and immutable identity, plus the frozen numerical charter.
The production baseline is labeled `OFFLINE_FULL_WINDOW` because it may use every observation in the supplied sequence.
It does not claim causal behavior.

The decoder uses linked backpointers and deterministic beam-pruned Viterbi search.
The charter fixes a beam width of 64, two retained hypotheses per terminal candidate, a 50-log-likelihood beam delta, a 100,000-observation input budget, and score rounding to 12 decimal places.
Impossible directed transitions are discarded and cannot enter the retained beam.
Score ties are resolved by deterministic candidate-sequence ranks.

The best complete path and the next retained complete path provide runner-up evidence.
A path-separation value at or below the chartered threshold is labeled `AMBIGUOUS` wherever the two paths select different candidates.
Ambiguous intervals retain their evidence but contribute zero usable coverage distance.

Every matched point retains the source observation, chosen directed arc or off-map state, along-arc offset, stationary flag, confidence, and any differing runner-up candidate identity.
Contiguous points with the same state, directed arc, stationary classification, and confidence become matched intervals.
Intervals retain exact observation support and first and last typed timestamps.
Off-map, ambiguous, and stationary intervals contribute zero usable distance.
A stationary run requires at least two observations at or below 0.5 m/s whose positions remain within 1 m of the first point in the run.

## Synthetic qualification

`benchmarks/m5_3_map_matching_gate.yaml` freezes the M5.3 authorities, exact scenario subsets, numerical thresholds, bootstrap unit, seed, replicate count, confidence level, support requirement, and fail-closed treatment of degenerate resamples.
The gate binds `benchmarks/m5_3_map_matching_truth.yaml` by exact file SHA-256.
That independent truth artifact freezes each scenario family, source fixture identity, observation-specification identity, expected directed path, confidence class, stationary class, and metric eligibility before acceptance.
The suite contains 28 synthetic families covering forward and reverse one-way travel, divided roads, a ramp merge, grade separation, parallel roads, a loop, a roundabout, a U-turn, sparse samples, GPS noise, a near-boundary stress case, a stopped vehicle, and 12 missing-edge off-map controls.
The 12 missing-edge controls use distinct pinned OSM topology fixtures and distinct predeclared WGS84 paths rather than translated copies of one cluster.
Twenty-five unambiguous scenarios have exact expected directed paths.
The parallel-road midpoint and stopped-vehicle cases are required to remain explicitly ambiguous.

Run the complete gate through the public CLI with:

```console
uv run cartosentry qualify-road-matching \
  --output output/m5-3-road-matching.json
```

The verified local M5.3 run used 10,000 fixed-seed cluster-bootstrap replicates.
Its synthetic directed-arc accuracy was 0.991870 with a one-sided lower 95 percent bound of 0.971429 against a 0.95 gate.
Its synthetic off-map F1 was 0.995851 with a one-sided lower 95 percent bound of 0.985915 against a 0.90 gate.
Off-map precision was 1.000000 and off-map recall was 0.991736.
All 25 exact-path scenarios passed with aggregate path edit distance zero, so the tiny-path mismatch count was zero.
Both ambiguity cases and all 26 confident controls were classified as frozen, and the stationary case suppressed usable distance.
Both gated confidence intervals were nondegenerate, all 13 off-map-positive truth clusters were present, and every bootstrap metric had zero degenerate resamples.
The canonical report is byte-identical on macOS ARM64 and pinned Linux x86-64 with SHA-256 `76a349e8fa4a7180a2c1939362947a8e6df709963c32c5a6fe0ace2dd28fed03`.
These measurements apply only to the frozen hand-authored synthetic suite and are not public-route accuracy claims.

## Current limitations

M5.3 has not yet completed manual public-route adjudication and makes no public-route accuracy or coverage claim.
The current decoder is offline only and must not be used where future observations are unavailable or prohibited.
The runner-up is the next path retained by the frozen beam, not an exhaustive posterior probability over every possible path.
The graph and projection remain two-dimensional under the authenticated flattened-height policy from M5.1.
The scorer enforces only the access and restriction semantics represented by the pinned graph-import profile and does not claim legal, safe, or current road access.
