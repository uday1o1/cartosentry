"""Persisted contracts for deterministic V1 synthetic fixtures."""

from __future__ import annotations

import math
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from . import _core
from .contracts import ContractModel, FrameId, Sha256, TimePoint
from .identifiers import (
    assert_portable,
    make_road_graph_id,
    make_synthetic_fixture_id,
)

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]
PortableKey = Annotated[str, StringConstraints(min_length=1)]
StableId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]*-sha256-[0-9a-f]{64}$"),
]
NonnegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]
Vector3 = tuple[float, float, float]
Matrix4 = tuple[
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
    float,
]


class SyntheticScenario(StrEnum):
    STRAIGHT = "straight"
    CONSTANT_RADIUS_TURN = "constant_radius_turn"
    STOP_START = "stop_start"
    PARALLEL_ROADS = "parallel_roads"
    RAMP = "ramp"
    OVERPASS = "overpass"
    OFF_MAP_CONNECTION = "off_map_connection"
    STATIONARY = "stationary"


class ScenarioFeature(StrEnum):
    STRAIGHT = "straight"
    TURN = "turn"
    STOP_START = "stop_start"
    PARALLEL_ROAD = "parallel_road"
    RAMP = "ramp"
    OVERPASS = "overpass"
    OFF_MAP_CONNECTION = "off_map_connection"
    LOW_EXCITATION = "low_excitation"


class MotionState(StrEnum):
    MOVING = "MOVING"
    STOPPED = "STOPPED"
    OFF_MAP = "OFF_MAP"


class SyntheticTransform(ContractModel):
    """Checked row-major 4 by 4 T_target_source transform."""

    target_frame: FrameId
    source_frame: FrameId
    row_major_4x4: Matrix4
    convention: Literal["T_target_source"] = "T_target_source"
    serialization_order: Literal["row_major"] = "row_major"

    @model_validator(mode="after")
    def validate_rigid_matrix(self) -> Self:
        values = self.row_major_4x4
        if values[12:] != (0.0, 0.0, 0.0, 1.0):
            raise ValueError("rigid transform bottom row must be 0, 0, 0, 1")
        rotation = (
            values[0],
            values[1],
            values[2],
            values[4],
            values[5],
            values[6],
            values[8],
            values[9],
            values[10],
        )
        _core.quaternion_from_rotation_matrix(rotation)
        return self

    @property
    def translation_m(self) -> Vector3:
        values = self.row_major_4x4
        return (values[3], values[7], values[11])

    def apply(self, point_source: Vector3) -> Vector3:
        values = self.row_major_4x4
        x, y, z = point_source
        return (
            values[0] * x + values[1] * y + values[2] * z + values[3],
            values[4] * x + values[5] * y + values[6] * z + values[7],
            values[8] * x + values[9] * y + values[10] * z + values[11],
        )

    def apply_direction(self, direction_source: Vector3) -> Vector3:
        values = self.row_major_4x4
        x, y, z = direction_source
        return (
            values[0] * x + values[1] * y + values[2] * z,
            values[4] * x + values[5] * y + values[6] * z,
            values[8] * x + values[9] * y + values[10] * z,
        )


class RoadNode(ContractModel):
    node_id: Identifier
    position_m: Vector3


class DirectedRoadArc(ContractModel):
    directed_arc_id: Identifier
    from_node_id: Identifier
    to_node_id: Identifier
    polyline_m: tuple[Vector3, ...]
    layer: int
    traversal: Literal["FORWARD_ONLY"] = "FORWARD_ONLY"

    @model_validator(mode="after")
    def validate_polyline(self) -> Self:
        if len(self.polyline_m) < 2:
            raise ValueError("directed road arc needs at least two polyline points")
        return self


class DirectedRoadGraph(ContractModel):
    road_graph_id: StableId
    nodes: tuple[RoadNode, ...]
    directed_arcs: tuple[DirectedRoadArc, ...]

    def identity_payload(self) -> dict[str, object]:
        return {
            "directed_arcs": [
                item.model_dump(mode="json") for item in self.directed_arcs
            ],
            "nodes": [item.model_dump(mode="json") for item in self.nodes],
        }

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        node_by_id = {item.node_id: item for item in self.nodes}
        if len(node_by_id) != len(self.nodes) or not node_by_id:
            raise ValueError("road graph node identifiers must be nonempty and unique")
        arc_ids = [item.directed_arc_id for item in self.directed_arcs]
        if len(arc_ids) != len(set(arc_ids)) or not arc_ids:
            raise ValueError("road graph arc identifiers must be nonempty and unique")
        for arc in self.directed_arcs:
            if arc.from_node_id not in node_by_id or arc.to_node_id not in node_by_id:
                raise ValueError("road arc references an unknown endpoint")
            if arc.polyline_m[0] != node_by_id[arc.from_node_id].position_m:
                raise ValueError("road arc polyline does not begin at from node")
            if arc.polyline_m[-1] != node_by_id[arc.to_node_id].position_m:
                raise ValueError("road arc polyline does not end at to node")
        if self.road_graph_id != make_road_graph_id(self.identity_payload()):
            raise ValueError("road_graph_id does not match graph contents")
        return self


