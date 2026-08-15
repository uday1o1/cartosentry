# Versioned artifact schemas

CartoSentry persists six public artifact types under explicit V1 schema names.
The committed JSON Schemas are generated from the strict Pydantic models and use JSON Schema draft 2020-12.
Every model rejects unknown fields, implicit coercion at in-process construction boundaries, nonfinite numbers, and incomplete required records.

| Artifact | Schema name | Public identifier |
|---|---|---|
| Sequence manifest | `cartosentry.sequence-manifest.v1` | `sequence_id` |
| Run | `cartosentry.run.v1` | `run_id` |
| Finding | `cartosentry.finding.v1` | `finding_id` |
| Readiness profile | `cartosentry.readiness-profile.v1` | `profile_id` |
| Recapture plan | `cartosentry.recapture-plan.v1` | `recapture_plan_id` |
| Accepted-data bundle | `cartosentry.accepted-data-bundle.v1` | `bundle_id` |

The files under `schemas/v1` are the portable interchange contract.
The files under `schemas/examples/valid` are cross-language conformance examples.
Each file under `schemas/examples/invalid` names the intended failure class and must remain invalid.

## Deterministic identity

Hashed identifiers use canonical UTF-8 JSON with lexicographically sorted object keys, no insignificant whitespace, finite numbers, and a full SHA-256 digest.
Array order remains semantic unless a particular identifier contract declares the input to be a set.
Finding stream identifiers and run configuration hashes are normalized as unordered inputs before hashing.

`sequence_id` excludes local roots and derives from normalized source identity, adapter identity, source hashes, sensor identity, calibration identity, timestamp metadata, and coordinate metadata.
`stream_id` binds the sequence, modality, and sensor identity.
`frame_id` binds its stream, portable source frame key, and capture interval.
`finding_id` binds detector and rule identity, source interval, sorted stream set, and evidence fingerprint.
`road_bin_id` binds road-graph identity, directed arc identity, and longitudinal bin index.
`run_id` binds sequence, road graph, profile, engine version, and relevant configuration hashes.

M5.4 also defines the strict identity-bound `cartosentry.directed-road-coverage.v1` derived evidence ledger in `cartosentry.road_bins`.
That ledger materializes every directed graph bin and joins traversal, modality, and finding evidence, but it is not yet one of the six top-level run artifacts exported by the public artifact registry.
Its promotion into the complete run artifact set occurs with the later readiness and end-to-end pipeline milestones.

Identifier inputs reject absolute POSIX paths, Windows paths, file URLs, path traversal, and machine-identity fields.
Local source roots therefore cannot change a portable identifier accidentally.

## Portable export

Portable artifacts cannot contain absolute paths, parent traversal, host names, machine identifiers, or local source-root fields.
The local run artifact is the only schema that may contain a `local_context` record.
Portable export removes that complete record before native validation or serialization.
Portable source references use stable relative `source_key` values instead of filesystem paths.

Validate a committed example through the public Python and C++ path:

```console
cartosentry validate-artifact schemas/examples/valid/finding.json
```

Export a local run without its machine-local context:

```console
cartosentry export-portable-artifact local-run.json portable-run.json
```

Both commands reject malformed JSON, duplicate keys, unsupported schema versions, semantic validation failures, and path leakage.
The export command validates the native round trip before it atomically publishes the destination file.

## Regeneration

Regenerate schemas and examples after an intentional contract change:

```console
uv run python scripts/generate_artifact_schemas.py
```

Check that committed generated files match the models without writing:

```console
uv run python scripts/generate_artifact_schemas.py --check
```

Any incompatible contract change requires a new schema version rather than rewriting the meaning of an existing version.
