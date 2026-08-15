"""Frozen blind review and public-route qualification for Milestone 5.6."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry.adapters.boreas_v1 import BoreasAdapter
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
from cartosentry.road_decoder import (
    MatchConfidence,
    decode_road_path,
    load_map_decoder_profile,
)
from cartosentry.road_graph import (
    PROFILE_IMMUTABLE_SHA256 as GRAPH_PROFILE_IMMUTABLE_SHA256,
)
from cartosentry.road_graph import (
    DirectedRoadArc,
    DirectedRoadGraph,
    GraphSourceKind,
    import_osm_road_graph,
    load_graph_import_profile,
    normalize_matching_observation,
)
from cartosentry.road_matching import (
    ALGORITHM_BACKEND,
    CandidateState,
    MapMatchingProfile,
    RoadMatchObservation,
    generate_road_candidate_batches,
    load_map_matching_profile,
    make_road_match_observation,
)
from cartosentry.road_matching import (
    PROFILE_IMMUTABLE_SHA256 as MATCHING_PROFILE_IMMUTABLE_SHA256,
)

GATE_IMMUTABLE_SHA256 = (
    "eeea210b92986b55d3a5380d18a36b92df300b5d7cfc9687943bb3f6ac40f598"
)
ADJUDICATION_IMMUTABLE_SHA256 = (
    "47cc45d667e84e33cdb91bea264301e6c42a56687b471c04f810deac8e77d773"
)
MAXIMUM_GATE_BYTES = 256 * 1024
MAXIMUM_ADJUDICATION_BYTES = 4 * 1024 * 1024
MAXIMUM_AUTHORITY_BYTES = 2 * 1024 * 1024
UTC_TIMESTAMP_PATTERN = r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


class GateAuthorities(StrictModel):
    protocol_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    data_manifest_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_groups_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    split_manifest_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    numerical_charter_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    graph_import_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    graph_import_profile_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    map_matching_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    map_matching_profile_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    map_decoder_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    map_decoder_profile_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]


class PublicSourceContract(StrictModel):
    sequence_id: Literal["boreas-2021-09-02-11-42"]
    source_group_id: Literal["boreas-glen-shields-family-v1"]
    partition: Literal["development"]
    trajectory_object_key: Literal[
        "boreas-2021-09-02-11-42/applanix/gps_post_process.csv"
    ]
    trajectory_source_key: Literal["applanix/gps_post_process.csv"]
    trajectory_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    trajectory_bytes: Annotated[int, Field(gt=0)]
    road_graph_object_key: Literal["road_graphs/toronto-glen-shields-v1.osm"]
    road_graph_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    road_graph_bytes: Annotated[int, Field(gt=0)]


class ReviewSampleContract(StrictModel):
    selection: Literal["EVERY_NTH_SOURCE_RECORD_PLUS_FINAL"]
    source_record_stride: Literal[200]
    include_final_source_record: Literal[True]
    minimum_moving_speed_mps: Annotated[float, Field(ge=1.0, le=1.0)]
    distance_coverage_method: Literal["endpoint-half-distance-v1"]
    distance_coordinate_basis: Literal["SOURCE_APPLANIX_EASTING_NORTHING"]
    travel_heading_basis: Literal["ATAN2_VELOCITY_NORTH_VELOCITY_EAST"]
    review_candidate_source: Literal[
        "PRODUCTION_CANDIDATE_GENERATION_WITH_SCORES_REDACTED"
    ]
    review_candidate_order: Literal["DIRECTED_ARC_ID_ASCENDING"]
    forbidden_packet_fields: tuple[str, ...]


ReviewLabel = Literal[
    "DIRECTED_ARC",
    "AMBIGUOUS",
    "OFF_MAP",
    "GRAPH_DATA_LIMITATION",
    "UNRESOLVED",
]


class DecisionContract(StrictModel):
    allowed_labels: tuple[ReviewLabel, ...]
    only_directed_arc_label_carries_expected_arc: Literal[True]
    all_moving_observations_require_one_decision: Literal[True]
    unresolved_decisions_must_remain_without_expected_arc: Literal[True]


class PublicGateThresholds(StrictModel):
    confident_moving_distance_fraction_minimum: Annotated[
        float, Field(ge=0.85, le=0.85)
    ]


class PublicRoadMatchingGate(StrictModel):
    schema_version: Literal[1]
    gate_id: Literal["m5.6-public-road-matching-v1"]
    gate_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_PUBLIC_ROUTE_REVIEW"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: GateAuthorities
    public_source: PublicSourceContract
    review_sample: ReviewSampleContract
    decision_contract: DecisionContract
    thresholds: PublicGateThresholds
    result_scope: Literal["SINGLE_PINNED_DEVELOPMENT_ROUTE_CASE_STUDY_NOT_CONFIRMATORY"]

    @model_validator(mode="after")
    def validate_exact_contract(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"immutable_sha256"})
        expected_labels = (
            "DIRECTED_ARC",
            "AMBIGUOUS",
            "OFF_MAP",
            "GRAPH_DATA_LIMITATION",
            "UNRESOLVED",
        )
        required_forbidden = {
            "emission_log_likelihood",
            "transition_log_likelihood",
            "decoder_candidate_id",
            "decoder_directed_arc_id",
            "runner_up_candidate_id",
            "path_separation_log_likelihood",
            "decoder_confidence",
            "aggregate_outcome",
            "final_test_identity",
        }
        authorities = self.authorities
        if (
            self.immutable_sha256 != GATE_IMMUTABLE_SHA256
            or _canonical_hash(payload) != self.immutable_sha256
            or self.decision_contract.allowed_labels != expected_labels
            or set(self.review_sample.forbidden_packet_fields) != required_forbidden
            or len(self.review_sample.forbidden_packet_fields)
            != len(required_forbidden)
            or authorities.graph_import_profile_immutable_sha256
            != GRAPH_PROFILE_IMMUTABLE_SHA256
            or authorities.map_matching_profile_immutable_sha256
            != MATCHING_PROFILE_IMMUTABLE_SHA256
            or authorities.map_decoder_profile_immutable_sha256
            != DECODER_PROFILE_IMMUTABLE_SHA256
            or authorities.map_decoder_profile_file_sha256
            != DECODER_PROFILE_FILE_SHA256
        ):
            raise ValueError("M5.6 public road-matching gate identity is not exact")
        return self


def load_public_road_matching_gate(
    path: Path,
) -> tuple[PublicRoadMatchingGate, str]:
    """Load and self-authenticate the frozen pre-review M5.6 gate."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="M5.6 public road-matching gate",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="M5.6 public road-matching gate",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "M5.6 public road-matching gate is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("M5.6 public road-matching gate must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("M5.6 public road-matching gate immutable hash is invalid")
    return (
        PublicRoadMatchingGate.model_validate_json(content),
        hashlib.sha256(content).hexdigest(),
    )


EvidenceCode = Literal[
    "LATERAL_GEOMETRY",
    "TRAVEL_DIRECTION",
    "TOPOLOGY_CONTINUITY",
    "ONE_WAY_SEMANTICS",
    "DIVIDED_CARRIAGEWAY",
    "INTERSECTION_AMBIGUITY",
    "PARALLEL_ROAD_AMBIGUITY",
    "NO_GRAPH_CANDIDATE",
    "GRAPH_MISSING_OR_EXCLUDED",
    "INSUFFICIENT_EVIDENCE",
    "OFF_GRAPH_TRAVEL",
]


class ReviewDecision(StrictModel):
    source_record_index: Annotated[int, Field(ge=0)]
    observation_id: Annotated[str, Field(pattern=r"^observation-sha256-[0-9a-f]{64}$")]
    label: ReviewLabel
    expected_directed_arc_id: Annotated[
        str | None, Field(pattern=r"^osm-arc-sha256-[0-9a-f]{64}$")
    ]
    evidence_codes: tuple[EvidenceCode, ...]

    @model_validator(mode="after")
    def validate_label_payload(self) -> Self:
        carries_arc = self.expected_directed_arc_id is not None
        if carries_arc != (self.label == "DIRECTED_ARC"):
            raise ValueError(
                "only a directed-arc review decision may carry an expected arc"
            )
        if not self.evidence_codes or len(self.evidence_codes) != len(
            set(self.evidence_codes)
        ):
            raise ValueError("review evidence codes must be nonempty and unique")
        return self


class AdjudicationAuthorities(StrictModel):
    gate_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    gate_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    protocol_freeze_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    review_packet_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    graph_id: Annotated[str, Field(pattern=r"^road-graph-sha256-[0-9a-f]{64}$")]


class ReviewRecord(StrictModel):
    reviewer_role: Literal["IMPLEMENTATION_OWNER_MANUAL_ROUTE_REVIEW"]
    review_started_at_utc: Annotated[str, Field(pattern=UTC_TIMESTAMP_PATTERN)]
    review_completed_at_utc: Annotated[str, Field(pattern=UTC_TIMESTAMP_PATTERN)]
    allowed_evidence: tuple[
        Literal[
            "BLIND_REVIEW_PACKET",
            "PINNED_OSM_EXTRACT",
            "PINNED_BOREAS_ROUTE_VISUALIZATION",
            "PINNED_BOREAS_TRAJECTORY_FIELDS",
        ],
        ...,
    ]
    production_decoder_output_viewed_before_completion: Literal[False]
    final_test_material_accessed: Literal[False]
    all_moving_observations_reviewed: Literal[True]
    unresolved_decisions_preserved_without_expected_arc: Literal[True]


class LicenseRecord(StrictModel):
    database_classification: Literal["ODBL_DERIVATIVE_DATABASE"]
    osm_attribution: Literal[
        "Contains information from OpenStreetMap, which is made available under "
        "the Open Database License."
    ]
    osm_license_url: Literal["https://opendatacommons.org/licenses/odbl/1-0/"]
    boreas_attribution: Literal[
        "Burnett et al., Boreas: A Multi-Season Autonomous Driving Dataset, IJRR "
        "2023; data provided by the University of Toronto Institute for Aerospace "
        "Studies under CC BY 4.0."
    ]
    boreas_license_url: Literal["https://creativecommons.org/licenses/by/4.0/"]


class PublicRouteAdjudication(StrictModel):
    schema_version: Literal[1]
    adjudication_id: Literal["m5.6-public-route-adjudication-v1"]
    adjudication_version: Literal["1.0.0"]
    review_state: Literal["COMPLETED_BLIND_REVIEW"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: AdjudicationAuthorities
    review: ReviewRecord
    license: LicenseRecord
    decisions: tuple[ReviewDecision, ...]

    @model_validator(mode="after")
    def validate_adjudication(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"immutable_sha256"})
        record_indices = tuple(item.source_record_index for item in self.decisions)
        observation_ids = tuple(item.observation_id for item in self.decisions)
        if (
            _canonical_hash(payload) != self.immutable_sha256
            or not self.decisions
            or record_indices != tuple(sorted(record_indices))
            or len(record_indices) != len(set(record_indices))
            or len(observation_ids) != len(set(observation_ids))
            or set(self.review.allowed_evidence)
            != {
                "BLIND_REVIEW_PACKET",
                "PINNED_OSM_EXTRACT",
                "PINNED_BOREAS_ROUTE_VISUALIZATION",
                "PINNED_BOREAS_TRAJECTORY_FIELDS",
            }
        ):
            raise ValueError("M5.6 public-route adjudication is inconsistent")
        return self


def load_public_route_adjudication(
    path: Path,
) -> tuple[PublicRouteAdjudication, str]:
    """Load the completed review only after its identity is frozen in code."""

    if ADJUDICATION_IMMUTABLE_SHA256 is None:
        raise ValueError("M5.6 public-route adjudication has not been frozen")
    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_ADJUDICATION_BYTES,
            context="M5.6 public-route adjudication",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_ADJUDICATION_BYTES,
            context="M5.6 public-route adjudication",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "M5.6 public-route adjudication is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("M5.6 public-route adjudication must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if (
        raw.get("immutable_sha256") != _canonical_hash(canonical)
        or raw.get("immutable_sha256") != ADJUDICATION_IMMUTABLE_SHA256
    ):
        raise ValueError("M5.6 public-route adjudication identity is invalid")
    return (
        PublicRouteAdjudication.model_validate_json(content),
        hashlib.sha256(content).hexdigest(),
    )


@dataclass(frozen=True)
class _SampledObservation:
    source_record_index: int
    source_easting_m: float
    source_northing_m: float
    speed_mps: float
    travel_heading_rad: float
    latitude_deg: float
    longitude_deg: float
    observation: RoadMatchObservation


@dataclass(frozen=True)
class _PreparedReview:
    gate: PublicRoadMatchingGate
    gate_file_sha256: str
    graph: DirectedRoadGraph
    matching_profile: MapMatchingProfile
    matching_profile_file_sha256: str
    decoder_profile_file_sha256: str
    sampled: tuple[_SampledObservation, ...]
    endpoint_weights_m: tuple[float, ...]
    packet: dict[str, object]


def _read_authority_object(path: Path, *, context: str) -> dict[str, object]:
    try:
        content = read_bounded_regular_bytes(
            path, maximum_bytes=MAXIMUM_AUTHORITY_BYTES, context=context
        )
        decoded = decode_bounded_json(
            content, maximum_bytes=MAXIMUM_AUTHORITY_BYTES, context=context
        )
    except ManifestBoundaryError as error:
        raise ValueError(f"{context} is unavailable or malformed") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"{context} must be an object")
    return cast(dict[str, object], decoded)


