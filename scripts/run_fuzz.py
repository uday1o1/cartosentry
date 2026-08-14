#!/usr/bin/env python3
"""Run the frozen sanitizer-backed LibFuzzer qualification suites."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import struct
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIGURATION = REPOSITORY_ROOT / "benchmarks/fuzzing.yaml"
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_PORTABLE_KEYS = frozenset(
    {
        "absolute_path",
        "host_name",
        "hostname",
        "local_context",
        "local_path",
        "machine_id",
        "source_root",
        "source_roots",
    }
)
SCHEMA_EXAMPLES = (
    "sequence-manifest.json",
    "run.json",
    "finding.json",
    "readiness-profile.json",
    "recapture-plan.json",
    "accepted-data-bundle.json",
)
SeedClass = Literal[
    "clean",
    "truncated",
    "oversized",
    "malformed",
    "duplicate",
    "endian-swapped",
]
NonemptyString = Annotated[str, StringConstraints(min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ContainerImageId = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )


def _assert_portable(value: object, *, location: str = "artifact") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"portable {location} contains a non-string key")
            if key.lower() in _FORBIDDEN_PORTABLE_KEYS:
                raise ValueError(
                    f"portable {location} contains machine-local field {key!r}"
                )
            _assert_portable(item, location=f"{location}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _assert_portable(item, location=f"{location}[{index}]")
        return
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        if (
            normalized.startswith(("/", "~/", "//", "file://"))
            or _WINDOWS_ABSOLUTE.match(value) is not None
            or ".." in normalized.split("/")
        ):
            raise ValueError(f"portable {location} contains a local or traversing path")


def _canonical_json_bytes(value: object) -> bytes:
    _assert_portable(value, location="identifier input")
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class FuzzSuiteConfig(ContractModel):
    max_total_time_seconds_per_target: int = Field(gt=0)


class FuzzLimits(ContractModel):
    artifact_json_bytes: int = Field(gt=0)
    boreas_lidar_frame_bytes: int = Field(gt=0)
    decimal_time_bytes: int = Field(gt=0)
    fault_manifest_bytes: int = Field(gt=0)
    fixture_set_manifest_bytes: int = Field(gt=0)
    ingestion_budget_bytes: int = Field(gt=0)
    frame_index_line_bytes: int = Field(gt=0)
    run_control_bytes: int = Field(gt=0)
    fuzzer_max_input_bytes: int = Field(gt=0)
    fuzzer_rss_limit_mb: int = Field(gt=0)
    fuzzer_timeout_seconds: int = Field(gt=0)


class FuzzTargetConfig(ContractModel):
    name: NonemptyString
    engine: Literal["native-libfuzzer", "atheris"]
    boundary: NonemptyString
    binary: NonemptyString | None = None
    entrypoint: NonemptyString | None = None
    minimum_instrumented_counters: int = Field(ge=0)
    minimum_covered_edges: int = Field(gt=0)
    not_applicable_seed_classes: tuple[SeedClass, ...]

    @model_validator(mode="after")
    def validate_engine_contract(self) -> FuzzTargetConfig:
        if self.engine == "native-libfuzzer":
            if self.binary is None or self.entrypoint is not None:
                raise ValueError("native fuzz targets require only a binary")
        elif self.entrypoint is None or self.binary is not None:
            raise ValueError("Atheris fuzz targets require only an entrypoint")
        return self


class FuzzConfiguration(ContractModel):
    schema_version: Literal["cartosentry.fuzz-qualification.v1"]
    qualification_id: Literal["m2.5-v1"]
    seed: int = Field(ge=0)
    required_seed_classes: tuple[SeedClass, ...]
    suites: dict[Literal["local", "nightly"], FuzzSuiteConfig]
    limits: FuzzLimits
    targets: tuple[FuzzTargetConfig, ...]


class SeedRecord(ContractModel):
    seed_class: SeedClass
    sha256: Sha256
    byte_count: int = Field(ge=0)


class FuzzTargetResult(ContractModel):
    name: NonemptyString
    engine: Literal["native-libfuzzer", "atheris"]
    boundary: NonemptyString
    target_sha256: Sha256
    accepted: bool
    return_code: int
    timed_out: bool
    elapsed_seconds: float = Field(ge=0.0)
    crash_artifact_count: int = Field(ge=0)
    instrumented_counters: int = Field(ge=0)
    covered_edges: int = Field(ge=0)
    executed_units: int = Field(ge=0)
    new_units_added: int = Field(ge=0)
    peak_rss_mb: int = Field(ge=0)
    log_sha256: Sha256
    seed_corpus_sha256: Sha256
    seed_count: int = Field(gt=0)
    seed_classes: tuple[SeedClass, ...]
    not_applicable_seed_classes: tuple[SeedClass, ...]


class FuzzQualificationReport(ContractModel):
    schema_version: Literal["cartosentry.fuzz-qualification-report.v1"]
    qualification_id: Literal["m2.5-v1"]
    accepted: bool
    suite: Literal["local", "nightly"]
    configuration_sha256: Sha256
    source_tree_sha256: Sha256
    source_revision: NonemptyString
    cmake_cache_sha256: Sha256
    container_recipe_sha256: Sha256
    container_image_id: ContainerImageId | Literal["unavailable-non-containerized"]
    architecture: NonemptyString
    operating_system: NonemptyString
    python_implementation: NonemptyString
    python_version: NonemptyString
    atheris_version: NonemptyString
    compiler_id: NonemptyString
    compiler_version: NonemptyString
    sanitizer_set: Literal[
        "native:libfuzzer+address+undefined;python:atheris-libfuzzer"
    ]
    duration_seconds_per_target: int = Field(gt=0)
    required_seed_classes: tuple[SeedClass, ...]
    targets: tuple[FuzzTargetResult, ...]

    def portable_dict(self) -> dict[str, object]:
        value = self.model_dump(mode="json")
        _assert_portable(value)
        return value


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tree_sha256() -> str:
    excluded_roots = {
        ".git",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "artifacts",
        "benchmark-results",
        "data",
        "dist",
        "evidence",
        "runs",
    }
    records: list[dict[str, object]] = []
    for directory, directory_names, file_names in os.walk(REPOSITORY_ROOT):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in excluded_roots
            and name != "__pycache__"
            and not name.startswith("build")
        )
        root = Path(directory)
        for name in sorted(file_names):
            path = root / name
            relative = path.relative_to(REPOSITORY_ROOT)
            if path.is_symlink():
                raise ValueError("source identity does not permit symbolic links")
            records.append(
                {
                    "byte_count": path.stat().st_size,
                    "path": relative.as_posix(),
                    "sha256": _file_sha256(path),
                }
            )
    return _sha256(_canonical_json_bytes(records))


def _libfuzzer_stat(log: bytes, name: str) -> int:
    prefix = f"stat::{name}:"
    for line in reversed(log.decode(errors="replace").splitlines()):
        if line.startswith(prefix):
            return int(line.removeprefix(prefix).strip())
    raise ValueError(f"LibFuzzer log is missing final statistic {name}")


def _optional_libfuzzer_stat(log: bytes, name: str) -> int:
    try:
        return _libfuzzer_stat(log, name)
    except ValueError:
        return 0


def _instrumented_counters(log: bytes) -> int:
    matches = re.findall(rb"\(([0-9]+) inline 8-bit counters\)", log)
    return sum(int(value) for value in matches)


def _covered_edges(log: bytes) -> int:
    matches = re.findall(rb"#[0-9]+\s+DONE\s+cov:\s*([0-9]+)", log)
    return int(matches[-1]) if matches else 0


def _accepted_fuzz_run(
    *,
    return_code: int,
    timed_out: bool,
    log: bytes,
    instrumented_counters: int,
    minimum_instrumented_counters: int,
    covered_edges: int,
    minimum_covered_edges: int,
    executed_units: int,
    crash_artifact_count: int,
) -> bool:
    return (
        return_code == 0
        and not timed_out
        and b"DONE" in log
        and instrumented_counters >= minimum_instrumented_counters
        and covered_edges >= minimum_covered_edges
        and executed_units > 0
        and crash_artifact_count == 0
    )


def _load_configuration(path: Path) -> tuple[FuzzConfiguration, str]:
    content = path.read_bytes()

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("fuzz qualification configuration has a duplicate key")
            result[key] = item
        return result

    value = json.loads(content, object_pairs_hook=reject_duplicate_keys)
    if not isinstance(value, dict):
        raise ValueError("fuzz qualification configuration must be an object")
    required_seed_classes = value.get("required_seed_classes")
    targets = value.get("targets")
    if not isinstance(required_seed_classes, list) or not isinstance(targets, list):
        raise ValueError("fuzz qualification lists are missing")
    converted_targets: list[dict[str, object]] = []
    for target in targets:
        if not isinstance(target, dict) or not isinstance(
            target.get("not_applicable_seed_classes"), list
        ):
            raise ValueError("fuzz target seed applicability is missing")
        converted_targets.append(
            {
                **target,
                "not_applicable_seed_classes": tuple(
                    target["not_applicable_seed_classes"]
                ),
            }
        )
    configuration = FuzzConfiguration.model_validate(
        {
            **value,
            "required_seed_classes": tuple(required_seed_classes),
            "targets": tuple(converted_targets),
        }
    )
    if len(configuration.required_seed_classes) != len(
        set(configuration.required_seed_classes)
    ):
        raise ValueError("required fuzz seed classes must be unique")
    if len(configuration.targets) != len({item.name for item in configuration.targets}):
        raise ValueError("fuzz target names must be unique")
    required_classes = set(configuration.required_seed_classes)
    for target in configuration.targets:
        not_applicable = set(target.not_applicable_seed_classes)
        if len(not_applicable) != len(target.not_applicable_seed_classes):
            raise ValueError("not-applicable fuzz seed classes must be unique")
        if not_applicable.difference(required_classes):
            raise ValueError("unknown not-applicable fuzz seed class")
    limits = configuration.limits
    required_maximum = max(
        limits.artifact_json_bytes + 2,
        limits.boreas_lidar_frame_bytes + 24,
        limits.decimal_time_bytes + 1,
        limits.fault_manifest_bytes + 2,
        limits.fixture_set_manifest_bytes + 2,
        limits.ingestion_budget_bytes + 2,
        limits.frame_index_line_bytes + 2,
        limits.run_control_bytes + 2,
    )
    if limits.fuzzer_max_input_bytes < required_maximum:
        raise ValueError("fuzzer max input does not cover oversized seeds")
    return configuration, _sha256(content)


def _artifact_seeds(limits: FuzzLimits) -> dict[SeedClass, list[bytes]]:
    clean = [
        bytes([index])
        + (REPOSITORY_ROOT / "schemas/examples/valid" / name).read_bytes()
        for index, name in enumerate(SCHEMA_EXAMPLES)
    ]
    duplicate = (
        b'1{"schema_version":"cartosentry.run.v1",'
        b'"schema_version":"cartosentry.run.v1"}'
    )
    return {
        "clean": clean,
        "truncated": [clean[0][: max(1, len(clean[0]) // 2)]],
        "oversized": [b"1" + b" " * (limits.artifact_json_bytes + 1)],
        "malformed": [b"1{"],
        "duplicate": [duplicate],
    }


def _lidar_seeds(limits: FuzzLimits) -> dict[SeedClass, list[bytes]]:
    clean = struct.pack("<6f", 1.0, 2.0, 3.0, 0.5, 1.0, 0.0)
    malformed = struct.pack("<6f", float("nan"), 2.0, 3.0, 0.5, 1.5, 0.0)
    endian_swapped = b"".join(
        clean[index : index + 4][::-1] for index in range(0, len(clean), 4)
    )
    oversized_bytes = ((limits.boreas_lidar_frame_bytes // 24) + 1) * 24
    return {
        "clean": [clean],
        "truncated": [clean[:-1]],
        "oversized": [bytes(oversized_bytes)],
        "malformed": [malformed],
        "duplicate": [clean + clean],
        "endian-swapped": [endian_swapped],
    }


def _time_seeds(limits: FuzzLimits) -> dict[SeedClass, list[bytes]]:
    return {
        "clean": [b"1630597311.041161", b"-0.0000000005"],
        "truncated": [b"-"],
        "oversized": [b"1" * (limits.decimal_time_bytes + 1)],
        "malformed": [b"nan"],
    }


def _run_control_clean_seeds() -> list[bytes]:
    from cartosentry.recovery import demo_run_inputs
    from cartosentry.runstate import (
        ArtifactManifestEntry,
        AttemptManifest,
        CompletionPointer,
    )

    cache_key = "a" * 64
    attempt = AttemptManifest(
        schema_version="cartosentry.stage-attempt.v1",
        workflow_id="run-recovery-v1",
        stage_id="normalize",
        attempt_id="attempt-000001",
        attempt_number=1,
        cache_key=cache_key,
        artifacts=(
            ArtifactManifestEntry(
                artifact_key="normalized",
                relative_path="normalized.json",
                sha256="b" * 64,
                byte_count=3,
                media_type="application/json",
            ),
        ),
    )
    completion = CompletionPointer(
        schema_version="cartosentry.stage-completion.v1",
        workflow_id="run-recovery-v1",
        stage_id="normalize",
        cache_key=cache_key,
        attempt_id="attempt-000001",
        attempt_number=1,
        artifact_directory="artifacts/normalize/attempt-000001",
        attempt_manifest_sha256="c" * 64,
    )
    return [
        b"\x02" + _canonical_json_bytes(demo_run_inputs().model_dump(mode="json")),
        b"\x03" + _canonical_json_bytes(attempt.model_dump(mode="json")),
        b"\x04" + _canonical_json_bytes(completion.model_dump(mode="json")),
    ]


def _fault_manifest_clean_seed() -> bytes:
    from cartosentry.faults import (
        FaultOperatorId,
        FaultRequest,
        inject_fault,
        load_fault_registry,
        serialize_fault_manifest,
    )

    source = (
        REPOSITORY_ROOT / "tests/fixtures/synthetic/v1/fixtures/sensor-map-dev-001.json"
    ).read_bytes()
    result = inject_fault(
        source,
        FaultRequest(
            operator_id=FaultOperatorId.TIMESTAMP_DISCONTINUITY,
            case_id="timestamp-gap-20ms-below",
            seed=1701,
            clean_source_truth_sha256="d" * 64,
        ),
        load_fault_registry(REPOSITORY_ROOT / "benchmarks/fault_matrix_v1.yaml"),
    )
    return b"\x05" + serialize_fault_manifest(result.manifest)


def _python_boundary_seeds(limits: FuzzLimits) -> dict[SeedClass, list[bytes]]:
    budget = (REPOSITORY_ROOT / "benchmarks/ingestion_budget.yaml").read_bytes()
    frame = (REPOSITORY_ROOT / "benchmarks/fuzz_seed_frame_index.json").read_bytes()
    clean = [b"\x00" + budget, b"\x01" + frame, *_run_control_clean_seeds()]
    clean.append(_fault_manifest_clean_seed())
    lidar = struct.pack("<6f", 1.0, 2.0, 3.0, 0.5, 1.0, 0.0)
    clean.append(b"\x06" + lidar)
    clean.extend(
        b"\x07" + (REPOSITORY_ROOT / "schemas/examples/valid" / name).read_bytes()
        for name in SCHEMA_EXAMPLES
    )
    clean.append(
        b"\x08"
        + (REPOSITORY_ROOT / "tests/fixtures/synthetic/v1/manifest.json").read_bytes()
    )
    endian_swapped = b"".join(
        lidar[index : index + 4][::-1] for index in range(0, len(lidar), 4)
    )
    oversized_lidar_bytes = ((limits.boreas_lidar_frame_bytes // 24) + 1) * 24
    return {
        "clean": clean,
        "truncated": [item[: max(2, len(item) // 2)] for item in clean],
        "oversized": [
            b"\x00" + b" " * (limits.ingestion_budget_bytes + 1),
            b"\x01" + b" " * (limits.frame_index_line_bytes + 1),
            b"\x02" + b" " * (limits.run_control_bytes + 1),
            b"\x05" + b" " * (limits.fault_manifest_bytes + 1),
            b"\x06" + bytes(oversized_lidar_bytes),
            b"\x07" + b" " * (limits.artifact_json_bytes + 1),
            b"\x08" + b" " * (limits.fixture_set_manifest_bytes + 1),
        ],
        "malformed": [bytes([selector]) + b"{" for selector in range(9)],
        "duplicate": [
            b'\x00{"schema_version":1,"schema_version":1}',
            b'\x01{"schema_version":"cartosentry.frame-index-entry.v1",'
            b'"schema_version":"cartosentry.frame-index-entry.v1"}',
            *[
                bytes([selector])
                + b'{"schema_version":"first","schema_version":"second"}'
                for selector in range(2, 6)
            ],
            b'\x07{"schema_version":"cartosentry.run.v1",'
            b'"schema_version":"cartosentry.run.v1"}',
            b'\x08{"schema_version":"cartosentry.synthetic-fixture-set.v1",'
            b'"schema_version":"cartosentry.synthetic-fixture-set.v1"}',
        ],
        "endian-swapped": [b"\x06" + endian_swapped],
    }


def _seed_groups(target: str, limits: FuzzLimits) -> dict[SeedClass, list[bytes]]:
    if target == "cartosentry_fuzz_artifact_json":
        return _artifact_seeds(limits)
    if target == "cartosentry_fuzz_boreas_lidar":
        return _lidar_seeds(limits)
    if target == "cartosentry_fuzz_decimal_time":
        return _time_seeds(limits)
    if target == "cartosentry_fuzz_python_boundaries":
        return _python_boundary_seeds(limits)
    raise ValueError(f"unsupported fuzz target: {target}")


def _materialize_seeds(
    target: FuzzTargetConfig,
    configuration: FuzzConfiguration,
    corpus: Path,
) -> tuple[tuple[SeedRecord, ...], str]:
    groups = _seed_groups(target.name, configuration.limits)
    applicable_classes = set(configuration.required_seed_classes).difference(
        target.not_applicable_seed_classes
    )
    if set(groups) != applicable_classes:
        raise ValueError("fuzz target seed applicability does not match its corpus")
    records: list[SeedRecord] = []
    for seed_class in configuration.required_seed_classes:
        if seed_class in target.not_applicable_seed_classes:
            continue
        for index, content in enumerate(groups[seed_class]):
            digest = _sha256(content)
            (corpus / f"{seed_class}-{index:02d}-{digest[:16]}").write_bytes(content)
            records.append(
                SeedRecord(
                    seed_class=seed_class,
                    sha256=digest,
                    byte_count=len(content),
                )
            )
    corpus_identity = _sha256(
        _canonical_json_bytes([item.model_dump(mode="json") for item in records])
    )
    return tuple(records), corpus_identity


def _cmake_set_value(document: str, key: str) -> str:
    prefix = f'set({key} "'
    for line in document.splitlines():
        if line.startswith(prefix) and line.endswith('")'):
            return line.removeprefix(prefix).removesuffix('")')
    raise ValueError(f"CMake compiler record is missing {key}")


def qualify_fuzzers(
    *,
    suite: Literal["local", "nightly"],
    configuration_path: Path,
    build_directory: Path,
    output_root: Path,
) -> FuzzQualificationReport:
    configuration, configuration_sha256 = _load_configuration(configuration_path)
    if output_root.exists():
        raise ValueError("fuzz qualification output must not already exist")
    if build_directory.is_symlink() or not build_directory.is_dir():
        raise ValueError("fuzz build directory is missing or unsafe")
    compiler_records = tuple(
        (build_directory / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake")
    )
    if len(compiler_records) != 1:
        raise ValueError("fuzz build has no unambiguous CMake compiler record")
    compiler_record = compiler_records[0].read_text()
    compiler_id = _cmake_set_value(compiler_record, "CMAKE_CXX_COMPILER_ID")
    compiler_version = _cmake_set_value(compiler_record, "CMAKE_CXX_COMPILER_VERSION")
    if "Clang" not in compiler_id:
        raise ValueError("fuzz qualification requires a Clang build")
    cmake_cache = build_directory / "CMakeCache.txt"
    if not cmake_cache.is_file() or cmake_cache.is_symlink():
        raise ValueError("fuzz build has no safe CMake cache")
    container_recipe = REPOSITORY_ROOT / "docker/fuzz-linux-x86_64.Dockerfile"
    container_image_id = os.environ.get(
        "CARTOSENTRY_CONTAINER_IMAGE_ID", "unavailable-non-containerized"
    )
    output_root.mkdir(parents=True)
    duration = configuration.suites[suite].max_total_time_seconds_per_target
    results: list[FuzzTargetResult] = []
    for target in configuration.targets:
        if target.engine == "native-libfuzzer":
            assert target.binary is not None
            target_path = build_directory / target.binary
            command_prefix = [str(target_path)]
        else:
            assert target.entrypoint is not None
            entrypoint = Path(target.entrypoint)
            if entrypoint.is_absolute() or ".." in entrypoint.parts:
                raise ValueError("Atheris entrypoint must be repository relative")
            target_path = REPOSITORY_ROOT / entrypoint
            command_prefix = [sys.executable, str(target_path)]
        if target_path.is_symlink() or not target_path.is_file():
            raise ValueError(f"fuzz target is missing or unsafe: {target.name}")
        target_root = output_root / target.name
        corpus = target_root / "corpus"
        artifacts = target_root / "artifacts"
        corpus.mkdir(parents=True)
        artifacts.mkdir()
        seeds, corpus_sha256 = _materialize_seeds(target, configuration, corpus)
        command = [
            *command_prefix,
            str(corpus),
            f"-artifact_prefix={artifacts}{os.sep}",
            f"-max_total_time={duration}",
            f"-max_len={configuration.limits.fuzzer_max_input_bytes}",
            f"-rss_limit_mb={configuration.limits.fuzzer_rss_limit_mb}",
            f"-seed={configuration.seed}",
            f"-timeout={configuration.limits.fuzzer_timeout_seconds}",
            "-print_final_stats=1",
        ]
        environment = os.environ.copy()
        environment["ASAN_OPTIONS"] = "abort_on_error=1:detect_leaks=1:symbolize=1"
        environment["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                env=environment,
                timeout=duration + configuration.limits.fuzzer_timeout_seconds + 30,
            )
            return_code = completed.returncode
            log = completed.stdout + completed.stderr
        except subprocess.TimeoutExpired as error:
            timed_out = True
            return_code = 124
            stdout = error.stdout if isinstance(error.stdout, bytes) else b""
            stderr = error.stderr if isinstance(error.stderr, bytes) else b""
            log = stdout + stderr + b"\nCartoSentry runner timeout\n"
        elapsed = time.monotonic() - started
        log_path = target_root / "fuzzer.log"
        log_path.write_bytes(log)
        instrumented_counters = _instrumented_counters(log)
        covered_edges = _covered_edges(log)
        executed_units = _optional_libfuzzer_stat(log, "number_of_executed_units")
        new_units_added = _optional_libfuzzer_stat(log, "new_units_added")
        peak_rss_mb = _optional_libfuzzer_stat(log, "peak_rss_mb")
        seed_classes = tuple(dict.fromkeys(item.seed_class for item in seeds))
        crash_artifact_count = sum(1 for _ in artifacts.iterdir())
        accepted = _accepted_fuzz_run(
            return_code=return_code,
            timed_out=timed_out,
            log=log,
            instrumented_counters=instrumented_counters,
            minimum_instrumented_counters=target.minimum_instrumented_counters,
            covered_edges=covered_edges,
            minimum_covered_edges=target.minimum_covered_edges,
            executed_units=executed_units,
            crash_artifact_count=crash_artifact_count,
        )
        results.append(
            FuzzTargetResult(
                name=target.name,
                engine=target.engine,
                boundary=target.boundary,
                target_sha256=_file_sha256(target_path),
                accepted=accepted,
                return_code=return_code,
                timed_out=timed_out,
                elapsed_seconds=round(elapsed, 6),
                crash_artifact_count=crash_artifact_count,
                instrumented_counters=instrumented_counters,
                covered_edges=covered_edges,
                executed_units=executed_units,
                new_units_added=new_units_added,
                peak_rss_mb=peak_rss_mb,
                log_sha256=_sha256(log),
                seed_corpus_sha256=corpus_sha256,
                seed_count=len(seeds),
                seed_classes=seed_classes,
                not_applicable_seed_classes=target.not_applicable_seed_classes,
            )
        )
    report = FuzzQualificationReport(
        schema_version="cartosentry.fuzz-qualification-report.v1",
        qualification_id=configuration.qualification_id,
        accepted=(
            container_image_id != "unavailable-non-containerized"
            and all(item.accepted for item in results)
        ),
        suite=suite,
        configuration_sha256=configuration_sha256,
        source_tree_sha256=_source_tree_sha256(),
        source_revision=os.environ.get("CARTOSENTRY_SOURCE_REVISION", "working-tree"),
        cmake_cache_sha256=_file_sha256(cmake_cache),
        container_recipe_sha256=_file_sha256(container_recipe),
        container_image_id=container_image_id,
        architecture=platform.machine(),
        operating_system=platform.system(),
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),
        atheris_version=importlib.metadata.version("atheris"),
        compiler_id=compiler_id,
        compiler_version=compiler_version,
        sanitizer_set=("native:libfuzzer+address+undefined;python:atheris-libfuzzer"),
        duration_seconds_per_target=duration,
        required_seed_classes=configuration.required_seed_classes,
        targets=tuple(results),
    )
    serialized = _canonical_json_bytes(report.portable_dict()) + b"\n"
    (output_root / "qualification.json").write_bytes(serialized)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("local", "nightly"), required=True)
    parser.add_argument("--configuration", type=Path, default=DEFAULT_CONFIGURATION)
    parser.add_argument("--build-dir", type=Path, default=Path("build/fuzz"))
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    report = qualify_fuzzers(
        suite=arguments.suite,
        configuration_path=arguments.configuration.resolve(),
        build_directory=arguments.build_dir.resolve(),
        output_root=arguments.output_root.resolve(),
    )
    print(json.dumps(report.portable_dict(), indent=2, sort_keys=True))
    return 0 if report.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
