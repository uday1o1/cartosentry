"""End-to-end tests for immutable manifest scanning and frame indexing."""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from cartosentry.adapters.base import AdapterSourceFile, ReadOnlyAdapter
from cartosentry.adapters.boreas_v1 import BoreasAdapter
from cartosentry.artifacts import SequenceManifest, validate_artifact_json
from cartosentry.cli import app
from cartosentry.ingestion import (
    COMPLETION_FILE,
    INDEX_FILE,
    MANIFEST_FILE,
    SUMMARY_FILE,
    IngestionBudget,
    index_boreas_recording,
    index_recording,
    load_ingestion_budget,
    read_frame_index,
)
from cartosentry.manifest_boundaries import (
    MAXIMUM_FRAME_INDEX_LINE_BYTES,
    MAXIMUM_INGESTION_BUDGET_BYTES,
    ManifestBoundaryError,
)
from typer.testing import CliRunner

from tests.adapters_boreas_test import SOURCE_GROUP, _build_fixture

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUDGET_PATH = REPOSITORY_ROOT / "benchmarks/ingestion_budget.yaml"
SPLIT_MANIFEST = REPOSITORY_ROOT / "benchmarks/split_manifest.yaml"
PUBLIC_SEQUENCE = REPOSITORY_ROOT / "data/public/boreas-2021-09-02-11-42"


def _budget() -> tuple[IngestionBudget, str]:
    return load_ingestion_budget(BUDGET_PATH)


def _index(sequence: Path, output: Path):  # type: ignore[no-untyped-def]
    budget, budget_sha256 = _budget()
    return index_boreas_recording(
        sequence,
        output,
        source_group_id=SOURCE_GROUP,
        budget=budget,
        budget_sha256=budget_sha256,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_public_cli_atomically_publishes_valid_portable_artifacts(
    tmp_path: Path,
) -> None:
    sequence = _build_fixture(tmp_path / "source")
    output = tmp_path / "indexed"
    result = CliRunner().invoke(
        app,
        [
            "index-boreas",
            str(sequence),
            str(output),
            "--split-manifest",
            str(SPLIT_MANIFEST),
            "--budget",
            str(BUDGET_PATH),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["accepted"] is True
    assert report["published"] is True
    assert report["frame_index_entry_count"] == 1
    assert report["maximum_retained_index_batch_bytes"] <= 1_048_576
    assert sorted(item.name for item in output.iterdir()) == [
        COMPLETION_FILE,
        SUMMARY_FILE,
        INDEX_FILE,
        MANIFEST_FILE,
    ]
    manifest = validate_artifact_json((output / MANIFEST_FILE).read_text())
    assert isinstance(manifest, SequenceManifest)
    assert len(manifest.source_files) == 6
    entries = tuple(read_frame_index(output / INDEX_FILE))
    assert len(entries) == 1
    assert entries[0].source_key == "lidar/1000000.bin"
    assert entries[0].capture_interval_state == "SENSOR_TIME_ONLY"
    completion = json.loads((output / COMPLETION_FILE).read_text())
    assert completion["artifacts"] == {
        "index": _sha256(output / INDEX_FILE),
        "manifest": _sha256(output / MANIFEST_FILE),
        "summary": _sha256(output / SUMMARY_FILE),
    }
    serialized = "".join(path.read_text() for path in output.iterdir())
    assert str(tmp_path) not in serialized
    assert not tuple(tmp_path.glob(".indexed.attempt-*"))


def test_identical_sources_at_different_roots_produce_identical_bytes(
    tmp_path: Path,
) -> None:
    left_source = _build_fixture(tmp_path / "left")
    right_source = _build_fixture(tmp_path / "right")
    left_output = tmp_path / "left-index"
    right_output = tmp_path / "right-index"
    left = _index(left_source, left_output)
    right = _index(right_source, right_output)
    assert left.accepted is True
    assert right.accepted is True
    assert left.artifact_sha256 == right.artifact_sha256
    for name in (MANIFEST_FILE, INDEX_FILE, SUMMARY_FILE, COMPLETION_FILE):
        assert (left_output / name).read_bytes() == (right_output / name).read_bytes()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing", "MISSING_REQUIRED_SOURCE"),
        ("corrupt", "CORRUPT_SOURCE"),
        ("duplicate-time", "DUPLICATE_TIMESTAMP"),
    ],
)
def test_invalid_sources_emit_structural_findings_without_publication(
    tmp_path: Path, mutation: str, expected_code: str
) -> None:
    sequence = _build_fixture(tmp_path / mutation)
    lidar = sequence / "lidar/1000000.bin"
    if mutation == "missing":
        (sequence / "applanix/gps_post_process.csv").unlink()
    elif mutation == "corrupt":
        lidar.write_bytes(lidar.read_bytes() + b"\x00")
    else:
        shutil.copyfile(lidar, sequence / "lidar/01000000.bin")
    output = tmp_path / f"{mutation}-index"
    report = _index(sequence, output)
    assert report.accepted is False
    assert report.published is False
    assert expected_code in {item.code for item in report.structural_findings}
    assert not output.exists()
    assert not tuple(tmp_path.glob(f".{output.name}.attempt-*"))


class _DuplicateSourceAdapter:
    def __init__(self, wrapped: BoreasAdapter) -> None:
        self._wrapped = wrapped

    def source_files(self) -> Iterator[AdapterSourceFile]:
        sources = tuple(self._wrapped.source_files())
        yield sources[0]
        yield from sources

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._wrapped, name)


