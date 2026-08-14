"""Deterministic, bounded-memory source manifest and frame indexing."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from cartosentry.adapters.base import (
    AdapterSourceFile,
    LidarFrameView,
    ReadOnlyAdapter,
)
from cartosentry.adapters.boreas_v1 import (
    CALIBRATIONS,
    GPS_SOURCE,
    LIDAR_DIRECTORY,
    LIDAR_POSE_SOURCE,
    BoreasAdapter,
    BoreasAdapterError,
)
from cartosentry.artifacts import (
    AdapterIdentity,
    CalibrationIdentity,
    CoordinateMetadata,
    SensorDescriptor,
    SensorModality,
    SequenceManifest,
    SourceFile,
    SourcePartition,
    StreamId,
    TimestampMetadata,
    canonicalize_portable_artifact,
)
from cartosentry.contracts import (
    ContractModel,
    FrameTimes,
    TimeEpoch,
    TimePoint,
    TimeReference,
    VerticalDatum,
)
from cartosentry.identifiers import (
    assert_portable,
    make_calibration_id,
    make_frame_id,
    make_sequence_id,
    make_stream_id,
)

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]
PortableKey = Annotated[str, StringConstraints(min_length=1)]
StableId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]*-sha256-[0-9a-f]{64}$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonnegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
type StructuralCode = Literal[
    "CORRUPT_SOURCE",
    "DUPLICATE_SOURCE_KEY",
    "DUPLICATE_TIMESTAMP",
    "FINDING_LIMIT_EXCEEDED",
    "MISSING_REQUIRED_SOURCE",
    "PARSE_ERROR",
    "SOURCE_CHANGED",
    "SOURCE_ORDER_REORDERED",
    "SOURCE_READ_ERROR",
]
type CaptureIntervalState = Literal[
    "SOURCE_CAPTURE_INTERVAL", "SENSOR_TIME_ONLY", "UNAVAILABLE"
]

MANIFEST_FILE = "sequence-manifest.json"
INDEX_FILE = "frame-index.jsonl"
SUMMARY_FILE = "frame-index-summary.json"
COMPLETION_FILE = "completion.json"


class IngestionBudget(ContractModel):
    schema_version: Literal[1]
    budget_id: Identifier
    hash_chunk_bytes: PositiveInt
    index_batch_bytes: PositiveInt
    maximum_source_files: PositiveInt
    maximum_structural_findings: PositiveInt


class StructuralFinding(ContractModel):
    code: StructuralCode
    severity: Literal["INFO", "BLOCKING_ANALYSIS"]
    source_key: PortableKey | None
    stream_key: Identifier | None
    record_kind: Identifier | None
    record_index: NonnegativeInt | None
    timestamp_ns: int | None
    detail: str


class FrameIndexEntry(ContractModel):
    schema_version: Literal["cartosentry.frame-index-entry.v1"]
    stream_id: StreamId
    frame_id: StableId
    source_key: PortableKey
    source_ordinal: NonnegativeInt
    source_record_index: NonnegativeInt | None
    byte_offset: NonnegativeInt | None
    byte_count: PositiveInt | None
    times: FrameTimes
    capture_interval_state: CaptureIntervalState
    parse_status: Literal["PARSED"]
    payload_record_count: PositiveInt | None


class SourceRangeSummary(ContractModel):
    stream_id: StreamId
    record_kind: Identifier
    record_count: PositiveInt
    minimum_time_ns: int
    maximum_time_ns: int
    timestamps_nondecreasing_in_source_order: bool
    duplicate_timestamp_count: NonnegativeInt


class FrameIndexSummary(ContractModel):
    schema_version: Literal["cartosentry.frame-index-summary.v1"]
    sequence_id: StableId
    source_identity_sha256: Sha256
    manifest_sha256: Sha256
    index_sha256: Sha256
    source_file_count: PositiveInt
    source_byte_count: PositiveInt
    frame_index_entry_count: PositiveInt
    source_ranges: tuple[SourceRangeSummary, ...]
    structural_findings: tuple[StructuralFinding, ...]
    budget_id: Identifier
    budget_sha256: Sha256
    index_memory_budget_bytes: PositiveInt
    maximum_retained_index_batch_bytes: PositiveInt
    hash_chunk_bytes: PositiveInt


class IngestionCompletion(ContractModel):
    schema_version: Literal["cartosentry.ingestion-completion.v1"]
    sequence_id: StableId
    source_identity_sha256: Sha256
    artifacts: dict[Literal["manifest", "index", "summary"], Sha256]


class IngestionQualification(ContractModel):
    schema_version: Literal["cartosentry.ingestion-qualification.v1"]
    accepted: bool
    published: bool
    sequence_id: StableId | None
    source_identity_sha256: Sha256 | None
    source_file_count: NonnegativeInt
    source_byte_count: NonnegativeInt
    frame_index_entry_count: NonnegativeInt
    source_ranges: tuple[SourceRangeSummary, ...]
    structural_findings: tuple[StructuralFinding, ...]
    budget_id: Identifier
    budget_sha256: Sha256
    index_memory_budget_bytes: PositiveInt
    maximum_retained_index_batch_bytes: NonnegativeInt
    artifact_sha256: dict[Identifier, Sha256]

    def portable_dict(self) -> dict[str, object]:
        value = self.model_dump(mode="json")
        assert_portable(value)
        return value


@dataclass
class _RangeAccumulator:
    stream_id: str
    record_kind: str
    count: int = 0
    minimum: int | None = None
    maximum: int | None = None
    previous: int | None = None
    nondecreasing: bool = True
    duplicates: int = 0

    def observe(self, timestamp_ns: int) -> None:
        self.count += 1
        self.minimum = (
            timestamp_ns if self.minimum is None else min(self.minimum, timestamp_ns)
        )
        self.maximum = (
            timestamp_ns if self.maximum is None else max(self.maximum, timestamp_ns)
        )
        if self.previous is not None and timestamp_ns < self.previous:
            self.nondecreasing = False
        self.previous = timestamp_ns

    def finish(self) -> SourceRangeSummary:
        if self.count <= 0 or self.minimum is None or self.maximum is None:
            raise ValueError("source range cannot be empty")
        return SourceRangeSummary(
            stream_id=self.stream_id,
            record_kind=self.record_kind,
            record_count=self.count,
            minimum_time_ns=self.minimum,
            maximum_time_ns=self.maximum,
            timestamps_nondecreasing_in_source_order=self.nondecreasing,
            duplicate_timestamp_count=self.duplicates,
        )


class _FindingCollector:
    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._findings: list[StructuralFinding] = []
        self._limit_reported = False

    def add(self, finding: StructuralFinding) -> None:
        if len(self._findings) < self._maximum:
            self._findings.append(finding)
            return
        if not self._limit_reported:
            self._findings[-1] = StructuralFinding(
                code="FINDING_LIMIT_EXCEEDED",
                severity="BLOCKING_ANALYSIS",
                source_key=None,
                stream_key=None,
                record_kind=None,
                record_index=None,
                timestamp_ns=None,
                detail="The frozen structural-finding limit was exceeded.",
            )
            self._limit_reported = True

    def values(self) -> tuple[StructuralFinding, ...]:
        return tuple(
            sorted(
                self._findings,
                key=lambda item: (
                    item.code,
                    item.stream_key or "",
                    item.record_kind or "",
                    item.source_key or "",
                    -1 if item.record_index is None else item.record_index,
                    -(2**63) if item.timestamp_ns is None else item.timestamp_ns,
                ),
            )
        )

    def has_blocking(self) -> bool:
        return any(item.severity == "BLOCKING_ANALYSIS" for item in self._findings)


class _IndexWriter:
    def __init__(self, path: Path, byte_budget: int) -> None:
        self._stream = path.open("wb")
        self._byte_budget = byte_budget
        self._buffer: list[bytes] = []
        self.maximum_buffer_bytes = 0
        self.entry_count = 0
        self._sha256 = hashlib.sha256()

    def append(self, entry: FrameIndexEntry) -> None:
        serialized = _canonical_json(entry.model_dump(mode="json")) + b"\n"
        minimum_retained = sys.getsizeof([]) + sys.getsizeof(serialized)
        if minimum_retained > self._byte_budget:
            raise ValueError("one frame-index record exceeds the frozen byte budget")
        self._buffer.append(serialized)
        retained = self._retained_buffer_bytes()
        if retained > self._byte_budget and len(self._buffer) > 1:
            removed = self._buffer.pop()
            self.flush()
            self._buffer.append(removed)
            retained = self._retained_buffer_bytes()
        if retained > self._byte_budget:
            raise ValueError("one frame-index record exceeds the frozen byte budget")
        self.maximum_buffer_bytes = max(self.maximum_buffer_bytes, retained)
        self.entry_count += 1

    def _retained_buffer_bytes(self) -> int:
        return sys.getsizeof(self._buffer) + sum(
            sys.getsizeof(item) for item in self._buffer
        )

    def flush(self) -> None:
        for serialized in self._buffer:
            self._stream.write(serialized)
            self._sha256.update(serialized)
        self._buffer.clear()

    def finish(self) -> str:
        self.flush()
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        return self._sha256.hexdigest()

    def close(self) -> None:
        if not self._stream.closed:
            self._stream.close()


def _canonical_json(value: object) -> bytes:
    assert_portable(value)
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def load_ingestion_budget(path: Path) -> tuple[IngestionBudget, str]:
    content = path.read_bytes()
    value = json.loads(content)
    return IngestionBudget.model_validate(value), hashlib.sha256(content).hexdigest()


def _write_json(path: Path, value: object) -> str:
    serialized = _canonical_json(value) + b"\n"
    with path.open("wb") as stream:
        stream.write(serialized)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(serialized).hexdigest()


def _source_snapshot_key(source: AdapterSourceFile) -> tuple[int, int, int, int]:
    return (
        source.byte_count,
        source.modified_time_ns,
        source.device_id,
        source.file_id,
    )


def _failure(
    *,
    budget: IngestionBudget,
    budget_sha256: str,
    findings: tuple[StructuralFinding, ...],
    source_file_count: int = 0,
    source_byte_count: int = 0,
    maximum_batch_bytes: int = 0,
    source_ranges: tuple[SourceRangeSummary, ...] = (),
) -> IngestionQualification:
    return IngestionQualification(
        schema_version="cartosentry.ingestion-qualification.v1",
        accepted=False,
        published=False,
        sequence_id=None,
        source_identity_sha256=None,
        source_file_count=source_file_count,
        source_byte_count=source_byte_count,
        frame_index_entry_count=0,
        source_ranges=source_ranges,
        structural_findings=findings,
        budget_id=budget.budget_id,
        budget_sha256=budget_sha256,
        index_memory_budget_bytes=budget.index_batch_bytes,
        maximum_retained_index_batch_bytes=maximum_batch_bytes,
        artifact_sha256={},
    )


def _adapter_failure(error: BoreasAdapterError) -> StructuralFinding:
    code: StructuralCode
    if error.field == "record_layout":
        code = "CORRUPT_SOURCE"
        detail = "The selected source has an invalid record layout."
    elif error.field == "source_snapshot":
        code = "SOURCE_CHANGED"
        detail = "The selected source changed during immutable indexing."
    else:
        code = "PARSE_ERROR"
        detail = "The selected source did not satisfy the adapter contract."
    return StructuralFinding(
        code=code,
        severity="BLOCKING_ANALYSIS",
        source_key=error.source_key,
        stream_key=None,
        record_kind=None,
        record_index=error.record or None,
        timestamp_ns=None,
        detail=detail,
    )


def _hash_sources(
    adapter: ReadOnlyAdapter,
    sources: tuple[AdapterSourceFile, ...],
    chunk_bytes: int,
) -> tuple[tuple[SourceFile, ...], str]:
    source_files: list[SourceFile] = []
    source_identity = hashlib.sha256()
    for source in sources:
        digest = hashlib.sha256()
        observed_bytes = 0
        for chunk in adapter.source_chunks(source, chunk_bytes=chunk_bytes):
            if len(chunk) > chunk_bytes:
                raise ValueError("adapter exceeded the requested source chunk size")
            digest.update(chunk)
            source_identity.update(chunk)
            observed_bytes += len(chunk)
        if observed_bytes != source.byte_count:
            raise ValueError("adapter source byte count changed during hashing")
        source_files.append(
            SourceFile(
                source_key=source.source_key,
                sha256=digest.hexdigest(),
                byte_count=source.byte_count,
            )
        )
    return tuple(source_files), source_identity.hexdigest()


def _build_manifest(
    adapter: ReadOnlyAdapter,
    source_files: tuple[SourceFile, ...],
    source_identity_sha256: str,
) -> tuple[SequenceManifest, dict[str, str]]:
    metadata = adapter.sequence_metadata()
    calibration_views = tuple(adapter.calibrations())
    calibrations: list[CalibrationIdentity] = []
    calibration_by_key: dict[str, str] = {}
    for view in calibration_views:
        identity = {
            "sha256": view.source_sha256,
            "source_frame": view.transform.source_frame,
            "target_frame": view.transform.target_frame,
        }
        calibration_id = make_calibration_id(identity)
        calibration_by_key[view.calibration_key] = calibration_id
        calibrations.append(
            CalibrationIdentity(
                calibration_id=calibration_id,
                sha256=view.source_sha256,
                target_frame=view.transform.target_frame,
                source_frame=view.transform.source_frame,
            )
        )
    calibrations.sort(key=lambda item: item.calibration_id)
    adapter_identity = AdapterIdentity(
        adapter_id=metadata.adapter_id,
        adapter_version=metadata.adapter_version,
        capabilities=tuple(
            sorted(
                item.capability_id
                for item in metadata.capabilities
                if item.state.value == "AVAILABLE"
            )
        ),
    )
    coordinate_metadata = CoordinateMetadata(
        global_frame="WGS84",
        local_frame="enu_ref",
        rig_frame="applanix",
        vertical_datum=VerticalDatum.UNKNOWN_VERTICAL_DATUM,
    )
    adapter_sensors = adapter.sensors()
    sensor_identity = [
        {
            "modality": item.modality,
            "sensor_id": item.sensor_id,
            "coordinate_frame": item.coordinate_frame,
            "calibration_ids": sorted(
                calibration_by_key[key] for key in item.required_calibration_keys
            ),
        }
        for item in adapter_sensors
    ]
    timestamp_identity = [
        {
            "epoch": item.time_epoch,
            "clock_id": item.clock_id,
            "reference": item.reference,
            "raw_unit": "s" if item.modality == "trajectory" else "us",
            "modality": item.modality,
            "sensor_id": item.sensor_id,
        }
        for item in adapter_sensors
    ]
    manifest_identity: dict[str, object] = {
        "adapter": adapter_identity.model_dump(mode="json"),
        "calibrations": [item.model_dump(mode="json") for item in calibrations],
        "coordinate_metadata": coordinate_metadata.model_dump(mode="json"),
        "sensors": sorted(
            sensor_identity,
            key=lambda item: (str(item["modality"]), str(item["sensor_id"])),
        ),
        "source_files": [item.model_dump(mode="json") for item in source_files],
        "source_identity_sha256": source_identity_sha256,
        "timestamp_metadata": sorted(
            timestamp_identity,
            key=lambda item: (str(item["modality"]), str(item["sensor_id"])),
        ),
    }
    sequence_id = make_sequence_id(manifest_identity)
    stream_by_key: dict[str, str] = {}
    sensors: list[SensorDescriptor] = []
    timestamps: list[TimestampMetadata] = []
    for item in adapter_sensors:
        stream_id = make_stream_id(sequence_id, item.modality, item.sensor_id)
        stream_by_key[item.stream_key] = stream_id
        sensors.append(
            SensorDescriptor(
                stream_id=stream_id,
                modality=SensorModality(item.modality),
                sensor_id=item.sensor_id,
                coordinate_frame=item.coordinate_frame,
                calibration_ids=tuple(
                    sorted(
                        calibration_by_key[key]
                        for key in item.required_calibration_keys
                    )
                ),
            )
        )
        timestamps.append(
            TimestampMetadata(
                stream_id=stream_id,
                epoch=TimeEpoch(item.time_epoch),
                clock_id=item.clock_id,
                reference=TimeReference(item.reference),
                raw_unit="s" if item.modality == "trajectory" else "us",
            )
        )
    manifest = SequenceManifest(
        schema_version="cartosentry.sequence-manifest.v1",
        sequence_id=sequence_id,
        source_identity_sha256=source_identity_sha256,
        source_group_id=metadata.source_group_id,
        partition=SourcePartition(metadata.partition),
        adapter=adapter_identity,
        sensors=tuple(sorted(sensors, key=lambda item: item.stream_id)),
        source_files=source_files,
        calibrations=tuple(calibrations),
        timestamp_metadata=tuple(sorted(timestamps, key=lambda item: item.stream_id)),
        coordinate_metadata=coordinate_metadata,
        declared_gaps=(),
    )
    return manifest, stream_by_key


def _entry_times_state(times: FrameTimes) -> CaptureIntervalState:
    if times.capture_start is not None:
        return "SOURCE_CAPTURE_INTERVAL"
    if times.sensor_time is not None:
        return "SENSOR_TIME_ONLY"
    return "UNAVAILABLE"


def _canonical_frame_id(stream_id: str, frame: LidarFrameView) -> str:
    return make_frame_id(
        stream_id,
        frame.source_frame_key,
        {"times": frame.times.model_dump(mode="json")},
    )


def _observe_timestamp(
    *,
    database: sqlite3.Connection,
    accumulator: _RangeAccumulator,
    timestamp: TimePoint,
    source_key: str,
    record_index: int,
    findings: _FindingCollector,
) -> None:
    accumulator.observe(timestamp.value_ns)
    try:
        database.execute(
            "INSERT INTO seen_timestamp(record_kind, stream_id, timestamp_ns) "
            "VALUES (?, ?, ?)",
            (accumulator.record_kind, accumulator.stream_id, timestamp.value_ns),
        )
    except sqlite3.IntegrityError:
        accumulator.duplicates += 1
        findings.add(
            StructuralFinding(
                code="DUPLICATE_TIMESTAMP",
                severity="BLOCKING_ANALYSIS",
                source_key=source_key,
                stream_key=accumulator.stream_id,
                record_kind=accumulator.record_kind,
                record_index=record_index,
                timestamp_ns=timestamp.value_ns,
                detail="A stream record repeats a canonical timestamp.",
            )
        )


def _scan_ranges_and_frames(
    adapter: ReadOnlyAdapter,
    stream_by_key: dict[str, str],
    database: sqlite3.Connection,
    writer: _IndexWriter,
    findings: _FindingCollector,
) -> tuple[SourceRangeSummary, ...]:
    trajectory = _RangeAccumulator(
        stream_id=stream_by_key["trajectory-postprocessed"],
        record_kind="trajectory-sample",
    )
    for trajectory_sample in adapter.pose_samples():
        _observe_timestamp(
            database=database,
            accumulator=trajectory,
            timestamp=trajectory_sample.time,
            source_key=trajectory_sample.provenance.source_key,
            record_index=trajectory_sample.provenance.record_index,
            findings=findings,
        )
    lidar_pose = _RangeAccumulator(
        stream_id=stream_by_key["lidar-lidar"],
        record_kind="lidar-pose-sample",
    )
    for lidar_pose_sample in adapter.lidar_pose_samples():
        _observe_timestamp(
            database=database,
            accumulator=lidar_pose,
            timestamp=lidar_pose_sample.time,
            source_key=lidar_pose_sample.provenance.source_key,
            record_index=lidar_pose_sample.provenance.record_index,
            findings=findings,
        )
    lidar_frames = _RangeAccumulator(
        stream_id=stream_by_key["lidar-lidar"],
        record_kind="lidar-frame",
    )
    for source_ordinal, frame in enumerate(adapter.frames()):
        timestamp = frame.times.sensor_time
        if timestamp is None:
            raise ValueError("indexed lidar frame has no sensor timestamp")
        _observe_timestamp(
            database=database,
            accumulator=lidar_frames,
            timestamp=timestamp,
            source_key=frame.payload.source_key,
            record_index=source_ordinal,
            findings=findings,
        )
        stream_id = stream_by_key[frame.stream_key]
        writer.append(
            FrameIndexEntry(
                schema_version="cartosentry.frame-index-entry.v1",
                stream_id=stream_id,
                frame_id=_canonical_frame_id(stream_id, frame),
                source_key=frame.payload.source_key,
                source_ordinal=source_ordinal,
                source_record_index=None,
                byte_offset=0,
                byte_count=frame.payload.byte_count,
                times=frame.times,
                capture_interval_state=_entry_times_state(frame.times),
                parse_status="PARSED",
                payload_record_count=frame.payload.record_count,
            )
        )
    ranges = (trajectory, lidar_pose, lidar_frames)
    for accumulator in ranges:
        if not accumulator.nondecreasing:
            findings.add(
                StructuralFinding(
                    code="SOURCE_ORDER_REORDERED",
                    severity="INFO",
                    source_key=None,
                    stream_key=accumulator.stream_id,
                    record_kind=accumulator.record_kind,
                    record_index=None,
                    timestamp_ns=None,
                    detail=(
                        "Source order differs from timestamp order; the index retains "
                        "explicit timestamps for ordered queries."
                    ),
                )
            )
    return tuple(item.finish() for item in ranges)


def index_recording(
    adapter: ReadOnlyAdapter,
    output: Path,
    *,
    budget: IngestionBudget,
    budget_sha256: str,
) -> IngestionQualification:
    """Build and atomically publish one immutable manifest and frame index."""

    if output.exists():
        raise ValueError("ingestion output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    findings = _FindingCollector(budget.maximum_structural_findings)
    enumerated_values: list[AdapterSourceFile] = []
    try:
        for source in adapter.source_files():
            if len(enumerated_values) >= budget.maximum_source_files:
                findings.add(
                    StructuralFinding(
                        code="SOURCE_READ_ERROR",
                        severity="BLOCKING_ANALYSIS",
                        source_key="sequence",
                        stream_key=None,
                        record_kind=None,
                        record_index=None,
                        timestamp_ns=None,
                        detail=(
                            "The source-file count exceeds the frozen ingestion limit."
                        ),
                    )
                )
                break
            enumerated_values.append(source)
    except BoreasAdapterError as error:
        findings.add(_adapter_failure(error))
        return _failure(
            budget=budget,
            budget_sha256=budget_sha256,
            findings=findings.values(),
        )
    enumerated = tuple(enumerated_values)
    if not enumerated:
        findings.add(
            StructuralFinding(
                code="MISSING_REQUIRED_SOURCE",
                severity="BLOCKING_ANALYSIS",
                source_key="sequence",
                stream_key=None,
                record_kind=None,
                record_index=None,
                timestamp_ns=None,
                detail="The adapter selected no source files.",
            )
        )
    source_keys: set[str] = set()
    for source in enumerated:
        if source.source_key in source_keys:
            findings.add(
                StructuralFinding(
                    code="DUPLICATE_SOURCE_KEY",
                    severity="BLOCKING_ANALYSIS",
                    source_key=source.source_key,
                    stream_key=None,
                    record_kind=None,
                    record_index=None,
                    timestamp_ns=None,
                    detail="The adapter enumerated a source key more than once.",
                )
            )
        source_keys.add(source.source_key)
    sources = tuple(sorted(enumerated, key=lambda item: item.source_key))
    if findings.has_blocking():
        return _failure(
            budget=budget,
            budget_sha256=budget_sha256,
            findings=findings.values(),
            source_file_count=len(sources),
            source_byte_count=sum(item.byte_count for item in sources),
        )
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.attempt-", dir=output.parent)
    )
    writer: _IndexWriter | None = None
    try:
        source_files, source_identity = _hash_sources(
            adapter, sources, budget.hash_chunk_bytes
        )
        manifest, stream_by_key = _build_manifest(
            adapter, source_files, source_identity
        )
        manifest_text = canonicalize_portable_artifact(manifest) + "\n"
        manifest_path = temporary / MANIFEST_FILE
        with manifest_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(manifest_text)
            stream.flush()
            os.fsync(stream.fileno())
        manifest_sha256 = hashlib.sha256(manifest_text.encode()).hexdigest()
        database_path = temporary / ".timestamp-uniqueness.sqlite3"
        database = sqlite3.connect(database_path)
        try:
            database.execute("PRAGMA temp_store=FILE")
            database.execute("PRAGMA cache_size=-1024")
            database.execute(
                "CREATE TABLE seen_timestamp ("
                "record_kind TEXT NOT NULL, stream_id TEXT NOT NULL, "
                "timestamp_ns INTEGER NOT NULL, "
                "PRIMARY KEY(record_kind, stream_id, timestamp_ns)) WITHOUT ROWID"
            )
            writer = _IndexWriter(temporary / INDEX_FILE, budget.index_batch_bytes)
            source_ranges = _scan_ranges_and_frames(
                adapter, stream_by_key, database, writer, findings
            )
            database.commit()
            index_sha256 = writer.finish()
        finally:
            database.close()
            database_path.unlink(missing_ok=True)
        latest_sources = {
            item.source_key: _source_snapshot_key(item)
            for item in adapter.source_files()
        }
        original_sources = {
            item.source_key: _source_snapshot_key(item) for item in sources
        }
        if latest_sources != original_sources:
            findings.add(
                StructuralFinding(
                    code="SOURCE_CHANGED",
                    severity="BLOCKING_ANALYSIS",
                    source_key="sequence",
                    stream_key=None,
                    record_kind=None,
                    record_index=None,
                    timestamp_ns=None,
                    detail="One or more sources changed during immutable indexing.",
                )
            )
        maximum_batch = writer.maximum_buffer_bytes
        if maximum_batch > budget.index_batch_bytes:
            raise ValueError("frame-index writer exceeded its frozen byte budget")
        if findings.has_blocking():
            return _failure(
                budget=budget,
                budget_sha256=budget_sha256,
                findings=findings.values(),
                source_file_count=len(sources),
                source_byte_count=sum(item.byte_count for item in sources),
                maximum_batch_bytes=maximum_batch,
                source_ranges=source_ranges,
            )
        summary = FrameIndexSummary(
            schema_version="cartosentry.frame-index-summary.v1",
            sequence_id=manifest.sequence_id,
            source_identity_sha256=source_identity,
            manifest_sha256=manifest_sha256,
            index_sha256=index_sha256,
            source_file_count=len(source_files),
            source_byte_count=sum(item.byte_count for item in source_files),
            frame_index_entry_count=writer.entry_count,
            source_ranges=source_ranges,
            structural_findings=findings.values(),
            budget_id=budget.budget_id,
            budget_sha256=budget_sha256,
            index_memory_budget_bytes=budget.index_batch_bytes,
            maximum_retained_index_batch_bytes=maximum_batch,
            hash_chunk_bytes=budget.hash_chunk_bytes,
        )
        summary_sha256 = _write_json(
            temporary / SUMMARY_FILE, summary.model_dump(mode="json")
        )
        completion = IngestionCompletion(
            schema_version="cartosentry.ingestion-completion.v1",
            sequence_id=manifest.sequence_id,
            source_identity_sha256=source_identity,
            artifacts={
                "manifest": manifest_sha256,
                "index": index_sha256,
                "summary": summary_sha256,
            },
        )
        completion_sha256 = _write_json(
            temporary / COMPLETION_FILE, completion.model_dump(mode="json")
        )
        directory_descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        os.replace(temporary, output)
        parent_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        return IngestionQualification(
            schema_version="cartosentry.ingestion-qualification.v1",
            accepted=True,
            published=True,
            sequence_id=manifest.sequence_id,
            source_identity_sha256=source_identity,
            source_file_count=len(source_files),
            source_byte_count=sum(item.byte_count for item in source_files),
            frame_index_entry_count=writer.entry_count,
            source_ranges=source_ranges,
            structural_findings=findings.values(),
            budget_id=budget.budget_id,
            budget_sha256=budget_sha256,
            index_memory_budget_bytes=budget.index_batch_bytes,
            maximum_retained_index_batch_bytes=maximum_batch,
            artifact_sha256={
                "completion": completion_sha256,
                "index": index_sha256,
                "manifest": manifest_sha256,
                "summary": summary_sha256,
            },
        )
    except BoreasAdapterError as error:
        findings.add(_adapter_failure(error))
        return _failure(
            budget=budget,
            budget_sha256=budget_sha256,
            findings=findings.values(),
            source_file_count=len(sources),
            source_byte_count=sum(item.byte_count for item in sources),
            maximum_batch_bytes=(0 if writer is None else writer.maximum_buffer_bytes),
        )
    except (OSError, sqlite3.Error):
        findings.add(
            StructuralFinding(
                code="SOURCE_READ_ERROR",
                severity="BLOCKING_ANALYSIS",
                source_key="sequence",
                stream_key=None,
                record_kind=None,
                record_index=None,
                timestamp_ns=None,
                detail="The immutable ingestion pass could not read or stage data.",
            )
        )
        return _failure(
            budget=budget,
            budget_sha256=budget_sha256,
            findings=findings.values(),
            source_file_count=len(sources),
            source_byte_count=sum(item.byte_count for item in sources),
            maximum_batch_bytes=(0 if writer is None else writer.maximum_buffer_bytes),
        )
    finally:
        if writer is not None:
            writer.close()
        if temporary.exists():
            shutil.rmtree(temporary)


def _missing_boreas_sources(sequence_root: Path) -> tuple[str, ...]:
    required = (
        GPS_SOURCE,
        LIDAR_POSE_SOURCE,
        *(item[0] for item in CALIBRATIONS if item[3]),
    )
    missing = [
        source_key
        for source_key in required
        if not (sequence_root / source_key).is_file()
    ]
    lidar = sequence_root / LIDAR_DIRECTORY
    if not lidar.is_dir() or not any(lidar.glob("*.bin")):
        missing.append(f"{LIDAR_DIRECTORY}/*.bin")
    return tuple(sorted(missing))


def index_boreas_recording(
    sequence_root: Path,
    output: Path,
    *,
    source_group_id: str,
    budget: IngestionBudget,
    budget_sha256: str,
) -> IngestionQualification:
    """Preflight and index one local Boreas sequence without modifying it."""

    root = sequence_root.resolve(strict=True)
    destination = output.resolve(strict=False)
    try:
        destination.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("ingestion output must be outside the source recording")
    missing = _missing_boreas_sources(root)
    if missing:
        findings = tuple(
            StructuralFinding(
                code="MISSING_REQUIRED_SOURCE",
                severity="BLOCKING_ANALYSIS",
                source_key=source_key,
                stream_key=None,
                record_kind=None,
                record_index=None,
                timestamp_ns=None,
                detail="A required V1 source is unavailable.",
            )
            for source_key in missing
        )
        return _failure(
            budget=budget,
            budget_sha256=budget_sha256,
            findings=findings,
        )
    try:
        adapter = BoreasAdapter(root, source_group_id=source_group_id)
    except BoreasAdapterError as error:
        return _failure(
            budget=budget,
            budget_sha256=budget_sha256,
            findings=(_adapter_failure(error),),
        )
    return index_recording(
        adapter,
        destination,
        budget=budget,
        budget_sha256=budget_sha256,
    )


def read_frame_index(path: Path) -> Iterator[FrameIndexEntry]:
    """Validate an index incrementally without materializing it."""

    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                yield FrameIndexEntry.model_validate_json(line)
            except ValueError as error:
                raise ValueError(
                    f"invalid frame-index record at line {line_number}"
                ) from error


__all__ = [
    "COMPLETION_FILE",
    "INDEX_FILE",
    "MANIFEST_FILE",
    "SUMMARY_FILE",
    "FrameIndexEntry",
    "FrameIndexSummary",
    "IngestionBudget",
    "IngestionCompletion",
    "IngestionQualification",
    "SourceRangeSummary",
    "StructuralFinding",
    "index_boreas_recording",
    "index_recording",
    "load_ingestion_budget",
    "read_frame_index",
]
