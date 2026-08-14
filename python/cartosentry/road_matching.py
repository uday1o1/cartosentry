"""Deterministic HMM road-candidate generation and scoring."""

from __future__ import annotations

import hashlib
import json
import math
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry import _core
from cartosentry.contracts import TimePoint
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.road_graph import (
    PROFILE_IMMUTABLE_SHA256 as GRAPH_PROFILE_IMMUTABLE_SHA256,
)
from cartosentry.road_graph import (
    DirectedRoadGraph,
    RoadGraphSpatialIndex,
    validate_graph_identity,
)

PROFILE_IMMUTABLE_SHA256 = (
    "dc4da969cb9f9d85492be6ed7f44798dd48b6d0c935e656979c67a27b2c3b5f1"
)
ALGORITHM_BACKEND: Literal["C++20_NATIVE_BATCH_V1"] = "C++20_NATIVE_BATCH_V1"
MAXIMUM_PROFILE_BYTES = 256 * 1024
NEGATIVE_INFINITY = float("-inf")


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class MapMatchingAuthorities(StrictModel):
    graph_import_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    graph_import_profile_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    numerical_charter_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CandidateParameters(StrictModel):
    minimum_search_radius_m: Annotated[float, Field(gt=0.0)]
    default_search_radius_m: Annotated[float, Field(gt=0.0)]
    maximum_search_radius_m: Annotated[float, Field(gt=0.0)]
    uncertainty_radius_multiplier: Annotated[float, Field(gt=0.0)]
    maximum_on_road_candidates: Annotated[int, Field(gt=0)]
    distance_rounding_decimal_places: Annotated[int, Field(ge=0, le=12)]


class EmissionParameters(StrictModel):
    base_lateral_sigma_m: Annotated[float, Field(gt=0.0)]
    maximum_lateral_sigma_m: Annotated[float, Field(gt=0.0)]
    heading_sigma_rad: Annotated[float, Field(gt=0.0, le=math.pi)]
    heading_disabled_below_speed_mps: Annotated[float, Field(ge=0.0)]
    heading_full_weight_speed_mps: Annotated[float, Field(gt=0.0)]
    off_map_log_likelihood: float
    score_rounding_decimal_places: Annotated[int, Field(ge=0, le=15)]


class TransitionParameters(StrictModel):
    path_discrepancy_scale_m: Annotated[float, Field(gt=0.0)]
    maximum_absolute_speed_mps: Annotated[float, Field(gt=0.0)]
    observed_speed_excess_allowance_mps: Annotated[float, Field(ge=0.0)]
    speed_excess_penalty_per_mps: Annotated[float, Field(ge=0.0)]
    turn_penalty: Annotated[float, Field(ge=0.0)]
    u_turn_penalty: Annotated[float, Field(ge=0.0)]
    off_map_enter_log_likelihood: Annotated[float, Field(le=0.0)]
    off_map_exit_log_likelihood: Annotated[float, Field(le=0.0)]
    off_map_stay_log_likelihood: Annotated[float, Field(le=0.0)]
    maximum_graph_search_distance_m: Annotated[float, Field(gt=0.0)]
    maximum_graph_search_states: Annotated[int, Field(gt=0)]
    score_rounding_decimal_places: Annotated[int, Field(ge=0, le=15)]


class MapMatchingParameterCharter(StrictModel):
    candidate: CandidateParameters
    emission: EmissionParameters
    transition: TransitionParameters


