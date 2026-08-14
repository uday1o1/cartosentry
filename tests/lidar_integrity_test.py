"""LiDAR integrity detector, fault, qualification, and memory tests."""

from __future__ import annotations

import hashlib
import json
import tracemalloc
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from cartosentry.cli import app
from cartosentry.lidar_faults import (
    GATE_IMMUTABLE_SHA256,
    inject_lidar_fault,
    load_lidar_qualification_gate,
    registered_lidar_fault_cases,
)
from cartosentry.lidar_integrity import (
    PROFILE_IMMUTABLE_SHA256,
    LidarFrameInput,
    LidarIntegrityReport,
    LidarPointInput,
    LidarRule,
    analyze_lidar_integrity,
    load_lidar_integrity_profile,
    synthetic_lidar_frames,
)
from cartosentry.lidar_integrity_qualification import (
    _load_mapping,
    _manifest_public_frames,
    _qualify_partition,
)
from cartosentry.synthetic import generate_fixture, serialize_fixture
from cartosentry.synthetic_models import SyntheticFixture, SyntheticScenario
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPOSITORY_ROOT / "profiles/lidar_integrity_v1.yaml"
GATE_PATH = REPOSITORY_ROOT / "benchmarks/m4_1_lidar_gate.yaml"
SPLIT_PATH = REPOSITORY_ROOT / "benchmarks/split_manifest.yaml"


def _fixture_inputs() -> tuple[SyntheticFixture, bytes, tuple[LidarFrameInput, ...]]:
    fixture = generate_fixture(
        "sensor-map-dev-001",
        SyntheticScenario.STRAIGHT,
        10_000,
    )
    content = serialize_fixture(fixture)
    return fixture, content, synthetic_lidar_frames(fixture)


def _analyze(
    frames: tuple[LidarFrameInput, ...], source_sha256: str
) -> LidarIntegrityReport:
    profile, profile_file_sha256 = load_lidar_integrity_profile(PROFILE_PATH)
    return analyze_lidar_integrity(
        frames,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        sensor_model_id="synthetic-spinning-v1",
        source_sha256=source_sha256,
        source_group_id="sensor-map-dev-001",
        partition="development",
    )


