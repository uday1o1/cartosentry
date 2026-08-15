"""Frozen M5.4 directed-road bin and spatial-localization qualification."""

from __future__ import annotations

import hashlib
import json
import math
import random
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from cartosentry.identifiers import make_finding_id, make_road_bin_id, make_stream_id
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.road_bins import (
    FindingLocalizationRequest,
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
    ALGORITHM_BACKEND,
    MapMatchingProfile,
    load_map_matching_profile,
    make_road_match_observation,
)

GATE_IMMUTABLE_SHA256 = (
    "034d18730464f239fd58c4ff2af921d128571db6f7674feb0dfaaa08f564bf6f"
)
MAXIMUM_GATE_BYTES = 256 * 1024
MAXIMUM_CHARTER_BYTES = 1024 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


class GateAuthorities(StrictModel):
    graph_import_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    map_matching_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    map_decoder_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    road_binning_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    numerical_charter_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    fixture_object_key: Literal["tests/fixtures/road_graphs/topology_v1.osm"]
    fixture_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SyntheticPopulation(StrictModel):
    family_count: Literal[12]
    injected_events_per_family: Literal[3]
    total_injected_events: Literal[36]
    fault_interval_start_seconds: Literal[10]
    fault_interval_end_seconds: Literal[30]
    path_start_offset_m: Annotated[float, Field(ge=5.0, le=5.0)]
    path_end_offset_m: Annotated[float, Field(ge=45.0, le=45.0)]
    path_duration_seconds: Literal[40]


class GateThresholds(StrictModel):
    exact_bin_coverage_mismatch_count_maximum: Literal[0]
    adjacent_window_traversal_inflation_count_maximum: Literal[0]
    spatial_affected_bin_f1_minimum: Annotated[float, Field(ge=0.9, le=0.9)]


class GateStatistics(StrictModel):
    bootstrap_unit: Literal["synthetic_family_id"]
    bootstrap_seed: Literal[2026081402]
    bootstrap_replicates: Literal[10000]
    confidence_level: Annotated[float, Field(ge=0.95, le=0.95)]
    minimum_independent_clusters: Literal[12]
    minimum_injected_events: Literal[30]
    degenerate_resample_behavior: Literal["FAIL_CONFIRMATORY_GATE"]


class RoadBinGate(StrictModel):
    schema_version: Literal[1]
    gate_id: Literal["m5.4-directed-road-bins-v1"]
    gate_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_M5_4_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: GateAuthorities
    synthetic_population: SyntheticPopulation
    thresholds: GateThresholds
    statistics: GateStatistics

    @model_validator(mode="after")
    def validate_identity_and_population(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"immutable_sha256"})
        if (
            self.immutable_sha256 != GATE_IMMUTABLE_SHA256
            or _canonical_hash(payload) != self.immutable_sha256
        ):
            raise ValueError("M5.4 road-bin gate identity is not pinned")
        population = self.synthetic_population
        if (
            population.family_count * population.injected_events_per_family
            != population.total_injected_events
        ):
            raise ValueError("M5.4 synthetic population support is inconsistent")
        return self


