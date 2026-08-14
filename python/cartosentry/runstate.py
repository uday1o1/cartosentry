"""Crash-safe run state, atomic stage publication, and deterministic resume."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self, cast

from pydantic import Field, StringConstraints, model_validator

from cartosentry.artifacts import (
    ArtifactReference,
    Run,
    StageRecord,
    StageState,
    canonicalize_portable_artifact,
)
from cartosentry.contracts import ContractModel, Sha256
from cartosentry.identifiers import (
    assert_portable,
    canonical_json_bytes,
    canonical_sha256,
    make_run_id,
    make_sequence_id,
)
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
)

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]
StableId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]*-sha256-[0-9a-f]{64}$"),
]
SemanticVersion = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"),
]
NonemptyString = Annotated[str, StringConstraints(min_length=1)]
NonnegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]

RUN_INPUTS_FILE = "run-inputs.json"
RUN_DATABASE_FILE = "run.sqlite3"
RUN_ARTIFACT_FILE = "run.json"
ATTEMPT_MANIFEST_FILE = "attempt-manifest.json"
RUN_LOCK_FILE = ".run.lock"
RUN_DATABASE_SCHEMA_VERSION = 1
MAX_CONTROL_FILE_BYTES = 1024 * 1024


class CommitBoundary(StrEnum):
    AFTER_DB_RUNNING = "after-db-running"
    AFTER_OUTPUTS_FSYNCED = "after-outputs-fsynced"
    AFTER_MANIFEST_FSYNCED = "after-manifest-fsynced"
    AFTER_ARTIFACT_PUBLISHED = "after-artifact-published"
    AFTER_POINTER_PUBLISHED = "after-pointer-published"
    AFTER_DB_COMMITTED = "after-db-committed"


class StageAction(StrEnum):
    ADOPTED_PUBLISHED = "ADOPTED_PUBLISHED"
    BLOCKED = "BLOCKED"
    EXECUTED = "EXECUTED"
    FAILED_FINAL = "FAILED_FINAL"
    FORCE_INVALIDATED = "FORCE_INVALIDATED"
    INTERRUPTED_RETRY = "INTERRUPTED_RETRY"
    INVALIDATED_CACHE = "INVALIDATED_CACHE"
    REPAIRED_POINTER = "REPAIRED_POINTER"
    SKIPPED_COMPLETE = "SKIPPED_COMPLETE"


class RunInputs(ContractModel):
    schema_version: Literal["cartosentry.run-inputs.v1"]
    workflow_id: Identifier
    sequence_id: StableId
    road_graph_id: StableId
    profile_id: Identifier
    engine_version: SemanticVersion
    source_hashes: dict[Identifier, Sha256]
    configuration_hashes: dict[Identifier, Sha256]
    numerical_backend: Identifier

    @model_validator(mode="after")
    def validate_source_identity(self) -> Self:
        if not self.source_hashes:
            raise ValueError("run inputs require at least one immutable source hash")
        if self.sequence_id != make_sequence_id(
            {"source_hashes": dict(sorted(self.source_hashes.items()))}
        ):
            raise ValueError("sequence_id does not match the immutable source hashes")
        return self


class ArtifactManifestEntry(ContractModel):
    artifact_key: Identifier
    relative_path: NonemptyString
    sha256: Sha256
    byte_count: NonnegativeInt
    media_type: NonemptyString

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        _validate_relative_path(self.relative_path)
        return self


class AttemptManifest(ContractModel):
    schema_version: Literal["cartosentry.stage-attempt.v1"]
    workflow_id: Identifier
    stage_id: Identifier
    attempt_id: Identifier
    attempt_number: PositiveInt
    cache_key: Sha256
    artifacts: tuple[ArtifactManifestEntry, ...]

    @model_validator(mode="after")
    def validate_artifacts(self) -> Self:
        keys = [item.artifact_key for item in self.artifacts]
        paths = [item.relative_path for item in self.artifacts]
        if not self.artifacts:
            raise ValueError("a completed attempt must contain an artifact")
        if len(keys) != len(set(keys)) or len(paths) != len(set(paths)):
            raise ValueError("attempt artifact keys and paths must be unique")
        return self


class CompletionPointer(ContractModel):
    schema_version: Literal["cartosentry.stage-completion.v1"]
    workflow_id: Identifier
    stage_id: Identifier
    cache_key: Sha256
    attempt_id: Identifier
    attempt_number: PositiveInt
    artifact_directory: NonemptyString
    attempt_manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_artifact_directory(self) -> Self:
        _validate_relative_path(self.artifact_directory)
        return self


class StageActionRecord(ContractModel):
    stage_id: Identifier
    action: StageAction
    detail: NonemptyString


class RunExecutionReport(ContractModel):
    schema_version: Literal["cartosentry.run-execution.v1"]
    workflow_id: Identifier
    run_id: StableId
    complete: bool
    forced_scope: tuple[Identifier, ...]
    actions: tuple[StageActionRecord, ...]
    stage_states: dict[Identifier, StageState]
    semantic_sha256: Sha256 | None
    incomplete_attempt_count: NonnegativeInt

    def portable_dict(self) -> dict[str, object]:
        value = self.model_dump(mode="json")
        assert_portable(value)
        return value


@dataclass(frozen=True)
class ArtifactPayload:
    artifact_key: str
    relative_path: str
    content: bytes
    media_type: str = "application/json"


@dataclass(frozen=True)
class StageExecutionContext:
    inputs: RunInputs
    stage_id: str
    cache_key: str
    upstream_artifact_hashes: Mapping[str, str]


StageExecutor = Callable[[StageExecutionContext], Sequence[ArtifactPayload]]
StageOutputValidator = Callable[
    [StageExecutionContext, Sequence[ArtifactPayload]],
    None,
]
CrashCallback = Callable[[CommitBoundary, str], None]


@dataclass(frozen=True)
class StageDefinition:
    stage_id: str
    dependencies: tuple[str, ...]
    algorithm_version: str
    relevant_configuration_keys: tuple[str, ...]
    output_keys: tuple[str, ...]
    execute: StageExecutor
    validate_outputs: StageOutputValidator


class ArtifactIntegrityError(RuntimeError):
    """A published immutable artifact failed its recorded identity."""


@dataclass(frozen=True)
class _StageRow:
    state: StageState
    attempt_id: str | None
    cache_key: str | None
    attempt_counter: int
    required_attempt_number: int
    error_code: str | None


@dataclass(frozen=True)
class _PublishedAttempt:
    directory: Path
    manifest: AttemptManifest
    manifest_sha256: str


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.as_posix() != value
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("artifact path must be a normalized portable relative path")
    assert_portable(value)


def _canonical_document(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)
    return hashlib.sha256(content).hexdigest()


def _write_json(path: Path, value: object) -> str:
    return _write_bytes(path, _canonical_document(value))


def _write_json_atomically(path: Path, value: object) -> str:
    content = _canonical_document(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return hashlib.sha256(content).hexdigest()


def _read_bounded_regular_file(path: Path, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArtifactIntegrityError("control artifact is missing or unsafe") from error
    try:
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        with stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
                raise ArtifactIntegrityError(
                    "control artifact is unsafe or exceeds the size limit"
                )
            content = stream.read(maximum_bytes + 1)
    except BaseException:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        raise
    if len(content) > maximum_bytes:
        raise ArtifactIntegrityError("control artifact exceeds the size limit")
    return content


def _decode_json(content: bytes) -> object:
    try:
        return decode_bounded_json(
            content,
            maximum_bytes=MAX_CONTROL_FILE_BYTES,
            context="control artifact",
        )
    except ManifestBoundaryError as error:
        raise ArtifactIntegrityError("control artifact is not valid JSON") from error


def parse_run_inputs_bytes(content: bytes) -> RunInputs:
    """Parse the exact persisted run-input boundary from bounded bytes."""

    _decode_json(content)
    return RunInputs.model_validate_json(content)


def parse_attempt_manifest_bytes(content: bytes) -> AttemptManifest:
    """Parse the exact persisted stage-attempt boundary from bounded bytes."""

    try:
        _decode_json(content)
        return AttemptManifest.model_validate_json(content)
    except (ArtifactIntegrityError, ValueError) as error:
        raise ArtifactIntegrityError("attempt manifest is invalid") from error


def parse_completion_pointer_bytes(content: bytes) -> CompletionPointer:
    """Parse the exact persisted stage-completion boundary from bounded bytes."""

    try:
        _decode_json(content)
        return CompletionPointer.model_validate_json(content)
    except (ArtifactIntegrityError, ValueError) as error:
        raise ArtifactIntegrityError("completion pointer is invalid") from error


def _hash_regular_file(path: Path, expected_byte_count: int) -> str:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArtifactIntegrityError(
            "published artifact is missing or unsafe"
        ) from error
    digest = hashlib.sha256()
    observed_byte_count = 0
    try:
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        with stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ArtifactIntegrityError("published artifact is not regular")
            if metadata.st_size != expected_byte_count:
                raise ArtifactIntegrityError("published artifact size mismatch")
            while chunk := stream.read(1024 * 1024):
                observed_byte_count += len(chunk)
                if observed_byte_count > expected_byte_count:
                    raise ArtifactIntegrityError("published artifact size changed")
                digest.update(chunk)
    except BaseException:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        raise
    if observed_byte_count != expected_byte_count:
        raise ArtifactIntegrityError("published artifact size changed")
    return digest.hexdigest()


def build_stage_cache_key(
    *,
    workflow_id: str,
    stage_id: str,
    source_hashes: Mapping[str, str],
    upstream_artifact_hashes: Mapping[str, str],
    relevant_configuration_hashes: Mapping[str, str],
    algorithm_version: str,
    numerical_backend: str,
) -> str:
    """Bind every semantic stage input while excluding unrelated configuration."""

    return canonical_sha256(
        {
            "algorithm_version": algorithm_version,
            "numerical_backend": numerical_backend,
            "relevant_configuration_hashes": dict(
                sorted(relevant_configuration_hashes.items())
            ),
            "source_hashes": dict(sorted(source_hashes.items())),
            "stage_id": stage_id,
            "upstream_artifact_hashes": dict(sorted(upstream_artifact_hashes.items())),
            "workflow_id": workflow_id,
        }
    )


def load_run_inputs(root: Path) -> RunInputs:
    return parse_run_inputs_bytes(
        _read_bounded_regular_file(root / RUN_INPUTS_FILE, MAX_CONTROL_FILE_BYTES)
    )


class RunDatabase:
    """Small transactional state store whose filesystem side is reconciled by hash."""

    def __init__(
        self,
        path: Path,
        *,
        inputs: RunInputs,
        definitions: Sequence[StageDefinition],
    ) -> None:
        self.path = path
        self.inputs = inputs
        self.definitions = tuple(definitions)
        self.connection = sqlite3.connect(
            path,
            timeout=5.0,
            isolation_level=None,
        )
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.execute("PRAGMA trusted_schema=OFF")
            self.connection.row_factory = sqlite3.Row
            self._initialize()
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> RunDatabase:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS run_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS stage (
              stage_id TEXT PRIMARY KEY,
              position INTEGER NOT NULL UNIQUE,
              state TEXT NOT NULL CHECK(state IN (
                'PENDING', 'RUNNING', 'COMPLETE', 'FAILED_RETRYABLE',
                'FAILED_FINAL', 'INVALIDATED', 'SKIPPED_NOT_APPLICABLE'
              )),
              attempt_id TEXT,
              cache_key TEXT,
              attempt_counter INTEGER NOT NULL DEFAULT 0 CHECK(attempt_counter >= 0),
              required_attempt_number INTEGER NOT NULL DEFAULT 0
                CHECK(required_attempt_number >= 0),
              error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS dependency (
              stage_id TEXT NOT NULL REFERENCES stage(stage_id),
              upstream_stage_id TEXT NOT NULL REFERENCES stage(stage_id),
              PRIMARY KEY(stage_id, upstream_stage_id)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS attempt (
              attempt_id TEXT PRIMARY KEY,
              stage_id TEXT NOT NULL REFERENCES stage(stage_id),
              attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
              cache_key TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('RUNNING', 'COMPLETE')),
              manifest_sha256 TEXT,
              UNIQUE(stage_id, attempt_number)
            );
            CREATE TABLE IF NOT EXISTS artifact (
              stage_id TEXT NOT NULL REFERENCES stage(stage_id),
              artifact_key TEXT NOT NULL,
              relative_path TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
              media_type TEXT NOT NULL,
              PRIMARY KEY(stage_id, artifact_key)
            ) WITHOUT ROWID;
            """
        )
        input_sha256 = canonical_sha256(self.inputs.model_dump(mode="json"))
        topology = [
            {
                "stage_id": item.stage_id,
                "dependencies": list(item.dependencies),
            }
            for item in self.definitions
        ]
        topology_sha256 = canonical_sha256(topology)
        existing = {
            str(row["key"]): str(row["value"])
            for row in self.connection.execute("SELECT key, value FROM run_meta")
        }
        expected = {
            "schema_version": str(RUN_DATABASE_SCHEMA_VERSION),
            "workflow_id": self.inputs.workflow_id,
            "input_sha256": input_sha256,
            "topology_sha256": topology_sha256,
        }
        if existing:
            if existing != expected:
                raise ValueError(
                    "run database identity or stage topology does not match this run"
                )
            return
        with self.transaction():
            self.connection.executemany(
                "INSERT INTO run_meta(key, value) VALUES (?, ?)", expected.items()
            )
            for position, definition in enumerate(self.definitions):
                self.connection.execute(
                    "INSERT INTO stage(stage_id, position, state) VALUES (?, ?, ?)",
                    (definition.stage_id, position, StageState.PENDING.value),
                )
            for definition in self.definitions:
                for upstream in definition.dependencies:
                    self.connection.execute(
                        "INSERT INTO dependency(stage_id, upstream_stage_id) "
                        "VALUES (?, ?)",
                        (definition.stage_id, upstream),
                    )

    def quick_check(self) -> None:
        row = self.connection.execute("PRAGMA quick_check").fetchone()
        if row is None or str(row[0]) != "ok":
            raise ArtifactIntegrityError("SQLite quick_check did not pass")

    def stage_row(self, stage_id: str) -> _StageRow:
        row = self.connection.execute(
            "SELECT state, attempt_id, cache_key, attempt_counter, "
            "required_attempt_number, error_code FROM stage WHERE stage_id=?",
            (stage_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown stage: {stage_id}")
        return _StageRow(
            state=StageState(str(row["state"])),
            attempt_id=cast(str | None, row["attempt_id"]),
            cache_key=cast(str | None, row["cache_key"]),
            attempt_counter=int(row["attempt_counter"]),
            required_attempt_number=int(row["required_attempt_number"]),
            error_code=cast(str | None, row["error_code"]),
        )

    def stage_states(self) -> dict[str, StageState]:
        return {
            str(row["stage_id"]): StageState(str(row["state"]))
            for row in self.connection.execute(
                "SELECT stage_id, state FROM stage ORDER BY position"
            )
        }

    def artifact_hashes(self, stage_id: str) -> dict[str, str]:
        return {
            str(row["artifact_key"]): str(row["sha256"])
            for row in self.connection.execute(
                "SELECT artifact_key, sha256 FROM artifact "
                "WHERE stage_id=? ORDER BY artifact_key",
                (stage_id,),
            )
        }

    def artifact_rows(self) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.connection.execute(
                "SELECT stage_id, artifact_key, relative_path, sha256, "
                "byte_count, media_type FROM artifact "
                "ORDER BY stage_id, artifact_key"
            )
        )

    def begin_attempt(self, stage_id: str, cache_key: str) -> tuple[str, int]:
        with self.transaction():
            row = self.stage_row(stage_id)
            if row.state not in {
                StageState.PENDING,
                StageState.FAILED_RETRYABLE,
                StageState.INVALIDATED,
            }:
                raise ValueError(
                    f"stage {stage_id} cannot begin from state {row.state.value}"
                )
            incomplete_upstream = self.connection.execute(
                "SELECT COUNT(*) FROM dependency d JOIN stage s "
                "ON s.stage_id=d.upstream_stage_id "
                "WHERE d.stage_id=? AND s.state != 'COMPLETE'",
                (stage_id,),
            ).fetchone()
            if incomplete_upstream is None or int(incomplete_upstream[0]) != 0:
                raise ValueError("stage cannot begin before every dependency completes")
            attempt_number = row.attempt_counter + 1
            attempt_id = f"attempt-{stage_id}-{attempt_number:08d}-{uuid.uuid4().hex}"
            self.connection.execute(
                "UPDATE stage SET state='RUNNING', attempt_id=?, cache_key=?, "
                "attempt_counter=?, error_code=NULL WHERE stage_id=?",
                (attempt_id, cache_key, attempt_number, stage_id),
            )
            self.connection.execute(
                "INSERT INTO attempt(attempt_id, stage_id, attempt_number, "
                "cache_key, state) VALUES (?, ?, ?, ?, 'RUNNING')",
                (attempt_id, stage_id, attempt_number, cache_key),
            )
        return attempt_id, attempt_number

    def record_complete(
        self,
        manifest: AttemptManifest,
        manifest_sha256: str,
        artifact_directory: str,
    ) -> None:
        with self.transaction():
            row = self.stage_row(manifest.stage_id)
            if row.state not in {
                StageState.PENDING,
                StageState.RUNNING,
                StageState.COMPLETE,
                StageState.FAILED_RETRYABLE,
                StageState.INVALIDATED,
            }:
                raise ValueError(
                    f"stage {manifest.stage_id} cannot reconcile completion from "
                    f"state {row.state.value}"
                )
            self.connection.execute(
                "DELETE FROM artifact WHERE stage_id=?", (manifest.stage_id,)
            )
            self.connection.executemany(
                "INSERT INTO artifact(stage_id, artifact_key, relative_path, "
                "sha256, byte_count, media_type) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        manifest.stage_id,
                        item.artifact_key,
                        f"{artifact_directory}/{item.relative_path}",
                        item.sha256,
                        item.byte_count,
                        item.media_type,
                    )
                    for item in manifest.artifacts
                ],
            )
            self.connection.execute(
                "INSERT INTO attempt(attempt_id, stage_id, attempt_number, "
                "cache_key, state, manifest_sha256) VALUES (?, ?, ?, ?, "
                "'COMPLETE', ?) ON CONFLICT(attempt_id) DO UPDATE SET "
                "state='COMPLETE', manifest_sha256=excluded.manifest_sha256",
                (
                    manifest.attempt_id,
                    manifest.stage_id,
                    manifest.attempt_number,
                    manifest.cache_key,
                    manifest_sha256,
                ),
            )
            self.connection.execute(
                "UPDATE stage SET state='COMPLETE', attempt_id=?, cache_key=?, "
                "attempt_counter=MAX(attempt_counter, ?), "
                "required_attempt_number=0, error_code=NULL WHERE stage_id=?",
                (
                    manifest.attempt_id,
                    manifest.cache_key,
                    manifest.attempt_number,
                    manifest.stage_id,
                ),
            )

    def mark_state(
        self,
        stage_id: str,
        state: StageState,
        *,
        error_code: str | None,
    ) -> None:
        with self.transaction():
            current = self.stage_row(stage_id).state
            allowed: dict[StageState, set[StageState]] = {
                StageState.PENDING: {
                    StageState.PENDING,
                    StageState.FAILED_FINAL,
                },
                StageState.RUNNING: {
                    StageState.RUNNING,
                    StageState.FAILED_RETRYABLE,
                    StageState.FAILED_FINAL,
                },
                StageState.COMPLETE: {
                    StageState.COMPLETE,
                    StageState.FAILED_FINAL,
                },
                StageState.FAILED_RETRYABLE: {
                    StageState.FAILED_RETRYABLE,
                    StageState.FAILED_FINAL,
                },
                StageState.FAILED_FINAL: {StageState.FAILED_FINAL},
                StageState.INVALIDATED: {
                    StageState.INVALIDATED,
                    StageState.FAILED_FINAL,
                },
                StageState.SKIPPED_NOT_APPLICABLE: {StageState.SKIPPED_NOT_APPLICABLE},
            }
            if state not in allowed[current]:
                raise ValueError(
                    f"invalid stage transition: {current.value} -> {state.value}"
                )
            self.connection.execute(
                "UPDATE stage SET state=?, error_code=? WHERE stage_id=?",
                (state.value, error_code, stage_id),
            )

    def invalidate(self, stage_ids: Sequence[str], *, force: bool) -> None:
        with self.transaction():
            for stage_id in stage_ids:
                row = self.stage_row(stage_id)
                required = row.attempt_counter + 1 if force else 0
                self.connection.execute(
                    "DELETE FROM artifact WHERE stage_id=?", (stage_id,)
                )
                self.connection.execute(
                    "UPDATE stage SET state='INVALIDATED', attempt_id=NULL, "
                    "cache_key=NULL, required_attempt_number=?, error_code=NULL "
                    "WHERE stage_id=?",
                    (required, stage_id),
                )


