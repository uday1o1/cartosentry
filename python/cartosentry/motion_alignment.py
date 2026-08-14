"""Per-point-time motion compensation and bounded multi-frame alignment."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry.artifacts import Observability
from cartosentry.contracts import RigidTransform, UnitQuaternion
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.synthetic_models import SyntheticFixture, SyntheticTransform
from cartosentry.trajectory import TrajectoryEvaluation, TrajectorySupport

PROFILE_IMMUTABLE_SHA256 = (
    "e51d033344c3191bd86d6b05a91631cb9b28b25deec8a58919867b6ec7813166"
)
MAXIMUM_PROFILE_BYTES = 256 * 1024


class PointMotionClass(StrEnum):
    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"
    UNKNOWN = "UNKNOWN"


class AlignmentSupport(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNKNOWN_TRAJECTORY = "UNKNOWN_TRAJECTORY"
    UNKNOWN_OBSERVABILITY = "UNKNOWN_OBSERVABILITY"


class AlignmentState(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class ProfileAuthorities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trajectory_gate_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    trajectory_gate_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    lidar_integrity_profile_file_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]


class AlignmentMasks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    near_ego_horizontal_radius_m: Annotated[float, Field(gt=0.0)]
    near_ego_maximum_absolute_height_m: Annotated[float, Field(gt=0.0)]
    exclude_declared_dynamic_points: Literal[True]


class AlignmentObservability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    minimum_valid_points_per_frame: Annotated[int, Field(gt=0)]
    minimum_occupied_voxels_per_frame: Annotated[int, Field(gt=0)]
    minimum_shared_voxels_per_pair: Annotated[int, Field(gt=0)]
    minimum_translation_m: Annotated[float, Field(gt=0.0)]
    minimum_rotation_rad: Annotated[float, Field(gt=0.0)]


class AlignmentThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    minimum_occupancy_jaccard: Annotated[float, Field(gt=0.0, lt=1.0)]
    maximum_shared_surface_thickness_m: Annotated[float, Field(gt=0.0)]


class AlignmentBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    maximum_frames: Annotated[int, Field(gt=1)]
    maximum_points_per_frame: Annotated[int, Field(gt=0)]
    maximum_voxels_per_frame: Annotated[int, Field(gt=0)]
    maximum_pairs: Annotated[int, Field(gt=0)]
    maximum_representative_offsets: Annotated[int, Field(gt=0)]


class LidarAlignmentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    profile_id: Literal["lidar-alignment-v1"]
    profile_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_AFTER_THRESHOLD_CALIBRATION_BEFORE_M4_2_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: ProfileAuthorities
    target_frame: Literal["trajectory_local_world"]
    voxel_size_m: Annotated[float, Field(gt=0.0)]
    masks: AlignmentMasks
    observability: AlignmentObservability
    alignment: AlignmentThresholds
    budgets: AlignmentBudgets

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.immutable_sha256 != PROFILE_IMMUTABLE_SHA256:
            raise ValueError("lidar alignment profile identity is not pinned")
        return self


@dataclass(frozen=True)
class AlignmentPointInput:
    position_lidar_m: tuple[float, float, float]
    relative_time_ns: int
    source_offset: int
    motion_class: PointMotionClass = PointMotionClass.UNKNOWN
    expected_world_m: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class AlignmentFrameInput:
    frame_index: int
    source_key: str
    reference_time_ns: int
    points: Iterable[AlignmentPointInput]


class TrajectoryEvaluator(Protocol):
    def evaluate(self, query_time_ns: int) -> TrajectoryEvaluation: ...


class FrameAlignmentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    frame_index: Annotated[int, Field(ge=0)]
    source_key: Annotated[str, Field(min_length=1)]
    reference_time_ns: int
    support: AlignmentSupport
    trajectory_support_failures: Annotated[int, Field(ge=0)]
    input_point_count: Annotated[int, Field(ge=0)]
    retained_static_or_unknown_point_count: Annotated[int, Field(ge=0)]
    excluded_dynamic_point_count: Annotated[int, Field(ge=0)]
    excluded_near_ego_point_count: Annotated[int, Field(ge=0)]
    occupied_voxel_count: Annotated[int, Field(ge=0)]
    distinct_measurement_time_count: Annotated[int, Field(ge=0)]
    per_point_pose_evaluation_count: Annotated[int, Field(ge=0)]
    analytic_truth_point_count: Annotated[int, Field(ge=0)]
    analytic_truth_coverage_fraction: Annotated[float, Field(ge=0.0, le=1.0)]
    analytic_truth_rmse_m: Annotated[float, Field(ge=0.0)] | None
    representative_unsupported_offsets: tuple[Annotated[int, Field(ge=0)], ...]


class PairAlignmentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    left_frame_index: Annotated[int, Field(ge=0)]
    right_frame_index: Annotated[int, Field(ge=0)]
    start_time_ns: int
    end_time_ns: int
    support: AlignmentSupport
    observability: Observability
    state: AlignmentState
    translation_excitation_m: Annotated[float, Field(ge=0.0)]
    rotation_excitation_rad: Annotated[float, Field(ge=0.0)]
    left_occupied_voxels: Annotated[int, Field(ge=0)]
    right_occupied_voxels: Annotated[int, Field(ge=0)]
    shared_voxels: Annotated[int, Field(ge=0)]
    occupancy_jaccard: Annotated[float, Field(ge=0.0, le=1.0)] | None
    shared_surface_thickness_m: Annotated[float, Field(ge=0.0)] | None
    compatible_causes: tuple[str, ...]


class AlignmentStatistics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    frame_count: Annotated[int, Field(ge=0)]
    pair_count: Annotated[int, Field(ge=0)]
    maximum_input_points_per_frame: Annotated[int, Field(ge=0)]
    maximum_occupied_voxels_per_frame: Annotated[int, Field(ge=0)]
    retained_voxel_frame_upper_bound: Literal[1]


class LidarAlignmentReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["cartosentry.lidar-alignment-report.v1"]
    profile_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    target_frame: Literal["trajectory_local_world"]
    state: AlignmentState
    statistics: AlignmentStatistics
    frames: tuple[FrameAlignmentEvidence, ...]
    pairs: tuple[PairAlignmentEvidence, ...]


@dataclass
class _VoxelAccumulator:
    count: int = 0
    sum_x: float = 0.0
    sum_y: float = 0.0
    sum_z: float = 0.0
    sum_x2: float = 0.0
    sum_y2: float = 0.0
    sum_z2: float = 0.0

    def add(self, point: tuple[float, float, float]) -> None:
        self.count += 1
        self.sum_x += point[0]
        self.sum_y += point[1]
        self.sum_z += point[2]
        self.sum_x2 += point[0] * point[0]
        self.sum_y2 += point[1] * point[1]
        self.sum_z2 += point[2] * point[2]

    @classmethod
    def combine(cls, left: _VoxelAccumulator, right: _VoxelAccumulator) -> Self:
        return cls(
            count=left.count + right.count,
            sum_x=left.sum_x + right.sum_x,
            sum_y=left.sum_y + right.sum_y,
            sum_z=left.sum_z + right.sum_z,
            sum_x2=left.sum_x2 + right.sum_x2,
            sum_y2=left.sum_y2 + right.sum_y2,
            sum_z2=left.sum_z2 + right.sum_z2,
        )

    def thickness(self) -> float:
        if self.count <= 1:
            return 0.0
        inverse = 1.0 / self.count
        variance = (
            max(0.0, self.sum_x2 * inverse - (self.sum_x * inverse) ** 2)
            + max(0.0, self.sum_y2 * inverse - (self.sum_y * inverse) ** 2)
            + max(0.0, self.sum_z2 * inverse - (self.sum_z * inverse) ** 2)
        )
        return math.sqrt(variance)


@dataclass(frozen=True)
class _ProcessedFrame:
    evidence: FrameAlignmentEvidence
    reference_world_from_lidar: RigidTransform | None
    voxels: dict[tuple[int, int, int], _VoxelAccumulator]


def _canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def load_lidar_alignment_profile(
    path: Path,
) -> tuple[LidarAlignmentProfile, str]:
    """Load and self-authenticate the M4.2 profile."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="lidar alignment profile",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="lidar alignment profile",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "lidar alignment profile is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("lidar alignment profile must be an object")
    raw = cast(dict[str, object], decoded)
    expected = raw.get("immutable_sha256")
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if expected != _canonical_hash(canonical):
        raise ValueError("lidar alignment profile immutable hash is invalid")
    return (
        LidarAlignmentProfile.model_validate_json(content),
        hashlib.sha256(content).hexdigest(),
    )