def load_road_bin_gate(path: Path) -> tuple[RoadBinGate, str]:
    """Load and authenticate the frozen M5.4 acceptance gate."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="M5.4 road-bin gate",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="M5.4 road-bin gate",
        )
    except ManifestBoundaryError as error:
        raise ValueError("M5.4 road-bin gate is unavailable or malformed") from error
    if not isinstance(decoded, dict):
        raise ValueError("M5.4 road-bin gate must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("M5.4 road-bin gate immutable hash is invalid")
    return RoadBinGate.model_validate_json(content), hashlib.sha256(content).hexdigest()


def _time(seconds: int) -> TimePoint:
    return TimePoint.from_decimal_seconds(
        str(seconds),
        source_key="qualification/m5.4",
        field="time",
        epoch=TimeEpoch.UNIX_UTC,
        clock_id="m5.4-synthetic-clock",
        reference=TimeReference.SAMPLE,
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
    raise ValueError("M5.4 qualification offset exceeds its source arc")


def _decode_offsets(
    graph: DirectedRoadGraph,
    matching_profile: MapMatchingProfile,
    matching_profile_sha256: str,
    decoder_profile: MapDecoderProfile,
    decoder_profile_sha256: str,
    arc: DirectedRoadArc,
    offsets: tuple[float, ...],
    seconds: tuple[int, ...],
    *,
    sequence_id: str,
    source_group_id: str,
) -> DecodedRoadPath:
    observations = []
    for offset, second in zip(offsets, seconds, strict=True):
        position, heading = _point_at_offset(arc, offset)
        observations.append(
            make_road_match_observation(
                time=_time(second),
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
        source_group_id=source_group_id,
        partition="synthetic",
        matching_profile=matching_profile,
        matching_profile_file_sha256=matching_profile_sha256,
        decoder_profile=decoder_profile,
        decoder_profile_file_sha256=decoder_profile_sha256,
    )
    if {item.candidate.directed_arc_id for item in result.points} != {arc.arc_id}:
        raise ValueError("M5.4 qualification path did not retain its directed arc")
    return result


def _finding(sequence_id: str, event_index: int, start: int, end: int) -> Finding:
    interval = SourceInterval(start=_time(start), end=_time(end))
    stream_id = make_stream_id(sequence_id, "trajectory", "reference")
    evidence = EvidenceReference(
        source_artifact_sha256=hashlib.sha256(
            f"m5.4-source-{event_index}".encode()
        ).hexdigest(),
        source_interval=interval,
        frame_ids=(),
        derived_artifact_sha256=hashlib.sha256(
            f"m5.4-derived-{event_index}".encode()
        ).hexdigest(),
        detector_version="1.0.0",
        transformation_lineage=("m5.4-spatial-fault-v1",),
    )
    finding_id = make_finding_id(
        detector_id="synthetic-spatial-fault",
        detector_version="1.0.0",
        rule_id="affected-interval",
        source_interval=interval.model_dump(mode="json"),
        stream_ids=(stream_id,),
        evidence_fingerprint=[evidence.model_dump(mode="json")],
    )
    return Finding(
        schema_version="cartosentry.finding.v1",
        finding_id=finding_id,
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
            value=float((end - start) * 1_000_000_000),
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


def _load_numerical_threshold(path: Path, expected_sha256: str) -> float:
    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_CHARTER_BYTES,
            context="numerical charter",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_CHARTER_BYTES,
            context="numerical charter",
        )
    except ManifestBoundaryError as error:
        raise ValueError("numerical charter is unavailable or malformed") from error
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("M5.4 numerical charter authority is foreign")
    if not isinstance(decoded, dict) or not isinstance(decoded.get("gates"), dict):
        raise ValueError("numerical charter does not declare gates")
    gate = cast(dict[str, object], decoded["gates"]).get(
        "content.spatial_affected_bin_f1"
    )
    if not isinstance(gate, dict) or gate.get("operator") != "fraction_ge":
        raise ValueError("numerical charter spatial-bin gate is malformed")
    value = gate.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("numerical charter spatial-bin threshold is invalid")
    return float(value)


def _f1(predicted: set[str], expected: set[str]) -> tuple[float, int, int, int]:
    true_positive = len(predicted & expected)
    false_positive = len(predicted - expected)
    false_negative = len(expected - predicted)
    denominator = 2 * true_positive + false_positive + false_negative
    value = 1.0 if denominator == 0 else 2 * true_positive / denominator
    return value, true_positive, false_positive, false_negative


def _cluster_bootstrap_lower_bound(
    observations: dict[str, tuple[tuple[set[str], set[str]], ...]],
    *,
    seed: int,
    replicates: int,
    confidence_level: float,
) -> float | None:
    cluster_ids = tuple(sorted(observations))
    if not cluster_ids:
        return None
    generator = random.Random(seed)
    values: list[float] = []
    for _ in range(replicates):
        predicted: set[str] = set()
        expected: set[str] = set()
        for draw_index in range(len(cluster_ids)):
            selected = cluster_ids[generator.randrange(len(cluster_ids))]
            for event_index, (event_predicted, event_expected) in enumerate(
                observations[selected]
            ):
                prefix = f"{draw_index}:{event_index}:"
                predicted.update(prefix + item for item in event_predicted)
                expected.update(prefix + item for item in event_expected)
        values.append(_f1(predicted, expected)[0])
    values.sort()
    quantile_index = max(
        0,
        min(
            len(values) - 1,
            math.floor((1.0 - confidence_level) * len(values)),
        ),
    )
    return values[quantile_index]


def _arc(
    graph: DirectedRoadGraph, way_id: int, direction: ArcDirection
) -> DirectedRoadArc:
    return next(
        item
        for item in graph.arcs
        if item.source_way_id == way_id and item.direction is direction
    )


def _adjacent_window_inflation(
    graph: DirectedRoadGraph,
    matching_profile: MapMatchingProfile,
    matching_sha256: str,
    decoder_profile: MapDecoderProfile,
    decoder_sha256: str,
    binning_profile: RoadBinningProfile,
    binning_sha256: str,
    arc: DirectedRoadArc,
) -> int:
    first = _decode_offsets(
        graph,
        matching_profile,
        matching_sha256,
        decoder_profile,
        decoder_sha256,
        arc,
        (5.0, 15.0),
        (0, 10),
        sequence_id="m5.4-adjacent-window",
        source_group_id="m5.4-adjacent-family",
    )
    second = _decode_offsets(
        graph,
        matching_profile,
        matching_sha256,
        decoder_profile,
        decoder_sha256,
        arc,
        (15.0, 19.0),
        (10, 14),
        sequence_id="m5.4-adjacent-window",
        source_group_id="m5.4-adjacent-family",
    )
    ledger = aggregate_directed_road_bins(
        graph,
        (first, second),
        modality_evidence=(),
        findings=(),
        profile=binning_profile,
        profile_file_sha256=binning_sha256,
    )
    selected = next(
        item
        for item in ledger.bins
        if item.directed_arc_id == arc.arc_id and item.longitudinal_bin_index == 0
    )
    return abs(selected.independent_traversal_count - 1)


def qualify_directed_road_bins(
    *,
    graph_profile_path: Path,
    matching_profile_path: Path,
    decoder_profile_path: Path,
    binning_profile_path: Path,
    gate_path: Path,
    numerical_charter_path: Path,
    fixture_path: Path,
) -> dict[str, object]:
    """Run the public frozen M5.4 synthetic acceptance workflow."""

    gate, gate_file_sha256 = load_road_bin_gate(gate_path)
    graph_profile, graph_profile_sha256 = load_graph_import_profile(graph_profile_path)
    matching_profile, matching_sha256 = load_map_matching_profile(matching_profile_path)
    decoder_profile, decoder_sha256 = load_map_decoder_profile(decoder_profile_path)
    binning_profile, binning_sha256 = load_road_binning_profile(binning_profile_path)
    authorities = gate.authorities
    if (
        graph_profile_sha256 != authorities.graph_import_profile_file_sha256
        or matching_sha256 != authorities.map_matching_profile_file_sha256
        or decoder_sha256 != authorities.map_decoder_profile_file_sha256
        or binning_sha256 != authorities.road_binning_profile_file_sha256
        or hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        != authorities.fixture_sha256
    ):
        raise ValueError("M5.4 qualification authority is foreign")
    numerical_threshold = _load_numerical_threshold(
        numerical_charter_path,
        authorities.numerical_charter_file_sha256,
    )
    if numerical_threshold != gate.thresholds.spatial_affected_bin_f1_minimum:
        raise ValueError("M5.4 gate does not match the numerical charter")
    graph = import_osm_road_graph(
        fixture_path,
        profile=graph_profile,
        profile_file_sha256=graph_profile_sha256,
        source_object_key=authorities.fixture_object_key,
        expected_source_sha256=authorities.fixture_sha256,
        source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
    )
    forward = _arc(graph, 100, ArcDirection.FORWARD)
    reverse = _arc(graph, 101, ArcDirection.REVERSE)
    population = gate.synthetic_population
    expected_bin_ids = tuple(
        make_road_bin_id(graph.graph_id, forward.arc_id, index) for index in (0, 1)
    )
    paths: list[DecodedRoadPath] = []
    evidence = []
    finding_requests: list[FindingLocalizationRequest] = []
    family_by_finding: dict[str, str] = {}
    for family_index in range(population.family_count):
        source_group_id = f"m5.4-family-{family_index:02d}"
        for event_in_family in range(population.injected_events_per_family):
            event_index = (
                family_index * population.injected_events_per_family + event_in_family
            )
            sequence_id = f"m5.4-event-{event_index:02d}"
            path = _decode_offsets(
                graph,
                matching_profile,
                matching_sha256,
                decoder_profile,
                decoder_sha256,
                forward,
                (population.path_start_offset_m, population.path_end_offset_m),
                (0, population.path_duration_seconds),
                sequence_id=sequence_id,
                source_group_id=source_group_id,
            )
            paths.append(path)
            evidence.append(
                make_modality_evidence_interval(
                    sequence_id=sequence_id,
                    modality=SensorModality.LIDAR,
                    interval=SourceInterval(start=_time(5), end=_time(35)),
                    usable=True,
                    point_count=300.0,
                    lidar_overlap_support_m=0.2,
                    timestamp_supported=True,
                    source_artifact_sha256=hashlib.sha256(
                        f"m5.4-lidar-{event_index}".encode()
                    ).hexdigest(),
                    transformation_lineage=("m5.4-lidar-evidence-v1",),
                )
            )
            finding = _finding(
                sequence_id,
                event_index,
                population.fault_interval_start_seconds,
                population.fault_interval_end_seconds,
            )
            finding_requests.append(
                FindingLocalizationRequest(sequence_id=sequence_id, finding=finding)
            )
            family_by_finding[finding.finding_id] = source_group_id
    ledger = aggregate_directed_road_bins(
        graph,
        tuple(paths),
        modality_evidence=tuple(evidence),
        findings=tuple(finding_requests),
        profile=binning_profile,
        profile_file_sha256=binning_sha256,
    )

    reverse_path = _decode_offsets(
        graph,
        matching_profile,
        matching_sha256,
        decoder_profile,
        decoder_sha256,
        reverse,
        (5.0, 45.0),
        (0, 40),
        sequence_id="m5.4-reverse-control",
        source_group_id="m5.4-reverse-family",
    )
    reverse_ledger = aggregate_directed_road_bins(
        graph,
        (reverse_path,),
        modality_evidence=(),
        findings=(),
        profile=binning_profile,
        profile_file_sha256=binning_sha256,
    )

    expected_distances = {0: 15.0, 1: 20.0, 2: 5.0}
    coverage_mismatches = 0
    for arc, selected_ledger, expected_traversal_count in (
        (forward, ledger, population.total_injected_events),
        (reverse, reverse_ledger, 1),
    ):
        selected_bins = {
            item.longitudinal_bin_index: item
            for item in selected_ledger.bins
            if item.directed_arc_id == arc.arc_id
        }
        for bin_index, distance in expected_distances.items():
            item = selected_bins[bin_index]
            if (
                item.independent_traversal_count != expected_traversal_count
                or not math.isclose(
                    item.usable_trajectory_distance_m,
                    distance * expected_traversal_count,
                    abs_tol=1e-6,
                )
                or any(
                    not math.isclose(
                        traversal.usable_distance_m, distance, abs_tol=1e-6
                    )
                    for traversal in item.traversals
                )
            ):
                coverage_mismatches += 1
        if any(
            item.usable_trajectory_distance_m != 0.0
            for index, item in selected_bins.items()
            if index not in expected_distances
        ):
            coverage_mismatches += 1

    localization_by_finding = {
        item.finding_id: set(item.road_bin_ids) for item in ledger.finding_localizations
    }
    predicted_all: set[str] = set()
    expected_all: set[str] = set()
    clustered: dict[str, list[tuple[set[str], set[str]]]] = {}
    for request in finding_requests:
        predicted = localization_by_finding[request.finding.finding_id]
        expected = set(expected_bin_ids)
        prefix = f"{request.finding.finding_id}:"
        predicted_all.update(prefix + item for item in predicted)
        expected_all.update(prefix + item for item in expected)
        clustered.setdefault(family_by_finding[request.finding.finding_id], []).append(
            (predicted, expected)
        )
    f1, true_positive, false_positive, false_negative = _f1(predicted_all, expected_all)
    lower_bound = _cluster_bootstrap_lower_bound(
        {key: tuple(value) for key, value in clustered.items()},
        seed=gate.statistics.bootstrap_seed,
        replicates=gate.statistics.bootstrap_replicates,
        confidence_level=gate.statistics.confidence_level,
    )
    adjacent_inflation = _adjacent_window_inflation(
        graph,
        matching_profile,
        matching_sha256,
        decoder_profile,
        decoder_sha256,
        binning_profile,
        binning_sha256,
        forward,
    )
    support_gate = (
        len(clustered) >= gate.statistics.minimum_independent_clusters
        and len(finding_requests) >= gate.statistics.minimum_injected_events
    )
    coverage_gate = (
        coverage_mismatches <= gate.thresholds.exact_bin_coverage_mismatch_count_maximum
    )
    traversal_gate = (
        adjacent_inflation
        <= gate.thresholds.adjacent_window_traversal_inflation_count_maximum
    )
    spatial_gate = (
        support_gate
        and lower_bound is not None
        and lower_bound >= gate.thresholds.spatial_affected_bin_f1_minimum
    )
    accepted = coverage_gate and traversal_gate and spatial_gate
    sample_localization = ledger.finding_localizations[0]
    return {
        "schema_version": "cartosentry.m5.4-road-bin-qualification.v1",
        "accepted": accepted,
        "algorithm_backend": ALGORITHM_BACKEND,
        "authorities": {
            "gate_file_sha256": gate_file_sha256,
            "gate_immutable_sha256": gate.immutable_sha256,
            "graph_id": graph.graph_id,
            "graph_import_profile_file_sha256": graph_profile_sha256,
            "map_matching_profile_file_sha256": matching_sha256,
            "map_decoder_profile_file_sha256": decoder_sha256,
            "road_binning_profile_file_sha256": binning_sha256,
            "road_binning_profile_immutable_sha256": binning_profile.immutable_sha256,
            "numerical_charter_file_sha256": authorities.numerical_charter_file_sha256,
            "fixture_sha256": authorities.fixture_sha256,
        },
        "support": {
            "independent_synthetic_family_count": len(clustered),
            "injected_event_count": len(finding_requests),
            "minimum_independent_clusters": (
                gate.statistics.minimum_independent_clusters
            ),
            "minimum_injected_events": gate.statistics.minimum_injected_events,
        },
        "metrics": {
            "exact_bin_coverage_mismatch_count": coverage_mismatches,
            "adjacent_window_traversal_inflation_count": adjacent_inflation,
            "spatial_affected_bin_f1": f1,
            "spatial_affected_bin_f1_lower_95": lower_bound,
            "true_positive_affected_bins": true_positive,
            "false_positive_affected_bins": false_positive,
            "false_negative_affected_bins": false_negative,
            "materialized_directed_bin_count": len(ledger.bins),
            "final_partial_bin_count": sum(
                item.final_partial_bin for item in ledger.bins
            ),
        },
        "gates": {
            "confirmatory_support": support_gate,
            "exact_bin_coverage": coverage_gate,
            "adjacent_windows_preserve_pass_identity": traversal_gate,
            "spatial_affected_bin_f1": spatial_gate,
        },
        "demonstration": {
            "coverage_ledger_id": ledger.coverage_ledger_id,
            "sample_finding_id": sample_localization.finding_id,
            "sample_affected_road_bin_ids": list(sample_localization.road_bin_ids),
            "sample_expected_road_bin_ids": list(expected_bin_ids),
            "forward_arc_id": forward.arc_id,
            "reverse_arc_id": reverse.arc_id,
            "bin_length_m": binning_profile.parameter_charter.bin_length_m,
        },
    }


__all__ = [
    "GATE_IMMUTABLE_SHA256",
    "RoadBinGate",
    "load_road_bin_gate",
    "qualify_directed_road_bins",
]