def validate_stage_definitions(definitions: Sequence[StageDefinition]) -> None:
    if not definitions:
        raise ValueError("at least one stage definition is required")
    seen: set[str] = set()
    for item in definitions:
        if not item.stage_id or item.stage_id in seen:
            raise ValueError("stage identifiers must be nonempty and unique")
        if any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_."
            for character in item.stage_id
        ):
            raise ValueError("stage identifier is not portable")
        if not item.algorithm_version or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for character in item.algorithm_version
        ):
            raise ValueError("stage algorithm version is required")
        if not item.output_keys or len(item.output_keys) != len(set(item.output_keys)):
            raise ValueError("stage output keys must be nonempty and unique")
        if len(item.dependencies) != len(set(item.dependencies)):
            raise ValueError("stage dependencies must be unique")
        if any(upstream not in seen for upstream in item.dependencies):
            raise ValueError("stage definitions must be in dependency order")
        if len(item.relevant_configuration_keys) != len(
            set(item.relevant_configuration_keys)
        ):
            raise ValueError("relevant configuration keys must be unique")
        seen.add(item.stage_id)


class RunEngine:
    """Execute a fixed stage DAG with durable state and immutable attempts."""

    def __init__(
        self,
        root: Path,
        inputs: RunInputs,
        definitions: Sequence[StageDefinition],
    ) -> None:
        validate_stage_definitions(definitions)
        self.root = root
        self.inputs = inputs
        self.definitions = tuple(definitions)
        self.definition_by_id = {item.stage_id: item for item in self.definitions}
        self._closed = False
        lock_flags = os.O_CREAT | os.O_RDWR
        lock_flags |= getattr(os, "O_CLOEXEC", 0)
        lock_flags |= getattr(os, "O_NOFOLLOW", 0)
        lock_descriptor = os.open(root / RUN_LOCK_FILE, lock_flags, 0o600)
        try:
            self._lock_stream = os.fdopen(lock_descriptor, "a+b")
        except BaseException:
            os.close(lock_descriptor)
            raise
        try:
            fcntl.flock(
                self._lock_stream.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            self.database = RunDatabase(
                root / RUN_DATABASE_FILE,
                inputs=inputs,
                definitions=self.definitions,
            )
        except BlockingIOError as error:
            self._lock_stream.close()
            self._closed = True
            raise ValueError("run is already active in another process") from error
        except BaseException:
            self._lock_stream.close()
            self._closed = True
            raise

    @classmethod
    def create(
        cls,
        root: Path,
        inputs: RunInputs,
        definitions: Sequence[StageDefinition],
    ) -> RunEngine:
        if root.exists():
            raise ValueError("run root must not already exist")
        root.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir()
        for name in (".attempts", "artifacts", "completions"):
            (root / name).mkdir()
        _write_json(root / RUN_INPUTS_FILE, inputs.model_dump(mode="json"))
        _fsync_directory(root)
        engine = cls(root, inputs, definitions)
        try:
            _fsync_directory(root)
            engine.export_run_artifact()
        except BaseException:
            engine.close()
            raise
        return engine

    @classmethod
    def open(
        cls,
        root: Path,
        definitions: Sequence[StageDefinition],
    ) -> RunEngine:
        if not root.is_dir() or root.is_symlink():
            raise ValueError("run root is missing or invalid")
        inputs = load_run_inputs(root)
        return cls(root, inputs, definitions)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.database.close()
        finally:
            try:
                fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_stream.close()

    def __enter__(self) -> RunEngine:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def dependency_closure(self, stage_id: str) -> tuple[str, ...]:
        if stage_id not in self.definition_by_id:
            raise ValueError(f"unknown stage: {stage_id}")
        affected = {stage_id}
        changed = True
        while changed:
            changed = False
            for item in self.definitions:
                if item.stage_id in affected:
                    continue
                if any(upstream in affected for upstream in item.dependencies):
                    affected.add(item.stage_id)
                    changed = True
        return tuple(
            item.stage_id for item in self.definitions if item.stage_id in affected
        )

    def _upstream_hashes(self, definition: StageDefinition) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for upstream in definition.dependencies:
            if self.database.stage_row(upstream).state is not StageState.COMPLETE:
                raise ValueError("upstream stage is incomplete")
            for artifact_key, digest in self.database.artifact_hashes(upstream).items():
                hashes[f"{upstream}:{artifact_key}"] = digest
        return hashes

    def cache_key(self, definition: StageDefinition) -> str:
        relevant: dict[str, str] = {}
        for key in definition.relevant_configuration_keys:
            try:
                relevant[key] = self.inputs.configuration_hashes[key]
            except KeyError as error:
                raise ValueError(
                    f"stage {definition.stage_id} requires configuration hash {key}"
                ) from error
        return build_stage_cache_key(
            workflow_id=self.inputs.workflow_id,
            stage_id=definition.stage_id,
            source_hashes=self.inputs.source_hashes,
            upstream_artifact_hashes=self._upstream_hashes(definition),
            relevant_configuration_hashes=relevant,
            algorithm_version=definition.algorithm_version,
            numerical_backend=self.inputs.numerical_backend,
        )

    def _attempt_directory(self, attempt_id: str) -> Path:
        return self.root / ".attempts" / attempt_id

    def _published_cache_root(self, stage_id: str, cache_key: str) -> Path:
        return self.root / "artifacts" / stage_id / cache_key

    def _completion_path(self, stage_id: str) -> Path:
        return self.root / "completions" / f"{stage_id}.json"

    def _verify_attempt_directory(self, directory: Path) -> _PublishedAttempt:
        manifest_path = directory / ATTEMPT_MANIFEST_FILE
        try:
            manifest_bytes = _read_bounded_regular_file(
                manifest_path, MAX_CONTROL_FILE_BYTES
            )
            manifest = parse_attempt_manifest_bytes(manifest_bytes)
        except (ArtifactIntegrityError, ValueError) as error:
            raise ArtifactIntegrityError("attempt manifest is invalid") from error
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if directory.name != manifest.attempt_id:
            raise ArtifactIntegrityError("attempt directory identity mismatch")
        observed_keys: set[str] = set()
        for artifact in manifest.artifacts:
            path = directory / PurePosixPath(artifact.relative_path)
            if _hash_regular_file(path, artifact.byte_count) != artifact.sha256:
                raise ArtifactIntegrityError("published artifact hash mismatch")
            observed_keys.add(artifact.artifact_key)
        if observed_keys != {item.artifact_key for item in manifest.artifacts}:
            raise ArtifactIntegrityError("published artifact set mismatch")
        return _PublishedAttempt(directory, manifest, manifest_sha256)

    def _select_published(
        self,
        definition: StageDefinition,
        cache_key: str,
    ) -> _PublishedAttempt | None:
        cache_root = self._published_cache_root(definition.stage_id, cache_key)
        if not cache_root.exists():
            return None
        if cache_root.is_symlink() or not cache_root.is_dir():
            raise ArtifactIntegrityError("published cache root is invalid")
        attempts: list[_PublishedAttempt] = []
        for candidate in sorted(cache_root.iterdir(), key=lambda item: item.name):
            if candidate.is_symlink() or not candidate.is_dir():
                raise ArtifactIntegrityError("published attempt entry is invalid")
            attempt = self._verify_attempt_directory(candidate)
            if (
                attempt.manifest.workflow_id != self.inputs.workflow_id
                or attempt.manifest.stage_id != definition.stage_id
                or attempt.manifest.cache_key != cache_key
            ):
                raise ArtifactIntegrityError("published attempt identity mismatch")
            if tuple(item.artifact_key for item in attempt.manifest.artifacts) != tuple(
                sorted(definition.output_keys)
            ):
                raise ArtifactIntegrityError(
                    "published attempt output contract mismatch"
                )
            attempts.append(attempt)
        if not attempts:
            return None
        fingerprints = {
            tuple(
                (item.artifact_key, item.sha256, item.byte_count)
                for item in attempt.manifest.artifacts
            )
            for attempt in attempts
        }
        if len(fingerprints) != 1:
            raise ArtifactIntegrityError(
                "one cache key produced different immutable artifact bytes"
            )
        return max(
            attempts,
            key=lambda item: (
                item.manifest.attempt_number,
                item.manifest.attempt_id,
            ),
        )

    def _publish_pointer(self, attempt: _PublishedAttempt) -> bool:
        relative_directory = attempt.directory.relative_to(self.root).as_posix()
        pointer = CompletionPointer(
            schema_version="cartosentry.stage-completion.v1",
            workflow_id=self.inputs.workflow_id,
            stage_id=attempt.manifest.stage_id,
            cache_key=attempt.manifest.cache_key,
            attempt_id=attempt.manifest.attempt_id,
            attempt_number=attempt.manifest.attempt_number,
            artifact_directory=relative_directory,
            attempt_manifest_sha256=attempt.manifest_sha256,
        )
        path = self._completion_path(attempt.manifest.stage_id)
        expected = pointer.model_dump(mode="json")
        if path.exists():
            try:
                existing = parse_completion_pointer_bytes(
                    _read_bounded_regular_file(path, MAX_CONTROL_FILE_BYTES)
                )
                if existing.model_dump(mode="json") == expected:
                    return False
            except (ArtifactIntegrityError, ValueError):
                pass
        _write_json_atomically(path, expected)
        return True

    def reconcile(self) -> list[StageActionRecord]:
        self.database.quick_check()
        actions: list[StageActionRecord] = []
        invalidated: set[str] = set()
        for definition in self.definitions:
            row = self.database.stage_row(definition.stage_id)
            if any(
                self.database.stage_row(upstream).state is not StageState.COMPLETE
                for upstream in definition.dependencies
            ):
                if row.state is StageState.COMPLETE:
                    scope = self.dependency_closure(definition.stage_id)
                    self.database.invalidate(scope, force=False)
                    invalidated.update(scope)
                continue
            expected_cache = self.cache_key(definition)
            row = self.database.stage_row(definition.stage_id)
            if row.state is StageState.COMPLETE and row.cache_key != expected_cache:
                scope = self.dependency_closure(definition.stage_id)
                self.database.invalidate(scope, force=False)
                invalidated.update(scope)
                actions.append(
                    StageActionRecord(
                        stage_id=definition.stage_id,
                        action=StageAction.INVALIDATED_CACHE,
                        detail=(
                            "Semantic cache inputs changed for this dependency closure."
                        ),
                    )
                )
                row = self.database.stage_row(definition.stage_id)
            try:
                published = self._select_published(definition, expected_cache)
            except ArtifactIntegrityError:
                self.database.mark_state(
                    definition.stage_id,
                    StageState.FAILED_FINAL,
                    error_code="ARTIFACT_INTEGRITY",
                )
                actions.append(
                    StageActionRecord(
                        stage_id=definition.stage_id,
                        action=StageAction.FAILED_FINAL,
                        detail="Published immutable artifacts failed hash validation.",
                    )
                )
                continue
            if published is not None and (
                published.manifest.attempt_number >= row.required_attempt_number
            ):
                repaired = self._publish_pointer(published)
                was_complete = (
                    row.state is StageState.COMPLETE
                    and row.attempt_id == published.manifest.attempt_id
                )
                self.database.record_complete(
                    published.manifest,
                    published.manifest_sha256,
                    published.directory.relative_to(self.root).as_posix(),
                )
                if repaired:
                    actions.append(
                        StageActionRecord(
                            stage_id=definition.stage_id,
                            action=StageAction.REPAIRED_POINTER,
                            detail=(
                                "Rebuilt the completion pointer from verified hashes."
                            ),
                        )
                    )
                elif not was_complete:
                    actions.append(
                        StageActionRecord(
                            stage_id=definition.stage_id,
                            action=StageAction.ADOPTED_PUBLISHED,
                            detail="Adopted a verified filesystem publish into SQLite.",
                        )
                    )
                continue
            if row.state is StageState.COMPLETE:
                self.database.mark_state(
                    definition.stage_id,
                    StageState.FAILED_FINAL,
                    error_code="PUBLISHED_ARTIFACT_MISSING",
                )
                actions.append(
                    StageActionRecord(
                        stage_id=definition.stage_id,
                        action=StageAction.FAILED_FINAL,
                        detail=(
                            "SQLite recorded completion but verified artifacts are "
                            "missing."
                        ),
                    )
                )
            elif row.state is StageState.RUNNING:
                self.database.mark_state(
                    definition.stage_id,
                    StageState.FAILED_RETRYABLE,
                    error_code="INTERRUPTED_ATTEMPT",
                )
                actions.append(
                    StageActionRecord(
                        stage_id=definition.stage_id,
                        action=StageAction.INTERRUPTED_RETRY,
                        detail="A running attempt ended before immutable publication.",
                    )
                )
        if invalidated:
            for stage_id in invalidated:
                if not any(item.stage_id == stage_id for item in actions):
                    actions.append(
                        StageActionRecord(
                            stage_id=stage_id,
                            action=StageAction.INVALIDATED_CACHE,
                            detail="An upstream semantic cache input changed.",
                        )
                    )
        return actions

    def _execute_stage(
        self,
        definition: StageDefinition,
        *,
        crash_callback: CrashCallback | None,
    ) -> None:
        cache_key = self.cache_key(definition)
        attempt_id, attempt_number = self.database.begin_attempt(
            definition.stage_id, cache_key
        )
        self.export_run_artifact()
        if crash_callback is not None:
            crash_callback(CommitBoundary.AFTER_DB_RUNNING, definition.stage_id)
        attempt_directory = self._attempt_directory(attempt_id)
        attempt_directory.mkdir()
        try:
            context = StageExecutionContext(
                inputs=self.inputs,
                stage_id=definition.stage_id,
                cache_key=cache_key,
                upstream_artifact_hashes=self._upstream_hashes(definition),
            )
            payloads = tuple(definition.execute(context))
            if tuple(sorted(item.artifact_key for item in payloads)) != tuple(
                sorted(definition.output_keys)
            ):
                raise ValueError("stage output keys do not match their definition")
            if len({item.relative_path for item in payloads}) != len(payloads):
                raise ValueError("stage output paths must be unique")
            definition.validate_outputs(context, payloads)
            entries: list[ArtifactManifestEntry] = []
            for payload in sorted(payloads, key=lambda item: item.artifact_key):
                _validate_relative_path(payload.relative_path)
                if not payload.artifact_key or not payload.media_type:
                    raise ValueError("artifact identity and media type are required")
                digest = _write_bytes(
                    attempt_directory / PurePosixPath(payload.relative_path),
                    payload.content,
                )
                entries.append(
                    ArtifactManifestEntry(
                        artifact_key=payload.artifact_key,
                        relative_path=payload.relative_path,
                        sha256=digest,
                        byte_count=len(payload.content),
                        media_type=payload.media_type,
                    )
                )
            _fsync_directory(attempt_directory)
            if crash_callback is not None:
                crash_callback(
                    CommitBoundary.AFTER_OUTPUTS_FSYNCED, definition.stage_id
                )
            manifest = AttemptManifest(
                schema_version="cartosentry.stage-attempt.v1",
                workflow_id=self.inputs.workflow_id,
                stage_id=definition.stage_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                cache_key=cache_key,
                artifacts=tuple(entries),
            )
            _write_json(
                attempt_directory / ATTEMPT_MANIFEST_FILE,
                manifest.model_dump(mode="json"),
            )
            _fsync_directory(attempt_directory)
            if crash_callback is not None:
                crash_callback(
                    CommitBoundary.AFTER_MANIFEST_FSYNCED, definition.stage_id
                )
            cache_root = self._published_cache_root(definition.stage_id, cache_key)
            cache_root.mkdir(parents=True, exist_ok=True)
            _fsync_directory(cache_root.parent)
            published_directory = cache_root / attempt_id
            os.replace(attempt_directory, published_directory)
            _fsync_directory(cache_root)
            if crash_callback is not None:
                crash_callback(
                    CommitBoundary.AFTER_ARTIFACT_PUBLISHED, definition.stage_id
                )
            published = self._verify_attempt_directory(published_directory)
            self._publish_pointer(published)
            if crash_callback is not None:
                crash_callback(
                    CommitBoundary.AFTER_POINTER_PUBLISHED, definition.stage_id
                )
            self.database.record_complete(
                manifest,
                published.manifest_sha256,
                published_directory.relative_to(self.root).as_posix(),
            )
            self.export_run_artifact()
            if crash_callback is not None:
                crash_callback(CommitBoundary.AFTER_DB_COMMITTED, definition.stage_id)
        except BaseException:
            if self.database.stage_row(definition.stage_id).state is StageState.RUNNING:
                self.database.mark_state(
                    definition.stage_id,
                    StageState.FAILED_RETRYABLE,
                    error_code="STAGE_EXECUTION_FAILED",
                )
                self.export_run_artifact()
            raise

    def semantic_sha256(self) -> str | None:
        states = self.database.stage_states()
        if any(state is not StageState.COMPLETE for state in states.values()):
            return None
        return canonical_sha256(
            {stage_id: self.database.artifact_hashes(stage_id) for stage_id in states}
        )

    def incomplete_attempts(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                item.name
                for item in (self.root / ".attempts").iterdir()
                if item.is_dir() and not item.is_symlink()
            )
        )

    def export_run_artifact(self) -> None:
        states = self.database.stage_states()
        if states and all(
            state in {StageState.COMPLETE, StageState.SKIPPED_NOT_APPLICABLE}
            for state in states.values()
        ):
            run_state = StageState.COMPLETE
        elif any(state is StageState.FAILED_FINAL for state in states.values()):
            run_state = StageState.FAILED_FINAL
        elif any(state is StageState.RUNNING for state in states.values()):
            run_state = StageState.RUNNING
        elif any(state is StageState.FAILED_RETRYABLE for state in states.values()):
            run_state = StageState.FAILED_RETRYABLE
        elif any(state is StageState.INVALIDATED for state in states.values()):
            run_state = StageState.INVALIDATED
        else:
            run_state = StageState.PENDING
        stage_records = {
            definition.stage_id: StageRecord(
                state=self.database.stage_row(definition.stage_id).state,
                attempt_id=self.database.stage_row(definition.stage_id).attempt_id,
                output_hashes=self.database.artifact_hashes(definition.stage_id),
            )
            for definition in self.definitions
        }
        artifacts = tuple(
            ArtifactReference(
                source_key=str(row["relative_path"]),
                sha256=str(row["sha256"]),
                byte_count=int(row["byte_count"]),
                media_type=str(row["media_type"]),
            )
            for row in self.database.artifact_rows()
        )
        run_id = make_run_id(
            sequence_id=self.inputs.sequence_id,
            road_graph_id=self.inputs.road_graph_id,
            profile_id=self.inputs.profile_id,
            engine_version=self.inputs.engine_version,
            configuration_hashes=self.inputs.configuration_hashes,
        )
        run = Run(
            schema_version="cartosentry.run.v1",
            run_id=run_id,
            sequence_id=self.inputs.sequence_id,
            road_graph_id=self.inputs.road_graph_id,
            profile_id=self.inputs.profile_id,
            engine_version=self.inputs.engine_version,
            configuration_hashes=self.inputs.configuration_hashes,
            state=run_state,
            stages=stage_records,
            artifacts=artifacts,
            local_context=None,
        )
        serialized = canonicalize_portable_artifact(run) + "\n"
        _write_json_atomically(
            self.root / RUN_ARTIFACT_FILE,
            json.loads(serialized),
        )

    def run(
        self,
        *,
        force_stage: str | None = None,
        dry_run: bool = False,
        crash_callback: CrashCallback | None = None,
    ) -> RunExecutionReport:
        forced_scope: tuple[str, ...] = ()
        if force_stage is not None:
            forced_scope = self.dependency_closure(force_stage)
            if dry_run:
                return self._report((), forced_scope)
        elif dry_run:
            raise ValueError("dry-run requires force-stage")
        actions = self.reconcile()
        if force_stage is not None:
            self.database.invalidate(forced_scope, force=True)
            actions.extend(
                StageActionRecord(
                    stage_id=stage_id,
                    action=StageAction.FORCE_INVALIDATED,
                    detail=(
                        "The selected force-stage dependency closure was invalidated."
                    ),
                )
                for stage_id in forced_scope
            )
        force_set = set(forced_scope)
        for definition in self.definitions:
            row = self.database.stage_row(definition.stage_id)
            if (
                row.state is StageState.COMPLETE
                and definition.stage_id not in force_set
            ):
                actions.append(
                    StageActionRecord(
                        stage_id=definition.stage_id,
                        action=StageAction.SKIPPED_COMPLETE,
                        detail=(
                            "Verified complete artifacts matched the semantic cache "
                            "key."
                        ),
                    )
                )
                continue
            if row.state is StageState.FAILED_FINAL:
                actions.append(
                    StageActionRecord(
                        stage_id=definition.stage_id,
                        action=StageAction.FAILED_FINAL,
                        detail=(
                            "A final artifact-integrity failure requires intervention."
                        ),
                    )
                )
                break
            if any(
                self.database.stage_row(upstream).state is not StageState.COMPLETE
                for upstream in definition.dependencies
            ):
                actions.append(
                    StageActionRecord(
                        stage_id=definition.stage_id,
                        action=StageAction.BLOCKED,
                        detail="An upstream stage is not complete.",
                    )
                )
                continue
            self._execute_stage(definition, crash_callback=crash_callback)
            actions.append(
                StageActionRecord(
                    stage_id=definition.stage_id,
                    action=StageAction.EXECUTED,
                    detail="Published and committed a new immutable stage attempt.",
                )
            )
        self.export_run_artifact()
        return self._report(actions, forced_scope)

    def _report(
        self,
        actions: Sequence[StageActionRecord],
        forced_scope: Sequence[str],
    ) -> RunExecutionReport:
        states = self.database.stage_states()
        run_id = make_run_id(
            sequence_id=self.inputs.sequence_id,
            road_graph_id=self.inputs.road_graph_id,
            profile_id=self.inputs.profile_id,
            engine_version=self.inputs.engine_version,
            configuration_hashes=self.inputs.configuration_hashes,
        )
        return RunExecutionReport(
            schema_version="cartosentry.run-execution.v1",
            workflow_id=self.inputs.workflow_id,
            run_id=run_id,
            complete=all(state is StageState.COMPLETE for state in states.values()),
            forced_scope=tuple(forced_scope),
            actions=tuple(actions),
            stage_states=states,
            semantic_sha256=self.semantic_sha256(),
            incomplete_attempt_count=len(self.incomplete_attempts()),
        )


__all__ = [
    "ArtifactIntegrityError",
    "ArtifactPayload",
    "CommitBoundary",
    "CrashCallback",
    "RunEngine",
    "RunExecutionReport",
    "RunInputs",
    "StageAction",
    "StageActionRecord",
    "StageDefinition",
    "StageExecutionContext",
    "StageOutputValidator",
    "build_stage_cache_key",
    "load_run_inputs",
    "parse_attempt_manifest_bytes",
    "parse_completion_pointer_bytes",
    "parse_run_inputs_bytes",
    "validate_stage_definitions",
]