def _verify_file(path: Path, expected_sha256: str, *, context: str) -> None:
    if _file_sha256(path) != expected_sha256:
        raise ValueError(f"{context} does not match the frozen M5.6 authority")


def _verify_partition_authorities(
    gate: PublicRoadMatchingGate,
    *,
    source_groups_path: Path,
    split_manifest_path: Path,
) -> None:
    source = gate.public_source
    groups = _read_authority_object(source_groups_path, context="source-group manifest")
    raw_groups = groups.get("source_groups")
    if not isinstance(raw_groups, list):
        raise ValueError("source-group manifest has no source groups")
    matching_groups = [
        item
        for item in raw_groups
        if isinstance(item, dict)
        and item.get("source_group_id") == source.source_group_id
    ]
    if len(matching_groups) != 1:
        raise ValueError("public source group is not uniquely assigned")
    group = matching_groups[0]
    sequences = group.get("sequences")
    if (
        group.get("partition") != source.partition
        or not isinstance(sequences, list)
        or sum(
            isinstance(item, dict) and item.get("sequence_id") == source.sequence_id
            for item in sequences
        )
        != 1
    ):
        raise ValueError("public sequence partition assignment is inconsistent")

    split = _read_authority_object(split_manifest_path, context="split manifest")
    real_groups = split.get("real_source_groups")
    if not isinstance(real_groups, list):
        raise ValueError("split manifest has no real source groups")
    matches = [
        item
        for item in real_groups
        if isinstance(item, dict)
        and item.get("source_group_id") == source.source_group_id
    ]
    if len(matches) != 1:
        raise ValueError("public sequence is not uniquely assigned in the split")
    split_group = matches[0]
    sequence_ids = split_group.get("sequence_ids")
    if (
        split_group.get("partition") != source.partition
        or not isinstance(sequence_ids, list)
        or source.sequence_id not in sequence_ids
    ):
        raise ValueError("public sequence is not an ordinary development source")


