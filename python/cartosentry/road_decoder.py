"""Deterministic beam-pruned Viterbi road-path decoding."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry import _core
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.road_graph import DirectedRoadGraph, validate_graph_identity
from cartosentry.road_matching import (
    ALGORITHM_BACKEND,
    CandidateState,
    MapMatchingProfile,
    RoadCandidate,
    RoadMatchObservation,
    _generate_road_candidate_batches,
    _native_candidate_payload,
    _native_graph_payload,
    _native_observation_payload,
    validate_matching_graph_authority,
)
from cartosentry.road_matching import (
    PROFILE_IMMUTABLE_SHA256 as MATCHING_PROFILE_IMMUTABLE_SHA256,
)

PROFILE_IMMUTABLE_SHA256 = (
    "78a1c554c2895748b9bef5810772c7788d36c3f01be47cb00accc9dd4c0e5991"
)
PROFILE_FILE_SHA256 = "73ebeb73506a81214eb0f14fb2cbb835c8bef80b4a63e4096402817bafa99a15"
MATCHING_PROFILE_FILE_SHA256 = (
    "b249c83942244b8a014b0946b7621adda7dd005cfb2757b85658b08243ec0760"
)
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


class DecoderAuthorities(StrictModel):
    map_matching_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    map_matching_profile_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    numerical_charter_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class DecoderParameters(StrictModel):
    decoding_mode: Literal["OFFLINE_FULL_WINDOW"]
    beam_width: Annotated[int, Field(ge=2)]
    hypotheses_per_terminal_candidate: Annotated[int, Field(ge=2)]
    beam_score_delta_log_likelihood: Annotated[float, Field(gt=0.0)]
    ambiguity_path_separation_log_likelihood: Annotated[float, Field(ge=0.0)]
    stationary_speed_threshold_mps: Annotated[float, Field(gt=0.0)]
    stationary_position_tolerance_m: Annotated[float, Field(ge=0.0)]
    stationary_minimum_observations: Annotated[int, Field(ge=2)]
    maximum_sequence_observations: Annotated[int, Field(gt=0)]
    score_rounding_decimal_places: Annotated[int, Field(ge=0, le=15)]


class MapDecoderProfile(StrictModel):
    schema_version: Literal[1]
    profile_id: Literal["map-decoder-v1"]
    profile_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_M5_3_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: DecoderAuthorities
    parameter_charter: DecoderParameters

    def assert_identity(self) -> None:
        canonical = self.model_dump(mode="json", exclude={"immutable_sha256"})
        if (
            self.immutable_sha256 != PROFILE_IMMUTABLE_SHA256
            or _canonical_hash(canonical) != self.immutable_sha256
        ):
            raise ValueError("map-decoder profile identity is not pinned")

    @model_validator(mode="after")
    def validate_exact_profile(self) -> Self:
        self.assert_identity()
        if (
            self.authorities.map_matching_profile_immutable_sha256
            != MATCHING_PROFILE_IMMUTABLE_SHA256
        ):
            raise ValueError("map-decoder matching-profile authority is not exact")
        parameters = self.parameter_charter
        if parameters.hypotheses_per_terminal_candidate > parameters.beam_width:
            raise ValueError("terminal hypothesis retention exceeds the beam width")
        return self


def load_map_decoder_profile(path: Path) -> tuple[MapDecoderProfile, str]:
    """Load and self-authenticate the frozen M5.3 decoder charter."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="map-decoder profile",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="map-decoder profile",
        )
    except ManifestBoundaryError as error:
        raise ValueError("map-decoder profile is unavailable or malformed") from error
    if not isinstance(decoded, dict):
        raise ValueError("map-decoder profile must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("map-decoder profile immutable hash is invalid")
    return MapDecoderProfile.model_validate_json(content), hashlib.sha256(
        content
    ).hexdigest()


class MatchConfidence(StrEnum):
    CONFIDENT = "CONFIDENT"
    AMBIGUOUS = "AMBIGUOUS"


class DecodedMatchPoint(StrictModel):
    observation_index: Annotated[int, Field(ge=0)]
    observation: RoadMatchObservation
    candidate: RoadCandidate
    stationary: bool
    confidence: MatchConfidence
    runner_up_candidate_id: Annotated[
        str | None, Field(pattern=r"^candidate-sha256-[0-9a-f]{64}$")
    ]
    path_separation_log_likelihood: Annotated[float, Field(ge=0.0)] | None

    @model_validator(mode="after")
    def validate_ownership(self) -> Self:
        if self.observation.observation_id != self.candidate.observation_id:
            raise ValueError("decoded candidate does not belong to its observation")
        if self.confidence == MatchConfidence.AMBIGUOUS and (
            self.runner_up_candidate_id is None
            or self.path_separation_log_likelihood is None
        ):
            raise ValueError("ambiguous points require runner-up path evidence")
        return self


class MatchedRoadInterval(StrictModel):
    interval_id: Annotated[str, Field(pattern=r"^road-interval-sha256-[0-9a-f]{64}$")]
    start_observation_index: Annotated[int, Field(ge=0)]
    end_observation_index_exclusive: Annotated[int, Field(gt=0)]
    first_time_ns: int
    last_time_ns: int
    observation_count: Annotated[int, Field(gt=0)]
    state: CandidateState
    directed_arc_id: Annotated[
        str | None, Field(pattern=r"^osm-arc-sha256-[0-9a-f]{64}$")
    ]
    start_along_arc_offset_m: Annotated[float, Field(ge=0.0)] | None
    end_along_arc_offset_m: Annotated[float, Field(ge=0.0)] | None
    stationary: bool
    confidence: MatchConfidence
    path_separation_log_likelihood: Annotated[float, Field(ge=0.0)] | None
    usable_distance_m: Annotated[float, Field(ge=0.0)]
    source_observation_ids: tuple[
        Annotated[str, Field(pattern=r"^observation-sha256-[0-9a-f]{64}$")], ...
    ]

    def assert_identity(self) -> None:
        payload = self.model_dump(mode="json", exclude={"interval_id"})
        expected = _interval_id(payload)
        if self.interval_id != expected:
            raise ValueError("matched interval identity is invalid")

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if (
            self.end_observation_index_exclusive - self.start_observation_index
            != self.observation_count
            or len(self.source_observation_ids) != self.observation_count
            or self.last_time_ns < self.first_time_ns
        ):
            raise ValueError("matched interval bounds are inconsistent")
        if self.state == CandidateState.OFF_MAP:
            if (
                self.directed_arc_id is not None
                or self.start_along_arc_offset_m is not None
                or self.end_along_arc_offset_m is not None
                or self.usable_distance_m != 0.0
            ):
                raise ValueError("off-map intervals cannot carry road coverage")
        elif (
            self.directed_arc_id is None
            or self.start_along_arc_offset_m is None
            or self.end_along_arc_offset_m is None
        ):
            raise ValueError("on-road intervals require directed offsets")
        if (
            self.stationary or self.confidence == MatchConfidence.AMBIGUOUS
        ) and self.usable_distance_m != 0.0:
            raise ValueError("stationary or ambiguous intervals cannot add coverage")
        self.assert_identity()
        return self


class DecoderDiagnostics(StrictModel):
    generated_candidate_counts: tuple[Annotated[int, Field(gt=0)], ...]
    retained_hypothesis_counts: tuple[Annotated[int, Field(gt=0)], ...]
    rejected_transition_counts: tuple[Annotated[int, Field(ge=0)], ...]
    pruned_hypothesis_counts: tuple[Annotated[int, Field(ge=0)], ...]


class DecodedRoadPath(StrictModel):
    road_match_id: Annotated[str, Field(pattern=r"^road-match-sha256-[0-9a-f]{64}$")]
    algorithm_backend: Literal["C++20_NATIVE_BATCH_V1"]
    sequence_id: Annotated[str, Field(min_length=1)]
    source_group_id: Annotated[str, Field(min_length=1)]
    partition: Literal["synthetic", "development", "calibration", "final_test"]
    decode_mode: Literal["OFFLINE_FULL_WINDOW"]
    graph_id: Annotated[str, Field(pattern=r"^road-graph-sha256-[0-9a-f]{64}$")]
    map_matching_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    map_matching_profile_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    map_decoder_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    map_decoder_profile_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    best_total_log_likelihood: float
    runner_up_total_log_likelihood: float | None
    path_separation_log_likelihood: Annotated[float, Field(ge=0.0)] | None
    confidence: MatchConfidence
    points: tuple[DecodedMatchPoint, ...]
    intervals: tuple[MatchedRoadInterval, ...]
    runner_up_candidates: tuple[RoadCandidate, ...]
    diagnostics: DecoderDiagnostics

    def assert_identity(self) -> None:
        payload = self.model_dump(mode="json", exclude={"road_match_id"})
        expected = f"road-match-sha256-{_canonical_hash(payload)}"
        if self.road_match_id != expected:
            raise ValueError("decoded road-path identity is invalid")

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        if not self.points:
            raise ValueError("decoded road path cannot be empty")
        if (
            self.map_matching_profile_file_sha256 != MATCHING_PROFILE_FILE_SHA256
            or self.map_matching_profile_immutable_sha256
            != MATCHING_PROFILE_IMMUTABLE_SHA256
            or self.map_decoder_profile_file_sha256 != PROFILE_FILE_SHA256
            or self.map_decoder_profile_immutable_sha256 != PROFILE_IMMUTABLE_SHA256
        ):
            raise ValueError("decoded road path uses a foreign profile authority")
        diagnostic_lengths = (
            len(self.diagnostics.generated_candidate_counts),
            len(self.diagnostics.retained_hypothesis_counts),
            len(self.diagnostics.rejected_transition_counts),
            len(self.diagnostics.pruned_hypothesis_counts),
        )
        if set(diagnostic_lengths) != {len(self.points)}:
            raise ValueError("decoder diagnostics do not cover every observation")
        if any(
            point.observation_index != index
            or point.candidate.graph_id != self.graph_id
            for index, point in enumerate(self.points)
        ):
            raise ValueError("decoded point ordering or graph authority is invalid")
        if len({item.observation.observation_id for item in self.points}) != len(
            self.points
        ) or any(
            current.observation.time.difference(previous.observation.time).value_ns <= 0
            for previous, current in pairwise(self.points)
        ):
            raise ValueError("decoded observations are not unique and time ordered")
        if self.runner_up_total_log_likelihood is None:
            if (
                self.path_separation_log_likelihood is not None
                or self.runner_up_candidates
                or self.confidence != MatchConfidence.CONFIDENT
            ):
                raise ValueError("path separation requires a runner-up path")
        else:
            expected_separation = round(
                self.best_total_log_likelihood - self.runner_up_total_log_likelihood,
                12,
            )
            if (
                self.path_separation_log_likelihood != expected_separation
                or expected_separation < 0.0
                or len(self.runner_up_candidates) != len(self.points)
            ):
                raise ValueError("runner-up path evidence is incomplete")
            expected_confidence = (
                MatchConfidence.AMBIGUOUS
                if expected_separation <= 1.0
                else MatchConfidence.CONFIDENT
            )
            if self.confidence != expected_confidence:
                raise ValueError("sequence confidence contradicts path separation")
        if self.runner_up_candidates:
            for point, runner_up in zip(
                self.points, self.runner_up_candidates, strict=True
            ):
                if runner_up.observation_id != point.observation.observation_id:
                    raise ValueError(
                        "runner-up candidate does not belong to its observation"
                    )
                if runner_up.graph_id != self.graph_id:
                    raise ValueError("runner-up candidate uses a foreign graph")
                differs = runner_up.candidate_id != point.candidate.candidate_id
                expected_runner_id = runner_up.candidate_id if differs else None
                expected_confidence = (
                    MatchConfidence.AMBIGUOUS
                    if differs and self.confidence == MatchConfidence.AMBIGUOUS
                    else MatchConfidence.CONFIDENT
                )
                if (
                    point.runner_up_candidate_id != expected_runner_id
                    or point.path_separation_log_likelihood
                    != (self.path_separation_log_likelihood if differs else None)
                    or point.confidence != expected_confidence
                ):
                    raise ValueError("point runner-up evidence is inconsistent")
        elif any(
            point.runner_up_candidate_id is not None
            or point.path_separation_log_likelihood is not None
            or point.confidence != MatchConfidence.CONFIDENT
            for point in self.points
        ):
            raise ValueError("decoded points claim unavailable runner-up evidence")
        cursor = 0
        for interval in self.intervals:
            if (
                interval.start_observation_index != cursor
                or interval.end_observation_index_exclusive > len(self.points)
            ):
                raise ValueError("matched intervals do not partition the path")
            end_index = interval.end_observation_index_exclusive
            support = self.points[interval.start_observation_index : end_index]
            first = support[0]
            last = support[-1]
            support_keys = {
                (
                    item.candidate.state,
                    item.candidate.directed_arc_id,
                    item.stationary,
                    item.confidence,
                )
                for item in support
            }
            usable_distance = 0.0
            if (
                first.candidate.state == CandidateState.ON_ROAD
                and not first.stationary
                and first.confidence == MatchConfidence.CONFIDENT
            ):
                usable_distance = round(
                    sum(
                        abs(
                            cast(float, right.candidate.along_arc_offset_m)
                            - cast(float, left.candidate.along_arc_offset_m)
                        )
                        for left, right in pairwise(support)
                    ),
                    6,
                )
            separations = tuple(
                item.path_separation_log_likelihood
                for item in support
                if item.path_separation_log_likelihood is not None
            )
            if (
                len(support_keys) != 1
                or interval.state != first.candidate.state
                or interval.directed_arc_id != first.candidate.directed_arc_id
                or interval.stationary != first.stationary
                or interval.confidence != first.confidence
                or interval.first_time_ns != first.observation.time.value_ns
                or interval.last_time_ns != last.observation.time.value_ns
                or interval.start_along_arc_offset_m
                != first.candidate.along_arc_offset_m
                or interval.end_along_arc_offset_m != last.candidate.along_arc_offset_m
                or interval.source_observation_ids
                != tuple(item.observation.observation_id for item in support)
                or interval.usable_distance_m != usable_distance
                or interval.path_separation_log_likelihood
                != (min(separations) if separations else None)
            ):
                raise ValueError("matched interval evidence is inconsistent")
            cursor = interval.end_observation_index_exclusive
        if cursor != len(self.points):
            raise ValueError("matched intervals do not cover the path")
        self.assert_identity()
        return self


def _interval_id(payload: dict[str, object]) -> str:
    return f"road-interval-sha256-{_canonical_hash(payload)}"


def decode_road_path(
    graph: DirectedRoadGraph,
    observations: tuple[RoadMatchObservation, ...],
    *,
    sequence_id: str,
    source_group_id: str,
    partition: Literal["synthetic", "development", "calibration", "final_test"],
    matching_profile: MapMatchingProfile,
    matching_profile_file_sha256: str,
    decoder_profile: MapDecoderProfile,
    decoder_profile_file_sha256: str,
) -> DecodedRoadPath:
    """Decode native best and runner-up complete offline paths."""

    validate_graph_identity(graph)
    validate_matching_graph_authority(graph, matching_profile)
    decoder_profile.assert_identity()
    authorities = decoder_profile.authorities
    if (
        matching_profile_file_sha256 != authorities.map_matching_profile_file_sha256
        or matching_profile.immutable_sha256
        != authorities.map_matching_profile_immutable_sha256
    ):
        raise ValueError("map decoder uses a foreign matching-profile authority")
    if decoder_profile_file_sha256 != PROFILE_FILE_SHA256:
        raise ValueError("map decoder uses a foreign decoder-profile authority")
    parameters = decoder_profile.parameter_charter
    if not observations:
        raise ValueError("road decoding requires at least one observation")
    if len(observations) > parameters.maximum_sequence_observations:
        raise ValueError("road decoding exceeds the frozen observation budget")
    if len({item.observation_id for item in observations}) != len(observations):
        raise ValueError("road decoding observations must have unique identities")
    for previous, current in pairwise(observations):
        if current.time.difference(previous.time).value_ns <= 0:
            raise ValueError("road decoding observations must be strictly time ordered")

    candidate_sets = _generate_road_candidate_batches(
        graph, observations, profile=matching_profile
    )
    matching_parameters = matching_profile.parameter_charter
    raw = _core.decode_road_candidate_batches(
        _native_graph_payload(graph),
        [_native_observation_payload(item) for item in observations],
        [
            [
                _native_candidate_payload(
                    graph, candidate, observation_index=observation_index
                )
                for candidate in candidates
            ]
            for observation_index, candidates in enumerate(candidate_sets)
        ],
        matching_parameters.candidate.model_dump(mode="python"),
        matching_parameters.transition.model_dump(mode="python"),
        parameters.model_dump(mode="python"),
    )
    best_indices = tuple(cast(list[int], raw["best_candidate_indices"]))
    runner_indices = tuple(cast(list[int], raw["runner_up_candidate_indices"]))
    point_ambiguous = tuple(cast(list[bool], raw["point_ambiguous"]))
    stationary = tuple(cast(list[bool], raw["stationary"]))
    if (
        len(best_indices) != len(observations)
        or len(point_ambiguous) != len(observations)
        or len(stationary) != len(observations)
        or (runner_indices and len(runner_indices) != len(observations))
        or any(
            index < 0 or index >= len(candidate_sets[observation_index])
            for observation_index, index in enumerate(best_indices)
        )
        or any(
            index < 0 or index >= len(candidate_sets[observation_index])
            for observation_index, index in enumerate(runner_indices)
        )
    ):
        raise ValueError("native map decoder returned invalid candidate indices")
    runner_up_candidates = (
        tuple(
            candidate_sets[index][runner_indices[index]]
            for index in range(len(observations))
        )
        if runner_indices
        else ()
    )
    separation = cast(float | None, raw["path_separation_log_likelihood"])
    sequence_confidence = (
        MatchConfidence.AMBIGUOUS
        if cast(bool, raw["ambiguous"])
        else MatchConfidence.CONFIDENT
    )
    expected_ambiguous = (
        separation is not None
        and separation <= parameters.ambiguity_path_separation_log_likelihood
    )
    if (sequence_confidence == MatchConfidence.AMBIGUOUS) != expected_ambiguous:
        raise ValueError("native map decoder confidence contradicts its separation")
    points: list[DecodedMatchPoint] = []
    for index, observation in enumerate(observations):
        candidate = candidate_sets[index][best_indices[index]]
        runner = runner_up_candidates[index] if runner_up_candidates else None
        differs = runner is not None and runner.candidate_id != candidate.candidate_id
        if point_ambiguous[index] != (expected_ambiguous and differs):
            raise ValueError("native map decoder point confidence is invalid")
        points.append(
            DecodedMatchPoint(
                observation_index=index,
                observation=observation,
                candidate=candidate,
                stationary=stationary[index],
                confidence=(
                    MatchConfidence.AMBIGUOUS
                    if point_ambiguous[index]
                    else MatchConfidence.CONFIDENT
                ),
                runner_up_candidate_id=(
                    runner.candidate_id if runner is not None and differs else None
                ),
                path_separation_log_likelihood=separation if differs else None,
            )
        )
    point_tuple = tuple(points)
    intervals: list[MatchedRoadInterval] = []
    raw_intervals = cast(list[dict[str, Any]], raw["intervals"])
    for native_interval in raw_intervals:
        start = cast(int, native_interval["start_observation_index"])
        end = cast(int, native_interval["end_observation_index_exclusive"])
        if not 0 <= start < end <= len(point_tuple):
            raise ValueError("native map decoder interval bounds are invalid")
        group = point_tuple[start:end]
        first = group[0]
        interval_payload: dict[str, object] = {
            "start_observation_index": start,
            "end_observation_index_exclusive": end,
            "first_time_ns": group[0].observation.time.value_ns,
            "last_time_ns": group[-1].observation.time.value_ns,
            "observation_count": len(group),
            "state": first.candidate.state,
            "directed_arc_id": first.candidate.directed_arc_id,
            "start_along_arc_offset_m": first.candidate.along_arc_offset_m,
            "end_along_arc_offset_m": group[-1].candidate.along_arc_offset_m,
            "stationary": first.stationary,
            "confidence": first.confidence,
            "path_separation_log_likelihood": native_interval[
                "path_separation_log_likelihood"
            ],
            "usable_distance_m": native_interval["usable_distance_m"],
            "source_observation_ids": tuple(
                item.observation.observation_id for item in group
            ),
        }
        intervals.append(
            MatchedRoadInterval.model_validate(
                {"interval_id": _interval_id(interval_payload), **interval_payload}
            )
        )
    diagnostics_raw = cast(dict[str, list[int]], raw["diagnostics"])
    diagnostics = DecoderDiagnostics(
        generated_candidate_counts=tuple(diagnostics_raw["generated_candidate_counts"]),
        retained_hypothesis_counts=tuple(diagnostics_raw["retained_hypothesis_counts"]),
        rejected_transition_counts=tuple(diagnostics_raw["rejected_transition_counts"]),
        pruned_hypothesis_counts=tuple(diagnostics_raw["pruned_hypothesis_counts"]),
    )
    best_score = cast(float, raw["best_total_log_likelihood"])
    runner_score = cast(float | None, raw["runner_up_total_log_likelihood"])
    payload: dict[str, object] = {
        "algorithm_backend": ALGORITHM_BACKEND,
        "sequence_id": sequence_id,
        "source_group_id": source_group_id,
        "partition": partition,
        "decode_mode": parameters.decoding_mode,
        "graph_id": graph.graph_id,
        "map_matching_profile_file_sha256": matching_profile_file_sha256,
        "map_matching_profile_immutable_sha256": matching_profile.immutable_sha256,
        "map_decoder_profile_file_sha256": decoder_profile_file_sha256,
        "map_decoder_profile_immutable_sha256": decoder_profile.immutable_sha256,
        "best_total_log_likelihood": best_score,
        "runner_up_total_log_likelihood": runner_score,
        "path_separation_log_likelihood": separation,
        "confidence": sequence_confidence,
        "points": tuple(item.model_dump(mode="json") for item in point_tuple),
        "intervals": tuple(item.model_dump(mode="json") for item in intervals),
        "runner_up_candidates": tuple(
            item.model_dump(mode="json") for item in runner_up_candidates
        ),
        "diagnostics": diagnostics.model_dump(mode="json"),
    }
    return DecodedRoadPath(
        road_match_id=f"road-match-sha256-{_canonical_hash(payload)}",
        algorithm_backend=ALGORITHM_BACKEND,
        sequence_id=sequence_id,
        source_group_id=source_group_id,
        partition=partition,
        decode_mode=parameters.decoding_mode,
        graph_id=graph.graph_id,
        map_matching_profile_file_sha256=matching_profile_file_sha256,
        map_matching_profile_immutable_sha256=matching_profile.immutable_sha256,
        map_decoder_profile_file_sha256=decoder_profile_file_sha256,
        map_decoder_profile_immutable_sha256=decoder_profile.immutable_sha256,
        best_total_log_likelihood=best_score,
        runner_up_total_log_likelihood=runner_score,
        path_separation_log_likelihood=separation,
        confidence=sequence_confidence,
        points=point_tuple,
        intervals=tuple(intervals),
        runner_up_candidates=runner_up_candidates,
        diagnostics=diagnostics,
    )


__all__ = [
    "MATCHING_PROFILE_FILE_SHA256",
    "PROFILE_FILE_SHA256",
    "PROFILE_IMMUTABLE_SHA256",
    "DecodedMatchPoint",
    "DecodedRoadPath",
    "MapDecoderProfile",
    "MatchConfidence",
    "MatchedRoadInterval",
    "decode_road_path",
    "load_map_decoder_profile",
]
