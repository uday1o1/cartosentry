"""Authenticated directed-road binning and spatial evidence composition."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry import _core
from cartosentry.artifacts import Finding, SensorModality, Severity, SourceInterval
from cartosentry.contracts import TimePoint
from cartosentry.identifiers import make_road_bin_id
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.road_decoder import (
    PROFILE_FILE_SHA256 as DECODER_PROFILE_FILE_SHA256,
)
from cartosentry.road_decoder import (
    PROFILE_IMMUTABLE_SHA256 as DECODER_PROFILE_IMMUTABLE_SHA256,
)
from cartosentry.road_decoder import DecodedRoadPath, MatchConfidence
from cartosentry.road_graph import (
    PROFILE_IMMUTABLE_SHA256 as GRAPH_PROFILE_IMMUTABLE_SHA256,
)
from cartosentry.road_graph import (
    ArcDirection,
    DirectedRoadGraph,
    validate_graph_identity,
)
from cartosentry.road_matching import ALGORITHM_BACKEND, CandidateState

PROFILE_IMMUTABLE_SHA256 = (
    "ab6e30104c10d839d1175aecd392f62c8eb583fb2df66c8595473e1d16939fa4"
)
PROFILE_FILE_SHA256 = "4c5fd9837063b50c44bb4a25b3ee0eede0897b38335a71bd504488c0c70578ba"
MAXIMUM_PROFILE_BYTES = 256 * 1024


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


def _stable_id(prefix: str, payload: object) -> str:
    return f"{prefix}-sha256-{_canonical_hash(payload)}"


class RoadBinningAuthorities(StrictModel):
    graph_import_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    map_decoder_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    map_decoder_profile_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    numerical_charter_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RoadBinningParameters(StrictModel):
    bin_length_m: Annotated[float, Field(gt=0.0)]
    independent_traversal_minimum_gap_ns: Annotated[int, Field(ge=0)]
    maximum_paths: Annotated[int, Field(gt=0)]
    maximum_points_per_path: Annotated[int, Field(gt=1)]
    maximum_total_points: Annotated[int, Field(gt=1)]
    maximum_generated_bins: Annotated[int, Field(gt=0)]
    maximum_modality_evidence_intervals: Annotated[int, Field(gt=0)]
    maximum_findings: Annotated[int, Field(gt=0)]
    distance_rounding_decimal_places: Annotated[int, Field(ge=0, le=12)]


class RoadBinningProfile(StrictModel):
    schema_version: Literal[1]
    profile_id: Literal["road-binning-v1"]
    profile_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_M5_4_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: RoadBinningAuthorities
    parameter_charter: RoadBinningParameters

    def assert_identity(self) -> None:
        payload = self.model_dump(mode="json", exclude={"immutable_sha256"})
        if (
            self.immutable_sha256 != PROFILE_IMMUTABLE_SHA256
            or _canonical_hash(payload) != self.immutable_sha256
        ):
            raise ValueError("road-binning profile identity is not pinned")

    @model_validator(mode="after")
    def validate_authorities(self) -> Self:
        self.assert_identity()
        if (
            self.authorities.map_decoder_profile_file_sha256
            != DECODER_PROFILE_FILE_SHA256
            or self.authorities.map_decoder_profile_immutable_sha256
            != DECODER_PROFILE_IMMUTABLE_SHA256
        ):
            raise ValueError("road-binning decoder authority is not exact")
        return self


def load_road_binning_profile(path: Path) -> tuple[RoadBinningProfile, str]:
    """Load and self-authenticate the frozen M5.4 aggregation charter."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="road-binning profile",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="road-binning profile",
        )
    except ManifestBoundaryError as error:
        raise ValueError("road-binning profile is unavailable or malformed") from error
    if not isinstance(decoded, dict):
        raise ValueError("road-binning profile must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("road-binning profile immutable hash is invalid")
    file_sha256 = hashlib.sha256(content).hexdigest()
    if file_sha256 != PROFILE_FILE_SHA256:
        raise ValueError("road-binning profile file identity is not pinned")
    return RoadBinningProfile.model_validate_json(content), file_sha256


class ModalityEvidenceInterval(StrictModel):
    evidence_interval_id: Annotated[
        str, Field(pattern=r"^evidence-interval-sha256-[0-9a-f]{64}$")
    ]
    sequence_id: Annotated[str, Field(min_length=1)]
    modality: SensorModality
    interval: SourceInterval
    usable: bool
    point_count: Annotated[float, Field(ge=0.0)]
    lidar_overlap_support_m: Annotated[float, Field(ge=0.0)] | None
    timestamp_supported: bool
    source_artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    transformation_lineage: tuple[Annotated[str, Field(min_length=1)], ...]

    @model_validator(mode="after")
    def validate_identity_and_modality(self) -> Self:
        if self.modality is not SensorModality.LIDAR and (
            self.point_count != 0.0 or self.lidar_overlap_support_m is not None
        ):
            raise ValueError("only lidar evidence may carry point or overlap support")
        payload = self.model_dump(mode="json", exclude={"evidence_interval_id"})
        expected = _stable_id("evidence-interval", payload)
        if self.evidence_interval_id != expected:
            raise ValueError("modality evidence interval identity is invalid")
        return self


def make_modality_evidence_interval(
    *,
    sequence_id: str,
    modality: SensorModality,
    interval: SourceInterval,
    usable: bool,
    point_count: float = 0.0,
    lidar_overlap_support_m: float | None = None,
    timestamp_supported: bool,
    source_artifact_sha256: str,
    transformation_lineage: tuple[str, ...],
) -> ModalityEvidenceInterval:
    identity_payload: dict[str, object] = {
        "sequence_id": sequence_id,
        "modality": modality.value,
        "interval": interval.model_dump(mode="json"),
        "usable": usable,
        "point_count": float(point_count),
        "lidar_overlap_support_m": (
            None if lidar_overlap_support_m is None else float(lidar_overlap_support_m)
        ),
        "timestamp_supported": timestamp_supported,
        "source_artifact_sha256": source_artifact_sha256,
        "transformation_lineage": transformation_lineage,
    }
    return ModalityEvidenceInterval(
        evidence_interval_id=_stable_id("evidence-interval", identity_payload),
        sequence_id=sequence_id,
        modality=modality,
        interval=interval,
        usable=usable,
        point_count=float(point_count),
        lidar_overlap_support_m=(
            None if lidar_overlap_support_m is None else float(lidar_overlap_support_m)
        ),
        timestamp_supported=timestamp_supported,
        source_artifact_sha256=source_artifact_sha256,
        transformation_lineage=transformation_lineage,
    )


class FindingLocalizationRequest(StrictModel):
    sequence_id: Annotated[str, Field(min_length=1)]
    finding: Finding

    @model_validator(mode="after")
    def validate_unlocalized_input(self) -> Self:
        if self.finding.road_bin_ids:
            raise ValueError(
                "finding localization input must not already name road bins"
            )
        return self


class BinModalityEvidence(StrictModel):
    modality: SensorModality
    valid_duration_ns: Annotated[int, Field(ge=0)]
    point_support: Annotated[float, Field(ge=0.0)]
    mean_overlap_support_m: Annotated[float, Field(ge=0.0)] | None
    timestamp_supported_duration_ns: Annotated[int, Field(ge=0)]
    evidence_interval_ids: tuple[
        Annotated[str, Field(pattern=r"^evidence-interval-sha256-[0-9a-f]{64}$")],
        ...,
    ]

    @model_validator(mode="after")
    def validate_support(self) -> Self:
        if self.timestamp_supported_duration_ns > self.valid_duration_ns:
            raise ValueError("timestamp support exceeds valid modality duration")
        if self.modality is not SensorModality.LIDAR and (
            self.point_support != 0.0 or self.mean_overlap_support_m is not None
        ):
            raise ValueError("non-lidar bin evidence carries lidar-only support")
        if tuple(sorted(set(self.evidence_interval_ids))) != self.evidence_interval_ids:
            raise ValueError("bin modality evidence identities are not canonical")
        return self


class RoadBinTraversal(StrictModel):
    traversal_id: Annotated[str, Field(pattern=r"^road-traversal-sha256-[0-9a-f]{64}$")]
    road_bin_id: Annotated[str, Field(pattern=r"^road-bin-sha256-[0-9a-f]{64}$")]
    directed_arc_id: Annotated[str, Field(pattern=r"^osm-arc-sha256-[0-9a-f]{64}$")]
    arc_direction: ArcDirection
    longitudinal_bin_index: Annotated[int, Field(ge=0)]
    sequence_id: Annotated[str, Field(min_length=1)]
    source_group_id: Annotated[str, Field(min_length=1)]
    traversal_ordinal: Annotated[int, Field(ge=0)]
    first_time_ns: int
    last_time_ns: int
    entry_offset_m: Annotated[float, Field(ge=0.0)]
    exit_offset_m: Annotated[float, Field(gt=0.0)]
    usable_duration_ns: Annotated[int, Field(gt=0)]
    usable_distance_m: Annotated[float, Field(gt=0.0)]
    speed_sample_count: Annotated[int, Field(gt=0)]
    minimum_speed_mps: Annotated[float, Field(ge=0.0)]
    mean_speed_mps: Annotated[float, Field(ge=0.0)]
    maximum_speed_mps: Annotated[float, Field(ge=0.0)]
    yaw_excitation_rad: Annotated[float, Field(ge=0.0)]
    source_road_match_ids: tuple[
        Annotated[str, Field(pattern=r"^road-match-sha256-[0-9a-f]{64}$")], ...
    ]
    modalities: tuple[BinModalityEvidence, ...]
    finding_ids: tuple[
        Annotated[str, Field(pattern=r"^finding-sha256-[0-9a-f]{64}$")], ...
    ]
    critical_finding_ids: tuple[
        Annotated[str, Field(pattern=r"^finding-sha256-[0-9a-f]{64}$")], ...
    ]

    @model_validator(mode="after")
    def validate_traversal(self) -> Self:
        if self.last_time_ns <= self.first_time_ns:
            raise ValueError("road-bin traversal time bounds are invalid")
        if self.exit_offset_m <= self.entry_offset_m:
            raise ValueError("road-bin traversal must advance along its directed arc")
        if not (
            self.minimum_speed_mps <= self.mean_speed_mps <= self.maximum_speed_mps
        ):
            raise ValueError("road-bin traversal speed distribution is invalid")
        collections = (
            self.source_road_match_ids,
            self.finding_ids,
            self.critical_finding_ids,
        )
        if any(tuple(sorted(set(values))) != values for values in collections):
            raise ValueError("road-bin traversal references are not canonical")
        if not set(self.critical_finding_ids).issubset(self.finding_ids):
            raise ValueError("critical findings are not a subset of affected findings")
        identity = {
            "arc_direction": self.arc_direction,
            "directed_arc_id": self.directed_arc_id,
            "sequence_id": self.sequence_id,
            "source_group_id": self.source_group_id,
            "traversal_ordinal": self.traversal_ordinal,
        }
        if self.traversal_id != _stable_id("road-traversal", identity):
            raise ValueError("road-bin traversal identity is invalid")
        return self


class DirectedRoadBinCoverage(StrictModel):
    road_bin_id: Annotated[str, Field(pattern=r"^road-bin-sha256-[0-9a-f]{64}$")]
    road_graph_id: Annotated[str, Field(pattern=r"^road-graph-sha256-[0-9a-f]{64}$")]
    directed_arc_id: Annotated[str, Field(pattern=r"^osm-arc-sha256-[0-9a-f]{64}$")]
    arc_direction: ArcDirection
    longitudinal_bin_index: Annotated[int, Field(ge=0)]
    start_offset_m: Annotated[float, Field(ge=0.0)]
    end_offset_m: Annotated[float, Field(gt=0.0)]
    true_length_m: Annotated[float, Field(gt=0.0)]
    final_partial_bin: bool
    usable_duration_ns: Annotated[int, Field(ge=0)]
    usable_trajectory_distance_m: Annotated[float, Field(ge=0.0)]
    independent_traversal_count: Annotated[int, Field(ge=0)]
    speed_sample_count: Annotated[int, Field(ge=0)]
    minimum_speed_mps: Annotated[float, Field(ge=0.0)] | None
    mean_speed_mps: Annotated[float, Field(ge=0.0)] | None
    maximum_speed_mps: Annotated[float, Field(ge=0.0)] | None
    yaw_excitation_rad: Annotated[float, Field(ge=0.0)]
    traversals: tuple[RoadBinTraversal, ...]
    modalities: tuple[BinModalityEvidence, ...]
    finding_ids: tuple[
        Annotated[str, Field(pattern=r"^finding-sha256-[0-9a-f]{64}$")], ...
    ]
    critical_finding_ids: tuple[
        Annotated[str, Field(pattern=r"^finding-sha256-[0-9a-f]{64}$")], ...
    ]

    @model_validator(mode="after")
    def validate_bin(self) -> Self:
        if self.end_offset_m <= self.start_offset_m or not math.isclose(
            self.true_length_m,
            self.end_offset_m - self.start_offset_m,
            abs_tol=1e-6,
        ):
            raise ValueError("directed road-bin bounds are inconsistent")
        if self.road_bin_id != make_road_bin_id(
            self.road_graph_id,
            self.directed_arc_id,
            self.longitudinal_bin_index,
        ):
            raise ValueError("directed road-bin identity is invalid")
        if self.independent_traversal_count != len(self.traversals):
            raise ValueError("directed road-bin traversal count is inconsistent")
        if any(
            item.road_bin_id != self.road_bin_id
            or item.directed_arc_id != self.directed_arc_id
            or item.arc_direction != self.arc_direction
            or item.longitudinal_bin_index != self.longitudinal_bin_index
            for item in self.traversals
        ):
            raise ValueError("directed road-bin traversal ownership is invalid")
        speed_values = (
            self.minimum_speed_mps,
            self.mean_speed_mps,
            self.maximum_speed_mps,
        )
        if self.speed_sample_count == 0:
            if any(value is not None for value in speed_values):
                raise ValueError("empty road-bin speed distribution is not absent")
        elif any(value is None for value in speed_values) or not (
            cast(float, self.minimum_speed_mps)
            <= cast(float, self.mean_speed_mps)
            <= cast(float, self.maximum_speed_mps)
        ):
            raise ValueError("directed road-bin speed distribution is invalid")
        if not set(self.critical_finding_ids).issubset(self.finding_ids):
            raise ValueError("bin critical findings are not affected findings")
        return self


class FindingRoadBinLocalization(StrictModel):
    finding_id: Annotated[str, Field(pattern=r"^finding-sha256-[0-9a-f]{64}$")]
    sequence_id: Annotated[str, Field(min_length=1)]
    road_bin_ids: tuple[
        Annotated[str, Field(pattern=r"^road-bin-sha256-[0-9a-f]{64}$")], ...
    ]

    @model_validator(mode="after")
    def validate_canonical_bins(self) -> Self:
        if tuple(sorted(set(self.road_bin_ids))) != self.road_bin_ids:
            raise ValueError("finding road-bin localization is not canonical")
        return self


class DirectedRoadCoverageLedger(StrictModel):
    schema_version: Literal["cartosentry.directed-road-coverage.v1"]
    coverage_ledger_id: Annotated[
        str, Field(pattern=r"^road-coverage-sha256-[0-9a-f]{64}$")
    ]
    algorithm_backend: Literal["C++20_NATIVE_BATCH_V1"]
    road_graph_id: Annotated[str, Field(pattern=r"^road-graph-sha256-[0-9a-f]{64}$")]
    road_binning_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    road_binning_profile_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    source_road_match_ids: tuple[
        Annotated[str, Field(pattern=r"^road-match-sha256-[0-9a-f]{64}$")], ...
    ]
    bins: tuple[DirectedRoadBinCoverage, ...]
    finding_localizations: tuple[FindingRoadBinLocalization, ...]
    localized_findings: tuple[Finding, ...]

    def assert_identity(self) -> None:
        payload = self.model_dump(mode="json", exclude={"coverage_ledger_id"})
        if self.coverage_ledger_id != _stable_id("road-coverage", payload):
            raise ValueError("road coverage ledger identity is invalid")

    @model_validator(mode="after")
    def validate_ledger(self) -> Self:
        if (
            self.road_binning_profile_file_sha256 != PROFILE_FILE_SHA256
            or self.road_binning_profile_immutable_sha256 != PROFILE_IMMUTABLE_SHA256
        ):
            raise ValueError("road coverage ledger uses a foreign profile")
        if tuple(sorted(set(self.source_road_match_ids))) != self.source_road_match_ids:
            raise ValueError("road coverage source identities are not canonical")
        bin_ids = tuple(item.road_bin_id for item in self.bins)
        if len(bin_ids) != len(set(bin_ids)):
            raise ValueError("road coverage bins are not unique")
        localizations = {item.finding_id: item for item in self.finding_localizations}
        localized = {item.finding_id: item for item in self.localized_findings}
        if set(localizations) != set(localized) or any(
            localized[identity].road_bin_ids != item.road_bin_ids
            for identity, item in localizations.items()
        ):
            raise ValueError("road coverage finding outputs are inconsistent")
        self.assert_identity()
        return self


def _validate_time_domain(reference: TimePoint, candidate: TimePoint) -> None:
    if reference.epoch != candidate.epoch or reference.clock_id != candidate.clock_id:
        raise ValueError("road-bin temporal join uses incomparable clock domains")


def _native_modality(value: dict[str, Any]) -> BinModalityEvidence:
    return BinModalityEvidence(
        modality=SensorModality(cast(str, value["modality"])),
        valid_duration_ns=cast(int, value["valid_duration_ns"]),
        point_support=cast(float, value["point_support"]),
        mean_overlap_support_m=cast(float | None, value["mean_overlap_support_m"]),
        timestamp_supported_duration_ns=cast(
            int, value["timestamp_supported_duration_ns"]
        ),
        evidence_interval_ids=tuple(cast(list[str], value["evidence_ids"])),
    )


def aggregate_directed_road_bins(
    graph: DirectedRoadGraph,
    paths: tuple[DecodedRoadPath, ...],
    *,
    modality_evidence: tuple[ModalityEvidenceInterval, ...],
    findings: tuple[FindingLocalizationRequest, ...],
    profile: RoadBinningProfile,
    profile_file_sha256: str,
) -> DirectedRoadCoverageLedger:
    """Aggregate complete directed bins and localize evidence in native code."""

    validate_graph_identity(graph)
    profile.assert_identity()
    if profile_file_sha256 != PROFILE_FILE_SHA256:
        raise ValueError("road-binning profile file authority is foreign")
    if (
        graph.profile_immutable_sha256 != GRAPH_PROFILE_IMMUTABLE_SHA256
        or graph.profile_file_sha256
        != profile.authorities.graph_import_profile_file_sha256
    ):
        raise ValueError("road-binning graph authority is foreign")
    parameters = profile.parameter_charter
    if not paths:
        raise ValueError("directed road binning requires at least one decoded path")
    if len(paths) > parameters.maximum_paths:
        raise ValueError("directed road binning exceeds the frozen path budget")
    if len({item.road_match_id for item in paths}) != len(paths):
        raise ValueError("directed road binning paths must have unique identities")
    if sum(len(item.points) for item in paths) > parameters.maximum_total_points:
        raise ValueError("directed road binning exceeds the frozen total-point budget")
    if any(
        item.graph_id != graph.graph_id
        or item.map_decoder_profile_file_sha256
        != profile.authorities.map_decoder_profile_file_sha256
        or item.map_decoder_profile_immutable_sha256
        != profile.authorities.map_decoder_profile_immutable_sha256
        or len(item.points) > parameters.maximum_points_per_path
        for item in paths
    ):
        raise ValueError("decoded path uses a foreign authority or exceeds its budget")
    sequence_references: dict[str, TimePoint] = {}
    sequence_groups: dict[str, str] = {}
    for path in paths:
        reference = path.points[0].observation.time
        previous = sequence_references.setdefault(path.sequence_id, reference)
        _validate_time_domain(previous, reference)
        group = sequence_groups.setdefault(path.sequence_id, path.source_group_id)
        if group != path.source_group_id:
            raise ValueError("sequence source-group identity is inconsistent")
    if len(modality_evidence) > parameters.maximum_modality_evidence_intervals:
        raise ValueError("modality evidence exceeds the frozen aggregation budget")
    if len(findings) > parameters.maximum_findings:
        raise ValueError("findings exceed the frozen localization budget")
    if len({item.evidence_interval_id for item in modality_evidence}) != len(
        modality_evidence
    ):
        raise ValueError("modality evidence identities must be unique")
    if len({item.finding.finding_id for item in findings}) != len(findings):
        raise ValueError("finding localization identities must be unique")
    for evidence in modality_evidence:
        try:
            reference = sequence_references[evidence.sequence_id]
        except KeyError as error:
            raise ValueError(
                "modality evidence references an unknown sequence"
            ) from error
        _validate_time_domain(reference, evidence.interval.start)
        _validate_time_domain(reference, evidence.interval.end)
    for request in findings:
        try:
            reference = sequence_references[request.sequence_id]
        except KeyError as error:
            raise ValueError("finding references an unknown sequence") from error
        _validate_time_domain(reference, request.finding.interval.start)
        _validate_time_domain(reference, request.finding.interval.end)

    expected_bin_count = sum(
        math.ceil(item.length_m / parameters.bin_length_m) for item in graph.arcs
    )
    if expected_bin_count > parameters.maximum_generated_bins:
        raise ValueError(
            "directed road binning exceeds the frozen generated-bin budget"
        )
    arc_index = {item.arc_id: index for index, item in enumerate(graph.arcs)}
    raw = _core.aggregate_directed_road_bins(
        [
            {
                "arc_id": item.arc_id,
                "direction": item.direction.value,
                "length_m": item.length_m,
            }
            for item in graph.arcs
        ],
        [
            {
                "road_match_id": path.road_match_id,
                "sequence_id": path.sequence_id,
                "source_group_id": path.source_group_id,
                "points": [
                    {
                        "time_ns": point.observation.time.value_ns,
                        "arc_index": (
                            None
                            if point.candidate.directed_arc_id is None
                            else arc_index[point.candidate.directed_arc_id]
                        ),
                        "along_arc_offset_m": point.candidate.along_arc_offset_m,
                        "confident": (
                            point.confidence == MatchConfidence.CONFIDENT
                            and point.candidate.state == CandidateState.ON_ROAD
                        ),
                        "stationary": point.stationary,
                        "speed_mps": point.observation.speed_mps,
                        "heading_rad": point.observation.heading_rad,
                    }
                    for point in path.points
                ],
            }
            for path in paths
        ],
        [
            {
                "evidence_id": item.evidence_interval_id,
                "sequence_id": item.sequence_id,
                "modality": item.modality.value,
                "start_time_ns": item.interval.start.value_ns,
                "end_time_ns": item.interval.end.value_ns,
                "usable": item.usable,
                "point_count": item.point_count,
                "overlap_support_m": item.lidar_overlap_support_m,
                "timestamp_supported": item.timestamp_supported,
            }
            for item in modality_evidence
        ],
        [
            {
                "finding_id": item.finding.finding_id,
                "sequence_id": item.sequence_id,
                "start_time_ns": item.finding.interval.start.value_ns,
                "end_time_ns": item.finding.interval.end.value_ns,
                "critical": item.finding.severity
                in {Severity.CRITICAL, Severity.BLOCKING_ANALYSIS},
            }
            for item in findings
        ],
        parameters.model_dump(mode="python"),
    )
    raw_bins = cast(list[dict[str, Any]], raw["bins"])
    if len(raw_bins) != expected_bin_count:
        raise ValueError("native road-bin output omitted graph bins")

    bins: list[DirectedRoadBinCoverage] = []
    for raw_bin in raw_bins:
        selected_arc_index = cast(int, raw_bin["arc_index"])
        bin_index = cast(int, raw_bin["longitudinal_bin_index"])
        if not 0 <= selected_arc_index < len(graph.arcs):
            raise ValueError("native road-bin output references an invalid arc")
        arc = graph.arcs[selected_arc_index]
        bin_count = math.ceil(arc.length_m / parameters.bin_length_m)
        if not 0 <= bin_index < bin_count:
            raise ValueError("native road-bin output has an invalid bin index")
        road_bin_id = make_road_bin_id(graph.graph_id, arc.arc_id, bin_index)
        traversal_values: list[RoadBinTraversal] = []
        for raw_traversal in cast(list[dict[str, Any]], raw_bin["traversals"]):
            if (
                cast(int, raw_traversal["arc_index"]) != selected_arc_index
                or cast(int, raw_traversal["longitudinal_bin_index"]) != bin_index
            ):
                raise ValueError("native road-bin traversal ownership is invalid")
            identity = {
                "arc_direction": arc.direction,
                "directed_arc_id": arc.arc_id,
                "sequence_id": raw_traversal["sequence_id"],
                "source_group_id": raw_traversal["source_group_id"],
                "traversal_ordinal": raw_traversal["traversal_ordinal"],
            }
            traversal_values.append(
                RoadBinTraversal(
                    traversal_id=_stable_id("road-traversal", identity),
                    road_bin_id=road_bin_id,
                    directed_arc_id=arc.arc_id,
                    arc_direction=arc.direction,
                    longitudinal_bin_index=bin_index,
                    sequence_id=cast(str, raw_traversal["sequence_id"]),
                    source_group_id=cast(str, raw_traversal["source_group_id"]),
                    traversal_ordinal=cast(int, raw_traversal["traversal_ordinal"]),
                    first_time_ns=cast(int, raw_traversal["first_time_ns"]),
                    last_time_ns=cast(int, raw_traversal["last_time_ns"]),
                    entry_offset_m=cast(float, raw_traversal["entry_offset_m"]),
                    exit_offset_m=cast(float, raw_traversal["exit_offset_m"]),
                    usable_duration_ns=cast(int, raw_traversal["usable_duration_ns"]),
                    usable_distance_m=cast(float, raw_traversal["usable_distance_m"]),
                    speed_sample_count=cast(int, raw_traversal["speed_sample_count"]),
                    minimum_speed_mps=cast(float, raw_traversal["minimum_speed_mps"]),
                    mean_speed_mps=cast(float, raw_traversal["mean_speed_mps"]),
                    maximum_speed_mps=cast(float, raw_traversal["maximum_speed_mps"]),
                    yaw_excitation_rad=cast(float, raw_traversal["yaw_excitation_rad"]),
                    source_road_match_ids=tuple(
                        cast(list[str], raw_traversal["road_match_ids"])
                    ),
                    modalities=tuple(
                        _native_modality(item)
                        for item in cast(
                            list[dict[str, Any]], raw_traversal["modalities"]
                        )
                    ),
                    finding_ids=tuple(cast(list[str], raw_traversal["finding_ids"])),
                    critical_finding_ids=tuple(
                        cast(list[str], raw_traversal["critical_finding_ids"])
                    ),
                )
            )
        start_offset = cast(float, raw_bin["start_offset_m"])
        end_offset = cast(float, raw_bin["end_offset_m"])
        bins.append(
            DirectedRoadBinCoverage(
                road_bin_id=road_bin_id,
                road_graph_id=graph.graph_id,
                directed_arc_id=arc.arc_id,
                arc_direction=arc.direction,
                longitudinal_bin_index=bin_index,
                start_offset_m=start_offset,
                end_offset_m=end_offset,
                true_length_m=round(
                    end_offset - start_offset,
                    parameters.distance_rounding_decimal_places,
                ),
                final_partial_bin=(
                    bin_index == bin_count - 1
                    and not math.isclose(
                        end_offset - start_offset,
                        parameters.bin_length_m,
                        abs_tol=10 ** (-parameters.distance_rounding_decimal_places),
                    )
                ),
                usable_duration_ns=cast(int, raw_bin["usable_duration_ns"]),
                usable_trajectory_distance_m=cast(float, raw_bin["usable_distance_m"]),
                independent_traversal_count=cast(
                    int, raw_bin["independent_traversal_count"]
                ),
                speed_sample_count=cast(int, raw_bin["speed_sample_count"]),
                minimum_speed_mps=cast(float | None, raw_bin["minimum_speed_mps"]),
                mean_speed_mps=cast(float | None, raw_bin["mean_speed_mps"]),
                maximum_speed_mps=cast(float | None, raw_bin["maximum_speed_mps"]),
                yaw_excitation_rad=cast(float, raw_bin["yaw_excitation_rad"]),
                traversals=tuple(traversal_values),
                modalities=tuple(
                    _native_modality(item)
                    for item in cast(list[dict[str, Any]], raw_bin["modalities"])
                ),
                finding_ids=tuple(cast(list[str], raw_bin["finding_ids"])),
                critical_finding_ids=tuple(
                    cast(list[str], raw_bin["critical_finding_ids"])
                ),
            )
        )
    bin_ids = tuple(item.road_bin_id for item in bins)
    request_by_id = {item.finding.finding_id: item for item in findings}
    localization_values: list[FindingRoadBinLocalization] = []
    localized_findings: list[Finding] = []
    for raw_localization in cast(list[dict[str, Any]], raw["finding_localizations"]):
        finding_id = cast(str, raw_localization["finding_id"])
        try:
            request = request_by_id[finding_id]
        except KeyError as error:
            raise ValueError(
                "native road-bin output returned an unknown finding"
            ) from error
        selected_indices = tuple(
            cast(list[int], raw_localization["bin_result_indices"])
        )
        if any(not 0 <= index < len(bin_ids) for index in selected_indices):
            raise ValueError("native finding localization references an invalid bin")
        selected_ids = tuple(sorted({bin_ids[index] for index in selected_indices}))
        localization_values.append(
            FindingRoadBinLocalization(
                finding_id=finding_id,
                sequence_id=request.sequence_id,
                road_bin_ids=selected_ids,
            )
        )
        localized_findings.append(
            Finding.model_validate_json(
                json.dumps(
                    request.finding.model_dump(mode="json")
                    | {"road_bin_ids": selected_ids},
                    allow_nan=False,
                )
            )
        )
    payload: dict[str, object] = {
        "schema_version": "cartosentry.directed-road-coverage.v1",
        "algorithm_backend": ALGORITHM_BACKEND,
        "road_graph_id": graph.graph_id,
        "road_binning_profile_file_sha256": profile_file_sha256,
        "road_binning_profile_immutable_sha256": profile.immutable_sha256,
        "source_road_match_ids": tuple(sorted(item.road_match_id for item in paths)),
        "bins": tuple(item.model_dump(mode="json") for item in bins),
        "finding_localizations": tuple(
            item.model_dump(mode="json") for item in localization_values
        ),
        "localized_findings": tuple(
            item.model_dump(mode="json") for item in localized_findings
        ),
    }
    return DirectedRoadCoverageLedger(
        schema_version="cartosentry.directed-road-coverage.v1",
        coverage_ledger_id=_stable_id("road-coverage", payload),
        algorithm_backend=ALGORITHM_BACKEND,
        road_graph_id=graph.graph_id,
        road_binning_profile_file_sha256=profile_file_sha256,
        road_binning_profile_immutable_sha256=profile.immutable_sha256,
        source_road_match_ids=tuple(sorted(item.road_match_id for item in paths)),
        bins=tuple(bins),
        finding_localizations=tuple(localization_values),
        localized_findings=tuple(localized_findings),
    )


__all__ = [
    "PROFILE_FILE_SHA256",
    "PROFILE_IMMUTABLE_SHA256",
    "BinModalityEvidence",
    "DirectedRoadBinCoverage",
    "DirectedRoadCoverageLedger",
    "FindingLocalizationRequest",
    "FindingRoadBinLocalization",
    "ModalityEvidenceInterval",
    "RoadBinTraversal",
    "RoadBinningProfile",
    "aggregate_directed_road_bins",
    "load_road_binning_profile",
    "make_modality_evidence_interval",
]
