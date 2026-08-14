"""Frozen M5.3 synthetic road-matching qualification."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry.contracts import (
    GlobalCoordinate,
    TimeEpoch,
    TimePoint,
    TimeReference,
    VerticalDatum,
)
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.road_decoder import (
    DecodedRoadPath,
    MapDecoderProfile,
    MatchConfidence,
    decode_road_path,
    load_map_decoder_profile,
)
from cartosentry.road_graph import (
    ArcDirection,
    DirectedRoadArc,
    DirectedRoadGraph,
    GraphImportProfile,
    GraphSourceKind,
    Wgs84E7,
    import_osm_road_graph,
    load_graph_import_profile,
)
from cartosentry.road_matching import (
    ALGORITHM_BACKEND,
    CandidateState,
    MapMatchingProfile,
    RoadMatchObservation,
    load_map_matching_profile,
    make_road_match_observation,
)

GATE_IMMUTABLE_SHA256 = (
    "b9209f4b0656ce178b41f4958202aaca3bc15dbc7278c90b7afe3de71cb32e31"
)
TRUTH_IMMUTABLE_SHA256 = (
    "0f6050205d2715fc2dea264f04af3794f6e378e7e3f866893f43cfc60e04f8f4"
)
MAXIMUM_GATE_BYTES = 256 * 1024
MAXIMUM_TRUTH_BYTES = 1024 * 1024
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
    numerical_charter_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    map_matching_truth_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    fixture_object_key: Literal["tests/fixtures/road_graphs/topology_v1.osm"]
    fixture_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class GateThresholds(StrictModel):
    synthetic_directed_arc_accuracy_minimum: Annotated[float, Field(ge=0.95, le=0.95)]
    synthetic_off_map_f1_minimum: Annotated[float, Field(ge=0.90, le=0.90)]
    tiny_path_mismatch_count_maximum: Literal[0]


class GateStatistics(StrictModel):
    bootstrap_unit: Literal["synthetic_family_id"]
    bootstrap_seed: Literal[2026081401]
    bootstrap_replicates: Literal[10000]
    confidence_level: Annotated[float, Field(ge=0.95, le=0.95)]
    minimum_independent_clusters: Literal[12]
    degenerate_resample_behavior: Literal["FAIL_CONFIRMATORY_GATE"]


class MapMatchingGate(StrictModel):
    schema_version: Literal[1]
    gate_id: Literal["m5.3-synthetic-map-matching-v1"]
    gate_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_M5_3_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: GateAuthorities
    required_scenario_ids: tuple[str, ...]
    exact_path_scenario_ids: tuple[str, ...]
    ambiguity_scenario_ids: tuple[str, ...]
    confident_control_scenario_ids: tuple[str, ...]
    stationary_scenario_ids: tuple[str, ...]
    thresholds: GateThresholds
    statistics: GateStatistics

    @model_validator(mode="after")
    def validate_exact_sets(self) -> Self:
        if self.immutable_sha256 != GATE_IMMUTABLE_SHA256:
            raise ValueError("M5.3 map-matching gate identity is not pinned")
        collections = (
            self.required_scenario_ids,
            self.exact_path_scenario_ids,
            self.ambiguity_scenario_ids,
            self.confident_control_scenario_ids,
            self.stationary_scenario_ids,
        )
        if any(len(items) != len(set(items)) for items in collections):
            raise ValueError("M5.3 scenario collections must be unique")
        required = set(self.required_scenario_ids)
        if any(not set(items).issubset(required) for items in collections[1:]):
            raise ValueError("M5.3 scenario subsets are inconsistent")
        if not self.confident_control_scenario_ids or not self.stationary_scenario_ids:
            raise ValueError("M5.3 control scenario sets cannot be empty")
        if set(self.ambiguity_scenario_ids) & set(self.confident_control_scenario_ids):
            raise ValueError("M5.3 confidence scenario sets overlap")
        if (
            set(self.ambiguity_scenario_ids) | set(self.confident_control_scenario_ids)
            != required
        ):
            raise ValueError("M5.3 confidence scenario sets are not exhaustive")
        return self


def load_map_matching_gate(path: Path) -> tuple[MapMatchingGate, str]:
    """Load and authenticate the frozen M5.3 qualification gate."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="M5.3 map-matching gate",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="M5.3 map-matching gate",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "M5.3 map-matching gate is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("M5.3 map-matching gate must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("M5.3 map-matching gate immutable hash is invalid")
    return MapMatchingGate.model_validate_json(content), hashlib.sha256(
        content
    ).hexdigest()


class PathRun(StrictModel):
    directed_arc_id: str
    observation_count: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_arc_token(self) -> Self:
        if self.directed_arc_id != "OFF_MAP" and not (
            self.directed_arc_id.startswith("osm-arc-sha256-")
            and len(self.directed_arc_id) == len("osm-arc-sha256-") + 64
            and all(
                character in "0123456789abcdef"
                for character in self.directed_arc_id[len("osm-arc-sha256-") :]
            )
        ):
            raise ValueError("M5.3 truth has an invalid directed-arc token")
        return self


class MissingPathSpec(StrictModel):
    start_wgs84_e7: Wgs84E7
    end_wgs84_e7: Wgs84E7
    observation_count: Literal[10]
    speed_mps: Annotated[float, Field(gt=0.0)]


class ScenarioTruth(StrictModel):
    scenario_id: Annotated[str, Field(min_length=1)]
    synthetic_family_id: Annotated[str, Field(min_length=1)]
    suite: Literal["PRIMARY_TOPOLOGY", "MISSING_EDGE_TOPOLOGY"]
    fixture_object_key: Annotated[str, Field(min_length=1)]
    fixture_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    observation_spec_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_path_runs: tuple[PathRun, ...]
    expected_confidence: MatchConfidence
    expected_stationary: bool
    exact_path_gate_eligible: bool
    directed_accuracy_eligible: bool
    missing_path: MissingPathSpec | None

    @model_validator(mode="after")
    def validate_suite_contract(self) -> Self:
        if not self.expected_path_runs:
            raise ValueError("M5.3 truth paths cannot be empty")
        if (self.suite == "MISSING_EDGE_TOPOLOGY") != (self.missing_path is not None):
            raise ValueError("M5.3 missing-edge truth requires a path specification")
        if self.suite == "MISSING_EDGE_TOPOLOGY" and (
            any(item.directed_arc_id != "OFF_MAP" for item in self.expected_path_runs)
            or self.directed_accuracy_eligible
        ):
            raise ValueError("M5.3 missing-edge truth must remain off-map")
        return self


class MapMatchingTruth(StrictModel):
    schema_version: Literal[1]
    truth_id: Literal["m5.3-map-matching-truth-v1"]
    truth_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_M5_3_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    scenarios: tuple[ScenarioTruth, ...]

    @model_validator(mode="after")
    def validate_truth(self) -> Self:
        if self.immutable_sha256 != TRUTH_IMMUTABLE_SHA256:
            raise ValueError("M5.3 map-matching truth identity is not pinned")
        scenario_ids = tuple(item.scenario_id for item in self.scenarios)
        family_ids = tuple(item.synthetic_family_id for item in self.scenarios)
        if (
            scenario_ids != tuple(sorted(scenario_ids))
            or len(scenario_ids) != len(set(scenario_ids))
            or len(family_ids) != len(set(family_ids))
        ):
            raise ValueError("M5.3 truth scenario and family identities are not exact")
        return self


def load_map_matching_truth(path: Path) -> tuple[MapMatchingTruth, str]:
    """Load and authenticate the independent M5.3 scenario truth."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_TRUTH_BYTES,
            context="M5.3 map-matching truth",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_TRUTH_BYTES,
            context="M5.3 map-matching truth",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "M5.3 map-matching truth is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("M5.3 map-matching truth must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("M5.3 map-matching truth immutable hash is invalid")
    return MapMatchingTruth.model_validate_json(content), hashlib.sha256(
        content
    ).hexdigest()


@dataclass(frozen=True)
class _ObservationSpec:
    arc: DirectedRoadArc | None
    fraction: float
    lateral_m: float = 0.0
    speed_mps: float = 10.0
    heading_rad: float | None = None
    uncertainty_m: float | None = None
    remote_position_m: tuple[float, float] | None = None


@dataclass(frozen=True)
class _Scenario:
    scenario_id: str
    synthetic_family_id: str
    graph: DirectedRoadGraph
    observations: tuple[RoadMatchObservation, ...]
    observation_spec_sha256: str
    expected_arc_ids: tuple[str | None, ...]
    exact_path: bool
    expected_confidence: MatchConfidence
    expected_stationary: bool
    directed_accuracy_eligible: bool


def _spec_hash(specs: tuple[_ObservationSpec, ...]) -> str:
    return _canonical_hash(
        tuple(
            {
                "directed_arc_id": item.arc.arc_id if item.arc is not None else None,
                "fraction": item.fraction,
                "lateral_m": item.lateral_m,
                "speed_mps": item.speed_mps,
                "heading_rad": item.heading_rad,
                "uncertainty_m": item.uncertainty_m,
                "remote_position_m": item.remote_position_m,
            }
            for item in specs
        )
    )


def _arc(
    graph: DirectedRoadGraph, way_id: int, direction: ArcDirection
) -> DirectedRoadArc:
    return next(
        item
        for item in graph.arcs
        if item.source_way_id == way_id and item.direction == direction
    )


def _point_and_heading(
    arc: DirectedRoadArc, fraction: float, lateral_m: float
) -> tuple[tuple[float, float], float]:
    segments: list[tuple[tuple[float, float], tuple[float, float], float]] = []
    total = 0.0
    for left, right in pairwise(arc.geometry_local_m):
        start = (left[0], left[1])
        end = (right[0], right[1])
        length = math.dist(start, end)
        if length > 0.0:
            segments.append((start, end, length))
            total += length
    if not segments or not 0.0 <= fraction <= 1.0:
        raise ValueError("synthetic arc sampling requires valid geometry and fraction")
    remaining = fraction * total
    start, end, segment_length = segments[-1]
    for candidate_start, candidate_end, candidate_length in segments:
        start, end, segment_length = (
            candidate_start,
            candidate_end,
            candidate_length,
        )
        if remaining <= candidate_length:
            break
        remaining -= candidate_length
    ratio = min(1.0, remaining / segment_length)
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    normal = (-delta_y / segment_length, delta_x / segment_length)
    return (
        (
            start[0] + ratio * delta_x + lateral_m * normal[0],
            start[1] + ratio * delta_y + lateral_m * normal[1],
        ),
        round(math.atan2(delta_y, delta_x), 12),
    )


def _observations(
    graph: DirectedRoadGraph,
    scenario_id: str,
    specs: tuple[_ObservationSpec, ...],
) -> tuple[RoadMatchObservation, ...]:
    result: list[RoadMatchObservation] = []
    for index, spec in enumerate(specs):
        if spec.remote_position_m is not None:
            position = spec.remote_position_m
            tangent = (
                _point_and_heading(spec.arc, spec.fraction, 0.0)[1]
                if spec.arc is not None
                else 0.0
            )
        elif spec.arc is None:
            raise ValueError("off-map synthetic observations require positions")
        else:
            position, tangent = _point_and_heading(
                spec.arc, spec.fraction, spec.lateral_m
            )
        heading = spec.heading_rad if spec.heading_rad is not None else tangent
        result.append(
            make_road_match_observation(
                time=TimePoint.from_decimal_seconds(
                    str(1 + index * 10),
                    source_key=f"synthetic/map-matching/{scenario_id}",
                    field="time_seconds",
                    epoch=TimeEpoch.UNIX_UTC,
                    clock_id="synthetic-map-matching-clock",
                    reference=TimeReference.SAMPLE,
                ),
                local_frame_id=graph.local_frame.frame.frame_id,
                position_local_m=position,
                heading_rad=heading,
                speed_mps=spec.speed_mps,
                horizontal_uncertainty_m=spec.uncertainty_m,
                horizontal_uncertainty_basis=(
                    "DECLARED_TRUSTWORTHY" if spec.uncertainty_m is not None else None
                ),
            )
        )
    return tuple(result)


def _single_arc_specs(
    arc: DirectedRoadArc,
    *,
    count: int = 10,
    lateral_offsets: tuple[float, ...] | None = None,
    uncertainty_m: float | None = None,
) -> tuple[_ObservationSpec, ...]:
    offsets = lateral_offsets or (0.0,) * count
    if len(offsets) != count:
        raise ValueError("synthetic lateral offsets do not match observation count")
    return tuple(
        _ObservationSpec(
            arc=arc,
            fraction=0.05 + 0.90 * index / (count - 1),
            lateral_m=offsets[index],
            uncertainty_m=uncertainty_m,
        )
        for index in range(count)
    )


def _chain_specs(arcs: tuple[DirectedRoadArc, ...]) -> tuple[_ObservationSpec, ...]:
    if len(arcs) != 2:
        raise ValueError("synthetic two-arc chain requires exactly two arcs")
    return tuple(
        _ObservationSpec(
            arc=(arcs[0] if index < 5 else arcs[1]),
            fraction=0.1 + 0.2 * (index if index < 5 else index - 5),
        )
        for index in range(10)
    )


def _ordered_roundabout(graph: DirectedRoadGraph) -> tuple[DirectedRoadArc, ...]:
    arcs = tuple(item for item in graph.arcs if item.source_way_id == 130)
    if len(arcs) != 2:
        raise ValueError("roundabout fixture does not have two directed arcs")
    first = min(arcs, key=lambda item: item.arc_id)
    second = next(item for item in arcs if item.from_node_id == first.to_node_id)
    return first, second


def _scenario_suite(graph: DirectedRoadGraph) -> tuple[_Scenario, ...]:
    scenarios: list[_Scenario] = []

    def add(
        scenario_id: str,
        specs: tuple[_ObservationSpec, ...],
        *,
        exact_path: bool = True,
        expected_confidence: MatchConfidence = MatchConfidence.CONFIDENT,
        expected_stationary: bool = False,
        directed_accuracy_eligible: bool = True,
    ) -> None:
        scenarios.append(
            _Scenario(
                scenario_id=scenario_id,
                synthetic_family_id=f"synthetic-map-{scenario_id}",
                graph=graph,
                observations=_observations(graph, scenario_id, specs),
                observation_spec_sha256=_spec_hash(specs),
                expected_arc_ids=tuple(
                    item.arc.arc_id if item.arc is not None else None for item in specs
                ),
                exact_path=exact_path,
                expected_confidence=expected_confidence,
                expected_stationary=expected_stationary,
                directed_accuracy_eligible=directed_accuracy_eligible,
            )
        )

    add(
        "forward-oneway",
        _single_arc_specs(_arc(graph, 100, ArcDirection.FORWARD)),
    )
    add(
        "reverse-oneway",
        _single_arc_specs(_arc(graph, 101, ArcDirection.REVERSE)),
    )
    add(
        "divided-east",
        _single_arc_specs(_arc(graph, 110, ArcDirection.FORWARD)),
    )
    add(
        "divided-west",
        _single_arc_specs(_arc(graph, 111, ArcDirection.FORWARD)),
    )
    add(
        "ramp-merge",
        _chain_specs(
            (
                _arc(graph, 120, ArcDirection.FORWARD),
                _arc(graph, 121, ArcDirection.FORWARD),
            )
        ),
    )
    add(
        "grade-separated-overpass",
        _single_arc_specs(_arc(graph, 150, ArcDirection.FORWARD)),
    )
    add(
        "grade-separated-underpass",
        _single_arc_specs(_arc(graph, 151, ArcDirection.FORWARD)),
    )
    add(
        "parallel-south-control",
        _single_arc_specs(_arc(graph, 140, ArcDirection.FORWARD)),
    )
    roundabout = _ordered_roundabout(graph)
    add("roundabout", _chain_specs(roundabout))
    add("one-way-loop", _chain_specs((roundabout[1], roundabout[0])))
    add(
        "u-turn",
        _chain_specs(
            (
                _arc(graph, 140, ArcDirection.FORWARD),
                _arc(graph, 140, ArcDirection.REVERSE),
            )
        ),
    )
    sparse_arc = _arc(graph, 140, ArcDirection.FORWARD)
    add(
        "sparse-observations",
        (
            _ObservationSpec(arc=sparse_arc, fraction=0.05),
            _ObservationSpec(arc=sparse_arc, fraction=0.95),
        ),
    )
    add(
        "gps-noise",
        _single_arc_specs(
            sparse_arc,
            lateral_offsets=(-3.0, 2.0, -1.0, 3.0, -2.0, 1.0, -2.5, 2.5, -1.5, 0.5),
            uncertainty_m=3.0,
        ),
    )
    north = _arc(graph, 141, ArcDirection.FORWARD)
    north_position, north_heading = _point_and_heading(north, 0.6, 8.0)
    add(
        "gps-boundary-stress",
        (
            _ObservationSpec(arc=sparse_arc, fraction=0.5, lateral_m=20.0),
            _ObservationSpec(
                arc=None,
                fraction=0.0,
                heading_rad=north_heading,
                remote_position_m=north_position,
            ),
        ),
        exact_path=False,
    )

    parallel_specs: list[_ObservationSpec] = []
    for index in range(10):
        fraction = 0.05 + 0.90 * index / 9
        south_position, south_heading = _point_and_heading(sparse_arc, fraction, 0.0)
        north_position, _ = _point_and_heading(north, fraction, 0.0)
        midpoint = (
            (south_position[0] + north_position[0]) / 2.0,
            (south_position[1] + north_position[1]) / 2.0,
        )
        parallel_specs.append(
            _ObservationSpec(
                arc=sparse_arc,
                fraction=fraction,
                speed_mps=10.0,
                heading_rad=south_heading,
                uncertainty_m=5.0,
                remote_position_m=midpoint,
            )
        )
    add(
        "parallel-ambiguous",
        tuple(parallel_specs),
        exact_path=False,
        expected_confidence=MatchConfidence.AMBIGUOUS,
        directed_accuracy_eligible=False,
    )

    stopped_specs = tuple(
        _ObservationSpec(
            arc=sparse_arc,
            fraction=0.5,
            lateral_m=0.05 * (index % 2),
            speed_mps=0.0,
            heading_rad=-2.0 + 0.4 * index,
        )
        for index in range(10)
    )
    add(
        "stopped-vehicle",
        stopped_specs,
        exact_path=False,
        expected_confidence=MatchConfidence.AMBIGUOUS,
        expected_stationary=True,
        directed_accuracy_eligible=False,
    )
    return tuple(sorted(scenarios, key=lambda item: item.scenario_id))


def _expand_path(runs: tuple[PathRun, ...]) -> tuple[str | None, ...]:
    return tuple(
        None if run.directed_arc_id == "OFF_MAP" else run.directed_arc_id
        for run in runs
        for _ in range(run.observation_count)
    )


def _missing_edge_scenario(
    scenario_truth: ScenarioTruth,
    *,
    graph_profile: GraphImportProfile,
    graph_profile_file_sha256: str,
    fixture_directory: Path,
) -> _Scenario:
    missing_path = scenario_truth.missing_path
    if missing_path is None:
        raise ValueError("missing-edge scenario has no frozen missing path")
    fixture_path = fixture_directory / Path(scenario_truth.fixture_object_key).name
    graph = import_osm_road_graph(
        fixture_path,
        profile=graph_profile,
        profile_file_sha256=graph_profile_file_sha256,
        source_object_key=scenario_truth.fixture_object_key,
        expected_source_sha256=scenario_truth.fixture_sha256,
        source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
    )
    origin = graph.local_frame.local_origin()

    def local(coordinate: Wgs84E7) -> tuple[float, float]:
        result = origin.to_local(
            GlobalCoordinate(
                latitude_deg=coordinate.latitude_e7 / 10_000_000.0,
                longitude_deg=coordinate.longitude_e7 / 10_000_000.0,
                altitude_m=0.0,
                vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
            )
        )
        return (round(result.position_m[0], 7), round(result.position_m[1], 7))

    start = local(missing_path.start_wgs84_e7)
    end = local(missing_path.end_wgs84_e7)
    heading = round(math.atan2(end[1] - start[1], end[0] - start[0]), 12)
    specs = tuple(
        _ObservationSpec(
            arc=None,
            fraction=0.0,
            speed_mps=missing_path.speed_mps,
            heading_rad=heading,
            remote_position_m=(
                round(start[0] + (end[0] - start[0]) * fraction, 7),
                round(start[1] + (end[1] - start[1]) * fraction, 7),
            ),
        )
        for fraction in (
            0.05 + 0.90 * index / (missing_path.observation_count - 1)
            for index in range(missing_path.observation_count)
        )
    )
    return _Scenario(
        scenario_id=scenario_truth.scenario_id,
        synthetic_family_id=scenario_truth.synthetic_family_id,
        graph=graph,
        observations=_observations(graph, scenario_truth.scenario_id, specs),
        observation_spec_sha256=_canonical_hash(missing_path.model_dump(mode="json")),
        expected_arc_ids=_expand_path(scenario_truth.expected_path_runs),
        exact_path=scenario_truth.exact_path_gate_eligible,
        expected_confidence=scenario_truth.expected_confidence,
        expected_stationary=scenario_truth.expected_stationary,
        directed_accuracy_eligible=scenario_truth.directed_accuracy_eligible,
    )


def _bind_primary_truth(
    scenarios: tuple[_Scenario, ...],
    truth_by_id: dict[str, ScenarioTruth],
) -> None:
    for scenario in scenarios:
        expected = truth_by_id.get(scenario.scenario_id)
        if expected is None or expected.suite != "PRIMARY_TOPOLOGY":
            raise ValueError("primary synthetic scenario has no independent truth")
        if (
            scenario.synthetic_family_id != expected.synthetic_family_id
            or scenario.graph.source.source_object_key != expected.fixture_object_key
            or scenario.graph.source.source_sha256 != expected.fixture_sha256
            or scenario.observation_spec_sha256 != expected.observation_spec_sha256
            or scenario.expected_arc_ids != _expand_path(expected.expected_path_runs)
            or scenario.exact_path != expected.exact_path_gate_eligible
            or scenario.expected_confidence != expected.expected_confidence
            or scenario.expected_stationary != expected.expected_stationary
            or scenario.directed_accuracy_eligible
            != expected.directed_accuracy_eligible
        ):
            raise ValueError(
                "primary scenario implementation diverges from frozen truth"
            )


def _bind_missing_truth(
    scenarios: tuple[_Scenario, ...],
    truth_by_id: dict[str, ScenarioTruth],
) -> None:
    for scenario in scenarios:
        expected = truth_by_id.get(scenario.scenario_id)
        if expected is None or expected.suite != "MISSING_EDGE_TOPOLOGY":
            raise ValueError("missing-edge scenario has no independent truth")
        if (
            scenario.synthetic_family_id != expected.synthetic_family_id
            or scenario.graph.source.source_object_key != expected.fixture_object_key
            or scenario.graph.source.source_sha256 != expected.fixture_sha256
            or scenario.observation_spec_sha256 != expected.observation_spec_sha256
            or scenario.expected_arc_ids != _expand_path(expected.expected_path_runs)
            or scenario.exact_path != expected.exact_path_gate_eligible
            or scenario.expected_confidence != expected.expected_confidence
            or scenario.expected_stationary != expected.expected_stationary
            or scenario.directed_accuracy_eligible
            != expected.directed_accuracy_eligible
        ):
            raise ValueError(
                "missing-edge scenario implementation diverges from frozen truth"
            )


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("bootstrap quantile requires observations")
    location = probability * (len(ordered) - 1)
    lower = int(location)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = location - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _bootstrap(
    clusters: tuple[dict[str, int], ...],
    estimator: Callable[[tuple[dict[str, int], ...]], float],
    *,
    seed: int,
    replicates: int,
    confidence_level: float,
) -> dict[str, object]:
    point = estimator(clusters)
    generator = random.Random(seed)
    estimates: list[float] = []
    degenerate_resamples = 0
    for _ in range(replicates):
        resample = tuple(clusters[generator.randrange(len(clusters))] for _ in clusters)
        try:
            estimates.append(estimator(resample))
        except ValueError:
            degenerate_resamples += 1
    if not estimates:
        raise ValueError("every synthetic map bootstrap resample is degenerate")
    alpha = 1.0 - confidence_level
    lower = _quantile(estimates, alpha)
    upper = _quantile(estimates, confidence_level)
    return {
        "point_estimate": point,
        "one_sided_lower_95": lower,
        "one_sided_upper_95": upper,
        "two_sided_lower_95": _quantile(estimates, alpha / 2.0),
        "two_sided_upper_95": _quantile(estimates, 1.0 - alpha / 2.0),
        "degenerate_resample_count": degenerate_resamples,
        "interval_degenerate": min(estimates) == max(estimates),
    }


def _arc_accuracy(clusters: tuple[dict[str, int], ...]) -> float:
    total = sum(item["arc_total"] for item in clusters)
    if total <= 0:
        raise ValueError("synthetic directed-arc denominator is empty")
    return sum(item["arc_correct"] for item in clusters) / total


def _off_map_f1(clusters: tuple[dict[str, int], ...]) -> float:
    true_positive = sum(item["off_map_true_positive"] for item in clusters)
    false_positive = sum(item["off_map_false_positive"] for item in clusters)
    false_negative = sum(item["off_map_false_negative"] for item in clusters)
    denominator = 2 * true_positive + false_positive + false_negative
    if denominator <= 0:
        raise ValueError("synthetic off-map F1 denominator is empty")
    return 2 * true_positive / denominator


def _off_map_precision(clusters: tuple[dict[str, int], ...]) -> float:
    true_positive = sum(item["off_map_true_positive"] for item in clusters)
    false_positive = sum(item["off_map_false_positive"] for item in clusters)
    denominator = true_positive + false_positive
    if denominator <= 0:
        raise ValueError("synthetic off-map precision denominator is empty")
    return true_positive / denominator


def _off_map_recall(clusters: tuple[dict[str, int], ...]) -> float:
    true_positive = sum(item["off_map_true_positive"] for item in clusters)
    false_negative = sum(item["off_map_false_negative"] for item in clusters)
    denominator = true_positive + false_negative
    if denominator <= 0:
        raise ValueError("synthetic off-map recall denominator is empty")
    return true_positive / denominator


def _path_edit_distance(
    expected: tuple[str | None, ...], predicted: tuple[str | None, ...]
) -> int:
    previous = list(range(len(predicted) + 1))
    for expected_index, expected_item in enumerate(expected, start=1):
        current = [expected_index]
        for predicted_index, predicted_item in enumerate(predicted, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[predicted_index] + 1,
                    previous[predicted_index - 1]
                    + int(expected_item != predicted_item),
                )
            )
        previous = current
    return previous[-1]


def _verify_charter(path: Path, gate: MapMatchingGate) -> dict[str, Any]:
    content = read_bounded_regular_bytes(
        path,
        maximum_bytes=MAXIMUM_CHARTER_BYTES,
        context="numerical charter",
    )
    if (
        hashlib.sha256(content).hexdigest()
        != gate.authorities.numerical_charter_file_sha256
    ):
        raise ValueError("numerical charter does not match the M5.3 gate")
    decoded = decode_bounded_json(
        content,
        maximum_bytes=MAXIMUM_CHARTER_BYTES,
        context="numerical charter",
    )
    if not isinstance(decoded, dict):
        raise ValueError("numerical charter must be an object")
    charter = cast(dict[str, Any], decoded)
    canonical = {
        key: value for key, value in charter.items() if key != "immutable_sha256"
    }
    if charter.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("numerical charter immutable hash is invalid")
    gates = cast(dict[str, dict[str, Any]], charter["gates"])
    thresholds = gate.thresholds
    expected = {
        "map.synthetic_directed_arc_accuracy": (
            "fraction_ge",
            thresholds.synthetic_directed_arc_accuracy_minimum,
        ),
        "map.synthetic_off_map_f1": (
            "fraction_ge",
            thresholds.synthetic_off_map_f1_minimum,
        ),
        "map.tiny_path_mismatch_count": (
            "max_le",
            thresholds.tiny_path_mismatch_count_maximum,
        ),
    }
    for key, (operator, value) in expected.items():
        if gates[key]["operator"] != operator or gates[key]["value"] != value:
            raise ValueError(f"M5.3 gate diverges from numerical charter key {key}")
    statistics = cast(dict[str, Any], charter["statistics"])
    if (
        statistics["bootstrap_seed"] != gate.statistics.bootstrap_seed
        or statistics["bootstrap_replicates"] != gate.statistics.bootstrap_replicates
        or statistics["confidence_level"] != gate.statistics.confidence_level
        or cast(dict[str, str], statistics["bootstrap_unit_by_domain"])[
            "synthetic_sensor_and_map"
        ]
        != gate.statistics.bootstrap_unit
        or cast(dict[str, Any], charter["confirmatory_support"])[
            "minimum_independent_clusters"
        ]
        != gate.statistics.minimum_independent_clusters
    ):
        raise ValueError("M5.3 statistical contract diverges from numerical charter")
    return charter


def _decode_scenario(
    scenario: _Scenario,
    matching_profile: MapMatchingProfile,
    matching_profile_file_sha256: str,
    decoder_profile: MapDecoderProfile,
    decoder_profile_file_sha256: str,
) -> DecodedRoadPath:
    return decode_road_path(
        scenario.graph,
        scenario.observations,
        sequence_id=scenario.scenario_id,
        source_group_id=scenario.synthetic_family_id,
        partition="synthetic",
        matching_profile=matching_profile,
        matching_profile_file_sha256=matching_profile_file_sha256,
        decoder_profile=decoder_profile,
        decoder_profile_file_sha256=decoder_profile_file_sha256,
    )


def qualify_synthetic_road_matching(
    *,
    graph_profile_path: Path,
    matching_profile_path: Path,
    decoder_profile_path: Path,
    gate_path: Path,
    truth_path: Path,
    numerical_charter_path: Path,
    fixture_path: Path,
) -> dict[str, object]:
    """Run the complete frozen M5.3 synthetic topology suite."""

    gate, gate_file_sha256 = load_map_matching_gate(gate_path)
    truth, truth_file_sha256 = load_map_matching_truth(truth_path)
    _verify_charter(numerical_charter_path, gate)
    authorities = gate.authorities
    graph_profile, graph_profile_file_sha256 = load_graph_import_profile(
        graph_profile_path
    )
    matching_profile, matching_profile_file_sha256 = load_map_matching_profile(
        matching_profile_path
    )
    decoder_profile, decoder_profile_file_sha256 = load_map_decoder_profile(
        decoder_profile_path
    )
    if (
        graph_profile_file_sha256 != authorities.graph_import_profile_file_sha256
        or matching_profile_file_sha256 != authorities.map_matching_profile_file_sha256
        or decoder_profile_file_sha256 != authorities.map_decoder_profile_file_sha256
        or truth_file_sha256 != authorities.map_matching_truth_file_sha256
    ):
        raise ValueError("M5.3 source authority does not match the frozen gate")
    graph = import_osm_road_graph(
        fixture_path,
        profile=graph_profile,
        profile_file_sha256=graph_profile_file_sha256,
        source_object_key=authorities.fixture_object_key,
        expected_source_sha256=authorities.fixture_sha256,
        source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
    )
    truth_by_id = {item.scenario_id: item for item in truth.scenarios}
    primary_scenarios = _scenario_suite(graph)
    _bind_primary_truth(primary_scenarios, truth_by_id)
    missing_scenarios = tuple(
        _missing_edge_scenario(
            item,
            graph_profile=graph_profile,
            graph_profile_file_sha256=graph_profile_file_sha256,
            fixture_directory=fixture_path.parent / "missing_edges_v1",
        )
        for item in truth.scenarios
        if item.suite == "MISSING_EDGE_TOPOLOGY"
    )
    _bind_missing_truth(missing_scenarios, truth_by_id)
    scenarios = tuple(
        sorted(
            (*primary_scenarios, *missing_scenarios), key=lambda item: item.scenario_id
        )
    )
    if tuple(item.scenario_id for item in scenarios) != gate.required_scenario_ids:
        raise ValueError("M5.3 synthetic scenario suite is not exact")
    if (
        tuple(item.scenario_id for item in truth.scenarios)
        != gate.required_scenario_ids
    ):
        raise ValueError("M5.3 independent truth does not match the frozen gate")

    clusters: list[dict[str, int]] = []
    scenario_reports: list[dict[str, object]] = []
    exact_mismatches = 0
    exact_path_edit_distance = 0
    confidence_checks: list[bool] = []
    stationary_checks: list[bool] = []
    ambiguity_true_positive = 0
    ambiguity_true_negative = 0
    ambiguity_false_positive = 0
    ambiguity_false_negative = 0
    for scenario in scenarios:
        decoded = _decode_scenario(
            scenario,
            matching_profile,
            matching_profile_file_sha256,
            decoder_profile,
            decoder_profile_file_sha256,
        )
        predicted_arc_ids = tuple(
            item.candidate.directed_arc_id
            if item.candidate.state == CandidateState.ON_ROAD
            else None
            for item in decoded.points
        )
        exact_passed = predicted_arc_ids == scenario.expected_arc_ids
        path_edit_distance = _path_edit_distance(
            scenario.expected_arc_ids, predicted_arc_ids
        )
        if scenario.exact_path and not exact_passed:
            exact_mismatches += 1
        if scenario.exact_path:
            exact_path_edit_distance += path_edit_distance
        confidence_passed = decoded.confidence == scenario.expected_confidence
        confidence_checks.append(confidence_passed)
        expected_ambiguous = scenario.expected_confidence == MatchConfidence.AMBIGUOUS
        decoded_ambiguous = decoded.confidence == MatchConfidence.AMBIGUOUS
        if expected_ambiguous and decoded_ambiguous:
            ambiguity_true_positive += 1
        elif expected_ambiguous:
            ambiguity_false_negative += 1
        elif decoded_ambiguous:
            ambiguity_false_positive += 1
        else:
            ambiguity_true_negative += 1
        if scenario.expected_stationary:
            stationary_passed = (
                all(item.stationary for item in decoded.points)
                and len(decoded.intervals) == 1
                and decoded.intervals[0].usable_distance_m == 0.0
            )
        else:
            stationary_passed = not any(item.stationary for item in decoded.points)
        stationary_checks.append(stationary_passed)

        cluster = {
            "arc_correct": 0,
            "arc_total": 0,
            "off_map_truth_total": 0,
            "off_map_true_positive": 0,
            "off_map_false_positive": 0,
            "off_map_false_negative": 0,
        }
        for expected_arc_id, predicted_arc_id in zip(
            scenario.expected_arc_ids, predicted_arc_ids, strict=True
        ):
            truth_off_map = expected_arc_id is None
            predicted_off_map = predicted_arc_id is None
            cluster["off_map_truth_total"] += int(truth_off_map)
            if truth_off_map and predicted_off_map:
                cluster["off_map_true_positive"] += 1
            elif not truth_off_map and predicted_off_map:
                cluster["off_map_false_positive"] += 1
            elif truth_off_map and not predicted_off_map:
                cluster["off_map_false_negative"] += 1
            if scenario.directed_accuracy_eligible and expected_arc_id is not None:
                cluster["arc_total"] += 1
                cluster["arc_correct"] += int(predicted_arc_id == expected_arc_id)
        clusters.append(cluster)
        scenario_reports.append(
            {
                "scenario_id": scenario.scenario_id,
                "synthetic_family_id": scenario.synthetic_family_id,
                "graph_id": scenario.graph.graph_id,
                "road_match_id": decoded.road_match_id,
                "observation_count": len(decoded.points),
                "expected_arc_ids": scenario.expected_arc_ids,
                "predicted_arc_ids": predicted_arc_ids,
                "exact_path_gate_eligible": scenario.exact_path,
                "exact_path_passed": exact_passed,
                "path_edit_distance": path_edit_distance,
                "expected_confidence": scenario.expected_confidence.value,
                "decoded_confidence": decoded.confidence.value,
                "confidence_passed": confidence_passed,
                "expected_stationary": scenario.expected_stationary,
                "stationary_passed": stationary_passed,
                "cluster_counts": cluster,
            }
        )

    statistics = gate.statistics
    cluster_tuple = tuple(clusters)
    arc_clusters = tuple(item for item in cluster_tuple if item["arc_total"] > 0)
    directed_arc = _bootstrap(
        arc_clusters,
        _arc_accuracy,
        seed=statistics.bootstrap_seed,
        replicates=statistics.bootstrap_replicates,
        confidence_level=statistics.confidence_level,
    )
    off_map = _bootstrap(
        cluster_tuple,
        _off_map_f1,
        seed=statistics.bootstrap_seed + 1,
        replicates=statistics.bootstrap_replicates,
        confidence_level=statistics.confidence_level,
    )
    off_map_precision = _bootstrap(
        cluster_tuple,
        _off_map_precision,
        seed=statistics.bootstrap_seed + 2,
        replicates=statistics.bootstrap_replicates,
        confidence_level=statistics.confidence_level,
    )
    off_map_recall = _bootstrap(
        cluster_tuple,
        _off_map_recall,
        seed=statistics.bootstrap_seed + 3,
        replicates=statistics.bootstrap_replicates,
        confidence_level=statistics.confidence_level,
    )
    thresholds = gate.thresholds
    support_passed = (
        len(arc_clusters) >= statistics.minimum_independent_clusters
        and sum(item["off_map_truth_total"] > 0 for item in cluster_tuple)
        >= statistics.minimum_independent_clusters
    )
    directed_arc_lower = directed_arc["one_sided_lower_95"]
    off_map_lower = off_map["one_sided_lower_95"]
    if not isinstance(directed_arc_lower, float) or not isinstance(
        off_map_lower, float
    ):
        raise ValueError("synthetic bootstrap returned a non-numeric bound")
    directed_arc_passed = (
        directed_arc_lower >= thresholds.synthetic_directed_arc_accuracy_minimum
        and not bool(directed_arc["interval_degenerate"])
        and directed_arc["degenerate_resample_count"] == 0
    )
    off_map_passed = (
        off_map_lower >= thresholds.synthetic_off_map_f1_minimum
        and not bool(off_map["interval_degenerate"])
        and off_map["degenerate_resample_count"] == 0
    )
    exact_path_passed = (
        exact_mismatches <= thresholds.tiny_path_mismatch_count_maximum
        and exact_path_edit_distance == 0
        and {item.scenario_id for item in scenarios if item.exact_path}
        == set(gate.exact_path_scenario_ids)
    )
    expected_ambiguity_ids = {
        item.scenario_id
        for item in scenarios
        if item.expected_confidence == MatchConfidence.AMBIGUOUS
    }
    expected_confident_ids = {
        item.scenario_id
        for item in scenarios
        if item.expected_confidence == MatchConfidence.CONFIDENT
    }
    confidence_passed = (
        all(confidence_checks)
        and expected_ambiguity_ids == set(gate.ambiguity_scenario_ids)
        and expected_confident_ids == set(gate.confident_control_scenario_ids)
    )
    expected_stationary_ids = {
        item.scenario_id for item in scenarios if item.expected_stationary
    }
    stationary_passed = all(stationary_checks) and expected_stationary_ids == set(
        gate.stationary_scenario_ids
    )
    degenerate_resample_passed = all(
        metric["degenerate_resample_count"] == 0
        for metric in (directed_arc, off_map, off_map_precision, off_map_recall)
    )
    accepted = all(
        (
            support_passed,
            directed_arc_passed,
            off_map_passed,
            exact_path_passed,
            confidence_passed,
            stationary_passed,
            degenerate_resample_passed,
        )
    )
    return {
        "schema_version": "cartosentry.m5-3-map-matching-qualification.v1",
        "algorithm_backend": ALGORITHM_BACKEND,
        "accepted": accepted,
        "graph_id": graph.graph_id,
        "gate_file_sha256": gate_file_sha256,
        "gate_immutable_sha256": gate.immutable_sha256,
        "truth_file_sha256": truth_file_sha256,
        "truth_immutable_sha256": truth.immutable_sha256,
        "graph_import_profile_file_sha256": graph_profile_file_sha256,
        "map_matching_profile_file_sha256": matching_profile_file_sha256,
        "map_matching_profile_immutable_sha256": matching_profile.immutable_sha256,
        "map_decoder_profile_file_sha256": decoder_profile_file_sha256,
        "map_decoder_profile_immutable_sha256": decoder_profile.immutable_sha256,
        "numerical_charter_file_sha256": authorities.numerical_charter_file_sha256,
        "statistics": {
            "bootstrap_unit": statistics.bootstrap_unit,
            "bootstrap_seed": statistics.bootstrap_seed,
            "bootstrap_replicates": statistics.bootstrap_replicates,
            "confidence_level": statistics.confidence_level,
            "eligible_directed_arc_clusters": len(arc_clusters),
            "eligible_off_map_positive_clusters": sum(
                item["off_map_truth_total"] > 0 for item in cluster_tuple
            ),
            "support_passed": support_passed,
            "degenerate_resample_gate_passed": degenerate_resample_passed,
        },
        "metrics": {
            "map.synthetic_directed_arc_accuracy": {
                **directed_arc,
                "gate_value": thresholds.synthetic_directed_arc_accuracy_minimum,
                "decision_bound": "one_sided_lower_95",
                "passed": directed_arc_passed,
            },
            "map.synthetic_off_map_f1": {
                **off_map,
                "gate_value": thresholds.synthetic_off_map_f1_minimum,
                "decision_bound": "one_sided_lower_95",
                "passed": off_map_passed,
            },
            "map.synthetic_off_map_precision": {
                **off_map_precision,
                "decision_bound": "reported_non_gate",
            },
            "map.synthetic_off_map_recall": {
                **off_map_recall,
                "decision_bound": "reported_non_gate",
            },
            "map.synthetic_exact_path_edit_distance": {
                "value": exact_path_edit_distance,
                "eligible_scenario_count": len(gate.exact_path_scenario_ids),
                "decision_bound": "deterministic_exhaustive",
                "passed": exact_path_edit_distance == 0,
            },
            "map.synthetic_ambiguity_detection": {
                "true_positive": ambiguity_true_positive,
                "true_negative": ambiguity_true_negative,
                "false_positive": ambiguity_false_positive,
                "false_negative": ambiguity_false_negative,
                "accuracy": (ambiguity_true_positive + ambiguity_true_negative)
                / len(scenarios),
                "decision_bound": "deterministic_frozen_expectations",
                "passed": confidence_passed,
            },
            "map.tiny_path_mismatch_count": {
                "value": exact_mismatches,
                "gate_value": thresholds.tiny_path_mismatch_count_maximum,
                "decision_bound": "deterministic_exhaustive",
                "passed": exact_path_passed,
            },
        },
        "ambiguity_gate_passed": confidence_passed,
        "confidence_gate_passed": confidence_passed,
        "stationary_gate_passed": stationary_passed,
        "scenarios": scenario_reports,
    }


__all__ = [
    "GATE_IMMUTABLE_SHA256",
    "TRUTH_IMMUTABLE_SHA256",
    "MapMatchingGate",
    "MapMatchingTruth",
    "load_map_matching_gate",
    "load_map_matching_truth",
    "qualify_synthetic_road_matching",
]