class MapMatchingProfile(StrictModel):
    schema_version: Literal[1]
    profile_id: Literal["map-matching-v1"]
    profile_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_M5_2_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: MapMatchingAuthorities
    parameter_charter: MapMatchingParameterCharter

    def assert_identity(self) -> None:
        canonical = self.model_dump(mode="json", exclude={"immutable_sha256"})
        if (
            self.immutable_sha256 != PROFILE_IMMUTABLE_SHA256
            or _canonical_hash(canonical) != self.immutable_sha256
        ):
            raise ValueError("map-matching profile identity is not pinned")

    @model_validator(mode="after")
    def validate_exact_profile(self) -> Self:
        self.assert_identity()
        if (
            self.authorities.graph_import_profile_immutable_sha256
            != GRAPH_PROFILE_IMMUTABLE_SHA256
        ):
            raise ValueError("map-matching graph profile authority is not exact")
        candidate = self.parameter_charter.candidate
        if not (
            candidate.minimum_search_radius_m
            <= candidate.default_search_radius_m
            <= candidate.maximum_search_radius_m
        ):
            raise ValueError("map-matching search radii are not ordered")
        emission = self.parameter_charter.emission
        if (
            emission.maximum_lateral_sigma_m < emission.base_lateral_sigma_m
            or emission.heading_full_weight_speed_mps
            <= emission.heading_disabled_below_speed_mps
            or emission.off_map_log_likelihood > 0.0
        ):
            raise ValueError("map-matching emission parameters are inconsistent")
        return self


class CandidateState(StrEnum):
    ON_ROAD = "ON_ROAD"
    OFF_MAP = "OFF_MAP"


class RoadMatchObservation(StrictModel):
    observation_id: Annotated[str, Field(pattern=r"^observation-sha256-[0-9a-f]{64}$")]
    source_observation_id: Annotated[
        str | None, Field(pattern=r"^observation-sha256-[0-9a-f]{64}$")
    ]
    time: TimePoint
    local_frame_id: Annotated[str, Field(min_length=1)]
    position_local_m: tuple[float, float]
    heading_rad: Annotated[float, Field(ge=-math.pi, le=math.pi)] | None
    speed_mps: Annotated[float, Field(ge=0.0)]
    horizontal_uncertainty_m: Annotated[float, Field(gt=0.0)] | None
    horizontal_uncertainty_basis: Literal["DECLARED_TRUSTWORTHY"] | None

    def assert_identity(self) -> None:
        if (self.horizontal_uncertainty_m is None) != (
            self.horizontal_uncertainty_basis is None
        ):
            raise ValueError(
                "horizontal uncertainty requires an explicit trustworthy basis"
            )
        payload: dict[str, object] = {
            "source_observation_id": self.source_observation_id,
            "time": self.time.model_dump(mode="json"),
            "local_frame_id": self.local_frame_id,
            "position_local_m": self.position_local_m,
            "heading_rad": self.heading_rad,
            "speed_mps": self.speed_mps,
            "horizontal_uncertainty_m": self.horizontal_uncertainty_m,
            "horizontal_uncertainty_basis": self.horizontal_uncertainty_basis,
        }
        expected = f"observation-sha256-{_canonical_hash(payload)}"
        if self.observation_id != expected:
            raise ValueError("road-match observation identity is invalid")

    @model_validator(mode="after")
    def validate_observation_identity(self) -> Self:
        self.assert_identity()
        return self


def make_road_match_observation(
    *,
    time: TimePoint,
    local_frame_id: str,
    position_local_m: tuple[float, float],
    heading_rad: float | None,
    speed_mps: float,
    horizontal_uncertainty_m: float | None,
    horizontal_uncertainty_basis: Literal["DECLARED_TRUSTWORTHY"] | None = None,
    source_observation_id: str | None = None,
) -> RoadMatchObservation:
    """Construct an observation and retain an upstream identity when present."""

    numeric_values = (
        *position_local_m,
        speed_mps,
        *(() if heading_rad is None else (heading_rad,)),
        *(() if horizontal_uncertainty_m is None else (horizontal_uncertainty_m,)),
    )
    if any(isinstance(value, bool) for value in numeric_values):
        raise ValueError("road-match numeric features cannot be booleans")
    canonical_position = (float(position_local_m[0]), float(position_local_m[1]))
    canonical_heading = float(heading_rad) if heading_rad is not None else None
    canonical_speed = float(speed_mps)
    canonical_uncertainty = (
        float(horizontal_uncertainty_m)
        if horizontal_uncertainty_m is not None
        else None
    )
    payload: dict[str, object] = {
        "source_observation_id": source_observation_id,
        "time": time.model_dump(mode="json"),
        "local_frame_id": local_frame_id,
        "position_local_m": canonical_position,
        "heading_rad": canonical_heading,
        "speed_mps": canonical_speed,
        "horizontal_uncertainty_m": canonical_uncertainty,
        "horizontal_uncertainty_basis": horizontal_uncertainty_basis,
    }
    observation_id = f"observation-sha256-{_canonical_hash(payload)}"
    return RoadMatchObservation(
        observation_id=observation_id,
        source_observation_id=source_observation_id,
        time=time,
        local_frame_id=local_frame_id,
        position_local_m=canonical_position,
        heading_rad=canonical_heading,
        speed_mps=canonical_speed,
        horizontal_uncertainty_m=canonical_uncertainty,
        horizontal_uncertainty_basis=horizontal_uncertainty_basis,
    )