def test_duplicate_source_key_is_explicit_and_never_hashed(tmp_path: Path) -> None:
    sequence = _build_fixture(tmp_path)
    adapter = _DuplicateSourceAdapter(
        BoreasAdapter(sequence, source_group_id=SOURCE_GROUP)
    )
    budget, budget_sha256 = _budget()
    output = tmp_path / "duplicate-source-index"
    report = index_recording(
        cast(ReadOnlyAdapter, adapter),
        output,
        budget=budget,
        budget_sha256=budget_sha256,
    )
    assert report.accepted is False
    assert [item.code for item in report.structural_findings] == [
        "DUPLICATE_SOURCE_KEY"
    ]
    assert not output.exists()


class _ReorderedFrameAdapter:
    def __init__(self, wrapped: BoreasAdapter) -> None:
        self._wrapped = wrapped

    def frames(self):  # type: ignore[no-untyped-def]
        yield from reversed(tuple(self._wrapped.frames()))

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._wrapped, name)


def test_frame_index_tolerates_source_order_different_from_timestamp_order(
    tmp_path: Path,
) -> None:
    sequence = _build_fixture(tmp_path)
    original = sequence / "lidar/1000000.bin"
    (sequence / "lidar/1100000.bin").write_bytes(
        struct.pack("<6f", 1.0, 2.0, 3.0, 0.5, 0.0, -0.01)
    )
    adapter = _ReorderedFrameAdapter(
        BoreasAdapter(sequence, source_group_id=SOURCE_GROUP)
    )
    assert original.is_file()
    budget, budget_sha256 = _budget()
    output = tmp_path / "reordered-index"
    report = index_recording(
        cast(ReadOnlyAdapter, adapter),
        output,
        budget=budget,
        budget_sha256=budget_sha256,
    )
    assert report.accepted is True
    assert [
        entry.times.sensor_time.value_ns
        for entry in read_frame_index(output / INDEX_FILE)
    ] == [  # type: ignore[union-attr]
        1_100_000_000,
        1_000_000_000,
    ]
    lidar_range = next(
        item for item in report.source_ranges if item.record_kind == "lidar-frame"
    )
    assert lidar_range.timestamps_nondecreasing_in_source_order is False
    assert {item.code for item in report.structural_findings} == {
        "SOURCE_ORDER_REORDERED"
    }


@pytest.mark.skipif(
    not PUBLIC_SEQUENCE.is_dir(), reason="verified Boreas public-smoke data unavailable"
)
def test_actual_public_smoke_manifest_index_and_memory_gate(tmp_path: Path) -> None:
    output = tmp_path / "public-smoke-index"
    report = _index(PUBLIC_SEQUENCE, output)
    assert report.accepted is True
    assert report.source_file_count == 15
    assert report.source_byte_count == 131_213_082
    assert report.frame_index_entry_count == 10
    assert report.maximum_retained_index_batch_bytes <= 1_048_576
    ranges = {item.record_kind: item for item in report.source_ranges}
    assert ranges["trajectory-sample"].record_count == 214_719
    assert ranges["lidar-pose-sample"].record_count == 9_967
    assert ranges["lidar-frame"].record_count == 10
    assert all(item.duplicate_timestamp_count == 0 for item in ranges.values())
    assert report.structural_findings == ()


@pytest.mark.parametrize("mutation", ["duplicate", "oversized", "deep"])
def test_ingestion_budget_boundary_rejects_unsafe_documents(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "budget.json"
    if mutation == "duplicate":
        content = BUDGET_PATH.read_bytes()
        path.write_bytes(b'{"schema_version":1,' + content[1:])
    elif mutation == "oversized":
        path.write_bytes(b" " * (MAXIMUM_INGESTION_BUDGET_BYTES + 1))
    else:
        path.write_bytes(b"[" * 65 + b"0" + b"]" * 65)
    with pytest.raises(ManifestBoundaryError):
        load_ingestion_budget(path)


@pytest.mark.parametrize("mutation", ["duplicate", "oversized", "deep"])
def test_frame_index_boundary_rejects_unsafe_jsonl_records(
    tmp_path: Path, mutation: str
) -> None:
    sequence = _build_fixture(tmp_path / "source")
    output = tmp_path / "index"
    assert _index(sequence, output).accepted is True
    path = output / INDEX_FILE
    if mutation == "duplicate":
        content = path.read_bytes()
        path.write_bytes(
            b'{"schema_version":"cartosentry.frame-index-entry.v1",' + content[1:]
        )
    elif mutation == "oversized":
        path.write_bytes(b" " * (MAXIMUM_FRAME_INDEX_LINE_BYTES + 1))
    else:
        path.write_bytes(b"[" * 65 + b"0" + b"]" * 65 + b"\n")
    with pytest.raises(ManifestBoundaryError):
        tuple(read_frame_index(path))
