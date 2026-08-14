"""Deterministic HMM road-candidate generation and scoring."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
from collections import defaultdict
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shapely import LineString, Point  # type: ignore[import-untyped]

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
    DirectedRoadArc,
    DirectedRoadGraph,
    RoadGraphSpatialIndex,
    TransitionState,
    validate_graph_identity,
)

PROFILE_IMMUTABLE_SHA256 = (
    "dc4da969cb9f9d85492be6ed7f44798dd48b6d0c935e656979c67a27b2c3b5f1"
)
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

    payload: dict[str, object] = {
        "source_observation_id": source_observation_id,
        "time": time.model_dump(mode="json"),
        "local_frame_id": local_frame_id,
        "position_local_m": position_local_m,
        "heading_rad": heading_rad,
        "speed_mps": speed_mps,
        "horizontal_uncertainty_m": horizontal_uncertainty_m,
        "horizontal_uncertainty_basis": horizontal_uncertainty_basis,
    }
    observation_id = f"observation-sha256-{_canonical_hash(payload)}"
    return RoadMatchObservation(
        observation_id=observation_id,
        source_observation_id=source_observation_id,
        time=time,
        local_frame_id=local_frame_id,
        position_local_m=position_local_m,
        heading_rad=heading_rad,
        speed_mps=speed_mps,
        horizontal_uncertainty_m=horizontal_uncertainty_m,
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


def _gaussian_log_likelihood(residual: float, sigma: float) -> float:
    return -0.5 * (residual / sigma) ** 2 - math.log(sigma * math.sqrt(2.0 * math.pi))


def _wrapped_heading_difference(left: float, right: float) -> float:
    return abs((left - right + math.pi) % (2.0 * math.pi) - math.pi)


def _heading_weight(speed_mps: float, parameters: EmissionParameters) -> float:
    if speed_mps <= parameters.heading_disabled_below_speed_mps:
        return 0.0
    span = (
        parameters.heading_full_weight_speed_mps
        - parameters.heading_disabled_below_speed_mps
    )
    return min(1.0, (speed_mps - parameters.heading_disabled_below_speed_mps) / span)


def _search_radius(
    observation: RoadMatchObservation, parameters: CandidateParameters
) -> float:
    requested = parameters.default_search_radius_m
    if observation.horizontal_uncertainty_m is not None:
        requested = max(
            requested,
            observation.horizontal_uncertainty_m
            * parameters.uncertainty_radius_multiplier,
        )
    return min(
        parameters.maximum_search_radius_m,
        max(parameters.minimum_search_radius_m, requested),
    )


def _directed_tangent(
    geometry: tuple[tuple[float, float, float], ...], projected_distance_m: float
) -> float:
    remaining = projected_distance_m
    fallback: tuple[float, float] | None = None
    for left, right in pairwise(geometry):
        delta = (right[0] - left[0], right[1] - left[1])
        length = math.hypot(*delta)
        if length == 0.0:
            continue
        fallback = delta
        if remaining <= length:
            return math.atan2(delta[1], delta[0])
        remaining -= length
    if fallback is None:
        raise ValueError("directed arc has no nonzero horizontal segment")
    return math.atan2(fallback[1], fallback[0])


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


def _on_road_candidate(
    observation: RoadMatchObservation,
    arc: DirectedRoadArc,
    *,
    radius_m: float,
    graph_id: str,
    profile: MapMatchingProfile,
) -> RoadCandidate:
    candidate_parameters = profile.parameter_charter.candidate
    emission_parameters = profile.parameter_charter.emission
    geometry = LineString([(point[0], point[1]) for point in arc.geometry_local_m])
    point = Point(observation.position_local_m)
    projected_distance = geometry.project(point)
    projected = geometry.interpolate(projected_distance)
    lateral_distance = round(
        geometry.distance(point),
        candidate_parameters.distance_rounding_decimal_places,
    )
    if geometry.length <= 0.0:
        raise ValueError("directed arc has invalid horizontal geometry")
    along_offset = round(
        min(arc.length_m, projected_distance / geometry.length * arc.length_m),
        candidate_parameters.distance_rounding_decimal_places,
    )
    places = emission_parameters.score_rounding_decimal_places
    tangent = round(_directed_tangent(arc.geometry_local_m, projected_distance), places)
    uncertainty = observation.horizontal_uncertainty_m or 0.0
    lateral_sigma = min(
        emission_parameters.maximum_lateral_sigma_m,
        math.hypot(emission_parameters.base_lateral_sigma_m, uncertainty),
    )
    lateral_log = _gaussian_log_likelihood(lateral_distance, lateral_sigma)
    weight = (
        _heading_weight(observation.speed_mps, emission_parameters)
        if observation.heading_rad is not None
        else 0.0
    )
    heading_difference = (
        _wrapped_heading_difference(observation.heading_rad, tangent)
        if weight > 0.0 and observation.heading_rad is not None
        else None
    )
    heading_log = (
        _gaussian_log_likelihood(
            heading_difference, emission_parameters.heading_sigma_rad
        )
        if heading_difference is not None
        else None
    )
    total = lateral_log + weight * (heading_log or 0.0)
    projected_position = (
        round(projected.x, candidate_parameters.distance_rounding_decimal_places),
        round(projected.y, candidate_parameters.distance_rounding_decimal_places),
    )
    payload: dict[str, object] = {
        "profile_immutable_sha256": profile.immutable_sha256,
        "graph_id": graph_id,
        "observation_id": observation.observation_id,
        "state": CandidateState.ON_ROAD,
        "directed_arc_id": arc.arc_id,
        "projected_position_local_m": projected_position,
        "along_arc_offset_m": along_offset,
    }
    return RoadCandidate(
        candidate_id=_candidate_id(payload),
        observation_id=observation.observation_id,
        graph_id=graph_id,
        state=CandidateState.ON_ROAD,
        directed_arc_id=arc.arc_id,
        source_way_id=arc.source_way_id,
        projected_position_local_m=projected_position,
        lateral_distance_m=lateral_distance,
        tangent_heading_rad=tangent,
        along_arc_offset_m=along_offset,
        search_radius_m=radius_m,
        emission=EmissionFeatures(
            lateral_sigma_m=round(lateral_sigma, places),
            lateral_log_likelihood=round(lateral_log, places),
            heading_used=heading_difference is not None,
            heading_weight=round(weight, places),
            heading_difference_rad=(
                round(heading_difference, places)
                if heading_difference is not None
                else None
            ),
            heading_log_likelihood=(
                round(heading_log, places) if heading_log is not None else None
            ),
            off_map_log_likelihood=None,
            total_log_likelihood=round(total, places),
        ),
    )


def _off_map_candidate(
    observation: RoadMatchObservation,
    *,
    radius_m: float,
    graph_id: str,
    profile: MapMatchingProfile,
) -> RoadCandidate:
    score = profile.parameter_charter.emission.off_map_log_likelihood
    payload: dict[str, object] = {
        "profile_immutable_sha256": profile.immutable_sha256,
        "graph_id": graph_id,
        "observation_id": observation.observation_id,
        "state": CandidateState.OFF_MAP,
    }
    return RoadCandidate(
        candidate_id=_candidate_id(payload),
        observation_id=observation.observation_id,
        graph_id=graph_id,
        state=CandidateState.OFF_MAP,
        directed_arc_id=None,
        source_way_id=None,
        projected_position_local_m=None,
        lateral_distance_m=None,
        tangent_heading_rad=None,
        along_arc_offset_m=None,
        search_radius_m=radius_m,
        emission=EmissionFeatures(
            lateral_sigma_m=None,
            lateral_log_likelihood=None,
            heading_used=False,
            heading_weight=0.0,
            heading_difference_rad=None,
            heading_log_likelihood=None,
            off_map_log_likelihood=score,
            total_log_likelihood=score,
        ),
    )


def generate_road_candidates(
    graph: DirectedRoadGraph,
    observation: RoadMatchObservation,
    *,
    profile: MapMatchingProfile,
    spatial_index: RoadGraphSpatialIndex | None = None,
) -> tuple[RoadCandidate, ...]:
    """Generate bounded directed projections and an unconditional off-map state."""

    validate_graph_identity(graph)
    validate_matching_graph_authority(graph, profile)
    observation.assert_identity()
    if observation.local_frame_id != graph.local_frame.frame.frame_id:
        raise ValueError("road-match observation uses the wrong local frame")
    radius = _search_radius(observation, profile.parameter_charter.candidate)
    index = spatial_index or RoadGraphSpatialIndex(graph)
    if index.graph_id != graph.graph_id:
        raise ValueError("road spatial index does not belong to the directed graph")
    on_road = [
        _on_road_candidate(
            observation,
            arc,
            radius_m=radius,
            graph_id=graph.graph_id,
            profile=profile,
        )
        for arc in index.query_radius(observation.position_local_m, radius)
    ]
    on_road.sort(
        key=lambda item: (
            -item.emission.total_log_likelihood,
            cast(float, item.lateral_distance_m),
            cast(str, item.directed_arc_id),
        )
    )
    maximum = profile.parameter_charter.candidate.maximum_on_road_candidates
    selected = on_road[:maximum]
    selected.append(
        _off_map_candidate(
            observation,
            radius_m=radius,
            graph_id=graph.graph_id,
            profile=profile,
        )
    )
    return tuple(selected)


def best_emission_candidate(candidates: tuple[RoadCandidate, ...]) -> RoadCandidate:
    """Choose one emission-only candidate with deterministic identity tie-breaking."""

    if not candidates:
        raise ValueError("at least one road candidate is required")
    return min(
        candidates,
        key=lambda item: (-item.emission.total_log_likelihood, item.candidate_id),
    )


def _arc_by_id(graph: DirectedRoadGraph, candidate: RoadCandidate) -> DirectedRoadArc:
    arc_id = candidate.directed_arc_id
    if candidate.state != CandidateState.ON_ROAD or arc_id is None:
        raise ValueError("on-road transition requires an arc candidate")
    try:
        return next(item for item in graph.arcs if item.arc_id == arc_id)
    except StopIteration as error:
        raise ValueError("road candidate arc is absent from the graph") from error


def _blocked_transition(
    graph: DirectedRoadGraph, from_arc_id: str, to_arc_id: str
) -> str | None:
    states = {
        item.state
        for item in graph.transition_rules
        if item.from_arc_id == from_arc_id and item.to_arc_id == to_arc_id
    }
    if TransitionState.FORBIDDEN in states:
        return TransitionRejection.FORBIDDEN_TURN
    if TransitionState.UNKNOWN_RESTRICTION in states:
        return TransitionRejection.UNKNOWN_RESTRICTION
    return None


def _count_u_turns(path: tuple[str, ...], arcs: dict[str, DirectedRoadArc]) -> int:
    return sum(
        arcs[left].source_way_id == arcs[right].source_way_id
        and arcs[left].direction is not arcs[right].direction
        for left, right in pairwise(path)
    )


def _directed_route(
    graph: DirectedRoadGraph,
    previous: RoadCandidate,
    current: RoadCandidate,
    parameters: TransitionParameters,
) -> tuple[float, int, int, tuple[str, ...]] | str:
    previous_arc = _arc_by_id(graph, previous)
    current_arc = _arc_by_id(graph, current)
    previous_offset = cast(float, previous.along_arc_offset_m)
    current_offset = cast(float, current.along_arc_offset_m)
    if previous_arc.arc_id == current_arc.arc_id and current_offset >= previous_offset:
        return (
            current_offset - previous_offset,
            0,
            0,
            (previous_arc.arc_id,),
        )
    outgoing: dict[str, list[DirectedRoadArc]] = defaultdict(list)
    arc_lookup = {item.arc_id: item for item in graph.arcs}
    for arc in graph.arcs:
        outgoing[arc.from_node_id].append(arc)
    initial_distance = previous_arc.length_m - previous_offset
    if initial_distance > parameters.maximum_graph_search_distance_m:
        return TransitionRejection.GRAPH_SEARCH_BUDGET
    initial_path = (previous_arc.arc_id,)
    queue: list[tuple[float, int, tuple[str, ...], str, str]] = [
        (
            initial_distance,
            0,
            initial_path,
            previous_arc.to_node_id,
            previous_arc.arc_id,
        )
    ]
    best: dict[tuple[str, str], tuple[float, int, tuple[str, ...]]] = {}
    target_rejections: set[str] = set()
    visited = 0
    while queue:
        distance, turns, path, node_id, incoming_arc_id = heapq.heappop(queue)
        state = (node_id, incoming_arc_id)
        previous_best = best.get(state)
        ranking = (distance, turns, path)
        if previous_best is not None and ranking >= previous_best:
            continue
        best[state] = ranking
        visited += 1
        if visited > parameters.maximum_graph_search_states:
            return TransitionRejection.GRAPH_SEARCH_BUDGET
        if node_id == current_arc.from_node_id:
            blocked = _blocked_transition(graph, incoming_arc_id, current_arc.arc_id)
            if blocked is None:
                result_path = (
                    path
                    if path[-1] == current_arc.arc_id
                    else (*path, current_arc.arc_id)
                )
                total = distance + current_offset
                if total > parameters.maximum_graph_search_distance_m:
                    return TransitionRejection.GRAPH_SEARCH_BUDGET
                return (
                    total,
                    max(0, len(result_path) - 1),
                    _count_u_turns(result_path, arc_lookup),
                    result_path,
                )
            target_rejections.add(blocked)
        for next_arc in outgoing.get(node_id, []):
            if _blocked_transition(graph, incoming_arc_id, next_arc.arc_id) is not None:
                continue
            next_distance = distance + next_arc.length_m
            if next_distance > parameters.maximum_graph_search_distance_m:
                continue
            next_path = (*path, next_arc.arc_id)
            heapq.heappush(
                queue,
                (
                    next_distance,
                    turns + 1,
                    next_path,
                    next_arc.to_node_id,
                    next_arc.arc_id,
                ),
            )
    if TransitionRejection.FORBIDDEN_TURN in target_rejections:
        return TransitionRejection.FORBIDDEN_TURN
    if TransitionRejection.UNKNOWN_RESTRICTION in target_rejections:
        return TransitionRejection.UNKNOWN_RESTRICTION
    return TransitionRejection.NO_DIRECTED_PATH


def _impossible_transition(
    previous: RoadCandidate,
    current: RoadCandidate,
    *,
    reason: str,
    elapsed_seconds: float,
    observed_displacement_m: float,
    graph_distance_m: float | None = None,
    implied_graph_speed_mps: float | None = None,
    path_arc_ids: tuple[str, ...] = (),
) -> TransitionScore:
    return TransitionScore(
        from_candidate_id=previous.candidate_id,
        to_candidate_id=current.candidate_id,
        possible=False,
        rejection_reason=cast(
            Literal[
                "NON_POSITIVE_ELAPSED_TIME",
                "FORBIDDEN_TURN",
                "UNKNOWN_RESTRICTION",
                "NO_DIRECTED_PATH",
                "GRAPH_SEARCH_BUDGET",
                "IMPLAUSIBLE_ABSOLUTE_SPEED",
            ],
            reason,
        ),
        elapsed_seconds=max(0.0, elapsed_seconds),
        observed_displacement_m=observed_displacement_m,
        graph_distance_m=graph_distance_m,
        implied_graph_speed_mps=implied_graph_speed_mps,
        path_discrepancy_m=None,
        turn_count=None,
        u_turn_count=None,
        path_arc_ids=path_arc_ids,
        path_log_likelihood=None,
        speed_log_likelihood=None,
        turn_log_likelihood=None,
        off_map_log_likelihood=None,
        total_log_likelihood=None,
    )


def score_road_transition(
    graph: DirectedRoadGraph,
    previous_observation: RoadMatchObservation,
    previous: RoadCandidate,
    current_observation: RoadMatchObservation,
    current: RoadCandidate,
    *,
    profile: MapMatchingProfile,
) -> TransitionScore:
    """Score one graph-valid directed transition or return runtime negative infinity."""

    validate_graph_identity(graph)
    validate_matching_graph_authority(graph, profile)
    previous_observation.assert_identity()
    current_observation.assert_identity()
    if (
        previous.observation_id != previous_observation.observation_id
        or current.observation_id != current_observation.observation_id
    ):
        raise ValueError("road candidates do not belong to their observations")
    if previous.graph_id != graph.graph_id or current.graph_id != graph.graph_id:
        raise ValueError("road candidates do not belong to the directed graph")
    _validate_candidate_identity(previous, profile)
    _validate_candidate_identity(current, profile)
    observed_displacement = math.dist(
        previous_observation.position_local_m,
        current_observation.position_local_m,
    )
    elapsed_ns = current_observation.time.difference(previous_observation.time).value_ns
    elapsed_seconds = elapsed_ns / 1_000_000_000.0
    if elapsed_seconds <= 0.0:
        return _impossible_transition(
            previous,
            current,
            reason=TransitionRejection.NON_POSITIVE_ELAPSED_TIME,
            elapsed_seconds=elapsed_seconds,
            observed_displacement_m=observed_displacement,
        )
    parameters = profile.parameter_charter.transition
    places = parameters.score_rounding_decimal_places
    if (
        previous.state == CandidateState.OFF_MAP
        or current.state == CandidateState.OFF_MAP
    ):
        if previous.state == current.state:
            off_map_log = parameters.off_map_stay_log_likelihood
        elif previous.state == CandidateState.OFF_MAP:
            off_map_log = parameters.off_map_exit_log_likelihood
        else:
            off_map_log = parameters.off_map_enter_log_likelihood
        rounded = round(off_map_log, places)
        return TransitionScore(
            from_candidate_id=previous.candidate_id,
            to_candidate_id=current.candidate_id,
            possible=True,
            rejection_reason=None,
            elapsed_seconds=elapsed_seconds,
            observed_displacement_m=observed_displacement,
            graph_distance_m=None,
            implied_graph_speed_mps=None,
            path_discrepancy_m=None,
            turn_count=None,
            u_turn_count=None,
            path_arc_ids=(),
            path_log_likelihood=None,
            speed_log_likelihood=None,
            turn_log_likelihood=None,
            off_map_log_likelihood=rounded,
            total_log_likelihood=rounded,
        )

    route = _directed_route(graph, previous, current, parameters)
    if isinstance(route, str):
        return _impossible_transition(
            previous,
            current,
            reason=route,
            elapsed_seconds=elapsed_seconds,
            observed_displacement_m=observed_displacement,
        )
    graph_distance, turn_count, u_turn_count, path = route
    graph_distance = round(
        graph_distance,
        profile.parameter_charter.candidate.distance_rounding_decimal_places,
    )
    implied_speed = graph_distance / elapsed_seconds
    if implied_speed > parameters.maximum_absolute_speed_mps:
        return _impossible_transition(
            previous,
            current,
            reason=TransitionRejection.IMPLAUSIBLE_ABSOLUTE_SPEED,
            elapsed_seconds=elapsed_seconds,
            observed_displacement_m=observed_displacement,
            graph_distance_m=graph_distance,
            implied_graph_speed_mps=implied_speed,
            path_arc_ids=path,
        )
    discrepancy = abs(graph_distance - observed_displacement)
    path_log = -discrepancy / parameters.path_discrepancy_scale_m
    supported_speed = max(previous_observation.speed_mps, current_observation.speed_mps)
    speed_excess = max(
        0.0,
        implied_speed
        - supported_speed
        - parameters.observed_speed_excess_allowance_mps,
    )
    speed_log = -speed_excess * parameters.speed_excess_penalty_per_mps
    turn_log = -(
        turn_count * parameters.turn_penalty + u_turn_count * parameters.u_turn_penalty
    )
    total = path_log + speed_log + turn_log
    return TransitionScore(
        from_candidate_id=previous.candidate_id,
        to_candidate_id=current.candidate_id,
        possible=True,
        rejection_reason=None,
        elapsed_seconds=elapsed_seconds,
        observed_displacement_m=observed_displacement,
        graph_distance_m=graph_distance,
        implied_graph_speed_mps=round(implied_speed, places),
        path_discrepancy_m=round(discrepancy, places),
        turn_count=turn_count,
        u_turn_count=u_turn_count,
        path_arc_ids=path,
        path_log_likelihood=round(path_log, places),
        speed_log_likelihood=round(speed_log, places),
        turn_log_likelihood=round(turn_log, places),
        off_map_log_likelihood=None,
        total_log_likelihood=round(total, places),
    )


__all__ = [
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