class EmissionFeatures(StrictModel):
    lateral_sigma_m: Annotated[float, Field(gt=0.0)] | None
    lateral_log_likelihood: float | None
    heading_used: bool
    heading_weight: Annotated[float, Field(ge=0.0, le=1.0)]
    heading_difference_rad: Annotated[float, Field(ge=0.0, le=math.pi)] | None
    heading_log_likelihood: float | None
    off_map_log_likelihood: float | None
    total_log_likelihood: float


class RoadCandidate(StrictModel):
    candidate_id: Annotated[str, Field(pattern=r"^candidate-sha256-[0-9a-f]{64}$")]
    observation_id: Annotated[str, Field(pattern=r"^observation-sha256-[0-9a-f]{64}$")]
    graph_id: Annotated[str, Field(pattern=r"^road-graph-sha256-[0-9a-f]{64}$")]
    state: CandidateState
    directed_arc_id: Annotated[
        str | None, Field(pattern=r"^osm-arc-sha256-[0-9a-f]{64}$")
    ]
    source_way_id: Annotated[int, Field(gt=0)] | None
    projected_position_local_m: tuple[float, float] | None
    lateral_distance_m: Annotated[float, Field(ge=0.0)] | None
    tangent_heading_rad: Annotated[float, Field(ge=-math.pi, le=math.pi)] | None
    along_arc_offset_m: Annotated[float, Field(ge=0.0)] | None
    search_radius_m: Annotated[float, Field(gt=0.0)]
    emission: EmissionFeatures

    @model_validator(mode="after")
    def validate_state_fields(self) -> Self:
        on_road_fields = (
            self.directed_arc_id,
            self.source_way_id,
            self.projected_position_local_m,
            self.lateral_distance_m,
            self.tangent_heading_rad,
            self.along_arc_offset_m,
        )
        if self.state == CandidateState.ON_ROAD:
            if any(value is None for value in on_road_fields):
                raise ValueError(
                    "on-road candidates require complete projection evidence"
                )
            if self.emission.off_map_log_likelihood is not None:
                raise ValueError("on-road candidate cannot carry off-map emission")
        elif any(value is not None for value in on_road_fields):
            raise ValueError("off-map candidate cannot carry road projection evidence")
        return self


class TransitionRejection(str):
    NON_POSITIVE_ELAPSED_TIME = "NON_POSITIVE_ELAPSED_TIME"
    FORBIDDEN_TURN = "FORBIDDEN_TURN"
    UNKNOWN_RESTRICTION = "UNKNOWN_RESTRICTION"
    NO_DIRECTED_PATH = "NO_DIRECTED_PATH"
    GRAPH_SEARCH_BUDGET = "GRAPH_SEARCH_BUDGET"
    IMPLAUSIBLE_ABSOLUTE_SPEED = "IMPLAUSIBLE_ABSOLUTE_SPEED"