def _synthetic_transform(value: SyntheticTransform) -> RigidTransform:
    matrix = value.row_major_4x4
    return RigidTransform(
        target_frame=value.target_frame,
        source_frame=value.source_frame,
        translation_m=value.translation_m,
        rotation=UnitQuaternion.from_rotation_matrix(
            (
                matrix[0],
                matrix[1],
                matrix[2],
                matrix[4],
                matrix[5],
                matrix[6],
                matrix[8],
                matrix[9],
                matrix[10],
            )
        ),
    )


def synthetic_alignment_frames(
    fixture: SyntheticFixture,
) -> tuple[AlignmentFrameInput, ...]:
    """Adapt exact analytic LiDAR points into the production alignment contract."""

    origin = fixture.trajectory[0].world_from_rig.translation_m
    return tuple(
        AlignmentFrameInput(
            frame_index=frame_index,
            source_key=f"synthetic/lidar/{scan.frame_id}",
            reference_time_ns=scan.sensor_time.value_ns,
            points=tuple(
                AlignmentPointInput(
                    position_lidar_m=point.position_lidar_m,
                    relative_time_ns=point.relative_time_ns,
                    source_offset=point_index,
                    motion_class=PointMotionClass.STATIC,
                    expected_world_m=cast(
                        tuple[float, float, float],
                        tuple(
                            value - offset
                            for value, offset in zip(
                                point.expected_world_m, origin, strict=True
                            )
                        ),
                    ),
                )
                for point_index, point in enumerate(scan.points)
            ),
        )
        for frame_index, scan in enumerate(fixture.lidar_scans)
    )