def _verify_numerical_gate(gate: PublicRoadMatchingGate, charter_path: Path) -> None:
    charter = _read_authority_object(charter_path, context="numerical charter")
    gates = charter.get("gates")
    if not isinstance(gates, dict):
        raise ValueError("numerical charter has no gate collection")
    public_gate = gates.get("map.public_confident_moving_distance_fraction")
    threshold = gate.thresholds.confident_moving_distance_fraction_minimum
    if not isinstance(public_gate, dict) or public_gate != {
        "operator": "fraction_ge",
        "value": threshold,
        "unit": "fraction",
        "decision_bound": "deterministic_selected_route",
        "responsible_metric": (
            "endpoint-half-distance-v1 confident moving-distance coverage"
        ),
        "rationale": (
            "The public demonstration must have enough adjudicable directed-road "
            "support."
        ),
    }:
        raise ValueError("M5.6 public coverage gate does not match the charter")


def _verify_manifest_source(
    gate: PublicRoadMatchingGate, data_manifest_path: Path
) -> None:
    manifest = _read_authority_object(data_manifest_path, context="data manifest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("data manifest has no artifact collection")

    def object_for(artifact_id: str, object_key: str) -> dict[str, object]:
        artifact = next(
            (
                item
                for item in artifacts
                if isinstance(item, dict) and item.get("id") == artifact_id
            ),
            None,
        )
        if not isinstance(artifact, dict) or not isinstance(
            artifact.get("objects"), list
        ):
            raise ValueError(f"data manifest is missing {artifact_id}")
        found = next(
            (
                item
                for item in cast(list[object], artifact["objects"])
                if isinstance(item, dict) and item.get("key") == object_key
            ),
            None,
        )
        if not isinstance(found, dict):
            raise ValueError(f"data manifest is missing {object_key}")
        return cast(dict[str, object], found)

    source = gate.public_source
    trajectory = object_for(
        "boreas-public-smoke-clear-v1", source.trajectory_object_key
    )
    graph = object_for("osm-toronto-glen-shields-v1", source.road_graph_object_key)
    if (
        trajectory.get("sha256") != source.trajectory_sha256
        or trajectory.get("bytes") != source.trajectory_bytes
        or graph.get("sha256") != source.road_graph_sha256
        or graph.get("bytes") != source.road_graph_bytes
    ):
        raise ValueError("M5.6 public objects do not match the data manifest")


def _selected_observations(
    sequence_root: Path,
    graph: DirectedRoadGraph,
    gate: PublicRoadMatchingGate,
) -> tuple[_SampledObservation, ...]:
    source = gate.public_source
    contract = gate.review_sample
    adapter = BoreasAdapter(sequence_root, source_group_id=source.source_group_id)
    selected: list[Any] = []
    last_sample: Any | None = None
    for sample in adapter.pose_samples():
        record_index = sample.provenance.record_index
        last_sample = sample
        if record_index % contract.source_record_stride == 0:
            selected.append(sample)
    if last_sample is None:
        raise ValueError("public route contains no trajectory samples")
    if (
        contract.include_final_source_record
        and last_sample.provenance.record_index % contract.source_record_stride != 0
    ):
        selected.append(last_sample)

    result: list[_SampledObservation] = []
    for sample in selected:
        normalized = normalize_matching_observation(sample, graph)
        velocity_east, velocity_north, _ = sample.velocity_enu_mps
        speed = math.hypot(velocity_east, velocity_north)
        heading = math.atan2(velocity_north, velocity_east)
        observation = make_road_match_observation(
            time=sample.time,
            local_frame_id=normalized.local_frame_id,
            position_local_m=(
                normalized.position_local_m[0],
                normalized.position_local_m[1],
            ),
            heading_rad=heading,
            speed_mps=speed,
            horizontal_uncertainty_m=None,
            source_observation_id=normalized.observation_id,
        )
        result.append(
            _SampledObservation(
                source_record_index=sample.provenance.record_index,
                source_easting_m=sample.world_from_rig.translation_m[0],
                source_northing_m=sample.world_from_rig.translation_m[1],
                speed_mps=speed,
                travel_heading_rad=heading,
                latitude_deg=sample.geographic.coordinate.latitude_deg,
                longitude_deg=sample.geographic.coordinate.longitude_deg,
                observation=observation,
            )
        )
    if len({item.observation.observation_id for item in result}) != len(result):
        raise ValueError("public review sample has duplicate observation identities")
    return tuple(result)


def _endpoint_weights(
    sampled: tuple[_SampledObservation, ...], minimum_speed_mps: float
) -> tuple[float, ...]:
    weights = [0.0] * len(sampled)
    for index in range(1, len(sampled)):
        previous = sampled[index - 1]
        current = sampled[index]
        if (
            previous.speed_mps < minimum_speed_mps
            or current.speed_mps < minimum_speed_mps
        ):
            continue
        distance = math.hypot(
            current.source_easting_m - previous.source_easting_m,
            current.source_northing_m - previous.source_northing_m,
        )
        weights[index - 1] += distance / 2.0
        weights[index] += distance / 2.0
    if sum(weights) <= 0.0:
        raise ValueError("public review sample has no moving distance")
    return tuple(weights)


def _candidate_option(candidate: Any, arc: DirectedRoadArc) -> dict[str, object]:
    tags = {item.key: item.value for item in arc.source_tags}
    return {
        "directed_arc_id": candidate.directed_arc_id,
        "source_way_id": candidate.source_way_id,
        "source_chain_index": arc.source_chain_index,
        "direction": arc.direction,
        "highway_class": arc.highway_class,
        "road_name": tags.get("name"),
        "from_node_id": arc.from_node_id,
        "to_node_id": arc.to_node_id,
        "lateral_distance_m": round(cast(float, candidate.lateral_distance_m), 6),
        "tangent_heading_rad": round(cast(float, candidate.tangent_heading_rad), 12),
        "heading_difference_rad": (
            round(cast(float, candidate.emission.heading_difference_rad), 12)
            if candidate.emission.heading_difference_rad is not None
            else None
        ),
        "along_arc_offset_m": round(cast(float, candidate.along_arc_offset_m), 6),
        "arc_length_m": arc.length_m,
        "layer": arc.layer,
        "bridge": arc.bridge,
        "tunnel": arc.tunnel,
    }


def _contains_forbidden_field(value: object, forbidden: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            key in forbidden or _contains_forbidden_field(item, forbidden)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_forbidden_field(item, forbidden) for item in value)
    return False


def _prepare_review(
    *,
    public_data_root: Path,
    gate_path: Path,
    protocol_path: Path,
    data_manifest_path: Path,
    source_groups_path: Path,
    split_manifest_path: Path,
    numerical_charter_path: Path,
    graph_profile_path: Path,
    matching_profile_path: Path,
    decoder_profile_path: Path,
) -> _PreparedReview:
    gate, gate_file_sha256 = load_public_road_matching_gate(gate_path)
    authorities = gate.authorities
    authority_paths = (
        (protocol_path, authorities.protocol_file_sha256, "review protocol"),
        (data_manifest_path, authorities.data_manifest_file_sha256, "data manifest"),
        (source_groups_path, authorities.source_groups_file_sha256, "source groups"),
        (split_manifest_path, authorities.split_manifest_file_sha256, "split manifest"),
        (
            numerical_charter_path,
            authorities.numerical_charter_file_sha256,
            "numerical charter",
        ),
        (
            graph_profile_path,
            authorities.graph_import_profile_file_sha256,
            "graph import profile",
        ),
        (
            matching_profile_path,
            authorities.map_matching_profile_file_sha256,
            "map matching profile",
        ),
        (
            decoder_profile_path,
            authorities.map_decoder_profile_file_sha256,
            "map decoder profile",
        ),
    )
    for path, expected, context in authority_paths:
        _verify_file(path, expected, context=context)
    _verify_partition_authorities(
        gate,
        source_groups_path=source_groups_path,
        split_manifest_path=split_manifest_path,
    )
    _verify_numerical_gate(gate, numerical_charter_path)
    _verify_manifest_source(gate, data_manifest_path)

    graph_profile, graph_profile_sha256 = load_graph_import_profile(graph_profile_path)
    matching_profile, matching_profile_sha256 = load_map_matching_profile(
        matching_profile_path
    )
    decoder_profile, decoder_profile_sha256 = load_map_decoder_profile(
        decoder_profile_path
    )
    if (
        graph_profile_sha256 != authorities.graph_import_profile_file_sha256
        or graph_profile.immutable_sha256
        != authorities.graph_import_profile_immutable_sha256
        or matching_profile_sha256 != authorities.map_matching_profile_file_sha256
        or matching_profile.immutable_sha256
        != authorities.map_matching_profile_immutable_sha256
        or decoder_profile_sha256 != authorities.map_decoder_profile_file_sha256
        or decoder_profile.immutable_sha256
        != authorities.map_decoder_profile_immutable_sha256
    ):
        raise ValueError("M5.6 algorithm profiles are not the frozen authorities")

    source = gate.public_source
    graph_path = public_data_root / source.road_graph_object_key
    sequence_root = public_data_root / source.sequence_id
    trajectory_path = sequence_root / source.trajectory_source_key
    for path, expected_bytes, expected_sha256, context in (
        (
            graph_path,
            source.road_graph_bytes,
            source.road_graph_sha256,
            "public road graph",
        ),
        (
            trajectory_path,
            source.trajectory_bytes,
            source.trajectory_sha256,
            "public trajectory",
        ),
    ):
        if (
            path.stat().st_size != expected_bytes
            or _file_sha256(path) != expected_sha256
        ):
            raise ValueError(f"{context} does not match its frozen object identity")

    graph = import_osm_road_graph(
        graph_path,
        profile=graph_profile,
        profile_file_sha256=graph_profile_sha256,
        source_object_key=source.road_graph_object_key,
        expected_source_sha256=source.road_graph_sha256,
        source_kind=GraphSourceKind.OPENSTREETMAP_EXTRACT,
    )
    sampled = _selected_observations(sequence_root, graph, gate)
    weights = _endpoint_weights(sampled, gate.review_sample.minimum_moving_speed_mps)
    moving = tuple(
        (index, item)
        for index, item in enumerate(sampled)
        if item.speed_mps >= gate.review_sample.minimum_moving_speed_mps
    )
    candidate_batches = generate_road_candidate_batches(
        graph,
        tuple(item.observation for _, item in moving),
        profile=matching_profile,
    )
    arcs = {item.arc_id: item for item in graph.arcs}
    review_observations: list[dict[str, object]] = []
    for (sample_index, item), candidates in zip(moving, candidate_batches, strict=True):
        on_road = tuple(
            candidate
            for candidate in candidates
            if candidate.state == CandidateState.ON_ROAD
            and candidate.directed_arc_id is not None
        )
        options = [
            _candidate_option(candidate, arcs[cast(str, candidate.directed_arc_id)])
            for candidate in sorted(
                on_road, key=lambda value: cast(str, value.directed_arc_id)
            )
        ]
        review_observations.append(
            {
                "review_index": len(review_observations),
                "source_record_index": item.source_record_index,
                "observation_id": item.observation.observation_id,
                "source_observation_id": item.observation.source_observation_id,
                "time_ns": item.observation.time.value_ns,
                "latitude_deg": round(item.latitude_deg, 9),
                "longitude_deg": round(item.longitude_deg, 9),
                "source_easting_m": round(item.source_easting_m, 6),
                "source_northing_m": round(item.source_northing_m, 6),
                "speed_mps": round(item.speed_mps, 9),
                "travel_heading_rad": round(item.travel_heading_rad, 12),
                "endpoint_half_distance_weight_m": round(weights[sample_index], 9),
                "candidate_options": options,
            }
        )

    packet_payload: dict[str, object] = {
        "schema_version": "cartosentry.public-route-blind-review-packet.v1",
        "hash_contract": (
            "SHA-256 of canonical UTF-8 JSON with packet_immutable_sha256 omitted"
        ),
        "gate_id": gate.gate_id,
        "gate_file_sha256": gate_file_sha256,
        "gate_immutable_sha256": gate.immutable_sha256,
        "review_state": "BLIND_NO_PRODUCTION_DECODER_OR_FINAL_TEST_OUTPUT",
        "sequence_id": source.sequence_id,
        "source_group_id": source.source_group_id,
        "partition": source.partition,
        "graph_id": graph.graph_id,
        "graph_source_attribution": graph.source.attribution,
        "graph_license_url": graph.source.license_url,
        "trajectory_attribution": (
            "Burnett et al., Boreas: A Multi-Season Autonomous Driving Dataset, "
            "IJRR 2023; University of Toronto Institute for Aerospace Studies."
        ),
        "selection": gate.review_sample.selection,
        "source_record_stride": gate.review_sample.source_record_stride,
        "distance_coverage_method": gate.review_sample.distance_coverage_method,
        "distance_coordinate_basis": gate.review_sample.distance_coordinate_basis,
        "minimum_moving_speed_mps": gate.review_sample.minimum_moving_speed_mps,
        "selected_source_record_count": len(sampled),
        "moving_review_observation_count": len(review_observations),
        "moving_distance_m": round(sum(weights), 9),
        "candidate_order": gate.review_sample.review_candidate_order,
        "candidate_scores_redacted": True,
        "production_decoder_output_included": False,
        "final_test_material_included": False,
        "observations": review_observations,
    }
    forbidden = set(gate.review_sample.forbidden_packet_fields)
    if _contains_forbidden_field(packet_payload, forbidden):
        raise ValueError("blind public review packet exposes a forbidden field")
    packet = {
        **packet_payload,
        "packet_immutable_sha256": _canonical_hash(packet_payload),
    }
    return _PreparedReview(
        gate=gate,
        gate_file_sha256=gate_file_sha256,
        graph=graph,
        matching_profile=matching_profile,
        matching_profile_file_sha256=matching_profile_sha256,
        decoder_profile_file_sha256=decoder_profile_sha256,
        sampled=sampled,
        endpoint_weights_m=weights,
        packet=packet,
    )


def prepare_public_route_review(
    *,
    public_data_root: Path,
    gate_path: Path,
    protocol_path: Path,
    data_manifest_path: Path,
    source_groups_path: Path,
    split_manifest_path: Path,
    numerical_charter_path: Path,
    graph_profile_path: Path,
    matching_profile_path: Path,
    decoder_profile_path: Path,
) -> dict[str, object]:
    """Generate the frozen blind packet without running the decoder."""

    return _prepare_review(
        public_data_root=public_data_root,
        gate_path=gate_path,
        protocol_path=protocol_path,
        data_manifest_path=data_manifest_path,
        source_groups_path=source_groups_path,
        split_manifest_path=split_manifest_path,
        numerical_charter_path=numerical_charter_path,
        graph_profile_path=graph_profile_path,
        matching_profile_path=matching_profile_path,
        decoder_profile_path=decoder_profile_path,
    ).packet


def qualify_public_road_matching(
    *,
    public_data_root: Path,
    gate_path: Path,
    adjudication_path: Path,
    protocol_path: Path,
    data_manifest_path: Path,
    source_groups_path: Path,
    split_manifest_path: Path,
    numerical_charter_path: Path,
    graph_profile_path: Path,
    matching_profile_path: Path,
    decoder_profile_path: Path,
) -> dict[str, object]:
    """Compare the frozen production decoder with completed blind decisions."""

    prepared = _prepare_review(
        public_data_root=public_data_root,
        gate_path=gate_path,
        protocol_path=protocol_path,
        data_manifest_path=data_manifest_path,
        source_groups_path=source_groups_path,
        split_manifest_path=split_manifest_path,
        numerical_charter_path=numerical_charter_path,
        graph_profile_path=graph_profile_path,
        matching_profile_path=matching_profile_path,
        decoder_profile_path=decoder_profile_path,
    )
    adjudication, adjudication_file_sha256 = load_public_route_adjudication(
        adjudication_path
    )
    packet_hash = cast(str, prepared.packet["packet_immutable_sha256"])
    authorities = adjudication.authorities
    if (
        authorities.gate_file_sha256 != prepared.gate_file_sha256
        or authorities.gate_immutable_sha256 != prepared.gate.immutable_sha256
        or authorities.review_packet_immutable_sha256 != packet_hash
        or authorities.graph_id != prepared.graph.graph_id
    ):
        raise ValueError("completed adjudication uses foreign M5.6 authorities")

    moving = tuple(
        (index, item)
        for index, item in enumerate(prepared.sampled)
        if item.speed_mps >= prepared.gate.review_sample.minimum_moving_speed_mps
    )
    expected_keys = tuple(
        (item.source_record_index, item.observation.observation_id)
        for _, item in moving
    )
    decision_keys = tuple(
        (item.source_record_index, item.observation_id)
        for item in adjudication.decisions
    )
    if decision_keys != expected_keys:
        raise ValueError("completed adjudication is not exhaustive in source order")

    packet_observations = cast(list[dict[str, object]], prepared.packet["observations"])
    candidate_arcs = {
        cast(str, item["observation_id"]): {
            cast(str, option["directed_arc_id"])
            for option in cast(list[dict[str, object]], item["candidate_options"])
        }
        for item in packet_observations
    }
    for decision in adjudication.decisions:
        if (
            decision.expected_directed_arc_id is not None
            and decision.expected_directed_arc_id
            not in candidate_arcs[decision.observation_id]
        ):
            raise ValueError(
                "adjudicated directed arc was absent from the blind packet"
            )

    decoder_profile, decoder_profile_sha256 = load_map_decoder_profile(
        decoder_profile_path
    )
    decoded = decode_road_path(
        prepared.graph,
        tuple(item.observation for item in prepared.sampled),
        sequence_id=prepared.gate.public_source.sequence_id,
        source_group_id=prepared.gate.public_source.source_group_id,
        partition="development",
        matching_profile=prepared.matching_profile,
        matching_profile_file_sha256=prepared.matching_profile_file_sha256,
        decoder_profile=decoder_profile,
        decoder_profile_file_sha256=decoder_profile_sha256,
    )
    points = {item.observation.observation_id: item for item in decoded.points}
    label_counts = {
        label: 0 for label in prepared.gate.decision_contract.allowed_labels
    }
    label_distances = {
        label: 0.0 for label in prepared.gate.decision_contract.allowed_labels
    }
    directed_agreement_weight = 0.0
    directed_manual_weight = 0.0
    directed_agreement_count = 0
    off_map_agreement_count = 0
    for (sample_index, _), decision in zip(moving, adjudication.decisions, strict=True):
        weight = prepared.endpoint_weights_m[sample_index]
        point = points[decision.observation_id]
        label_counts[decision.label] += 1
        label_distances[decision.label] += weight
        if decision.label == "DIRECTED_ARC":
            directed_manual_weight += weight
            agrees = (
                point.candidate.state == CandidateState.ON_ROAD
                and point.candidate.directed_arc_id == decision.expected_directed_arc_id
                and point.confidence == MatchConfidence.CONFIDENT
                and not point.stationary
            )
            if agrees:
                directed_agreement_weight += weight
                directed_agreement_count += 1
        elif decision.label == "OFF_MAP" and (
            point.candidate.state == CandidateState.OFF_MAP
            and point.confidence == MatchConfidence.CONFIDENT
            and not point.stationary
        ):
            off_map_agreement_count += 1

    total_moving_distance = sum(prepared.endpoint_weights_m)
    confident_fraction = directed_agreement_weight / total_moving_distance
    directed_agreement = (
        directed_agreement_weight / directed_manual_weight
        if directed_manual_weight > 0.0
        else 0.0
    )
    unresolved_labels: set[ReviewLabel] = {
        "AMBIGUOUS",
        "OFF_MAP",
        "GRAPH_DATA_LIMITATION",
        "UNRESOLVED",
    }
    unresolved_distance = sum(label_distances[label] for label in unresolved_labels)
    unresolved_preserved = all(
        item.expected_directed_arc_id is None
        for item in adjudication.decisions
        if item.label in unresolved_labels
    )
    coverage_gate = (
        confident_fraction
        >= prepared.gate.thresholds.confident_moving_distance_fraction_minimum
    )
    accepted = coverage_gate and unresolved_preserved
    decoder_blind = (
        not adjudication.review.production_decoder_output_viewed_before_completion
    )
    return {
        "schema_version": "cartosentry.m5.6-public-road-matching-qualification.v1",
        "accepted": accepted,
        "state": "ACCEPTED" if accepted else "FAILED",
        "algorithm_backend": ALGORITHM_BACKEND,
        "claim_status": "SINGLE_PINNED_DEVELOPMENT_ROUTE_CASE_STUDY_NOT_CONFIRMATORY",
        "authorities": {
            "gate_file_sha256": prepared.gate_file_sha256,
            "gate_immutable_sha256": prepared.gate.immutable_sha256,
            "adjudication_file_sha256": adjudication_file_sha256,
            "adjudication_immutable_sha256": adjudication.immutable_sha256,
            "protocol_freeze_commit": authorities.protocol_freeze_commit,
            "review_packet_immutable_sha256": packet_hash,
            "graph_id": prepared.graph.graph_id,
            "road_match_id": decoded.road_match_id,
        },
        "support": {
            "sequence_id": prepared.gate.public_source.sequence_id,
            "source_group_id": prepared.gate.public_source.source_group_id,
            "partition": prepared.gate.public_source.partition,
            "independent_source_group_count": 1,
            "selected_source_record_count": len(prepared.sampled),
            "moving_review_observation_count": len(moving),
            "moving_distance_m": round(total_moving_distance, 9),
            "distance_coverage_method": (
                prepared.gate.review_sample.distance_coverage_method
            ),
        },
        "metrics": {
            "confident_moving_distance_m": round(directed_agreement_weight, 9),
            "confident_moving_distance_fraction": round(confident_fraction, 9),
            "manual_directed_arc_distance_m": round(directed_manual_weight, 9),
            "manual_directed_arc_agreement": round(directed_agreement, 9),
            "manual_directed_arc_agreement_observations": directed_agreement_count,
            "manual_ambiguous_distance_m": round(label_distances["AMBIGUOUS"], 9),
            "manual_off_map_distance_m": round(label_distances["OFF_MAP"], 9),
            "manual_graph_data_limitation_distance_m": round(
                label_distances["GRAPH_DATA_LIMITATION"], 9
            ),
            "manual_unresolved_distance_m": round(label_distances["UNRESOLVED"], 9),
            "manual_unresolved_fraction": round(
                unresolved_distance / total_moving_distance, 9
            ),
            "manual_label_observation_counts": label_counts,
            "manual_off_map_decoder_agreement_observations": (off_map_agreement_count),
        },
        "gates": {
            "confident_public_moving_distance": coverage_gate,
            "unresolved_samples_preserved": unresolved_preserved,
            "adjudication_exhaustive": decision_keys == expected_keys,
            "final_test_material_not_accessed": (
                not adjudication.review.final_test_material_accessed
            ),
            "production_decoder_blinded_until_review_complete": decoder_blind,
        },
        "limitations": [
            "One development route from one source group is descriptive support only.",
            "No unseen-route or confirmatory public accuracy claim is supported.",
            "Unresolved, ambiguous, off-map, and graph-limited samples are not "
            "forced into road coverage.",
        ],
    }


__all__ = [
    "ADJUDICATION_IMMUTABLE_SHA256",
    "GATE_IMMUTABLE_SHA256",
    "PublicRoadMatchingGate",
    "PublicRouteAdjudication",
    "ReviewDecision",
    "load_public_road_matching_gate",
    "load_public_route_adjudication",
    "prepare_public_route_review",
    "qualify_public_road_matching",
]