class TransitionScore(StrictModel):
    from_candidate_id: Annotated[str, Field(pattern=r"^candidate-sha256-[0-9a-f]{64}$")]
    to_candidate_id: Annotated[str, Field(pattern=r"^candidate-sha256-[0-9a-f]{64}$")]
    possible: bool
    rejection_reason: (
        Literal[
            "NON_POSITIVE_ELAPSED_TIME",
            "FORBIDDEN_TURN",
            "UNKNOWN_RESTRICTION",
            "NO_DIRECTED_PATH",
            "GRAPH_SEARCH_BUDGET",
            "IMPLAUSIBLE_ABSOLUTE_SPEED",
        ]
        | None
    )
    elapsed_seconds: Annotated[float, Field(ge=0.0)]
    observed_displacement_m: Annotated[float, Field(ge=0.0)]
    graph_distance_m: Annotated[float, Field(ge=0.0)] | None
    implied_graph_speed_mps: Annotated[float, Field(ge=0.0)] | None
    path_discrepancy_m: Annotated[float, Field(ge=0.0)] | None
    turn_count: Annotated[int, Field(ge=0)] | None
    u_turn_count: Annotated[int, Field(ge=0)] | None
    path_arc_ids: tuple[str, ...]
    path_log_likelihood: float | None
    speed_log_likelihood: float | None
    turn_log_likelihood: float | None
    off_map_log_likelihood: float | None
    total_log_likelihood: float | None

    @model_validator(mode="after")
    def validate_possible_contract(self) -> Self:
        component_values = (
            self.path_log_likelihood,
            self.speed_log_likelihood,
            self.turn_log_likelihood,
            self.off_map_log_likelihood,
        )
        if self.possible:
            if self.rejection_reason is not None or self.total_log_likelihood is None:
                raise ValueError("possible transitions require a finite score")
        elif self.rejection_reason is None or self.total_log_likelihood is not None:
            raise ValueError("impossible transitions require a rejection reason")
        if not self.possible and any(value is not None for value in component_values):
            raise ValueError("impossible transitions cannot carry score components")
        return self

    @property
    def score(self) -> float:
        """Return negative infinity for impossible transitions at runtime."""

        if self.total_log_likelihood is None:
            return NEGATIVE_INFINITY
        return self.total_log_likelihood


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def load_map_matching_profile(path: Path) -> tuple[MapMatchingProfile, str]:
    """Load and self-authenticate the frozen M5.2 parameter charter."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="map-matching profile",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="map-matching profile",
        )
    except ManifestBoundaryError as error:
        raise ValueError("map-matching profile is unavailable or malformed") from error
    if not isinstance(decoded, dict):
        raise ValueError("map-matching profile must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("map-matching profile immutable hash is invalid")
    return MapMatchingProfile.model_validate_json(content), hashlib.sha256(
        content
    ).hexdigest()


def validate_matching_graph_authority(
    graph: DirectedRoadGraph, profile: MapMatchingProfile
) -> None:
    """Require the exact graph-import authority frozen by the matching charter."""

    profile.assert_identity()
    authorities = profile.authorities
    if (
        graph.profile_immutable_sha256
        != authorities.graph_import_profile_immutable_sha256
        or graph.profile_file_sha256 != authorities.graph_import_profile_file_sha256
    ):
        raise ValueError("directed road graph uses a foreign import-profile authority")


def _candidate_id(payload: dict[str, object]) -> str:
    return f"candidate-sha256-{_canonical_hash(payload)}"


def _validate_candidate_identity(
    candidate: RoadCandidate, profile: MapMatchingProfile
) -> None:
    payload: dict[str, object] = {
        "profile_immutable_sha256": profile.immutable_sha256,
        "graph_id": candidate.graph_id,
        "observation_id": candidate.observation_id,
        "state": candidate.state,
    }
    if candidate.state == CandidateState.ON_ROAD:
        payload.update(
            {
                "directed_arc_id": candidate.directed_arc_id,
                "projected_position_local_m": candidate.projected_position_local_m,
                "along_arc_offset_m": candidate.along_arc_offset_m,
            }
        )
    if candidate.candidate_id != _candidate_id(payload):
        raise ValueError("road-candidate identity is invalid")


def _native_graph_payload(graph: DirectedRoadGraph) -> dict[str, object]:
    return {
        "arcs": [
            {
                "arc_id": arc.arc_id,
                "from_node_id": arc.from_node_id,
                "to_node_id": arc.to_node_id,
                "source_way_id": arc.source_way_id,
                "direction": arc.direction.value,
                "length_m": arc.length_m,
                "geometry": [[point[0], point[1]] for point in arc.geometry_local_m],
            }
            for arc in graph.arcs
        ],
        "transition_rules": [
            {
                "from_arc_id": rule.from_arc_id,
                "to_arc_id": rule.to_arc_id,
                "state": rule.state.value,
            }
            for rule in graph.transition_rules
        ],
    }


def _native_observation_payload(
    observation: RoadMatchObservation,
) -> dict[str, object]:
    return {
        "observation_id": observation.observation_id,
        "time_ns": observation.time.value_ns,
        "position_local_m": list(observation.position_local_m),
        "heading_rad": observation.heading_rad,
        "speed_mps": observation.speed_mps,
        "horizontal_uncertainty_m": observation.horizontal_uncertainty_m,
    }


def _candidate_from_native(
    graph: DirectedRoadGraph,
    observation: RoadMatchObservation,
    profile: MapMatchingProfile,
    raw: dict[str, Any],
    *,
    observation_index: int,
) -> RoadCandidate:
    if raw.get("observation_index") != observation_index:
        raise ValueError("native road candidate belongs to the wrong observation")
    directed_arc_id = cast(str | None, raw.get("directed_arc_id"))
    state = (
        CandidateState.ON_ROAD
        if directed_arc_id is not None
        else CandidateState.OFF_MAP
    )
    projected_raw = cast(list[float] | None, raw.get("projected_position_local_m"))
    projected = (
        (float(projected_raw[0]), float(projected_raw[1]))
        if projected_raw is not None and len(projected_raw) == 2
        else None
    )
    payload: dict[str, object] = {
        "profile_immutable_sha256": profile.immutable_sha256,
        "graph_id": graph.graph_id,
        "observation_id": observation.observation_id,
        "state": state,
    }
    if state == CandidateState.ON_ROAD:
        payload.update(
            {
                "directed_arc_id": directed_arc_id,
                "projected_position_local_m": projected,
                "along_arc_offset_m": raw.get("along_arc_offset_m"),
            }
        )
    candidate = RoadCandidate(
        candidate_id=_candidate_id(payload),
        observation_id=observation.observation_id,
        graph_id=graph.graph_id,
        state=state,
        directed_arc_id=directed_arc_id,
        source_way_id=cast(int | None, raw.get("source_way_id")),
        projected_position_local_m=projected,
        lateral_distance_m=cast(float | None, raw.get("lateral_distance_m")),
        tangent_heading_rad=cast(float | None, raw.get("tangent_heading_rad")),
        along_arc_offset_m=cast(float | None, raw.get("along_arc_offset_m")),
        search_radius_m=cast(float, raw["search_radius_m"]),
        emission=EmissionFeatures.model_validate(raw["emission"]),
    )
    _validate_candidate_identity(candidate, profile)
    return candidate


def _generate_road_candidate_batches(
    graph: DirectedRoadGraph,
    observations: tuple[RoadMatchObservation, ...],
    *,
    profile: MapMatchingProfile,
) -> tuple[tuple[RoadCandidate, ...], ...]:
    validate_graph_identity(graph)
    validate_matching_graph_authority(graph, profile)
    if not observations:
        raise ValueError("at least one road-match observation is required")
    for observation in observations:
        observation.assert_identity()
        if observation.local_frame_id != graph.local_frame.frame.frame_id:
            raise ValueError("road-match observation uses the wrong local frame")
    parameters = profile.parameter_charter
    raw_batches = _core.generate_road_candidate_batches(
        _native_graph_payload(graph),
        [_native_observation_payload(item) for item in observations],
        parameters.candidate.model_dump(mode="python"),
        parameters.emission.model_dump(mode="python"),
    )
    if len(raw_batches) != len(observations):
        raise ValueError("native road candidate batch count is invalid")
    return tuple(
        tuple(
            _candidate_from_native(
                graph,
                observation,
                profile,
                raw,
                observation_index=observation_index,
            )
            for raw in raw_batch
        )
        for observation_index, (observation, raw_batch) in enumerate(
            zip(observations, raw_batches, strict=True)
        )
    )


def generate_road_candidates(
    graph: DirectedRoadGraph,
    observation: RoadMatchObservation,
    *,
    profile: MapMatchingProfile,
    spatial_index: RoadGraphSpatialIndex | None = None,
) -> tuple[RoadCandidate, ...]:
    """Generate bounded directed projections and an unconditional off-map state."""

    if spatial_index is not None and spatial_index.graph_id != graph.graph_id:
        raise ValueError("road spatial index does not belong to the directed graph")
    return _generate_road_candidate_batches(graph, (observation,), profile=profile)[0]


def best_emission_candidate(candidates: tuple[RoadCandidate, ...]) -> RoadCandidate:
    """Use the native emission score and identity tie-break decision."""

    if not candidates:
        raise ValueError("at least one road candidate is required")
    selected = _core.select_best_road_emission_candidate(
        [
            {
                "candidate_id": item.candidate_id,
                "emission_total_log_likelihood": (item.emission.total_log_likelihood),
            }
            for item in candidates
        ]
    )
    if selected < 0 or selected >= len(candidates):
        raise ValueError("native emission selection returned an invalid index")
    return candidates[selected]


def _native_candidate_payload(
    graph: DirectedRoadGraph,
    candidate: RoadCandidate,
    *,
    observation_index: int,
) -> dict[str, object]:
    arc_index: int | None = None
    if candidate.state == CandidateState.ON_ROAD:
        try:
            arc_index = next(
                index
                for index, arc in enumerate(graph.arcs)
                if arc.arc_id == candidate.directed_arc_id
            )
        except StopIteration as error:
            raise ValueError(
                "road candidate arc is absent from the directed graph"
            ) from error
    return {
        "candidate_id": candidate.candidate_id,
        "observation_index": observation_index,
        "arc_index": arc_index,
        "along_arc_offset_m": candidate.along_arc_offset_m,
        "emission_total_log_likelihood": (candidate.emission.total_log_likelihood),
    }


def score_road_transition(
    graph: DirectedRoadGraph,
    previous_observation: RoadMatchObservation,
    previous: RoadCandidate,
    current_observation: RoadMatchObservation,
    current: RoadCandidate,
    *,
    profile: MapMatchingProfile,
) -> TransitionScore:
    """Score one graph-valid directed transition in the native batch kernel."""

    validate_graph_identity(graph)
    validate_matching_graph_authority(graph, profile)
    previous_observation.assert_identity()
    current_observation.assert_identity()
    current_observation.time.difference(previous_observation.time)
    if (
        previous.observation_id != previous_observation.observation_id
        or current.observation_id != current_observation.observation_id
    ):
        raise ValueError("road candidates do not belong to their observations")
    if previous.graph_id != graph.graph_id or current.graph_id != graph.graph_id:
        raise ValueError("road candidates do not belong to the directed graph")
    _validate_candidate_identity(previous, profile)
    _validate_candidate_identity(current, profile)
    parameters = profile.parameter_charter
    raw_results = _core.score_road_transition_batch(
        _native_graph_payload(graph),
        [
            _native_observation_payload(previous_observation),
            _native_observation_payload(current_observation),
        ],
        [
            {
                "previous_observation_index": 0,
                "previous_candidate": _native_candidate_payload(
                    graph, previous, observation_index=0
                ),
                "current_observation_index": 1,
                "current_candidate": _native_candidate_payload(
                    graph, current, observation_index=1
                ),
            }
        ],
        parameters.candidate.model_dump(mode="python"),
        parameters.transition.model_dump(mode="python"),
    )
    if len(raw_results) != 1:
        raise ValueError("native road transition result count is invalid")
    raw = raw_results[0]
    raw["from_candidate_id"] = previous.candidate_id
    raw["to_candidate_id"] = current.candidate_id
    raw["path_arc_ids"] = tuple(cast(list[str], raw["path_arc_ids"]))
    return TransitionScore.model_validate(raw)


__all__ = [
    "ALGORITHM_BACKEND",
    "NEGATIVE_INFINITY",
    "PROFILE_IMMUTABLE_SHA256",
    "CandidateState",
    "MapMatchingProfile",
    "RoadCandidate",
    "RoadMatchObservation",
    "TransitionRejection",
    "TransitionScore",
    "best_emission_candidate",
    "generate_road_candidates",
    "load_map_matching_profile",
    "make_road_match_observation",
    "score_road_transition",
    "validate_matching_graph_authority",
]