def synthetic_rig_from_lidar(fixture: SyntheticFixture) -> RigidTransform:
    """Return the exact named analytic rig-from-LiDAR calibration."""

    return _synthetic_transform(fixture.rig.rig_from_lidar)


def _rotation_distance(left: UnitQuaternion, right: UnitQuaternion) -> float:
    dot = abs(sum(a * b for a, b in zip(left.as_wxyz(), right.as_wxyz(), strict=True)))
    return 2.0 * math.acos(min(1.0, max(-1.0, dot)))


class LidarMotionAlignmentAnalyzer:
    """Bounded analyzer that evaluates the trajectory at every retained point time."""

    def __init__(
        self,
        *,
        trajectory: TrajectoryEvaluator,
        rig_from_lidar: RigidTransform,
        profile: LidarAlignmentProfile,
        profile_file_sha256: str,
        source_sha256: str,
    ) -> None:
        if (
            rig_from_lidar.target_frame != "rig"
            or rig_from_lidar.source_frame != "lidar"
        ):
            raise ValueError("alignment calibration must be T_rig_lidar")
        if len(source_sha256) != 64 or any(
            item not in "0123456789abcdef" for item in source_sha256
        ):
            raise ValueError("alignment source hash must be a lowercase SHA-256")
        self.trajectory = trajectory
        self.rig_from_lidar = rig_from_lidar
        self.profile = profile
        self.profile_file_sha256 = profile_file_sha256
        self.source_sha256 = source_sha256
        self.frames: list[FrameAlignmentEvidence] = []
        self.pairs: list[PairAlignmentEvidence] = []
        self.previous: _ProcessedFrame | None = None

    def _cell(self, point: tuple[float, float, float]) -> tuple[int, int, int]:
        size = self.profile.voxel_size_m
        return (
            math.floor(point[0] / size),
            math.floor(point[1] / size),
            math.floor(point[2] / size),
        )

    def process_frame(self, frame: AlignmentFrameInput) -> None:
        if len(self.frames) >= self.profile.budgets.maximum_frames:
            raise ValueError("alignment frame count exceeds the frozen budget")
        if self.frames and frame.frame_index <= self.frames[-1].frame_index:
            raise ValueError("alignment frame indices must increase")
        reference = self.trajectory.evaluate(frame.reference_time_ns)
        reference_world_from_lidar = (
            reference.pose.compose(self.rig_from_lidar)
            if reference.support is TrajectorySupport.SUPPORTED
            and reference.pose is not None
            else None
        )
        point_count = 0
        retained = 0
        dynamic = 0
        near_ego = 0
        unsupported = 0
        pose_evaluations = 0
        measurement_times: set[int] = set()
        offsets: list[int] = []
        truth_squared_error = 0.0
        truth_count = 0
        voxels: dict[tuple[int, int, int], _VoxelAccumulator] = {}
        for point in frame.points:
            point_count += 1
            if point_count > self.profile.budgets.maximum_points_per_frame:
                raise ValueError("alignment points per frame exceed the frozen budget")
            values_to_validate = (
                *point.position_lidar_m,
                float(point.relative_time_ns),
                *(point.expected_world_m or ()),
            )
            if not all(math.isfinite(float(value)) for value in values_to_validate):
                raise ValueError(
                    "alignment input must pass LiDAR structural validation"
                )
            if point.source_offset < 0:
                raise ValueError("alignment point source offsets must be nonnegative")
            if (
                point.motion_class is PointMotionClass.DYNAMIC
                and self.profile.masks.exclude_declared_dynamic_points
            ):
                dynamic += 1
                continue
            horizontal = math.hypot(
                point.position_lidar_m[0], point.position_lidar_m[1]
            )
            if (
                horizontal <= self.profile.masks.near_ego_horizontal_radius_m
                and abs(point.position_lidar_m[2])
                <= self.profile.masks.near_ego_maximum_absolute_height_m
            ):
                near_ego += 1
                continue
            measurement_time_ns = frame.reference_time_ns + point.relative_time_ns
            measurement_times.add(measurement_time_ns)
            evaluated = self.trajectory.evaluate(measurement_time_ns)
            pose_evaluations += 1
            if (
                evaluated.support is not TrajectorySupport.SUPPORTED
                or evaluated.pose is None
            ):
                unsupported += 1
                if len(offsets) < self.profile.budgets.maximum_representative_offsets:
                    offsets.append(point.source_offset)
                continue
            world_from_lidar = evaluated.pose.compose(self.rig_from_lidar)
            world_point = world_from_lidar.apply(point.position_lidar_m)
            if point.expected_world_m is not None:
                truth_squared_error += sum(
                    (observed - expected) ** 2
                    for observed, expected in zip(
                        world_point, point.expected_world_m, strict=True
                    )
                )
                truth_count += 1
            cell = self._cell(world_point)
            accumulator = voxels.get(cell)
            if accumulator is None:
                if len(voxels) >= self.profile.budgets.maximum_voxels_per_frame:
                    raise ValueError("alignment voxel count exceeds the frozen budget")
                accumulator = _VoxelAccumulator()
                voxels[cell] = accumulator
            accumulator.add(world_point)
            retained += 1
        support = (
            AlignmentSupport.SUPPORTED
            if reference_world_from_lidar is not None and unsupported == 0
            else AlignmentSupport.UNKNOWN_TRAJECTORY
        )
        if support is not AlignmentSupport.SUPPORTED:
            voxels = {}
        processed = _ProcessedFrame(
            evidence=FrameAlignmentEvidence(
                frame_index=frame.frame_index,
                source_key=frame.source_key,
                reference_time_ns=frame.reference_time_ns,
                support=support,
                trajectory_support_failures=unsupported
                + int(reference_world_from_lidar is None),
                input_point_count=point_count,
                retained_static_or_unknown_point_count=retained,
                excluded_dynamic_point_count=dynamic,
                excluded_near_ego_point_count=near_ego,
                occupied_voxel_count=len(voxels),
                distinct_measurement_time_count=len(measurement_times),
                per_point_pose_evaluation_count=pose_evaluations,
                analytic_truth_point_count=truth_count,
                analytic_truth_coverage_fraction=(
                    truth_count / retained if retained else 0.0
                ),
                analytic_truth_rmse_m=(
                    math.sqrt(truth_squared_error / truth_count)
                    if truth_count
                    else None
                ),
                representative_unsupported_offsets=tuple(offsets),
            ),
            reference_world_from_lidar=reference_world_from_lidar,
            voxels=voxels,
        )
        self.frames.append(processed.evidence)
        if self.previous is not None:
            if len(self.pairs) >= self.profile.budgets.maximum_pairs:
                raise ValueError("alignment pair count exceeds the frozen budget")
            self.pairs.append(self._pair(self.previous, processed))
        self.previous = processed

    def _pair(
        self, left: _ProcessedFrame, right: _ProcessedFrame
    ) -> PairAlignmentEvidence:
        if (
            left.evidence.support is not AlignmentSupport.SUPPORTED
            or right.evidence.support is not AlignmentSupport.SUPPORTED
            or left.reference_world_from_lidar is None
            or right.reference_world_from_lidar is None
        ):
            return PairAlignmentEvidence(
                left_frame_index=left.evidence.frame_index,
                right_frame_index=right.evidence.frame_index,
                start_time_ns=left.evidence.reference_time_ns,
                end_time_ns=right.evidence.reference_time_ns,
                support=AlignmentSupport.UNKNOWN_TRAJECTORY,
                observability=Observability.NOT_APPLICABLE,
                state=AlignmentState.UNKNOWN,
                translation_excitation_m=0.0,
                rotation_excitation_rad=0.0,
                left_occupied_voxels=len(left.voxels),
                right_occupied_voxels=len(right.voxels),
                shared_voxels=0,
                occupancy_jaccard=None,
                shared_surface_thickness_m=None,
                compatible_causes=("unsupported trajectory gap",),
            )
        left_pose = left.reference_world_from_lidar
        right_pose = right.reference_world_from_lidar
        translation = math.sqrt(
            sum(
                (a - b) ** 2
                for a, b in zip(
                    left_pose.translation_m,
                    right_pose.translation_m,
                    strict=True,
                )
            )
        )
        rotation = _rotation_distance(left_pose.rotation, right_pose.rotation)
        left_cells = set(left.voxels)
        right_cells = set(right.voxels)
        shared = left_cells & right_cells
        union = left_cells | right_cells
        overlap = len(shared) / len(union) if union else 0.0
        thicknesses = [
            _VoxelAccumulator.combine(left.voxels[cell], right.voxels[cell]).thickness()
            for cell in sorted(shared)
        ]
        thickness = sum(thicknesses) / len(thicknesses) if thicknesses else None
        enough_structure = (
            left.evidence.retained_static_or_unknown_point_count
            >= self.profile.observability.minimum_valid_points_per_frame
            and right.evidence.retained_static_or_unknown_point_count
            >= self.profile.observability.minimum_valid_points_per_frame
            and len(left_cells)
            >= self.profile.observability.minimum_occupied_voxels_per_frame
            and len(right_cells)
            >= self.profile.observability.minimum_occupied_voxels_per_frame
            and len(shared) >= self.profile.observability.minimum_shared_voxels_per_pair
        )
        enough_motion = (
            translation >= self.profile.observability.minimum_translation_m
            or rotation >= self.profile.observability.minimum_rotation_rad
        )
        if not enough_structure or not enough_motion:
            support = AlignmentSupport.UNKNOWN_OBSERVABILITY
            observability = Observability.NOT_OBSERVABLE
            state = AlignmentState.UNKNOWN
        else:
            support = AlignmentSupport.SUPPORTED
            observability = Observability.OBSERVABLE
            state = (
                AlignmentState.PASS
                if overlap >= self.profile.alignment.minimum_occupancy_jaccard
                and thickness is not None
                and thickness
                <= self.profile.alignment.maximum_shared_surface_thickness_m
                else AlignmentState.FAIL
            )
        return PairAlignmentEvidence(
            left_frame_index=left.evidence.frame_index,
            right_frame_index=right.evidence.frame_index,
            start_time_ns=left.evidence.reference_time_ns,
            end_time_ns=right.evidence.reference_time_ns,
            support=support,
            observability=observability,
            state=state,
            translation_excitation_m=translation,
            rotation_excitation_rad=rotation,
            left_occupied_voxels=len(left_cells),
            right_occupied_voxels=len(right_cells),
            shared_voxels=len(shared),
            occupancy_jaccard=overlap,
            shared_surface_thickness_m=thickness,
            compatible_causes=(
                "trajectory error",
                "point-time error",
                "extrinsic calibration error",
                "dynamic scene",
                "insufficient static overlap",
            ),
        )

    def finalize(self) -> LidarAlignmentReport:
        if len(self.frames) < 2:
            raise ValueError("alignment analysis requires at least two frames")
        pairs = tuple(self.pairs)
        if any(item.state is AlignmentState.FAIL for item in pairs):
            state = AlignmentState.FAIL
        elif any(item.state is AlignmentState.UNKNOWN for item in pairs):
            state = AlignmentState.UNKNOWN
        else:
            state = AlignmentState.PASS
        return LidarAlignmentReport(
            schema_version="cartosentry.lidar-alignment-report.v1",
            profile_immutable_sha256=self.profile.immutable_sha256,
            profile_file_sha256=self.profile_file_sha256,
            source_sha256=self.source_sha256,
            target_frame=self.profile.target_frame,
            state=state,
            statistics=AlignmentStatistics(
                frame_count=len(self.frames),
                pair_count=len(pairs),
                maximum_input_points_per_frame=max(
                    item.input_point_count for item in self.frames
                ),
                maximum_occupied_voxels_per_frame=max(
                    item.occupied_voxel_count for item in self.frames
                ),
                retained_voxel_frame_upper_bound=1,
            ),
            frames=tuple(self.frames),
            pairs=pairs,
        )


def analyze_motion_compensated_alignment(
    frames: Iterable[AlignmentFrameInput],
    *,
    trajectory: TrajectoryEvaluator,
    rig_from_lidar: RigidTransform,
    profile: LidarAlignmentProfile,
    profile_file_sha256: str,
    source_sha256: str,
) -> LidarAlignmentReport:
    """Analyze a frame sequence through the bounded production implementation."""

    analyzer = LidarMotionAlignmentAnalyzer(
        trajectory=trajectory,
        rig_from_lidar=rig_from_lidar,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_sha256=source_sha256,
    )
    for frame in frames:
        analyzer.process_frame(frame)
    return analyzer.finalize()


__all__ = [
    "PROFILE_IMMUTABLE_SHA256",
    "AlignmentFrameInput",
    "AlignmentPointInput",
    "AlignmentState",
    "AlignmentStatistics",
    "AlignmentSupport",
    "LidarAlignmentProfile",
    "LidarAlignmentReport",
    "LidarMotionAlignmentAnalyzer",
    "PointMotionClass",
    "analyze_motion_compensated_alignment",
    "load_lidar_alignment_profile",
    "synthetic_alignment_frames",
    "synthetic_rig_from_lidar",
]
