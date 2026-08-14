"""Deterministic analytic trajectory and spinning-lidar fixture generation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .contracts import (
    RawTime,
    RawTimeEncoding,
    TimeEpoch,
    TimePoint,
    TimeReference,
    TimeRounding,
)
from .identifiers import make_frame_id, make_road_graph_id, make_synthetic_fixture_id
from .manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from .synthetic_models import (
    CylinderLandmark,
    DirectedRoadArc,
    DirectedRoadGraph,
    FixtureFileRecord,
    FixtureSetManifest,
    LidarPoint,
    LidarScan,
    MotionState,
    RoadNode,
    ScenarioFeature,
    SpinningLidarConfig,
    SyntheticFixture,
    SyntheticRig,
    SyntheticScenario,
    SyntheticTransform,
    SyntheticTruth,
    SyntheticWorld,
    TrajectoryPose,
    Vector3,
)

GENERATOR_VERSION: Final = "1.0.1"
FIXTURE_SCHEMA_VERSION: Final = "cartosentry.synthetic-fixture.v1"
FIXTURE_SET_SCHEMA_VERSION: Final = "cartosentry.synthetic-fixture-set.v1"
CLOCK_ID: Final = "cartosentry-synthetic-clock"
DURATION_NS: Final = 4_000_000_000
SCAN_PERIOD_NS: Final = 500_000_000
AZIMUTH_COLUMNS: Final = 16
COLUMN_PERIOD_NS: Final = SCAN_PERIOD_NS // AZIMUTH_COLUMNS
ELEVATION_ANGLES_RAD: Final = (-0.14, -0.07, 0.02, 0.1)
MAXIMUM_RANGE_M: Final = 50.0
_SCENARIOS: Final = tuple(SyntheticScenario)
MAXIMUM_FIXTURE_SET_MANIFEST_BYTES: Final = 1024 * 1024


@dataclass(frozen=True)
class _Pose:
    position_m: Vector3
    yaw_rad: float
    directed_arc_id: str | None
    motion_state: MotionState


class _SplitMix64:
    """Small specified PRNG whose output is independent of Python and NumPy."""

    def __init__(self, seed: int) -> None:
        self._state = seed & 0xFFFFFFFFFFFFFFFF

    def unit(self) -> float:
        self._state = (self._state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        value = self._state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        value ^= value >> 31
        return ((value >> 11) & ((1 << 53) - 1)) / float(1 << 53)

    def symmetric(self, magnitude: float) -> float:
        return (2.0 * self.unit() - 1.0) * magnitude


def _q(value: float) -> float:
    rounded = round(value, 12)
    return 0.0 if rounded == 0.0 else rounded


def _vector(x: float, y: float, z: float) -> Vector3:
    return (_q(x), _q(y), _q(z))


def _time_point(
    value_ns: int,
    family_id: str,
    field: str,
    reference: TimeReference,
) -> TimePoint:
    source_key = f"synthetic/{family_id}/time"
    return TimePoint(
        value_ns=value_ns,
        epoch=TimeEpoch.SENSOR_BOOT,
        clock_id=CLOCK_ID,
        reference=reference,
        raw=RawTime(
            source_key=source_key,
            field=field,
            unit="ns",
            epoch=TimeEpoch.SENSOR_BOOT,
            reference=reference,
            encoding=RawTimeEncoding.UNSIGNED_INTEGER,
            integer_value=str(value_ns),
            rounding=TimeRounding.EXACT,
            maximum_conversion_error_ns=0.0,
        ),
    )


def _transform(
    target: str,
    source: str,
    position_m: Vector3,
    yaw_rad: float,
) -> SyntheticTransform:
    cosine = _q(math.cos(yaw_rad))
    sine = _q(math.sin(yaw_rad))
    x, y, z = position_m
    return SyntheticTransform(
        target_frame=target,
        source_frame=source,
        row_major_4x4=(
            cosine,
            -sine,
            0.0,
            x,
            sine,
            cosine,
            0.0,
            y,
            0.0,
            0.0,
            1.0,
            z,
            0.0,
            0.0,
            0.0,
            1.0,
        ),
    )


def _arc(
    arc_id: str,
    start_id: str,
    end_id: str,
    points: tuple[Vector3, ...],
    *,
    layer: int = 0,
) -> DirectedRoadArc:
    return DirectedRoadArc(
        directed_arc_id=arc_id,
        from_node_id=start_id,
        to_node_id=end_id,
        polyline_m=points,
        layer=layer,
    )


def _graph(
    nodes: tuple[RoadNode, ...], arcs: tuple[DirectedRoadArc, ...]
) -> DirectedRoadGraph:
    identity: dict[str, object] = {
        "directed_arcs": [item.model_dump(mode="json") for item in arcs],
        "nodes": [item.model_dump(mode="json") for item in nodes],
    }
    return DirectedRoadGraph(
        road_graph_id=make_road_graph_id(identity),
        nodes=nodes,
        directed_arcs=arcs,
    )


def _scenario_graph(scenario: SyntheticScenario) -> DirectedRoadGraph:
    nodes: tuple[RoadNode, ...]
    if scenario is SyntheticScenario.CONSTANT_RADIUS_TURN:
        start = RoadNode(node_id="turn-start", position_m=(-10.0, 0.0, 0.0))
        bend = RoadNode(node_id="turn-bend", position_m=(-5.0, 0.0, 0.0))
        exit_node = RoadNode(node_id="turn-exit", position_m=(0.0, 5.0, 0.0))
        end = RoadNode(node_id="turn-end", position_m=(0.0, 13.0, 0.0))
        curve = tuple(
            _vector(
                -5.0 + 5.0 * math.cos(-math.pi / 2.0 + index * math.pi / 16.0),
                5.0 + 5.0 * math.sin(-math.pi / 2.0 + index * math.pi / 16.0),
                0.0,
            )
            for index in range(9)
        )
        return _graph(
            (start, bend, exit_node, end),
            (
                _arc(
                    "turn-approach",
                    "turn-start",
                    "turn-bend",
                    (start.position_m, bend.position_m),
                ),
                _arc("turn-curve", "turn-bend", "turn-exit", curve),
                _arc(
                    "turn-departure",
                    "turn-exit",
                    "turn-end",
                    (exit_node.position_m, end.position_m),
                ),
            ),
        )
    if scenario is SyntheticScenario.PARALLEL_ROADS:
        nodes = (
            RoadNode(node_id="main-start", position_m=(-10.0, 0.0, 0.0)),
            RoadNode(node_id="main-end", position_m=(10.0, 0.0, 0.0)),
            RoadNode(node_id="parallel-start", position_m=(-10.0, 4.0, 0.0)),
            RoadNode(node_id="parallel-end", position_m=(10.0, 4.0, 0.0)),
        )
        return _graph(
            nodes,
            (
                _arc(
                    "main",
                    "main-start",
                    "main-end",
                    (nodes[0].position_m, nodes[1].position_m),
                ),
                _arc(
                    "parallel",
                    "parallel-start",
                    "parallel-end",
                    (nodes[2].position_m, nodes[3].position_m),
                ),
            ),
        )
    if scenario is SyntheticScenario.RAMP:
        nodes = (
            RoadNode(node_id="ramp-start", position_m=(-10.0, 0.0, 0.0)),
            RoadNode(node_id="ramp-end", position_m=(10.0, 0.0, 2.0)),
        )
        return _graph(
            nodes,
            (
                _arc(
                    "ramp",
                    "ramp-start",
                    "ramp-end",
                    (nodes[0].position_m, nodes[1].position_m),
                    layer=1,
                ),
            ),
        )
    if scenario is SyntheticScenario.OVERPASS:
        nodes = (
            RoadNode(node_id="upper-start", position_m=(-10.0, 0.0, 4.0)),
            RoadNode(node_id="upper-end", position_m=(10.0, 0.0, 4.0)),
            RoadNode(node_id="lower-start", position_m=(0.0, -10.0, 0.0)),
            RoadNode(node_id="lower-end", position_m=(0.0, 10.0, 0.0)),
        )
        return _graph(
            nodes,
            (
                _arc(
                    "upper",
                    "upper-start",
                    "upper-end",
                    (nodes[0].position_m, nodes[1].position_m),
                    layer=1,
                ),
                _arc(
                    "lower",
                    "lower-start",
                    "lower-end",
                    (nodes[2].position_m, nodes[3].position_m),
                    layer=0,
                ),
            ),
        )
    if scenario is SyntheticScenario.OFF_MAP_CONNECTION:
        nodes = (
            RoadNode(node_id="known-start", position_m=(-10.0, 0.0, 0.0)),
            RoadNode(node_id="known-end", position_m=(0.0, 0.0, 0.0)),
        )
        return _graph(
            nodes,
            (
                _arc(
                    "known",
                    "known-start",
                    "known-end",
                    (nodes[0].position_m, nodes[1].position_m),
                ),
            ),
        )
    if scenario is SyntheticScenario.STOP_START:
        end_x = 2.0
        arc_id = "stop-start"
    elif scenario is SyntheticScenario.STATIONARY:
        end_x = 1.0
        arc_id = "stationary-road"
    else:
        end_x = 10.0
        arc_id = "main"
    nodes = (
        RoadNode(node_id="start", position_m=(-10.0, 0.0, 0.0)),
        RoadNode(node_id="end", position_m=(end_x, 0.0, 0.0)),
    )
    return _graph(
        nodes,
        (_arc(arc_id, "start", "end", (nodes[0].position_m, nodes[1].position_m)),),
    )


def _pose_at(scenario: SyntheticScenario, time_ns: int) -> _Pose:
    seconds = time_ns / 1_000_000_000.0
    if scenario is SyntheticScenario.CONSTANT_RADIUS_TURN:
        curve_end = 1.0 + math.pi / 2.0
        if seconds < 1.0:
            return _Pose(
                _vector(-10.0 + 5.0 * seconds, 0.0, 0.0),
                0.0,
                "turn-approach",
                MotionState.MOVING,
            )
        if seconds < curve_end:
            angle = -math.pi / 2.0 + (seconds - 1.0)
            return _Pose(
                _vector(-5.0 + 5.0 * math.cos(angle), 5.0 + 5.0 * math.sin(angle), 0.0),
                _q(angle + math.pi / 2.0),
                "turn-curve",
                MotionState.MOVING,
            )
        return _Pose(
            _vector(0.0, 5.0 + 5.0 * (seconds - curve_end), 0.0),
            math.pi / 2.0,
            "turn-departure",
            MotionState.MOVING,
        )
    if scenario is SyntheticScenario.STOP_START:
        if seconds < 1.5:
            x = -10.0 + 4.0 * seconds
            state = MotionState.MOVING
        elif seconds < 2.5:
            x = -4.0
            state = MotionState.STOPPED
        else:
            x = -4.0 + 4.0 * (seconds - 2.5)
            state = MotionState.MOVING
        return _Pose(_vector(x, 0.0, 0.0), 0.0, "stop-start", state)
    if scenario is SyntheticScenario.RAMP:
        x = -10.0 + 5.0 * seconds
        return _Pose(
            _vector(x, 0.0, (x + 10.0) / 10.0), 0.0, "ramp", MotionState.MOVING
        )
    if scenario is SyntheticScenario.OVERPASS:
        return _Pose(
            _vector(-10.0 + 5.0 * seconds, 0.0, 4.0), 0.0, "upper", MotionState.MOVING
        )
    if scenario is SyntheticScenario.OFF_MAP_CONNECTION:
        x = -10.0 + 5.0 * seconds
        if time_ns >= 2_000_000_000:
            return _Pose(_vector(x, 0.0, 0.0), 0.0, None, MotionState.OFF_MAP)
        return _Pose(_vector(x, 0.0, 0.0), 0.0, "known", MotionState.MOVING)
    if scenario is SyntheticScenario.STATIONARY:
        return _Pose((0.0, 0.0, 0.0), 0.0, "stationary-road", MotionState.STOPPED)
    arc_id = "main"
    return _Pose(
        _vector(-10.0 + 5.0 * seconds, 0.0, 0.0), 0.0, arc_id, MotionState.MOVING
    )


def _feature(scenario: SyntheticScenario) -> ScenarioFeature:
    return {
        SyntheticScenario.STRAIGHT: ScenarioFeature.STRAIGHT,
        SyntheticScenario.CONSTANT_RADIUS_TURN: ScenarioFeature.TURN,
        SyntheticScenario.STOP_START: ScenarioFeature.STOP_START,
        SyntheticScenario.PARALLEL_ROADS: ScenarioFeature.PARALLEL_ROAD,
        SyntheticScenario.RAMP: ScenarioFeature.RAMP,
        SyntheticScenario.OVERPASS: ScenarioFeature.OVERPASS,
        SyntheticScenario.OFF_MAP_CONNECTION: ScenarioFeature.OFF_MAP_CONNECTION,
        SyntheticScenario.STATIONARY: ScenarioFeature.LOW_EXCITATION,
    }[scenario]


def _landmarks(scenario: SyntheticScenario, seed: int) -> tuple[CylinderLandmark, ...]:
    random = _SplitMix64(seed)
    result: list[CylinderLandmark] = []
    for index, time_ns in enumerate(
        (
            250_000_000,
            900_000_000,
            1_550_000_000,
            2_200_000_000,
            2_850_000_000,
            3_500_000_000,
        )
    ):
        pose = _pose_at(scenario, time_ns)
        side = -1.0 if index % 2 else 1.0
        offset = side * (4.0 + random.symmetric(0.6))
        x, y, z = pose.position_m
        center_x = x - math.sin(pose.yaw_rad) * offset + random.symmetric(0.25)
        center_y = y + math.cos(pose.yaw_rad) * offset + random.symmetric(0.25)
        result.append(
            CylinderLandmark(
                landmark_id=f"landmark-{index:02d}",
                center_xy_m=(_q(center_x), _q(center_y)),
                base_z_m=z,
                radius_m=_q(0.35 + 0.1 * random.unit()),
                height_m=_q(3.0 + 0.5 * random.unit()),
            )
        )
    return tuple(result)


def _raycast(
    origin_world: Vector3,
    direction_world: Vector3,
    world: SyntheticWorld,
    maximum_range_m: float,
) -> tuple[float, str] | None:
    candidates: list[tuple[float, str]] = []
    origin_x, origin_y, origin_z = origin_world
    direction_x, direction_y, direction_z = direction_world
    if direction_z < 0.0:
        distance = (world.ground_z_m - origin_z) / direction_z
        if distance > 0.0:
            candidates.append((distance, "ground"))
    quadratic_a = direction_x * direction_x + direction_y * direction_y
    if quadratic_a > 1e-15:
        for landmark in world.landmarks:
            offset_x = origin_x - landmark.center_xy_m[0]
            offset_y = origin_y - landmark.center_xy_m[1]
            quadratic_b = 2.0 * (offset_x * direction_x + offset_y * direction_y)
            quadratic_c = (
                offset_x * offset_x
                + offset_y * offset_y
                - landmark.radius_m * landmark.radius_m
            )
            discriminant = quadratic_b * quadratic_b - 4.0 * quadratic_a * quadratic_c
            if discriminant < 0.0:
                continue
            root = math.sqrt(discriminant)
            for distance in (
                (-quadratic_b - root) / (2.0 * quadratic_a),
                (-quadratic_b + root) / (2.0 * quadratic_a),
            ):
                hit_z = origin_z + distance * direction_z
                if (
                    distance > 0.0
                    and landmark.base_z_m
                    <= hit_z
                    <= landmark.base_z_m + landmark.height_m
                ):
                    candidates.append((distance, landmark.landmark_id))
                    break
    in_range = [item for item in candidates if item[0] <= maximum_range_m]
    return min(in_range) if in_range else None


def _lidar_scan(
    family_id: str,
    scenario: SyntheticScenario,
    scan_start_ns: int,
    world: SyntheticWorld,
    rig: SyntheticRig,
    lidar_config: SpinningLidarConfig,
) -> LidarScan:
    start = _time_point(
        scan_start_ns, family_id, "scan_start", TimeReference.SCAN_START
    )
    end = _time_point(
        scan_start_ns + lidar_config.scan_period_ns,
        family_id,
        "scan_end",
        TimeReference.SCAN_END,
    )
    midpoint_ns = scan_start_ns + lidar_config.scan_period_ns // 2
    midpoint = _time_point(
        midpoint_ns, family_id, "scan_midpoint", TimeReference.SCAN_MIDPOINT
    )
    points: list[LidarPoint] = []
    column_period_ns = lidar_config.scan_period_ns // lidar_config.azimuth_columns
    for column in range(lidar_config.azimuth_columns):
        firing_ns = scan_start_ns + column * column_period_ns
        pose = _pose_at(scenario, firing_ns)
        world_from_rig = _transform(
            "synthetic_world", "rig", pose.position_m, pose.yaw_rad
        )
        lidar_origin_rig = rig.rig_from_lidar.translation_m
        origin_world = world_from_rig.apply(lidar_origin_rig)
        azimuth = 2.0 * math.pi * column / lidar_config.azimuth_columns
        for ring_id, elevation in enumerate(lidar_config.elevation_angles_rad):
            cosine_elevation = math.cos(elevation)
            direction_lidar = (
                cosine_elevation * math.cos(azimuth),
                cosine_elevation * math.sin(azimuth),
                math.sin(elevation),
            )
            direction_rig = rig.rig_from_lidar.apply_direction(direction_lidar)
            direction_world = world_from_rig.apply_direction(direction_rig)
            hit = _raycast(
                origin_world,
                direction_world,
                world,
                lidar_config.maximum_range_m,
            )
            if hit is None:
                continue
            distance, surface_id = hit
            position_lidar = _vector(
                direction_lidar[0] * distance,
                direction_lidar[1] * distance,
                direction_lidar[2] * distance,
            )
            expected_world = _vector(
                origin_world[0] + direction_world[0] * distance,
                origin_world[1] + direction_world[1] * distance,
                origin_world[2] + direction_world[2] * distance,
            )
            points.append(
                LidarPoint(
                    column_index=column,
                    ring_id=ring_id,
                    relative_time_ns=firing_ns - midpoint_ns,
                    firing_azimuth_rad=_q(azimuth),
                    elevation_rad=elevation,
                    position_lidar_m=position_lidar,
                    range_m=_q(distance),
                    surface_id=surface_id,
                    expected_world_m=expected_world,
                )
            )
    interval = {
        "capture_end_ns": end.value_ns,
        "capture_start_ns": start.value_ns,
        "epoch": TimeEpoch.SENSOR_BOOT.value,
    }
    frame_id = make_frame_id(
        f"synthetic-lidar-{family_id}",
        f"scan-{scan_start_ns}",
        interval,
    )
    return LidarScan(
        frame_id=frame_id,
        capture_start=start,
        capture_end=end,
        sensor_time=midpoint,
        points=tuple(points),
    )


def generate_fixture(
    family_id: str,
    scenario: SyntheticScenario,
    seed: int,
    *,
    azimuth_columns: int = AZIMUTH_COLUMNS,
) -> SyntheticFixture:
    """Generate one complete deterministic fixture from semantic inputs."""

    road_graph = _scenario_graph(scenario)
    world = SyntheticWorld(
        ground_z_m=0.0,
        landmarks=_landmarks(scenario, seed),
    )
    rig = SyntheticRig(rig_from_lidar=_transform("rig", "lidar", (0.0, 0.0, 1.8), 0.0))
    if azimuth_columns <= 0 or SCAN_PERIOD_NS % azimuth_columns != 0:
        raise ValueError("azimuth columns must divide the fixed scan period exactly")
    sample_period_ns = SCAN_PERIOD_NS // azimuth_columns
    trajectory = tuple(
        TrajectoryPose(
            time=_time_point(
                time_ns, family_id, "trajectory_sample", TimeReference.SAMPLE
            ),
            world_from_rig=_transform(
                "synthetic_world",
                "rig",
                _pose_at(scenario, time_ns).position_m,
                _pose_at(scenario, time_ns).yaw_rad,
            ),
            directed_arc_id=_pose_at(scenario, time_ns).directed_arc_id,
            motion_state=_pose_at(scenario, time_ns).motion_state,
        )
        for time_ns in range(0, DURATION_NS + 1, sample_period_ns)
    )
    lidar_config = SpinningLidarConfig(
        scan_period_ns=SCAN_PERIOD_NS,
        azimuth_columns=azimuth_columns,
        elevation_angles_rad=ELEVATION_ANGLES_RAD,
        maximum_range_m=MAXIMUM_RANGE_M,
    )
    scans = tuple(
        _lidar_scan(family_id, scenario, start_ns, world, rig, lidar_config)
        for start_ns in range(0, DURATION_NS, SCAN_PERIOD_NS)
    )
    off_map = (
        (
            (
                _time_point(
                    2_000_000_000, family_id, "off_map_start", TimeReference.SAMPLE
                ),
                _time_point(
                    DURATION_NS, family_id, "off_map_end", TimeReference.SAMPLE
                ),
            ),
        )
        if scenario is SyntheticScenario.OFF_MAP_CONNECTION
        else ()
    )
    payload: dict[str, object] = {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "synthetic_family_id": family_id,
        "partition": "development",
        "scenario": scenario,
        "sample_period_ns": sample_period_ns,
        "world": world,
        "road_graph": road_graph,
        "rig": rig,
        "trajectory": trajectory,
        "lidar_config": lidar_config,
        "lidar_scans": scans,
        "truth": SyntheticTruth(
            scenario_features=(_feature(scenario),),
            off_map_intervals=off_map,
        ),
    }
    unchecked = SyntheticFixture.model_construct(
        fixture_id="unchecked",
        **payload,  # type: ignore[arg-type]
    )
    fixture_id = make_synthetic_fixture_id(unchecked.identity_payload())
    return SyntheticFixture(fixture_id=fixture_id, **payload)  # type: ignore[arg-type]


def _serialize(value: SyntheticFixture | FixtureSetManifest) -> bytes:
    dump_options: dict[str, object]
    if isinstance(value, SyntheticFixture):
        dump_options = {"separators": (",", ":")}
    else:
        dump_options = {"indent": 2}
    return (
        json.dumps(
            value.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            **dump_options,  # type: ignore[arg-type]
        )
        + "\n"
    ).encode("utf-8")


def serialize_fixture(fixture: SyntheticFixture) -> bytes:
    """Serialize one fixture in the canonical byte form accepted by fault injection."""

    return _serialize(fixture)


def _development_families(split_manifest_path: Path) -> tuple[tuple[str, int], ...]:
    split = json.loads(split_manifest_path.read_text(encoding="utf-8"))
    candidates = [
        item
        for item in split["synthetic_family_sets"]
        if item["family_set_id"] == "sensor-map-development-v0"
    ]
    if len(candidates) != 1:
        raise ValueError("split manifest must contain one development sensor-map set")
    family_set = candidates[0]
    if family_set["partition"] != "development" or family_set["family_count"] != len(
        _SCENARIOS
    ):
        raise ValueError(
            "development sensor-map family set no longer matches V1 scenarios"
        )
    prefix = str(family_set["family_prefix"])
    seed_start = int(family_set["seed_start"])
    return tuple(
        (f"{prefix}-{index + 1:03d}", seed_start + index)
        for index in range(len(_SCENARIOS))
    )


def render_fixture_set(
    split_manifest_path: Path,
) -> dict[str, bytes]:
    """Render all frozen M1.3 development fixtures and their manifest."""

    rendered: dict[str, bytes] = {}
    records: list[FixtureFileRecord] = []
    for scenario, (family_id, seed) in zip(
        _SCENARIOS, _development_families(split_manifest_path), strict=True
    ):
        fixture = generate_fixture(family_id, scenario, seed)
        relative_path = f"fixtures/{family_id}.json"
        content = _serialize(fixture)
        rendered[relative_path] = content
        records.append(
            FixtureFileRecord(
                synthetic_family_id=family_id,
                scenario=scenario,
                seed=seed,
                relative_path=relative_path,
                sha256=hashlib.sha256(content).hexdigest(),
                fixture_id=fixture.fixture_id,
            )
        )
    manifest = FixtureSetManifest(
        schema_version=FIXTURE_SET_SCHEMA_VERSION,
        generator_version=GENERATOR_VERSION,
        partition="development",
        split_manifest_sha256=hashlib.sha256(
            split_manifest_path.read_bytes()
        ).hexdigest(),
        fixtures=tuple(records),
    )
    rendered["manifest.json"] = _serialize(manifest)
    return rendered


def _write_atomically(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _matches_expected_file(path: Path, expected: bytes, relative: str) -> bool:
    try:
        observed = read_bounded_regular_bytes(
            path,
            maximum_bytes=len(expected),
            context=f"fixture file {relative}",
        )
    except ManifestBoundaryError:
        return False
    return observed == expected


def materialize_fixture_set(
    output_root: Path,
    split_manifest_path: Path,
    *,
    check: bool = False,
) -> dict[str, object]:
    expected = render_fixture_set(split_manifest_path)
    stale = sorted(
        relative
        for relative, content in expected.items()
        if not _matches_expected_file(output_root / relative, content, relative)
    )
    if not check:
        for relative, content in expected.items():
            _write_atomically(output_root / relative, content)
        stale = []
    return {
        "accepted": not stale,
        "file_count": len(expected),
        "generator_version": GENERATOR_VERSION,
        "output_root": output_root.as_posix(),
        "stale_files": stale,
    }


def parse_fixture_set_manifest_bytes(content: bytes) -> FixtureSetManifest:
    """Parse the persisted fixture-set manifest through a bounded boundary."""

    decode_bounded_json(
        content,
        maximum_bytes=MAXIMUM_FIXTURE_SET_MANIFEST_BYTES,
        context="fixture-set manifest",
    )
    return FixtureSetManifest.model_validate_json(content)


def _distance(left: Vector3, right: Vector3) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


def qualify_fixture_set(
    fixture_root: Path,
    split_manifest_path: Path,
    charter_path: Path,
) -> dict[str, object]:
    """Verify deterministic bytes, exact time, and analytic transform agreement."""

    expected = render_fixture_set(split_manifest_path)
    byte_mismatches = sorted(
        relative
        for relative, content in expected.items()
        if not _matches_expected_file(fixture_root / relative, content, relative)
    )
    manifest = parse_fixture_set_manifest_bytes(
        read_bounded_regular_bytes(
            fixture_root / "manifest.json",
            maximum_bytes=MAXIMUM_FIXTURE_SET_MANIFEST_BYTES,
            context="fixture-set manifest",
        )
    )
    charter = json.loads(charter_path.read_text(encoding="utf-8"))
    geometry_limit = float(charter["gates"]["geometry.se3_point_roundtrip_m"]["value"])
    time_limit = int(charter["gates"]["time.persisted_integer_mismatch_count"]["value"])
    maximum_error = 0.0
    time_mismatches = 0
    point_count = 0
    scenarios: set[SyntheticScenario] = set()
    for record in manifest.fixtures:
        fixture = SyntheticFixture.model_validate_json(
            (fixture_root / record.relative_path).read_text(encoding="utf-8")
        )
        scenarios.add(fixture.scenario)
        trajectory_by_time = {pose.time.value_ns: pose for pose in fixture.trajectory}
        for pose in fixture.trajectory:
            recovered = TimePoint.model_validate_json(pose.time.model_dump_json())
            if recovered.value_ns != pose.time.value_ns:
                time_mismatches += 1
        for scan in fixture.lidar_scans:
            for point in scan.points:
                point_count += 1
                expected_relative = (
                    point.column_index * COLUMN_PERIOD_NS - SCAN_PERIOD_NS // 2
                )
                if point.relative_time_ns != expected_relative:
                    time_mismatches += 1
                firing_ns = scan.sensor_time.value_ns + point.relative_time_ns
                firing_pose = trajectory_by_time.get(firing_ns)
                if firing_pose is None:
                    time_mismatches += 1
                    continue
                position_rig = fixture.rig.rig_from_lidar.apply(point.position_lidar_m)
                reconstructed = firing_pose.world_from_rig.apply(position_rig)
                maximum_error = max(
                    maximum_error,
                    _distance(reconstructed, point.expected_world_m),
                )
    scenario_coverage = scenarios == set(_SCENARIOS)
    accepted = (
        not byte_mismatches
        and scenario_coverage
        and maximum_error <= geometry_limit
        and time_mismatches <= time_limit
        and point_count > 0
    )
    return {
        "accepted": accepted,
        "byte_deterministic": not byte_mismatches,
        "byte_mismatches": byte_mismatches,
        "fixtures_checked": len(manifest.fixtures),
        "generator_version": manifest.generator_version,
        "maximum_trajectory_lidar_world_error_m": maximum_error,
        "point_count": point_count,
        "scenario_coverage_complete": scenario_coverage,
        "time_mismatch_count": time_mismatches,
        "thresholds": {
            "geometry.se3_point_roundtrip_m": geometry_limit,
            "time.persisted_integer_mismatch_count": time_limit,
        },
    }


__all__ = [
    "AZIMUTH_COLUMNS",
    "COLUMN_PERIOD_NS",
    "DURATION_NS",
    "GENERATOR_VERSION",
    "MAXIMUM_FIXTURE_SET_MANIFEST_BYTES",
    "generate_fixture",
    "materialize_fixture_set",
    "parse_fixture_set_manifest_bytes",
    "qualify_fixture_set",
    "render_fixture_set",
    "serialize_fixture",
]
