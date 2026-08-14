"""Frozen M5.1 directed road-graph qualification."""

from __future__ import annotations

import hashlib
import json
import math
from itertools import islice
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry.adapters.boreas_v1 import BoreasAdapter
from cartosentry.contracts import LocalCoordinate
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.road_graph import (
    PROFILE_IMMUTABLE_SHA256,
    ArcDirection,
    DirectedRoadGraph,
    GraphSourceKind,
    RestrictionInterpretation,
    RoadGraphSpatialIndex,
    TransitionState,
    import_osm_road_graph,
    load_graph_import_profile,
    normalize_matching_observation,
    validate_graph_identity,
)

GATE_IMMUTABLE_SHA256 = (
    "3c0290924624e2744be106658e7a2fd3367a69552615619da848bdb2adc1f133"
)
MAXIMUM_GATE_BYTES = 256 * 1024
MAXIMUM_DATA_MANIFEST_BYTES = 1024 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class GraphGateAuthorities(StrictModel):
    profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    profile_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    data_manifest_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    fixture_object_key: Literal["tests/fixtures/road_graphs/topology_v1.osm"]
    fixture_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    public_sequence_id: Literal["boreas-2021-09-02-11-42"]
    public_source_group_id: Literal["boreas-glen-shields-family-v1"]
    public_partition: Literal["development"]
    trajectory_object_key: Literal[
        "boreas-2021-09-02-11-42/applanix/gps_post_process.csv"
    ]
    trajectory_object_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    trajectory_object_bytes: Annotated[int, Field(gt=0)]


class ExpectedGraph(StrictModel):
    graph_id: Annotated[str, Field(pattern=r"^road-graph-sha256-[0-9a-f]{64}$")]
    parsed_node_count: Annotated[int, Field(gt=0)]
    parsed_way_count: Annotated[int, Field(gt=0)]
    parsed_relation_count: Annotated[int, Field(ge=0)]
    included_way_count: Annotated[int, Field(gt=0)]
    topology_node_count: Annotated[int, Field(gt=0)]
    directed_arc_count: Annotated[int, Field(gt=0)]
    transition_rule_count: Annotated[int, Field(ge=0)]
    applied_restriction_count: Annotated[int, Field(ge=0)]
    unknown_restriction_count: Annotated[int, Field(ge=0)]


class ObservationContract(StrictModel):
    selection: Literal["FIRST_N_SOURCE_ORDER"]
    sample_count: Annotated[int, Field(gt=0)]
    source_key: Literal["applanix/gps_post_process.csv"]
    coordinate_provenance: Literal["WGS84_SOURCE_DERIVED_LOCAL_WORLD"]
    local_altitude_policy: Literal["ZERO_ELLIPSOID_FOR_HORIZONTAL_MATCHING"]
    maximum_horizontal_round_trip_error_m: Annotated[float, Field(gt=0.0)]


class DirectedRoadGraphGate(StrictModel):
    schema_version: Literal[1]
    gate_id: Literal["m5.1-directed-road-graph-v1"]
    gate_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_M5_1_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: GraphGateAuthorities
    expected_graphs: dict[Literal["fixture", "public"], ExpectedGraph]
    observation_contract: ObservationContract
    required_topology_checks: tuple[str, ...]

    @model_validator(mode="after")
    def validate_exact_contract(self) -> Self:
        if self.immutable_sha256 != GATE_IMMUTABLE_SHA256:
            raise ValueError("directed road-graph gate identity is not pinned")
        if self.authorities.profile_immutable_sha256 != PROFILE_IMMUTABLE_SHA256:
            raise ValueError("directed road-graph profile authority is not exact")
        if set(self.expected_graphs) != {"fixture", "public"}:
            raise ValueError("directed road-graph expected inputs are incomplete")
        expected_checks = {
            "forward_oneway",
            "reverse_oneway",
            "divided_road",
            "ramp",
            "roundabout",
            "parallel_road",
            "grade_separated",
            "simple_turn_restriction",
            "conditional_restriction_unknown",
            "spatial_index_order",
        }
        if set(self.required_topology_checks) != expected_checks or len(
            self.required_topology_checks
        ) != len(expected_checks):
            raise ValueError("directed road-graph topology checks are not exact")
        return self


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


