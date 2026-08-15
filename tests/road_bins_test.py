"""Directed-road binning, traversal, evidence, and localization tests."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path

import pytest
from cartosentry.artifacts import (
    EvidenceReference,
    Finding,
    Measurement,
    MeasurementUnit,
    Observability,
    ReadinessState,
    SensorModality,
    Severity,
    SourceInterval,
    Threshold,
    ThresholdOperator,
)
from cartosentry.contracts import TimeEpoch, TimePoint, TimeReference
from cartosentry.identifiers import (
    make_finding_id,
    make_road_bin_id,
    make_stream_id,
)
from cartosentry.road_bins import (
    PROFILE_FILE_SHA256,
    PROFILE_IMMUTABLE_SHA256,
    DirectedRoadCoverageLedger,
    FindingLocalizationRequest,
    ModalityEvidenceInterval,
    RoadBinningProfile,
    aggregate_directed_road_bins,
    load_road_binning_profile,
    make_modality_evidence_interval,
)
from cartosentry.road_decoder import (
    DecodedRoadPath,
    MapDecoderProfile,
    decode_road_path,
    load_map_decoder_profile,
)
from cartosentry.road_graph import (
    ArcDirection,
    DirectedRoadArc,
    DirectedRoadGraph,
    GraphSourceKind,
    import_osm_road_graph,
    load_graph_import_profile,
)
from cartosentry.road_matching import (
    CandidateState,
    MapMatchingProfile,
    load_map_matching_profile,
    make_road_match_observation,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GRAPH_PROFILE_PATH = REPOSITORY_ROOT / "profiles/graph_import_v1.yaml"
MATCH_PROFILE_PATH = REPOSITORY_ROOT / "profiles/map_matching_v1.yaml"
DECODER_PROFILE_PATH = REPOSITORY_ROOT / "profiles/map_decoder_v1.yaml"
BIN_PROFILE_PATH = REPOSITORY_ROOT / "profiles/road_binning_v1.yaml"
NUMERICAL_CHARTER_PATH = REPOSITORY_ROOT / "benchmarks/numerical_charter.yaml"
FIXTURE_PATH = REPOSITORY_ROOT / "tests/fixtures/road_graphs/topology_v1.osm"
FIXTURE_SHA256 = "eda30cc433fae67cd95584d89e7d6de0f124aed4b396990b0b3da6c3489a4616"
Profiles = tuple[
    MapMatchingProfile,
    str,
    MapDecoderProfile,
    str,
    RoadBinningProfile,
    str,
]


@pytest.fixture(scope="module")
def graph() -> DirectedRoadGraph:
    profile, file_sha256 = load_graph_import_profile(GRAPH_PROFILE_PATH)
    return import_osm_road_graph(
        FIXTURE_PATH,
        profile=profile,
        profile_file_sha256=file_sha256,
        source_object_key="tests/fixtures/road_graphs/topology_v1.osm",
        expected_source_sha256=FIXTURE_SHA256,
        source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
    )


@pytest.fixture(scope="module")
def profiles() -> Profiles:
    matching, matching_sha256 = load_map_matching_profile(MATCH_PROFILE_PATH)
    decoder, decoder_sha256 = load_map_decoder_profile(DECODER_PROFILE_PATH)
    binning, binning_sha256 = load_road_binning_profile(BIN_PROFILE_PATH)
    return (
        matching,
        matching_sha256,
        decoder,
        decoder_sha256,
        binning,
        binning_sha256,
    )


def _time(seconds: str) -> TimePoint:
    return TimePoint.from_decimal_seconds(
        seconds,
        source_key="tests/road-bins",
        field="time",
        epoch=TimeEpoch.UNIX_UTC,
        clock_id="road-bin-clock",
        reference=TimeReference.SAMPLE,
    )


def _arc(
    graph: DirectedRoadGraph, way_id: int, direction: ArcDirection
) -> DirectedRoadArc:
    return next(
        item
        for item in graph.arcs
        if item.source_way_id == way_id and item.direction is direction
    )


def _point_at_offset(
    arc: DirectedRoadArc, offset_m: float
) -> tuple[tuple[float, float], float]:
    remaining = offset_m
    for left, right in pairwise(arc.geometry_local_m):
        delta_x = right[0] - left[0]
        delta_y = right[1] - left[1]
        length = math.hypot(delta_x, delta_y)
        if remaining <= length:
            fraction = remaining / length
            return (
                left[0] + fraction * delta_x,
                left[1] + fraction * delta_y,
            ), math.atan2(delta_y, delta_x)
        remaining -= length
    left, right = arc.geometry_local_m[-2:]
    return (right[0], right[1]), math.atan2(right[1] - left[1], right[0] - left[0])


def _decode_offsets(
    graph: DirectedRoadGraph,
    profiles: Profiles,
    arc: DirectedRoadArc,
    offsets: tuple[float, ...],
    seconds: tuple[str, ...],
    sequence_id: str,
) -> DecodedRoadPath:
    matching, matching_sha, decoder, decoder_sha, _, _ = profiles
    observations = []
    for offset, time in zip(offsets, seconds, strict=True):
        position, heading = _point_at_offset(arc, offset)
        observations.append(
            make_road_match_observation(
                time=_time(time),
                local_frame_id=graph.local_frame.frame.frame_id,
                position_local_m=position,
                heading_rad=heading,
                speed_mps=10.0,
                horizontal_uncertainty_m=0.5,
                horizontal_uncertainty_basis="DECLARED_TRUSTWORTHY",
            )
        )
    result = decode_road_path(
        graph,
        tuple(observations),
        sequence_id=sequence_id,
        source_group_id=f"{sequence_id}-family",
        partition="synthetic",
        matching_profile=matching,
        matching_profile_file_sha256=matching_sha,
        decoder_profile=decoder,
        decoder_profile_file_sha256=decoder_sha,
    )
    assert {item.candidate.directed_arc_id for item in result.points} == {arc.arc_id}
    return result


def _aggregate(
    graph: DirectedRoadGraph,
    profiles: Profiles,
    paths: Sequence[DecodedRoadPath],
    *,
    evidence: Sequence[ModalityEvidenceInterval] = (),
    findings: Sequence[FindingLocalizationRequest] = (),
) -> DirectedRoadCoverageLedger:
    _, _, _, _, profile, profile_sha = profiles
    return aggregate_directed_road_bins(
        graph,
        tuple(paths),
        modality_evidence=tuple(evidence),
        findings=tuple(findings),
        profile=profile,
        profile_file_sha256=profile_sha,
    )


def _finding(sequence_id: str, start: str, end: str) -> Finding:
    interval = SourceInterval(start=_time(start), end=_time(end))
    stream_id = make_stream_id(sequence_id, "trajectory", "reference")
    evidence = EvidenceReference(
        source_artifact_sha256="1" * 64,
        source_interval=interval,
        frame_ids=(),
        derived_artifact_sha256="2" * 64,
        detector_version="1.0.0",
        transformation_lineage=("synthetic-fault-v1",),
    )
    return Finding(
        schema_version="cartosentry.finding.v1",
        finding_id=make_finding_id(
            detector_id="synthetic-spatial-fault",
            detector_version="1.0.0",
            rule_id="affected-interval",
            source_interval=interval.model_dump(mode="json"),
            stream_ids=(stream_id,),
            evidence_fingerprint=[evidence.model_dump(mode="json")],
        ),
        detector_id="synthetic-spatial-fault",
        detector_version="1.0.0",
        rule_id="affected-interval",
        severity=Severity.CRITICAL,
        observability=Observability.OBSERVABLE,
        readiness_effect=ReadinessState.FAIL,
        streams=(stream_id,),
        interval=interval,
        measurement=Measurement(
            name="affected-duration",
            value=20_000_000_000.0,
            unit=MeasurementUnit.NANOSECOND,
        ),
        threshold=Threshold(
            operator=ThresholdOperator.LESS_THAN_OR_EQUAL,
            value=0.0,
            unit=MeasurementUnit.NANOSECOND,
            charter_key="content.spatial-affected-bin-f1",
        ),
        road_bin_ids=(),
        evidence=(evidence,),
        hypotheses=(),
        remediation="Recollect only the localized directed road bins.",
    )


def test_profile_is_frozen_and_binds_decoder_and_numerical_authorities(
    profiles: Profiles,
) -> None:
    _, _, decoder, decoder_sha, profile, profile_sha = profiles
    assert profile.immutable_sha256 == PROFILE_IMMUTABLE_SHA256
    assert profile_sha == PROFILE_FILE_SHA256
    assert profile.authorities.map_decoder_profile_file_sha256 == decoder_sha
    assert (
        profile.authorities.map_decoder_profile_immutable_sha256
        == decoder.immutable_sha256
    )
    assert (
        profile.authorities.numerical_charter_file_sha256
        == hashlib.sha256(NUMERICAL_CHARTER_PATH.read_bytes()).hexdigest()
    )


def test_exact_boundaries_partial_bins_and_reverse_direction(
    graph: DirectedRoadGraph, profiles: Profiles
) -> None:
    forward = _arc(graph, 100, ArcDirection.FORWARD)
    reverse = _arc(graph, 101, ArcDirection.REVERSE)
    forward_path = _decode_offsets(
        graph,
        profiles,
        forward,
        (5.0, 20.0, 40.0, 45.0),
        ("0", "15", "35", "40"),
        "boundary-forward",
    )
    reverse_path = _decode_offsets(
        graph,
        profiles,
        reverse,
        (5.0, 25.0, 45.0),
        ("0", "20", "40"),
        "boundary-reverse",
    )
    ledger = _aggregate(graph, profiles, (forward_path, reverse_path))
    forward_bins = [
        item for item in ledger.bins if item.directed_arc_id == forward.arc_id
    ]
    reverse_bins = [
        item for item in ledger.bins if item.directed_arc_id == reverse.arc_id
    ]
    assert [item.usable_trajectory_distance_m for item in forward_bins[:3]] == [
        15.0,
        20.0,
        5.0,
    ]
    assert [item.usable_trajectory_distance_m for item in reverse_bins[:3]] == [
        15.0,
        20.0,
        5.0,
    ]
    assert {item.arc_direction for item in reverse_bins} == {ArcDirection.REVERSE}
    assert forward_bins[-1].final_partial_bin
    assert forward_bins[-1].true_length_m == pytest.approx(
        forward.length_m % 20.0, abs=1e-6
    )


def test_adjacent_windows_from_one_pass_do_not_inflate_traversal_count(
    graph: DirectedRoadGraph, profiles: Profiles
) -> None:
    arc = _arc(graph, 100, ArcDirection.FORWARD)
    first = _decode_offsets(
        graph, profiles, arc, (5.0, 15.0), ("0", "10"), "adjacent-pass"
    )
    second = _decode_offsets(
        graph, profiles, arc, (15.0, 19.0), ("10", "14"), "adjacent-pass"
    )
    ledger = _aggregate(graph, profiles, (first, second))
    selected = next(
        item
        for item in ledger.bins
        if item.directed_arc_id == arc.arc_id and item.longitudinal_bin_index == 0
    )
    assert selected.independent_traversal_count == 1
    assert selected.usable_trajectory_distance_m == 14.0
    assert selected.traversals[0].source_road_match_ids == tuple(
        sorted((first.road_match_id, second.road_match_id))
    )


def test_ambiguous_and_off_map_paths_do_not_create_usable_coverage(
    graph: DirectedRoadGraph, profiles: Profiles
) -> None:
    matching, matching_sha, decoder, decoder_sha, _, _ = profiles
    south = _arc(graph, 140, ArcDirection.FORWARD)
    north = _arc(graph, 141, ArcDirection.FORWARD)
    observations = []
    for index, offset in enumerate((20.0, 60.0, 100.0)):
        south_position, heading = _point_at_offset(south, offset)
        north_position, _ = _point_at_offset(north, offset)
        observations.append(
            make_road_match_observation(
                time=_time(str(index * 10)),
                local_frame_id=graph.local_frame.frame.frame_id,
                position_local_m=(
                    (south_position[0] + north_position[0]) / 2.0,
                    (south_position[1] + north_position[1]) / 2.0,
                ),
                heading_rad=heading,
                speed_mps=10.0,
                horizontal_uncertainty_m=5.0,
                horizontal_uncertainty_basis="DECLARED_TRUSTWORTHY",
            )
        )
    ambiguous = decode_road_path(
        graph,
        tuple(observations),
        sequence_id="ambiguous-bin-path",
        source_group_id="ambiguous-family",
        partition="synthetic",
        matching_profile=matching,
        matching_profile_file_sha256=matching_sha,
        decoder_profile=decoder,
        decoder_profile_file_sha256=decoder_sha,
    )
    offmap_observations = tuple(
        make_road_match_observation(
            time=_time(str(index * 10)),
            local_frame_id=graph.local_frame.frame.frame_id,
            position_local_m=(10_000.0 + index * 10.0, 10_000.0),
            heading_rad=0.0,
            speed_mps=5.0,
            horizontal_uncertainty_m=None,
        )
        for index in range(3)
    )
    offmap = decode_road_path(
        graph,
        offmap_observations,
        sequence_id="offmap-bin-path",
        source_group_id="offmap-family",
        partition="synthetic",
        matching_profile=matching,
        matching_profile_file_sha256=matching_sha,
        decoder_profile=decoder,
        decoder_profile_file_sha256=decoder_sha,
    )
    assert all(item.confidence.value == "AMBIGUOUS" for item in ambiguous.points)
    assert {item.candidate.state for item in offmap.points} == {CandidateState.OFF_MAP}
    ledger = _aggregate(graph, profiles, (ambiguous, offmap))
    assert sum(item.usable_trajectory_distance_m for item in ledger.bins) == 0.0
    assert sum(item.independent_traversal_count for item in ledger.bins) == 0


def test_modality_join_and_affected_finding_localize_to_exact_bins(
    graph: DirectedRoadGraph, profiles: Profiles
) -> None:
    arc = _arc(graph, 100, ArcDirection.FORWARD)
    path = _decode_offsets(
        graph, profiles, arc, (5.0, 45.0), ("0", "40"), "localized-fault"
    )
    evidence = make_modality_evidence_interval(
        sequence_id=path.sequence_id,
        modality=SensorModality.LIDAR,
        interval=SourceInterval(start=_time("5"), end=_time("35")),
        usable=True,
        point_count=300.0,
        lidar_overlap_support_m=0.2,
        timestamp_supported=True,
        source_artifact_sha256="3" * 64,
        transformation_lineage=("lidar-integrity-v1",),
    )
    finding = _finding(path.sequence_id, "10", "30")
    ledger = _aggregate(
        graph,
        profiles,
        (path,),
        evidence=(evidence,),
        findings=(
            FindingLocalizationRequest(sequence_id=path.sequence_id, finding=finding),
        ),
    )
    expected = tuple(
        sorted(
            (
                make_road_bin_id(graph.graph_id, arc.arc_id, 0),
                make_road_bin_id(graph.graph_id, arc.arc_id, 1),
            )
        )
    )
    assert ledger.finding_localizations[0].road_bin_ids == expected
    assert ledger.localized_findings[0].road_bin_ids == expected
    affected = [item for item in ledger.bins if finding.finding_id in item.finding_ids]
    assert tuple(item.road_bin_id for item in affected) == expected
    lidar_support = {
        item.longitudinal_bin_index: next(
            value for value in item.modalities if value.modality is SensorModality.LIDAR
        )
        for item in affected
    }
    assert lidar_support[0].valid_duration_ns == 10_000_000_000
    assert lidar_support[1].valid_duration_ns == 20_000_000_000
    assert lidar_support[0].point_support == 100.0
    assert lidar_support[1].point_support == 200.0
    ledger.assert_identity()


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"value":NaN}',
        b"[" * 65 + b"0" + b"]" * 65,
        b" " * (256 * 1024 + 1),
    ],
)
def test_profile_rejects_hostile_json(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "hostile.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        load_road_binning_profile(path)


def test_ledger_rejects_portable_identity_tampering(
    graph: DirectedRoadGraph, profiles: Profiles
) -> None:
    arc = _arc(graph, 100, ArcDirection.FORWARD)
    path = _decode_offsets(
        graph, profiles, arc, (5.0, 15.0), ("0", "10"), "identity-bin-path"
    )
    ledger = _aggregate(graph, profiles, (path,))
    raw = json.loads(ledger.model_dump_json())
    raw["bins"][0]["yaw_excitation_rad"] += 1.0
    with pytest.raises(ValueError, match="ledger identity"):
        type(ledger).model_validate_json(json.dumps(raw))