def test_profile_and_gate_are_self_hashed_strict_and_pinned(tmp_path: Path) -> None:
    profile, profile_file_sha256 = load_lidar_integrity_profile(PROFILE_PATH)
    gate, gate_file_sha256 = load_lidar_qualification_gate(GATE_PATH)

    assert profile.immutable_sha256 == PROFILE_IMMUTABLE_SHA256
    assert gate.immutable_sha256 == GATE_IMMUTABLE_SHA256
    assert profile_file_sha256 == gate.authorities.profile_file_sha256
    assert len(gate_file_sha256) == 64
    assert len(registered_lidar_fault_cases(gate)) == 23
    assert profile.model_json_schema()["additionalProperties"] is False

    profile_raw = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    profile_raw["models"]["synthetic-spinning-v1"]["maximum_range_m"] = 76.0
    invalid_profile = tmp_path / "profile.json"
    invalid_profile.write_text(json.dumps(profile_raw), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable hash"):
        load_lidar_integrity_profile(invalid_profile)

    gate_raw = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    gate_raw["operators"][0]["cases"][0]["parameters"]["drop_frames"] = 2
    invalid_gate = tmp_path / "gate.json"
    invalid_gate.write_text(json.dumps(gate_raw), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable hash"):
        load_lidar_qualification_gate(invalid_gate)


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"value":NaN}',
        b"[" * 65 + b"0" + b"]" * 65,
        b" " * (256 * 1024 + 1),
    ],
)
def test_profile_and_gate_boundaries_reject_hostile_json(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "hostile.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        load_lidar_integrity_profile(path)
    with pytest.raises(ValueError):
        load_lidar_qualification_gate(path)


def test_clean_stream_reports_statistics_without_events() -> None:
    _fixture, content, frames = _fixture_inputs()
    report = _analyze(frames, hashlib.sha256(content).hexdigest())

    expected_points = sum(len(tuple(frame.points)) for frame in frames)
    assert report.events == ()
    assert report.statistics.frame_count == len(frames)
    assert report.statistics.point_count == expected_points
    assert report.statistics.finite_point_count == expected_points
    assert report.statistics.finite_return_ratio == 1.0
    assert report.statistics.invalid_record_count == 0
    assert report.statistics.range_quantiles_m.probabilities == (0.05, 0.5, 0.95)
    assert report.statistics.range_quantiles_m.finite_count == expected_points
    assert sum(report.statistics.per_ring_counts.values()) == expected_points
    assert sum(report.statistics.per_azimuth_bin_counts) == expected_points
    assert report.statistics.minimum_scan_duration_ns == 500_000_000
    assert report.statistics.maximum_scan_duration_ns == 500_000_000
    assert report.statistics.minimum_observed_point_span_ns is not None


def test_invalid_fields_are_all_checked_but_invalid_records_are_unique() -> None:
    _fixture, content, frames = _fixture_inputs()
    modified = list(frames)
    for frame_position in (2, 3):
        frame = modified[frame_position]
        points = list(frame.points)
        point = points[0]
        points[0] = LidarPointInput(
            position_m=(100.0, 0.0, 0.0),
            intensity=2.0,
            ring_id=999,
            relative_time_ns=1_000_000_000.0,
            source_offset=point.source_offset,
        )
        modified[frame_position] = replace(frame, points=tuple(points))

    report = _analyze(tuple(modified), hashlib.sha256(content).hexdigest())
    rules = {event.rule for event in report.events}
    assert {
        LidarRule.RANGE,
        LidarRule.INTENSITY,
        LidarRule.INVALID_RING,
        LidarRule.POINT_TIME,
    }.issubset(rules)
    assert report.statistics.invalid_record_count == 4
    assert all(
        event.representative_source_offsets
        for event in report.events
        if event.rule in rules - {LidarRule.SCAN_DURATION, LidarRule.RING_LOSS}
    )


def test_nonfinite_is_fail_closed_and_reports_bounded_offsets() -> None:
    _fixture, content, frames = _fixture_inputs()
    frame = frames[2]
    points = list(frame.points)
    points[0] = replace(points[0], intensity=float("nan"))
    modified = (*frames[:2], replace(frame, points=tuple(points)), *frames[3:])

    report = _analyze(modified, hashlib.sha256(content).hexdigest())
    event = next(event for event in report.events if event.rule is LidarRule.NONFINITE)
    assert event.representative_source_offsets == (points[0].source_offset,)
    assert report.statistics.invalid_record_count == 1
    assert report.statistics.finite_return_ratio < 1.0


def test_all_registered_fault_cases_are_deterministic_and_match_outcome() -> None:
    _fixture, content, frames = _fixture_inputs()
    source_sha256 = hashlib.sha256(content).hexdigest()
    gate, _ = load_lidar_qualification_gate(GATE_PATH)

    for case in registered_lidar_fault_cases(gate):
        first = inject_lidar_fault(
            frames, case, source_sha256=source_sha256, seed=10_000
        )
        second = inject_lidar_fault(
            frames, case, source_sha256=source_sha256, seed=10_000
        )
        assert first.truth == second.truth
        report = _analyze(first.frames, first.truth.derived_sha256)
        observed = {event.rule for event in report.events}
        if case.expected_rule is None:
            primary = {
                "lidar.ring_loss": LidarRule.RING_LOSS,
                "lidar.sector_loss": LidarRule.SECTOR_LOSS,
                "lidar.density_reduction": LidarRule.DENSITY,
                "lidar.range_scale": LidarRule.RANGE,
                "lidar.point_time_corruption": LidarRule.POINT_TIME,
            }[case.operator_id]
            assert primary not in observed
        else:
            assert case.expected_rule in observed


def test_coverage_rules_require_two_consecutive_frames() -> None:
    _fixture, content, frames = _fixture_inputs()
    source_sha256 = hashlib.sha256(content).hexdigest()
    gate, _ = load_lidar_qualification_gate(GATE_PATH)
    cases = {case.case_id: case for case in registered_lidar_fault_cases(gate)}

    one = inject_lidar_fault(
        frames,
        cases["ring-loss-1-frame"],
        source_sha256=source_sha256,
        seed=10_000,
    )
    two = inject_lidar_fault(
        frames,
        cases["ring-loss-2-frames"],
        source_sha256=source_sha256,
        seed=10_000,
    )
    assert LidarRule.RING_LOSS not in {
        event.rule for event in _analyze(one.frames, one.truth.derived_sha256).events
    }
    assert LidarRule.RING_LOSS in {
        event.rule for event in _analyze(two.frames, two.truth.derived_sha256).events
    }


def test_streaming_state_remains_bounded_and_iterates_points_once() -> None:
    _fixture, content, frames = _fixture_inputs()
    template = frames[0]
    template_points = tuple(template.points)

    class OnePassPoints:
        def __init__(self) -> None:
            self.iterated = False

        def __iter__(self) -> Iterator[LidarPointInput]:
            if self.iterated:
                raise AssertionError("point payload was iterated more than once")
            self.iterated = True
            yield from template_points

    payloads: list[OnePassPoints] = []

    def generated_frames() -> Iterator[LidarFrameInput]:
        for index in range(500):
            points = OnePassPoints()
            payloads.append(points)
            start = index * 500_000_000
            yield LidarFrameInput(
                frame_index=index,
                source_key=f"synthetic/lidar/{index}",
                reference_time_ns=start + 250_000_000,
                capture_start_ns=start,
                capture_end_ns=start + 500_000_000,
                points=points,
            )

    profile, profile_file_sha256 = load_lidar_integrity_profile(PROFILE_PATH)
    tracemalloc.start()
    try:
        report = analyze_lidar_integrity(
            generated_frames(),
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            sensor_model_id="synthetic-spinning-v1",
            source_sha256=hashlib.sha256(content).hexdigest(),
            source_group_id="sensor-map-dev-001",
            partition="development",
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert report.statistics.frame_count == 500
    assert all(item.iterated for item in payloads)
    assert peak < 8 * 1024 * 1024
    assert report.statistics.retained_state_upper_bound_bytes <= 64 * 1024 * 1024


def test_development_partition_passes_every_frozen_case() -> None:
    gate, _ = load_lidar_qualification_gate(GATE_PATH)
    profile, profile_file_sha256 = load_lidar_integrity_profile(PROFILE_PATH)

    report = _qualify_partition(
        "development",
        gate=gate,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        split_manifest_path=SPLIT_PATH,
    )
    assert report["gate_passed"] is True
    assert report["source_group_count"] == 8
    assert report["case_count"] == 8 * 23
    assert report["clean_event_count"] == 0
    assert report["structural_expected_outcome_fraction"] == 1.0
    assert report["supported_coverage_expected_outcome_fraction"] == 1.0


def test_public_smoke_selection_is_exact_and_manifest_bound() -> None:
    manifest = _load_mapping(
        REPOSITORY_ROOT / "benchmarks/data_manifest.yaml",
        context="data manifest",
    )
    selected = _manifest_public_frames(
        data_manifest=manifest,
        sequence_id="boreas-2021-09-02-11-42",
        maximum_frames=10,
    )

    assert len(selected) == 10
    assert all(key.endswith(".bin") for key, _digest, _size in selected)
    assert all(len(digest) == 64 for _key, digest, _size in selected)
    assert all(size > 0 for _key, _digest, size in selected)


def test_public_cli_exposes_lidar_qualification_workflow() -> None:
    result = CliRunner().invoke(app, ["qualify-lidar-integrity", "--help"])
    assert result.exit_code == 0
    assert "streaming LiDAR integrity" in result.stdout
    assert "--public-data-root" in result.stdout