class CylinderLandmark(ContractModel):
    landmark_id: Identifier
    center_xy_m: tuple[float, float]
    base_z_m: float
    radius_m: PositiveFloat
    height_m: PositiveFloat


class SyntheticWorld(ContractModel):
    world_frame: Literal["synthetic_world"] = "synthetic_world"
    ground_z_m: float
    landmarks: tuple[CylinderLandmark, ...]

    @model_validator(mode="after")
    def validate_landmarks(self) -> Self:
        identifiers = [item.landmark_id for item in self.landmarks]
        if len(identifiers) != len(set(identifiers)) or not identifiers:
            raise ValueError("landmark identifiers must be nonempty and unique")
        return self


class SyntheticRig(ContractModel):
    rig_frame: Literal["rig"] = "rig"
    lidar_frame: Literal["lidar"] = "lidar"
    rig_from_lidar: SyntheticTransform

    @model_validator(mode="after")
    def validate_extrinsic_names(self) -> Self:
        if (
            self.rig_from_lidar.target_frame != self.rig_frame
            or self.rig_from_lidar.source_frame != self.lidar_frame
        ):
            raise ValueError("lidar extrinsic frame names do not match the rig")
        return self


class TrajectoryPose(ContractModel):
    time: TimePoint
    world_from_rig: SyntheticTransform
    directed_arc_id: Identifier | None
    motion_state: MotionState


class SpinningLidarConfig(ContractModel):
    scan_period_ns: PositiveInt
    azimuth_columns: PositiveInt
    elevation_angles_rad: tuple[float, ...]
    maximum_range_m: PositiveFloat

    @model_validator(mode="after")
    def validate_angles(self) -> Self:
        if not self.elevation_angles_rad:
            raise ValueError("spinning lidar needs at least one elevation angle")
        if self.scan_period_ns % self.azimuth_columns != 0:
            raise ValueError("scan period must divide exactly into azimuth columns")
        if any(abs(value) >= math.pi / 2.0 for value in self.elevation_angles_rad):
            raise ValueError("lidar elevation angle is outside the open vertical range")
        return self


class LidarPoint(ContractModel):
    column_index: NonnegativeInt
    ring_id: NonnegativeInt
    relative_time_ns: int
    firing_azimuth_rad: float
    elevation_rad: float
    position_lidar_m: Vector3
    range_m: PositiveFloat
    surface_id: Identifier
    expected_world_m: Vector3


class LidarScan(ContractModel):
    frame_id: StableId
    capture_start: TimePoint
    capture_end: TimePoint
    sensor_time: TimePoint
    points: tuple[LidarPoint, ...]

    @model_validator(mode="after")
    def validate_scan_interval(self) -> Self:
        duration = self.capture_end.difference(self.capture_start).value_ns
        midpoint_from_start = self.sensor_time.difference(self.capture_start).value_ns
        if duration <= 0 or midpoint_from_start * 2 != duration:
            raise ValueError("lidar sensor time must be the exact scan midpoint")
        if not self.points:
            raise ValueError("synthetic lidar scan must contain analytic returns")
        column_times: dict[int, int] = {}
        for point in self.points:
            if not -midpoint_from_start <= point.relative_time_ns < midpoint_from_start:
                raise ValueError("lidar point time lies outside the capture interval")
            previous = column_times.setdefault(
                point.column_index, point.relative_time_ns
            )
            if previous != point.relative_time_ns:
                raise ValueError("one lidar column has inconsistent point times")
        return self


class SyntheticTruth(ContractModel):
    scenario_features: tuple[ScenarioFeature, ...]
    off_map_intervals: tuple[tuple[TimePoint, TimePoint], ...]

    @model_validator(mode="after")
    def validate_off_map_intervals(self) -> Self:
        for start, end in self.off_map_intervals:
            if end.difference(start).value_ns <= 0:
                raise ValueError("off-map truth interval must be nonempty")
        return self