def load_directed_road_graph_gate(
    path: Path,
) -> tuple[DirectedRoadGraphGate, str]:
    """Load and self-authenticate the frozen M5.1 gate."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="directed road-graph qualification gate",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="directed road-graph qualification gate",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "directed road-graph gate is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("directed road-graph gate must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("directed road-graph gate hash is invalid")
    return DirectedRoadGraphGate.model_validate_json(content), hashlib.sha256(
        content
    ).hexdigest()


def _verify_data_manifest(
    path: Path, gate: DirectedRoadGraphGate, public_graph_path: Path
) -> None:
    authorities = gate.authorities
    if _file_sha256(path) != authorities.data_manifest_file_sha256:
        raise ValueError("data manifest does not match the road-graph gate")
    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_DATA_MANIFEST_BYTES,
            context="data manifest",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_DATA_MANIFEST_BYTES,
            context="data manifest",
        )
    except ManifestBoundaryError as error:
        raise ValueError("data manifest is unavailable or malformed") from error
    if not isinstance(decoded, dict) or not isinstance(decoded.get("artifacts"), list):
        raise ValueError("data manifest artifact collection is malformed")
    artifacts = cast(list[object], decoded["artifacts"])
    road = next(
        (
            item
            for item in artifacts
            if isinstance(item, dict)
            and item.get("id") == "osm-toronto-glen-shields-v1"
        ),
        None,
    )
    if not isinstance(road, dict):
        raise ValueError("data manifest is missing the pinned OSM extract")
    profile_key = "road_graphs/toronto-glen-shields-v1.osm"
    road_objects = road.get("objects")
    if not isinstance(road_objects, list):
        raise ValueError("OSM data-manifest object collection is malformed")
    road_object = next(
        (
            item
            for item in road_objects
            if isinstance(item, dict) and item.get("key") == profile_key
        ),
        None,
    )
    if not isinstance(road_object, dict):
        raise ValueError("data manifest is missing the pinned OSM object")
    expected_road = {
        "bytes": public_graph_path.stat().st_size,
        "sha256": _file_sha256(public_graph_path),
        "snapshot_utc": "2026-08-14T00:47:44Z",
    }
    if any(road_object.get(key) != value for key, value in expected_road.items()):
        raise ValueError("pinned OSM object does not match the data manifest")
    trajectory = next(
        (
            item
            for item in artifacts
            if isinstance(item, dict)
            and item.get("id") == "boreas-public-smoke-clear-v1"
        ),
        None,
    )
    if not isinstance(trajectory, dict) or not isinstance(
        trajectory.get("objects"), list
    ):
        raise ValueError("data manifest is missing the public trajectory artifact")
    trajectory_object = next(
        (
            item
            for item in cast(list[object], trajectory["objects"])
            if isinstance(item, dict)
            and item.get("key") == authorities.trajectory_object_key
        ),
        None,
    )
    if not isinstance(trajectory_object, dict) or (
        trajectory_object.get("bytes") != authorities.trajectory_object_bytes
        or trajectory_object.get("sha256") != authorities.trajectory_object_sha256
    ):
        raise ValueError("public trajectory authority is inconsistent")


def _expected_statistics(graph: DirectedRoadGraph) -> dict[str, int]:
    statistics = graph.statistics
    return {
        "parsed_node_count": statistics.parsed_node_count,
        "parsed_way_count": statistics.parsed_way_count,
        "parsed_relation_count": statistics.parsed_relation_count,
        "included_way_count": statistics.included_way_count,
        "topology_node_count": statistics.topology_node_count,
        "directed_arc_count": statistics.directed_arc_count,
        "transition_rule_count": statistics.transition_rule_count,
        "applied_restriction_count": statistics.applied_restriction_count,
        "unknown_restriction_count": statistics.unknown_restriction_count,
    }


def _graph_matches(graph: DirectedRoadGraph, expected: ExpectedGraph) -> bool:
    return graph.graph_id == expected.graph_id and _expected_statistics(
        graph
    ) == expected.model_dump(mode="json", exclude={"graph_id"})


def _fixture_topology_checks(graph: DirectedRoadGraph) -> dict[str, bool]:
    by_way = {
        way_id: tuple(arc for arc in graph.arcs if arc.source_way_id == way_id)
        for way_id in range(100, 163)
    }

    def directions(way_id: int) -> set[ArcDirection]:
        return {item.direction for item in by_way[way_id]}

    bridge_nodes = {
        item for arc in by_way[150] for item in arc.source_geometry_node_ids
    }
    tunnel_nodes = {
        item for arc in by_way[151] for item in arc.source_geometry_node_ids
    }
    bridge_arc = by_way[150][0]
    left = bridge_arc.geometry_local_m[0]
    right = bridge_arc.geometry_local_m[-1]
    crossing = ((left[0] + right[0]) / 2.0, (left[1] + right[1]) / 2.0)
    index = RoadGraphSpatialIndex(graph)
    first_query = index.query_radius(crossing, 0.1)
    second_query = index.query_radius(crossing, 0.1)
    simple = next(
        item for item in graph.restrictions if item.source_relation_id == 1000
    )
    conditional = next(
        item for item in graph.restrictions if item.source_relation_id == 1001
    )
    return {
        "forward_oneway": len(by_way[100]) == 1
        and by_way[100][0].source_geometry_node_ids == (1, 2)
        and directions(100) == {ArcDirection.FORWARD},
        "reverse_oneway": len(by_way[101]) == 1
        and by_way[101][0].source_geometry_node_ids == (4, 3)
        and directions(101) == {ArcDirection.REVERSE},
        "divided_road": directions(110) == directions(111) == {ArcDirection.FORWARD}
        and {item.source_way_id for item in (*by_way[110], *by_way[111])} == {110, 111},
        "ramp": directions(120) == directions(121) == {ArcDirection.FORWARD}
        and by_way[121][0].highway_class == "motorway_link",
        "roundabout": len(by_way[130]) == 2
        and directions(130) == {ArcDirection.FORWARD}
        and all(arc.from_node_id != arc.to_node_id for arc in by_way[130]),
        "parallel_road": len(by_way[140]) == len(by_way[141]) == 2
        and not (
            {item for arc in by_way[140] for item in arc.source_geometry_node_ids}
            & {item for arc in by_way[141] for item in arc.source_geometry_node_ids}
        ),
        "grade_separated": not bridge_nodes & tunnel_nodes
        and all(item.layer == 1 and item.bridge for item in by_way[150])
        and all(item.layer == -1 and item.tunnel for item in by_way[151])
        and {item.source_way_id for item in first_query} == {150, 151},
        "simple_turn_restriction": simple.interpretation
        is RestrictionInterpretation.APPLIED_NO
        and simple.generated_rule_count == 1
        and sum(
            item.source_relation_id == 1000 and item.state is TransitionState.FORBIDDEN
            for item in graph.transition_rules
        )
        == 1,
        "conditional_restriction_unknown": conditional.interpretation
        is RestrictionInterpretation.UNKNOWN_CONDITIONAL
        and conditional.generated_rule_count > 0
        and all(
            item.state is TransitionState.UNKNOWN_RESTRICTION
            for item in graph.transition_rules
            if item.source_relation_id == 1001
        ),
        "spatial_index_order": first_query == second_query
        and tuple(item.arc_id for item in first_query)
        == tuple(sorted(item.arc_id for item in first_query)),
    }


def _qualify_observation_provenance(
    graph: DirectedRoadGraph,
    *,
    sequence_root: Path,
    gate: DirectedRoadGraphGate,
) -> dict[str, object]:
    contract = gate.observation_contract
    trajectory_path = sequence_root / contract.source_key
    authorities = gate.authorities
    if (
        not trajectory_path.is_file()
        or trajectory_path.stat().st_size != authorities.trajectory_object_bytes
        or _file_sha256(trajectory_path) != authorities.trajectory_object_sha256
    ):
        raise ValueError("public trajectory does not match its frozen authority")
    adapter = BoreasAdapter(
        sequence_root,
        source_group_id=authorities.public_source_group_id,
    )
    observations = tuple(
        normalize_matching_observation(sample, graph)
        for sample in islice(adapter.pose_samples(), contract.sample_count)
    )
    if len(observations) != contract.sample_count:
        raise ValueError(
            "public trajectory has fewer samples than the frozen selection"
        )
    origin = graph.local_frame.local_origin()
    errors: list[float] = []
    for observation in observations:
        recovered = origin.to_global(
            LocalCoordinate(
                frame=observation.local_frame_id,
                position_m=observation.position_local_m,
            )
        )
        latitude_delta_m = (
            math.radians(
                recovered.latitude_deg - observation.coordinate_wgs84.latitude_deg
            )
            * 6_378_137.0
        )
        longitude_delta_m = (
            math.radians(
                recovered.longitude_deg - observation.coordinate_wgs84.longitude_deg
            )
            * 6_378_137.0
            * math.cos(math.radians(observation.coordinate_wgs84.latitude_deg))
        )
        errors.append(math.hypot(latitude_delta_m, longitude_delta_m))
    valid = sum(
        item.coordinate_provenance == contract.coordinate_provenance
        and item.local_altitude_policy == contract.local_altitude_policy
        and item.source_provenance.source_key == contract.source_key
        and item.coordinate_wgs84.altitude_m is None
        for item in observations
    )
    maximum_error = max(errors)
    reported_maximum_error = round(maximum_error, 8)
    return {
        "accepted": valid == len(observations)
        and maximum_error <= contract.maximum_horizontal_round_trip_error_m,
        "selection": contract.selection,
        "sample_count": len(observations),
        "source_key": contract.source_key,
        "source_sha256": authorities.trajectory_object_sha256,
        "source_group_id": authorities.public_source_group_id,
        "partition": authorities.public_partition,
        "coordinate_provenance": contract.coordinate_provenance,
        "local_altitude_policy": contract.local_altitude_policy,
        "provenance_valid_fraction": valid / len(observations),
        "maximum_horizontal_round_trip_error_m": reported_maximum_error,
        "horizontal_round_trip_gate_m": (
            contract.maximum_horizontal_round_trip_error_m
        ),
        "observation_set_sha256": _canonical_hash(
            [item.model_dump(mode="json") for item in observations]
        ),
    }


def qualify_directed_road_graph(
    *,
    profile_path: Path,
    gate_path: Path,
    data_manifest_path: Path,
    fixture_path: Path,
    public_graph_path: Path,
    public_sequence_root: Path,
) -> dict[str, object]:
    """Run the complete deterministic M5.1 gate through production boundaries."""

    gate, gate_file_sha256 = load_directed_road_graph_gate(gate_path)
    profile, profile_file_sha256 = load_graph_import_profile(profile_path)
    if (
        profile_file_sha256 != gate.authorities.profile_file_sha256
        or profile.immutable_sha256 != gate.authorities.profile_immutable_sha256
    ):
        raise ValueError("graph import profile does not match the frozen gate")
    _verify_data_manifest(data_manifest_path, gate, public_graph_path)
    fixture = import_osm_road_graph(
        fixture_path,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_object_key=gate.authorities.fixture_object_key,
        expected_source_sha256=gate.authorities.fixture_sha256,
        source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
    )
    public = import_osm_road_graph(
        public_graph_path,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_object_key=profile.authorities.public_object_key,
        expected_source_sha256=profile.authorities.public_object_sha256,
    )
    validate_graph_identity(fixture)
    validate_graph_identity(public)
    topology_checks = _fixture_topology_checks(fixture)
    observation_report = _qualify_observation_provenance(
        public,
        sequence_root=public_sequence_root,
        gate=gate,
    )
    fixture_expected = _graph_matches(fixture, gate.expected_graphs["fixture"])
    public_expected = _graph_matches(public, gate.expected_graphs["public"])
    topology_accepted = set(topology_checks) == set(
        gate.required_topology_checks
    ) and all(topology_checks.values())
    attribution_accepted = (
        public.source.attribution == profile.authorities.attribution
        and public.source.license_url == profile.authorities.license_url
        and public.source.database_classification == "ODBL_DERIVATIVE_DATABASE"
    )
    accepted = (
        fixture_expected
        and public_expected
        and topology_accepted
        and attribution_accepted
        and observation_report["accepted"] is True
    )
    return {
        "schema_version": "cartosentry.directed-road-graph-qualification-report.v1",
        "gate_id": gate.gate_id,
        "gate_file_sha256": gate_file_sha256,
        "gate_immutable_sha256": gate.immutable_sha256,
        "profile_file_sha256": profile_file_sha256,
        "profile_immutable_sha256": profile.immutable_sha256,
        "accepted": accepted,
        "claim_status": "DEVELOPMENT_ENGINEERING_GATE",
        "fixture": {
            "accepted": fixture_expected and topology_accepted,
            "graph_id": fixture.graph_id,
            "source": fixture.source.model_dump(mode="json"),
            "statistics": fixture.statistics.model_dump(mode="json"),
            "topology_checks": topology_checks,
        },
        "public_graph": {
            "accepted": public_expected and attribution_accepted,
            "graph_id": public.graph_id,
            "source": public.source.model_dump(mode="json"),
            "statistics": public.statistics.model_dump(mode="json"),
            "attribution_survives_portable_export": attribution_accepted,
        },
        "public_observations": observation_report,
    }


__all__ = [
    "GATE_IMMUTABLE_SHA256",
    "load_directed_road_graph_gate",
    "qualify_directed_road_graph",
]
