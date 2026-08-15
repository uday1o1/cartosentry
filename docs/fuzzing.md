# Parser fuzzing

CartoSentry fuzzes every custom binary parser and persisted manifest boundary in the implemented V1 core.
The native targets compile the production parser library with LibFuzzer edge coverage, AddressSanitizer, and UndefinedBehaviorSanitizer.
The Python target uses Atheris to instrument and call the exact production manifest decoders.

## Boundary inventory

| Target | Production boundaries |
| --- | --- |
| `cartosentry_fuzz_artifact_json` | The six portable artifact JSON schemas and their native canonicalization boundary. |
| `cartosentry_fuzz_boreas_lidar` | Boreas little-endian float32 lidar records through the decoder shared by inspection and observability. |
| `cartosentry_fuzz_decimal_time` | Exact decimal timestamp lexemes and checked signed-int64 nanosecond conversion. |
| `cartosentry_fuzz_python_boundaries` | The Python Boreas lidar decoder, all six portable artifact schemas, fixture-set manifests, ingestion budgets, frame-index JSONL records, run inputs, stage-attempt manifests, completion pointers, and synthetic-fault manifests. |

Repository-owned configuration such as the fault matrix and qualification configuration is immutable, hash-pinned input rather than an untrusted persisted manifest boundary.
Those files are still duplicate-safe and schema-validated by their owning workflow and regression tests.

## Frozen safety limits and corpus

The repository-owned qualification configuration is [`benchmarks/fuzzing.yaml`](../benchmarks/fuzzing.yaml).
It freezes a 16 MiB artifact and lidar-frame limit, a 1 MiB run-control, fixture-set-manifest, and frame-index-record limit, a 64 KiB ingestion-budget limit, a 64-byte timestamp limit, and a 2 GiB fuzzer RSS limit.
JSON decoding rejects empty and oversized documents, nesting deeper than 64 levels, duplicate object keys, non-standard numeric constants, malformed UTF-8, schema violations, and nonportable paths.
Regular-file readers use bounded reads and do not follow symbolic links.
The lidar decoder rejects empty and partial records, nonfinite values, invalid rings, timestamp overflow, and frames over the frozen limit before publication.

Each target has deterministic clean, truncated, oversized, malformed, and duplicate seeds wherever those semantics apply.
Both lidar-decoder targets also have an endian-swapped seed.
The manifest corpus contains a clean seed for every individual Python boundary selector.
Large and synthetic corpus files are generated into the ignored evidence directory and are not committed.

## Reproducible qualification

The supported gate uses the pinned amd64 container recipe with Debian snapshot Clang 14.0.6, the Clang 14 sanitizer runtime, Python 3.12.13, and Atheris 3.1.0.
The qualification runs outside the image build so evidence can be copied from both passing and failing containers.

```console
docker build --platform linux/amd64 \
  --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  --file docker/fuzz-linux-x86_64.Dockerfile \
  --tag cartosentry-fuzz:local \
  .
python3 scripts/run_fuzz_container.py \
  --image cartosentry-fuzz:local \
  --suite local \
  --output-root benchmark-results/fuzz-local
python3 scripts/run_fuzz_container.py \
  --image cartosentry-fuzz:local \
  --suite nightly \
  --output-root benchmark-results/fuzz-nightly
```

The local suite runs every target for 5 seconds.
The extended suite runs every target for 60 seconds and is selected only through an explicit repository-owned command.
Both suites use a deterministic seed, a 5-second per-input timeout, a 2 GiB RSS limit, and an isolated crash-artifact directory.

The qualification fails closed unless every target exits successfully, reaches its frozen minimum instrumented-counter and covered-edge thresholds, executes at least one unit, prints final LibFuzzer statistics, and creates no crash artifact.
The report binds the exact configuration, source tree, source revision, target executable or entrypoint, CMake cache, container recipe, resolved container image ID, architecture, operating system, compiler, Python runtime, Atheris runtime, corpus, and log identities.
Correctness fuzzing is not a performance benchmark.

A host with a complete Clang LibFuzzer runtime may exercise the native build directly, but a non-containerized report is deliberately rejected because only the pinned Linux x86-64 container is the reproducible acceptance environment.

```console
cmake --preset fuzz
cmake --build --preset fuzz -j 4
uv run python scripts/run_fuzz.py \
  --suite local \
  --build-dir build/fuzz \
  --output-root benchmark-results/fuzz-local-native
```

## Crash handling

An infrastructure failure, process timeout, sanitizer report, missing statistic, coverage-threshold miss, or saved artifact fails the gate.
The container launcher copies available evidence before removing only the container it created.
Use the affected target with LibFuzzer's `-minimize_crash=1` and a bounded run to minimize a saved input.
Add public-safe minimized bytes or a deterministic generation recipe to the frozen corpus.
Add a focused regression that fails for the original defect and passes for a nearby valid control.
Rerun both frozen durations before accepting the fix.