class SyntheticFixture(ContractModel):
    schema_version: Literal["cartosentry.synthetic-fixture.v1"]
    fixture_id: StableId
    generator_version: Literal["1.0.0"]
    seed: NonnegativeInt
    synthetic_family_id: Identifier
    partition: Literal["development"]
    scenario: SyntheticScenario
    sample_period_ns: PositiveInt
    world: SyntheticWorld
    road_graph: DirectedRoadGraph
    rig: SyntheticRig
    trajectory: tuple[TrajectoryPose, ...]
    lidar_config: SpinningLidarConfig
    lidar_scans: tuple[LidarScan, ...]
    truth: SyntheticTruth

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"fixture_id"})

    @model_validator(mode="after")
    def validate_fixture(self) -> Self:
        if not self.trajectory or not self.lidar_scans:
            raise ValueError(
                "synthetic fixture needs trajectory and lidar measurements"
            )
        times = [item.time.value_ns for item in self.trajectory]
        if any(right <= left for left, right in pairwise(times)):
            raise ValueError("trajectory sample times must increase strictly")
        if any(
            right - left != self.sample_period_ns for left, right in pairwise(times)
        ):
            raise ValueError(
                "trajectory sample interval does not match sample_period_ns"
            )
        arc_ids = {item.directed_arc_id for item in self.road_graph.directed_arcs}
        if any(
            item.directed_arc_id is not None and item.directed_arc_id not in arc_ids
            for item in self.trajectory
        ):
            raise ValueError("trajectory references an unknown directed road arc")
        if any(
            item.world_from_rig.target_frame != self.world.world_frame
            or item.world_from_rig.source_frame != self.rig.rig_frame
            for item in self.trajectory
        ):
            raise ValueError(
                "trajectory transform frame names do not match the fixture"
            )
        if any(
            point.column_index >= self.lidar_config.azimuth_columns
            or point.ring_id >= len(self.lidar_config.elevation_angles_rad)
            for scan in self.lidar_scans
            for point in scan.points
        ):
            raise ValueError("lidar point index lies outside the configured sensor")
        for scan in self.lidar_scans:
            duration = scan.capture_end.difference(scan.capture_start).value_ns
            if duration != self.lidar_config.scan_period_ns:
                raise ValueError(
                    "lidar capture duration does not match its configuration"
                )
            for point in scan.points:
                expected_time = (
                    point.column_index
                    * self.lidar_config.scan_period_ns
                    // self.lidar_config.azimuth_columns
                    - self.lidar_config.scan_period_ns // 2
                )
                if point.relative_time_ns != expected_time:
                    raise ValueError("lidar relative point time is not exact")
                expected_elevation = self.lidar_config.elevation_angles_rad[
                    point.ring_id
                ]
                if point.elevation_rad != expected_elevation:
                    raise ValueError("lidar point elevation does not match its ring")
                expected_azimuth = round(
                    2.0
                    * math.pi
                    * point.column_index
                    / self.lidar_config.azimuth_columns,
                    12,
                )
                if point.firing_azimuth_rad != expected_azimuth:
                    raise ValueError("lidar point azimuth does not match its column")
                norm = math.sqrt(sum(value * value for value in point.position_lidar_m))
                if abs(norm - point.range_m) > 1e-9:
                    raise ValueError("lidar point range disagrees with its coordinates")
        assert_portable(self.model_dump(mode="json"), location="synthetic fixture")
        if self.fixture_id != make_synthetic_fixture_id(self.identity_payload()):
            raise ValueError("fixture_id does not match generated contents")
        return self


class FixtureFileRecord(ContractModel):
    synthetic_family_id: Identifier
    scenario: SyntheticScenario
    seed: NonnegativeInt
    relative_path: PortableKey
    sha256: Sha256
    fixture_id: StableId


class FixtureSetManifest(ContractModel):
    schema_version: Literal["cartosentry.synthetic-fixture-set.v1"]
    generator_version: Literal["1.0.0"]
    partition: Literal["development"]
    split_manifest_sha256: Sha256
    fixtures: tuple[FixtureFileRecord, ...]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        families = [item.synthetic_family_id for item in self.fixtures]
        paths = [item.relative_path for item in self.fixtures]
        if len(families) != len(set(families)) or not families:
            raise ValueError("fixture families must be nonempty and unique")
        if len(paths) != len(set(paths)):
            raise ValueError("fixture paths must be unique")
        assert_portable(
            self.model_dump(mode="json"), location="synthetic fixture manifest"
        )
        return self


__all__ = [
    "CylinderLandmark",
    "DirectedRoadArc",
    "DirectedRoadGraph",
    "FixtureFileRecord",
    "FixtureSetManifest",
    "LidarPoint",
    "LidarScan",
    "MotionState",
    "RoadNode",
    "ScenarioFeature",
    "SpinningLidarConfig",
    "SyntheticFixture",
    "SyntheticRig",
    "SyntheticScenario",
    "SyntheticTransform",
    "SyntheticTruth",
    "SyntheticWorld",
    "TrajectoryPose",
]
