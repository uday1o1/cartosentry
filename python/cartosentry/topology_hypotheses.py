"""Authenticated review-only topology-disagreement hypotheses."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry import _core
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.road_bins import (
    PROFILE_FILE_SHA256 as ROAD_BINNING_PROFILE_FILE_SHA256,
)
from cartosentry.road_decoder import PROFILE_FILE_SHA256 as DECODER_PROFILE_FILE_SHA256
from cartosentry.road_graph import DirectedRoadGraph, validate_graph_identity
from cartosentry.road_matching import ALGORITHM_BACKEND

PROFILE_IMMUTABLE_SHA256 = (
    "fdd67acdbaa5587f4cfa0643b48ed1e380bd9b975fd8bf656dced57ecd546675"
)
PROFILE_FILE_SHA256 = "79202f14439bcc60cb985d903790f4243ff4c308088e6107f5763c5ed3a78084"
GRAPH_PROFILE_FILE_SHA256 = (
    "c1e11f4a78bb912d1ee94058bf37b1312062cfbd0fd5c6bf1497f9179cc8e4e0"
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


def _stable_id(prefix: str, payload: object) -> str:
    return f"{prefix}-sha256-{_canonical_hash(payload)}"


class TopologyHypothesisAuthorities(StrictModel):
    graph_import_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    map_decoder_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    road_binning_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    numerical_charter_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class TopologyHypothesisParameters(StrictModel):
    minimum_positioning_quality: Annotated[float, Field(ge=0.0, le=1.0)]
    minimum_interval_length_m: Annotated[float, Field(gt=0.0)]
    resample_point_count: Annotated[int, Field(ge=2)]
    maximum_cluster_mean_distance_m: Annotated[float, Field(gt=0.0)]
    maximum_cluster_endpoint_distance_m: Annotated[float, Field(gt=0.0)]
    maximum_heading_difference_rad: Annotated[
        float, Field(ge=0.0, le=3.141592653589793)
    ]
    minimum_independent_traversals: Annotated[int, Field(gt=0)]
    endpoint_snap_radius_m: Annotated[float, Field(gt=0.0)]
    geometry_disagreement_mean_distance_m: Annotated[float, Field(gt=0.0)]
    graph_endpoint_tolerance_m: Annotated[float, Field(ge=0.0)]
    maximum_intervals: Annotated[int, Field(gt=0)]
    maximum_points_per_interval: Annotated[int, Field(ge=2)]
    maximum_total_points: Annotated[int, Field(ge=2)]
    maximum_graph_nodes: Annotated[int, Field(gt=0)]
    maximum_graph_arcs: Annotated[int, Field(gt=0)]
    maximum_points_per_graph_arc: Annotated[int, Field(ge=2)]
    maximum_total_graph_points: Annotated[int, Field(ge=2)]
    maximum_pairwise_comparisons: Annotated[int, Field(gt=0)]
    maximum_clusters: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_work_budgets(self) -> Self:
        maximum_possible_pairs = (
            self.maximum_intervals * (self.maximum_intervals - 1) // 2
        )
        if self.maximum_pairwise_comparisons > maximum_possible_pairs:
            raise ValueError("pairwise topology budget exceeds the interval ceiling")
        return self


class TopologyHypothesisProfile(StrictModel):
    schema_version: Literal[1]
    profile_id: Literal["topology-hypotheses-v1"]
    profile_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_M5_5_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: TopologyHypothesisAuthorities
    parameter_charter: TopologyHypothesisParameters

    def assert_identity_and_authorities(self) -> None:
        payload = self.model_dump(mode="json", exclude={"immutable_sha256"})
        if (
            self.immutable_sha256 != PROFILE_IMMUTABLE_SHA256
            or _canonical_hash(payload) != self.immutable_sha256
        ):
            raise ValueError("topology-hypothesis profile identity is not pinned")
        authorities = self.authorities
        if (
            authorities.graph_import_profile_file_sha256 != GRAPH_PROFILE_FILE_SHA256
            or authorities.map_decoder_profile_file_sha256
            != DECODER_PROFILE_FILE_SHA256
            or authorities.road_binning_profile_file_sha256
            != ROAD_BINNING_PROFILE_FILE_SHA256
        ):
            raise ValueError("topology-hypothesis profile authorities are not exact")

    @model_validator(mode="after")
    def validate_identity_and_authorities(self) -> Self:
        self.assert_identity_and_authorities()
        return self


def load_topology_hypothesis_profile(
    path: Path,
) -> tuple[TopologyHypothesisProfile, str]:
    """Load and self-authenticate the frozen M5.5 parameter charter."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="topology-hypothesis profile",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="topology-hypothesis profile",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "topology-hypothesis profile is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("topology-hypothesis profile must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("topology-hypothesis profile immutable hash is invalid")
    file_sha256 = hashlib.sha256(content).hexdigest()
    if file_sha256 != PROFILE_FILE_SHA256:
        raise ValueError("topology-hypothesis profile file identity is not pinned")
    return TopologyHypothesisProfile.model_validate_json(content), file_sha256


Point2D = tuple[float, float]


class TopologyGraphNode(StrictModel):
    node_id: Annotated[str, Field(min_length=1)]
    position_local_m: Point2D


class TopologyGraphArc(StrictModel):
    arc_id: Annotated[str, Field(min_length=1)]
    from_node_id: Annotated[str, Field(min_length=1)]
    to_node_id: Annotated[str, Field(min_length=1)]
    geometry_local_m: tuple[Point2D, ...]

    @model_validator(mode="after")
    def validate_geometry(self) -> Self:
        if len(self.geometry_local_m) < 2:
            raise ValueError("topology graph arcs require at least two geometry points")
        return self


class TopologyGraphView(StrictModel):
    schema_version: Literal["cartosentry.topology-graph-view.v1"]
    graph_view_id: Annotated[
        str, Field(pattern=r"^topology-graph-view-sha256-[0-9a-f]{64}$")
    ]
    source_road_graph_id: Annotated[
        str, Field(pattern=r"^[a-z0-9-]+-sha256-[0-9a-f]{64}$")
    ]
    coordinate_frame_id: Annotated[str, Field(min_length=1)]
    nodes: tuple[TopologyGraphNode, ...]
    arcs: tuple[TopologyGraphArc, ...]

    @model_validator(mode="after")
    def validate_identity_and_topology(self) -> Self:
        node_ids = tuple(item.node_id for item in self.nodes)
        arc_ids = tuple(item.arc_id for item in self.arcs)
        if node_ids != tuple(sorted(set(node_ids))):
            raise ValueError("topology graph nodes are not unique and canonical")
        if arc_ids != tuple(sorted(set(arc_ids))):
            raise ValueError("topology graph arcs are not unique and canonical")
        node_id_set = set(node_ids)
        if any(
            item.from_node_id not in node_id_set or item.to_node_id not in node_id_set
            for item in self.arcs
        ):
            raise ValueError("topology graph arcs reference unknown nodes")
        payload = self.model_dump(mode="json", exclude={"graph_view_id"})
        if self.graph_view_id != _stable_id("topology-graph-view", payload):
            raise ValueError("topology graph view identity is invalid")
        return self


def make_topology_graph_view_from_primitives(
    *,
    source_road_graph_id: str,
    coordinate_frame_id: str,
    nodes: tuple[TopologyGraphNode, ...],
    arcs: tuple[TopologyGraphArc, ...],
) -> TopologyGraphView:
    """Create an authenticated comparison view from validated primitives."""

    canonical_nodes = tuple(sorted(nodes, key=lambda item: item.node_id))
    canonical_arcs = tuple(sorted(arcs, key=lambda item: item.arc_id))
    payload: dict[str, object] = {
        "schema_version": "cartosentry.topology-graph-view.v1",
        "source_road_graph_id": source_road_graph_id,
        "coordinate_frame_id": coordinate_frame_id,
        "nodes": tuple(item.model_dump(mode="json") for item in canonical_nodes),
        "arcs": tuple(item.model_dump(mode="json") for item in canonical_arcs),
    }
    return TopologyGraphView(
        schema_version="cartosentry.topology-graph-view.v1",
        graph_view_id=_stable_id("topology-graph-view", payload),
        source_road_graph_id=source_road_graph_id,
        coordinate_frame_id=coordinate_frame_id,
        nodes=canonical_nodes,
        arcs=canonical_arcs,
    )


def make_topology_graph_view(graph: DirectedRoadGraph) -> TopologyGraphView:
    """Create a minimal, authenticated comparison view from a road graph."""

    validate_graph_identity(graph)
    return make_topology_graph_view_from_primitives(
        source_road_graph_id=graph.graph_id,
        coordinate_frame_id=graph.local_frame.frame.frame_id,
        nodes=tuple(
            TopologyGraphNode(
                node_id=item.node_id,
                position_local_m=(
                    item.position_local_m[0],
                    item.position_local_m[1],
                ),
            )
            for item in graph.nodes
        ),
        arcs=tuple(
            TopologyGraphArc(
                arc_id=item.arc_id,
                from_node_id=item.from_node_id,
                to_node_id=item.to_node_id,
                geometry_local_m=tuple(
                    (point[0], point[1]) for point in item.geometry_local_m
                ),
            )
            for item in graph.arcs
        ),
    )


class OffMapTrajectoryInterval(StrictModel):
    interval_id: Annotated[
        str, Field(pattern=r"^off-map-interval-sha256-[0-9a-f]{64}$")
    ]
    sequence_id: Annotated[str, Field(min_length=1)]
    traversal_id: Annotated[str, Field(min_length=1)]
    source_group_id: Annotated[str, Field(min_length=1)]
    coordinate_frame_id: Annotated[str, Field(min_length=1)]
    off_map_state: bool
    positioning_observable: bool
    direction_confident: bool
    stationary: bool
    positioning_quality: Annotated[float, Field(ge=0.0, le=1.0)]
    points_local_m: tuple[Point2D, ...]

    @model_validator(mode="after")
    def validate_identity_and_points(self) -> Self:
        if len(self.points_local_m) < 2:
            raise ValueError("off-map intervals require at least two points")
        payload = self.model_dump(mode="json", exclude={"interval_id"})
        if self.interval_id != _stable_id("off-map-interval", payload):
            raise ValueError("off-map interval identity is invalid")
        return self


def make_off_map_trajectory_interval(
    *,
    sequence_id: str,
    traversal_id: str,
    source_group_id: str,
    coordinate_frame_id: str,
    off_map_state: bool,
    positioning_observable: bool,
    direction_confident: bool,
    stationary: bool,
    positioning_quality: float,
    points_local_m: tuple[Point2D, ...],
) -> OffMapTrajectoryInterval:
    payload: dict[str, object] = {
        "sequence_id": sequence_id,
        "traversal_id": traversal_id,
        "source_group_id": source_group_id,
        "coordinate_frame_id": coordinate_frame_id,
        "off_map_state": off_map_state,
        "positioning_observable": positioning_observable,
        "direction_confident": direction_confident,
        "stationary": stationary,
        "positioning_quality": float(positioning_quality),
        "points_local_m": points_local_m,
    }
    return OffMapTrajectoryInterval(
        interval_id=_stable_id("off-map-interval", payload),
        sequence_id=sequence_id,
        traversal_id=traversal_id,
        source_group_id=source_group_id,
        coordinate_frame_id=coordinate_frame_id,
        off_map_state=off_map_state,
        positioning_observable=positioning_observable,
        direction_confident=direction_confident,
        stationary=stationary,
        positioning_quality=float(positioning_quality),
        points_local_m=points_local_m,
    )


class TopologyHypothesisKind(StrEnum):
    POSSIBLE_MISSING_CONNECTION = "POSSIBLE_MISSING_CONNECTION"
    POSSIBLE_GEOMETRY_DISAGREEMENT = "POSSIBLE_GEOMETRY_DISAGREEMENT"


class RejectedIntervalCounts(StrictModel):
    not_off_map: Annotated[int, Field(ge=0)]
    unobservable_positioning: Annotated[int, Field(ge=0)]
    uncertain_direction: Annotated[int, Field(ge=0)]
    stationary: Annotated[int, Field(ge=0)]
    insufficient_positioning_quality: Annotated[int, Field(ge=0)]
    short_interval: Annotated[int, Field(ge=0)]

    def total(self) -> int:
        return sum(self.model_dump().values())


class TopologyTrajectoryCluster(StrictModel):
    cluster_id: Annotated[str, Field(pattern=r"^topology-cluster-sha256-[0-9a-f]{64}$")]
    supporting_interval_ids: tuple[
        Annotated[str, Field(pattern=r"^off-map-interval-sha256-[0-9a-f]{64}$")],
        ...,
    ]
    independent_traversal_count: Annotated[int, Field(gt=0)]
    fitted_corridor_local_m: tuple[Point2D, ...]
    start_node_id: str | None
    end_node_id: str | None
    start_endpoint_distance_m: Annotated[float, Field(ge=0.0)] | None
    end_endpoint_distance_m: Annotated[float, Field(ge=0.0)] | None

    @model_validator(mode="after")
    def validate_cluster(self) -> Self:
        if (
            tuple(sorted(set(self.supporting_interval_ids)))
            != self.supporting_interval_ids
        ):
            raise ValueError("topology cluster interval identities are not canonical")
        if len(self.fitted_corridor_local_m) < 2:
            raise ValueError("topology cluster corridor is underspecified")
        if (self.start_node_id is None) != (self.start_endpoint_distance_m is None):
            raise ValueError("topology cluster start-node evidence is incomplete")
        if (self.end_node_id is None) != (self.end_endpoint_distance_m is None):
            raise ValueError("topology cluster end-node evidence is incomplete")
        payload = self.model_dump(mode="json", exclude={"cluster_id"})
        if self.cluster_id != _stable_id("topology-cluster", payload):
            raise ValueError("topology cluster identity is invalid")
        return self


class TopologyReviewHypothesis(StrictModel):
    hypothesis_id: Annotated[
        str, Field(pattern=r"^topology-hypothesis-sha256-[0-9a-f]{64}$")
    ]
    result_label: Literal["REVIEW_HYPOTHESIS_NOT_GROUND_TRUTH"]
    kind: TopologyHypothesisKind
    source_road_graph_id: Annotated[str, Field(min_length=1)]
    cluster_id: Annotated[str, Field(pattern=r"^topology-cluster-sha256-[0-9a-f]{64}$")]
    start_node_id: Annotated[str, Field(min_length=1)]
    end_node_id: Annotated[str, Field(min_length=1)]
    compared_arc_id: str | None
    endpoint_localization_error_m: Annotated[float, Field(ge=0.0)]
    geometry_corridor_error_m: Annotated[float, Field(ge=0.0)] | None
    review_required: Literal[True]
    automatic_map_edit_permitted: Literal[False]
    ground_truth_status: Literal["NOT_GROUND_TRUTH"]

    @model_validator(mode="after")
    def validate_identity_and_kind(self) -> Self:
        if self.kind is TopologyHypothesisKind.POSSIBLE_MISSING_CONNECTION:
            if (
                self.compared_arc_id is not None
                or self.geometry_corridor_error_m is not None
            ):
                raise ValueError("missing-connection hypothesis names a compared arc")
        elif self.compared_arc_id is None or self.geometry_corridor_error_m is None:
            raise ValueError(
                "geometry-disagreement hypothesis lacks compared-arc evidence"
            )
        payload = self.model_dump(mode="json", exclude={"hypothesis_id"})
        if self.hypothesis_id != _stable_id("topology-hypothesis", payload):
            raise ValueError("topology hypothesis identity is invalid")
        return self


class TopologyHypothesisReport(StrictModel):
    schema_version: Literal["cartosentry.topology-hypothesis-report.v1"]
    report_id: Annotated[str, Field(pattern=r"^topology-report-sha256-[0-9a-f]{64}$")]
    algorithm_backend: Literal["C++20_NATIVE_BATCH_V1"]
    source_road_graph_id: Annotated[str, Field(min_length=1)]
    topology_graph_view_id: Annotated[str, Field(min_length=1)]
    profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    profile_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    selected_interval_ids: tuple[str, ...]
    rejected_interval_counts: RejectedIntervalCounts
    clusters: tuple[TopologyTrajectoryCluster, ...]
    hypotheses: tuple[TopologyReviewHypothesis, ...]
    result_semantics: Literal[
        "REVIEW_HYPOTHESES_ONLY_NO_GROUND_TRUTH_OR_AUTOMATIC_MAP_EDIT"
    ]

    @model_validator(mode="after")
    def validate_report_identity(self) -> Self:
        if tuple(sorted(set(self.selected_interval_ids))) != self.selected_interval_ids:
            raise ValueError("selected topology intervals are not canonical")
        cluster_ids = {item.cluster_id for item in self.clusters}
        if any(item.cluster_id not in cluster_ids for item in self.hypotheses):
            raise ValueError("topology hypothesis references an unknown cluster")
        payload = self.model_dump(mode="json", exclude={"report_id"})
        if self.report_id != _stable_id("topology-report", payload):
            raise ValueError("topology hypothesis report identity is invalid")
        return self


def _point(raw: object) -> Point2D:
    values = cast(tuple[float, float], raw)
    if len(values) != 2:
        raise ValueError("native topology point has invalid dimensionality")
    return (float(values[0]), float(values[1]))


def mine_topology_hypotheses(
    graph: TopologyGraphView,
    intervals: tuple[OffMapTrajectoryInterval, ...],
    *,
    profile: TopologyHypothesisProfile,
    profile_file_sha256: str,
) -> TopologyHypothesisReport:
    """Mine repeated disagreements without claiming ground truth or editing a map."""

    profile.assert_identity_and_authorities()
    if profile_file_sha256 != PROFILE_FILE_SHA256:
        raise ValueError("topology-hypothesis profile file hash is not exact")
    if any(item.coordinate_frame_id != graph.coordinate_frame_id for item in intervals):
        raise ValueError("off-map intervals and topology graph use different frames")
    interval_ids = [item.interval_id for item in intervals]
    if len(interval_ids) != len(set(interval_ids)):
        raise ValueError("off-map interval identities must be unique")
    parameters = profile.parameter_charter
    if len(intervals) > parameters.maximum_intervals:
        raise ValueError("off-map interval count exceeds the frozen budget")
    if (
        any(
            len(item.points_local_m) > parameters.maximum_points_per_interval
            for item in intervals
        )
        or sum(len(item.points_local_m) for item in intervals)
        > parameters.maximum_total_points
    ):
        raise ValueError("off-map interval points exceed the frozen budget")
    if (
        len(graph.nodes) > parameters.maximum_graph_nodes
        or len(graph.arcs) > parameters.maximum_graph_arcs
    ):
        raise ValueError("topology graph count exceeds the frozen budget")
    if (
        any(
            len(item.geometry_local_m) > parameters.maximum_points_per_graph_arc
            for item in graph.arcs
        )
        or sum(len(item.geometry_local_m) for item in graph.arcs)
        > parameters.maximum_total_graph_points
    ):
        raise ValueError("topology graph points exceed the frozen budget")

    node_indices = {item.node_id: index for index, item in enumerate(graph.nodes)}
    raw = _core.mine_repeated_topology_disagreements(
        [
            {
                "interval_id": item.interval_id,
                "sequence_id": item.sequence_id,
                "traversal_id": item.traversal_id,
                "source_group_id": item.source_group_id,
                "off_map": item.off_map_state,
                "positioning_observable": item.positioning_observable,
                "direction_confident": item.direction_confident,
                "stationary": item.stationary,
                "positioning_quality": item.positioning_quality,
                "points": list(item.points_local_m),
            }
            for item in intervals
        ],
        [
            {"node_id": item.node_id, "position": item.position_local_m}
            for item in graph.nodes
        ],
        [
            {
                "arc_id": item.arc_id,
                "source_node_index": node_indices[item.from_node_id],
                "target_node_index": node_indices[item.to_node_id],
                "geometry": list(item.geometry_local_m),
            }
            for item in graph.arcs
        ],
        parameters.model_dump(mode="python"),
    )
    selected_indices = tuple(cast(list[int], raw["selected_interval_indices"]))
    if any(not 0 <= index < len(intervals) for index in selected_indices):
        raise ValueError("native topology selection references an invalid interval")
    selected_ids = tuple(
        sorted(intervals[index].interval_id for index in selected_indices)
    )
    rejected = RejectedIntervalCounts.model_validate(raw["rejected_interval_counts"])
    if len(selected_indices) + rejected.total() != len(intervals):
        raise ValueError("native topology selection accounting is incomplete")

    raw_clusters = cast(list[dict[str, Any]], raw["clusters"])
    clusters: list[TopologyTrajectoryCluster] = []
    cluster_membership: list[tuple[int, ...]] = []
    for expected_ordinal, raw_cluster in enumerate(raw_clusters):
        if cast(int, raw_cluster["cluster_ordinal"]) != expected_ordinal:
            raise ValueError("native topology cluster order is not canonical")
        member_indices = tuple(cast(list[int], raw_cluster["interval_indices"]))
        if any(index not in selected_indices for index in member_indices):
            raise ValueError("native topology cluster contains an unselected interval")
        supporting_ids = tuple(
            sorted(intervals[index].interval_id for index in member_indices)
        )
        expected_traversal_count = len(
            {intervals[index].traversal_id for index in member_indices}
        )
        if (
            cast(int, raw_cluster["independent_traversal_count"])
            != expected_traversal_count
        ):
            raise ValueError("native topology traversal count is inconsistent")
        start_index = cast(int | None, raw_cluster["start_node_index"])
        end_index = cast(int | None, raw_cluster["end_node_index"])
        if start_index is not None and not 0 <= start_index < len(graph.nodes):
            raise ValueError("native topology cluster start node is invalid")
        if end_index is not None and not 0 <= end_index < len(graph.nodes):
            raise ValueError("native topology cluster end node is invalid")
        cluster_payload: dict[str, object] = {
            "supporting_interval_ids": supporting_ids,
            "independent_traversal_count": expected_traversal_count,
            "fitted_corridor_local_m": tuple(
                _point(item)
                for item in cast(list[object], raw_cluster["fitted_corridor"])
            ),
            "start_node_id": None
            if start_index is None
            else graph.nodes[start_index].node_id,
            "end_node_id": None
            if end_index is None
            else graph.nodes[end_index].node_id,
            "start_endpoint_distance_m": raw_cluster["start_endpoint_distance_m"],
            "end_endpoint_distance_m": raw_cluster["end_endpoint_distance_m"],
        }
        clusters.append(
            TopologyTrajectoryCluster.model_validate(
                cluster_payload
                | {"cluster_id": _stable_id("topology-cluster", cluster_payload)}
            )
        )
        cluster_membership.append(member_indices)
    flattened = [index for members in cluster_membership for index in members]
    if sorted(flattened) != sorted(selected_indices) or len(flattened) != len(
        set(flattened)
    ):
        raise ValueError("native topology clusters do not partition selected intervals")

    hypotheses: list[TopologyReviewHypothesis] = []
    seen_cluster_indices: set[int] = set()
    for raw_hypothesis in cast(list[dict[str, Any]], raw["hypotheses"]):
        cluster_index = cast(int, raw_hypothesis["cluster_result_index"])
        if (
            not 0 <= cluster_index < len(clusters)
            or cluster_index in seen_cluster_indices
        ):
            raise ValueError("native topology hypothesis cluster reference is invalid")
        seen_cluster_indices.add(cluster_index)
        start_index = cast(int, raw_hypothesis["start_node_index"])
        end_index = cast(int, raw_hypothesis["end_node_index"])
        if not 0 <= start_index < len(graph.nodes) or not 0 <= end_index < len(
            graph.nodes
        ):
            raise ValueError("native topology hypothesis endpoint is invalid")
        compared_arc_index = cast(int | None, raw_hypothesis["compared_arc_index"])
        if compared_arc_index is not None and not 0 <= compared_arc_index < len(
            graph.arcs
        ):
            raise ValueError("native topology hypothesis compared arc is invalid")
        hypothesis_payload: dict[str, object] = {
            "result_label": "REVIEW_HYPOTHESIS_NOT_GROUND_TRUTH",
            "kind": TopologyHypothesisKind(cast(str, raw_hypothesis["kind"])),
            "source_road_graph_id": graph.source_road_graph_id,
            "cluster_id": clusters[cluster_index].cluster_id,
            "start_node_id": graph.nodes[start_index].node_id,
            "end_node_id": graph.nodes[end_index].node_id,
            "compared_arc_id": (
                None
                if compared_arc_index is None
                else graph.arcs[compared_arc_index].arc_id
            ),
            "endpoint_localization_error_m": raw_hypothesis[
                "endpoint_localization_error_m"
            ],
            "geometry_corridor_error_m": raw_hypothesis["geometry_corridor_error_m"],
            "review_required": True,
            "automatic_map_edit_permitted": False,
            "ground_truth_status": "NOT_GROUND_TRUTH",
        }
        hypotheses.append(
            TopologyReviewHypothesis.model_validate(
                hypothesis_payload
                | {
                    "hypothesis_id": _stable_id(
                        "topology-hypothesis", hypothesis_payload
                    )
                }
            )
        )
    hypotheses.sort(key=lambda item: item.hypothesis_id)
    report_payload: dict[str, object] = {
        "schema_version": "cartosentry.topology-hypothesis-report.v1",
        "algorithm_backend": ALGORITHM_BACKEND,
        "source_road_graph_id": graph.source_road_graph_id,
        "topology_graph_view_id": graph.graph_view_id,
        "profile_file_sha256": profile_file_sha256,
        "profile_immutable_sha256": profile.immutable_sha256,
        "selected_interval_ids": selected_ids,
        "rejected_interval_counts": rejected.model_dump(mode="json"),
        "clusters": tuple(item.model_dump(mode="json") for item in clusters),
        "hypotheses": tuple(item.model_dump(mode="json") for item in hypotheses),
        "result_semantics": (
            "REVIEW_HYPOTHESES_ONLY_NO_GROUND_TRUTH_OR_AUTOMATIC_MAP_EDIT"
        ),
    }
    return TopologyHypothesisReport.model_validate_json(
        json.dumps(
            report_payload
            | {"report_id": _stable_id("topology-report", report_payload)},
            allow_nan=False,
        )
    )


__all__ = [
    "PROFILE_FILE_SHA256",
    "PROFILE_IMMUTABLE_SHA256",
    "OffMapTrajectoryInterval",
    "RejectedIntervalCounts",
    "TopologyGraphArc",
    "TopologyGraphNode",
    "TopologyGraphView",
    "TopologyHypothesisKind",
    "TopologyHypothesisProfile",
    "TopologyHypothesisReport",
    "TopologyReviewHypothesis",
    "TopologyTrajectoryCluster",
    "load_topology_hypothesis_profile",
    "make_off_map_trajectory_interval",
    "make_topology_graph_view",
    "make_topology_graph_view_from_primitives",
    "mine_topology_hypotheses",
]
