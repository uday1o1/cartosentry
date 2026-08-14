"""Frozen split-bound M4.2 motion-alignment qualification."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry.contracts import RigidTransform, UnitQuaternion
from cartosentry.lidar_faults import load_lidar_qualification_gate
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.motion_alignment import (
    AlignmentFrameInput,
    AlignmentPointInput,
    AlignmentState,
    AlignmentSupport,
    FrameAlignmentEvidence,
    LidarAlignmentProfile,
    LidarAlignmentReport,
    PointMotionClass,
    analyze_motion_compensated_alignment,
    load_lidar_alignment_profile,
)
from cartosentry.motion_alignment_fixtures import (
    AnalyticAlignmentFixture,
    alignment_input_sha256,
    generate_analytic_alignment_fixture,
)
from cartosentry.synthetic import sensor_map_family_assignments
from cartosentry.trajectory import (
    ContinuousReferenceTrajectory,
    ReferenceSample,
    ReferenceTrajectoryKind,
    TrajectoryGateParameters,
    load_trajectory_gate,
)

GATE_IMMUTABLE_SHA256 = (
    "09fb497248d2f19e2d188fe666945024922e43bc76fbf263c4109655e6d52e82"
)
MAXIMUM_GATE_BYTES = 256 * 1024


class AlignmentFaultOperator(StrEnum):
    POINT_TIME_SHIFT_NS = "point_time_shift_ns"
    TRAJECTORY_SINUSOID_M = "trajectory_sinusoid_m"
    EXTRINSIC_YAW_RAD = "extrinsic_yaw_rad"


class AlignmentFaultSeverity(StrEnum):
    BELOW_THRESHOLD = "below_threshold"
    NEAR_THRESHOLD = "near_threshold"
    DETECTABLE = "detectable"


class GateAuthorities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    profile_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    trajectory_gate_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    trajectory_gate_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    lidar_integrity_gate_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    lidar_integrity_gate_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    split_manifest_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    numerical_charter_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    representative_fault_matrix_file_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]


class GatePartition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    family_prefix: Annotated[str, Field(min_length=1)]
    family_count: Annotated[int, Field(gt=0)]
    claim_status: Literal["DESCRIPTIVE_ONLY", "CALIBRATION_ONLY"]


class AnalyticFixtureContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    frame_count: Literal[6]
    points_per_frame: Literal[68]
    distinct_measurement_times_per_frame: Literal[17]
    frame_period_ns: Literal[500_000_000]
    scan_duration_ns: Literal[400_000_000]
    motion_condition: Literal["changing_turn_rate_static_structure"]


class GapControlContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    unsupported_frame_index: Literal[2]
    affected_pairs: tuple[tuple[int, int], ...]
    unaffected_pass_pairs: tuple[tuple[int, int], ...]

    @model_validator(mode="after")
    def validate_exact_identities(self) -> Self:
        if self.affected_pairs != ((1, 2), (2, 3)):
            raise ValueError("gap affected-pair truth is not exact")
        if self.unaffected_pass_pairs != ((0, 1), (3, 4), (4, 5)):
            raise ValueError("gap unaffected-pair truth is not exact")
        return self


class RawAlignmentFaultCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    case_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")]
    operator: AlignmentFaultOperator
    severity: AlignmentFaultSeverity
    value: Annotated[float, Field(gt=0.0)]
    expected_state: Literal[AlignmentState.PASS, AlignmentState.FAIL]


class AlignmentFaultTruth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["cartosentry.alignment-fault-truth.v1"]
    operator: AlignmentFaultOperator
    case_id: Annotated[str, Field(min_length=1)]
    severity: AlignmentFaultSeverity
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    derived_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    requested_value: Annotated[float, Field(gt=0.0)]
    changed_point_count: Annotated[int, Field(ge=0)]
    changed_reference_sample_count: Annotated[int, Field(ge=0)]
    changed_extrinsic_component_count: Annotated[int, Field(ge=0)]
    maximum_point_time_delta_ns: Annotated[int, Field(ge=0)]
    maximum_trajectory_translation_delta_m: Annotated[float, Field(ge=0.0)]
    extrinsic_rotation_delta_rad: Annotated[float, Field(ge=0.0)]

    @model_validator(mode="after")
    def validate_operator_semantics(self) -> Self:
        changed = (
            self.changed_point_count,
            self.changed_reference_sample_count,
            self.changed_extrinsic_component_count,
        )
        expected_nonzero = {
            AlignmentFaultOperator.POINT_TIME_SHIFT_NS: (True, False, False),
            AlignmentFaultOperator.TRAJECTORY_SINUSOID_M: (False, True, False),
            AlignmentFaultOperator.EXTRINSIC_YAW_RAD: (False, False, True),
        }[self.operator]
        if tuple(value > 0 for value in changed) != expected_nonzero:
            raise ValueError("alignment fault changed fields do not match its operator")
        measured_nonzero = (
            self.maximum_point_time_delta_ns > 0,
            self.maximum_trajectory_translation_delta_m > 0.0,
            self.extrinsic_rotation_delta_rad > 0.0,
        )
        if measured_nonzero != expected_nonzero:
            raise ValueError(
                "alignment fault measured deltas do not match its operator"
            )
        return self


@dataclass(frozen=True)
class AlignmentFaultInputs:
    frames: tuple[AlignmentFrameInput, ...]
    reference_samples: tuple[ReferenceSample, ...]
    rig_from_lidar: RigidTransform
    truth: AlignmentFaultTruth


class GateThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operator: Literal["fraction_eq", "max_le"]
    value: Annotated[float, Field(ge=0.0)]
    unit: Literal["fraction", "m", "frame"]


class MotionAlignmentQualificationGate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    gate_id: Literal["m4.2-motion-alignment-v1"]
    gate_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_AFTER_THRESHOLD_CALIBRATION_BEFORE_M4_2_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    claim_scope: Annotated[str, Field(min_length=1)]
    authorities: GateAuthorities
    partitions: dict[str, GatePartition]
    fixture: AnalyticFixtureContract
    gap_control: GapControlContract
    cases: tuple[RawAlignmentFaultCase, ...]
    gates: dict[str, GateThreshold]

    @model_validator(mode="after")
    def validate_gate(self) -> Self:
        if self.immutable_sha256 != GATE_IMMUTABLE_SHA256:
            raise ValueError("motion-alignment gate identity is not pinned")
        expected_partitions = {
            "development": ("sensor-map-dev-", 8, "DESCRIPTIVE_ONLY"),
            "threshold_calibration": (
                "sensor-map-cal-",
                12,
                "CALIBRATION_ONLY",
            ),
        }
        observed = {
            key: (value.family_prefix, value.family_count, value.claim_status)
            for key, value in self.partitions.items()
        }
        if observed != expected_partitions:
            raise ValueError("motion-alignment partitions are not exact")
        expected_cases = tuple(
            (operator, severity)
            for operator in AlignmentFaultOperator
            for severity in AlignmentFaultSeverity
        )
        observed_cases = tuple((item.operator, item.severity) for item in self.cases)
        if observed_cases != expected_cases:
            raise ValueError("motion-alignment fault cases are incomplete or reordered")
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise ValueError("motion-alignment case identifiers must be unique")
        expected_gates = {
            "clean_state_fraction",
            "clean_analytic_truth_rmse_m",
            "clean_analytic_truth_coverage_fraction",
            "case_expected_state_fraction",
            "per_point_pose_evaluation_fraction",
            "unsupported_gap_unknown_fraction",
            "unobservable_control_unknown_fraction",
            "mask_expected_outcome_fraction",
            "retained_voxel_frame_upper_bound",
        }
        if set(self.gates) != expected_gates:
            raise ValueError("motion-alignment gate metrics are not exact")
        return self


def _canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def load_motion_alignment_gate(
    path: Path,
) -> tuple[MotionAlignmentQualificationGate, str]:
    """Load and self-authenticate the frozen M4.2 gate."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="motion-alignment qualification gate",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="motion-alignment qualification gate",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "motion-alignment qualification gate is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("motion-alignment qualification gate must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("motion-alignment qualification gate hash is invalid")
    return (
        MotionAlignmentQualificationGate.model_validate_json(content),
        hashlib.sha256(content).hexdigest(),
    )


def _yaw_rotation(yaw: float) -> UnitQuaternion:
    cosine = math.cos(yaw)
    sine = math.sin(yaw)
    return UnitQuaternion.from_rotation_matrix(
        (cosine, -sine, 0.0, sine, cosine, 0.0, 0.0, 0.0, 1.0)
    )


def _time_shifted_frames(
    frames: tuple[AlignmentFrameInput, ...], shift_ns: int
) -> tuple[AlignmentFrameInput, ...]:
    return tuple(
        replace(
            frame,
            points=tuple(
                replace(
                    point,
                    relative_time_ns=point.relative_time_ns + shift_ns,
                )
                for point in frame.points
            ),
        )
        for frame in frames
    )


def _displaced_samples(
    samples: tuple[ReferenceSample, ...], amplitude_m: float
) -> tuple[ReferenceSample, ...]:
    result: list[ReferenceSample] = []
    for sample in samples:
        phase = 2.0 * math.pi * sample.time.value_ns / 650_000_000.0
        translation = sample.world_from_rig.translation_m
        result.append(
            replace(
                sample,
                world_from_rig=RigidTransform(
                    target_frame=sample.world_from_rig.target_frame,
                    source_frame=sample.world_from_rig.source_frame,
                    translation_m=(
                        translation[0] + amplitude_m * math.sin(phase),
                        translation[1],
                        translation[2],
                    ),
                    rotation=sample.world_from_rig.rotation,
                ),
            )
        )
    return tuple(result)


def _fault_inputs(
    fixture: AnalyticAlignmentFixture,
    case: RawAlignmentFaultCase,
) -> AlignmentFaultInputs:
    frames = fixture.frames
    samples = fixture.reference_samples
    rig_from_lidar = fixture.rig_from_lidar
    if case.operator is AlignmentFaultOperator.POINT_TIME_SHIFT_NS:
        frames = _time_shifted_frames(frames, int(case.value))
    elif case.operator is AlignmentFaultOperator.TRAJECTORY_SINUSOID_M:
        samples = _displaced_samples(samples, case.value)
    else:
        rig_from_lidar = RigidTransform(
            target_frame="rig",
            source_frame="lidar",
            translation_m=fixture.rig_from_lidar.translation_m,
            rotation=_yaw_rotation(case.value),
        )
    provenance = {
        "schema_version": "cartosentry.alignment-fault-derivative.v1",
        "source_sha256": fixture.source_sha256,
        "case": case.model_dump(mode="json"),
    }
    derived_sha256 = alignment_input_sha256(
        frames,
        samples,
        rig_from_lidar,
        provenance=provenance,
    )
    original_points = tuple(point for frame in fixture.frames for point in frame.points)
    derived_points = tuple(point for frame in frames for point in frame.points)
    changed_point_count = sum(
        left != right
        for left, right in zip(original_points, derived_points, strict=True)
    )
    sample_translation_deltas = tuple(
        math.sqrt(
            sum(
                (left_value - right_value) ** 2
                for left_value, right_value in zip(
                    left.world_from_rig.translation_m,
                    right.world_from_rig.translation_m,
                    strict=True,
                )
            )
        )
        for left, right in zip(fixture.reference_samples, samples, strict=True)
    )
    changed_reference_sample_count = sum(
        delta > 0.0 for delta in sample_translation_deltas
    )
    rotation_dot = abs(
        sum(
            left * right
            for left, right in zip(
                fixture.rig_from_lidar.rotation.as_wxyz(),
                rig_from_lidar.rotation.as_wxyz(),
                strict=True,
            )
        )
    )
    extrinsic_rotation_delta_rad = 2.0 * math.acos(min(1.0, max(-1.0, rotation_dot)))
    changed_extrinsic_component_count = int(fixture.rig_from_lidar != rig_from_lidar)
    truth = AlignmentFaultTruth(
        schema_version="cartosentry.alignment-fault-truth.v1",
        operator=case.operator,
        case_id=case.case_id,
        severity=case.severity,
        source_sha256=fixture.source_sha256,
        derived_sha256=derived_sha256,
        requested_value=case.value,
        changed_point_count=changed_point_count,
        changed_reference_sample_count=changed_reference_sample_count,
        changed_extrinsic_component_count=changed_extrinsic_component_count,
        maximum_point_time_delta_ns=max(
            (
                abs(left.relative_time_ns - right.relative_time_ns)
                for left, right in zip(original_points, derived_points, strict=True)
            ),
            default=0,
        ),
        maximum_trajectory_translation_delta_m=max(
            sample_translation_deltas, default=0.0
        ),
        extrinsic_rotation_delta_rad=extrinsic_rotation_delta_rad,
    )
    return AlignmentFaultInputs(
        frames=frames,
        reference_samples=samples,
        rig_from_lidar=rig_from_lidar,
        truth=truth,
    )


def _trajectory(
    samples: tuple[ReferenceSample, ...], parameters: TrajectoryGateParameters
) -> ContinuousReferenceTrajectory:
    return ContinuousReferenceTrajectory(
        samples,
        kind=ReferenceTrajectoryKind.ANALYTIC,
        parameters=parameters,
    )


def _gap_samples(
    samples: tuple[ReferenceSample, ...],
) -> tuple[ReferenceSample, ...]:
    return tuple(
        item
        for item in samples
        if not 1_325_000_000 <= item.time.value_ns <= 1_675_000_000
    )


def _stationary_samples(
    samples: tuple[ReferenceSample, ...],
) -> tuple[ReferenceSample, ...]:
    pose = samples[0].world_from_rig
    return tuple(
        replace(
            item,
            world_from_rig=pose,
            source_velocity_world_mps=(0.0, 0.0, 0.0),
        )
        for item in samples
    )


def _sparse_frames(
    frames: tuple[AlignmentFrameInput, ...],
) -> tuple[AlignmentFrameInput, ...]:
    return tuple(replace(frame, points=tuple(frame.points)[:10]) for frame in frames)


def _masked_frames(
    frames: tuple[AlignmentFrameInput, ...],
) -> tuple[AlignmentFrameInput, ...]:
    result: list[AlignmentFrameInput] = []
    for frame in frames:
        source = tuple(frame.points)
        masked = tuple(
            replace(
                point,
                motion_class=(
                    PointMotionClass.DYNAMIC if index < 4 else point.motion_class
                ),
            )
            for index, point in enumerate(source)
        )
        near_ego = AlignmentPointInput(
            position_lidar_m=(0.0, 0.0, 0.0),
            relative_time_ns=0,
            source_offset=max(item.source_offset for item in source) + 24,
            motion_class=PointMotionClass.STATIC,
        )
        result.append(replace(frame, points=(*masked, near_ego)))
    return tuple(result)


def _analyze(
    frames: tuple[AlignmentFrameInput, ...],
    *,
    samples: tuple[ReferenceSample, ...],
    rig_from_lidar: RigidTransform,
    parameters: TrajectoryGateParameters,
    profile: LidarAlignmentProfile,
    profile_file_sha256: str,
    source_sha256: str,
) -> LidarAlignmentReport:
    return analyze_motion_compensated_alignment(
        frames,
        trajectory=_trajectory(samples, parameters),
        rig_from_lidar=rig_from_lidar,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_sha256=source_sha256,
    )


def _clean_truth_frame_passed(
    frame: FrameAlignmentEvidence,
    gate: MotionAlignmentQualificationGate,
) -> bool:
    return (
        frame.retained_static_or_unknown_point_count > 0
        and frame.analytic_truth_point_count
        == frame.retained_static_or_unknown_point_count
        and frame.analytic_truth_coverage_fraction
        == gate.gates["clean_analytic_truth_coverage_fraction"].value
        and frame.analytic_truth_rmse_m is not None
        and frame.analytic_truth_rmse_m
        <= gate.gates["clean_analytic_truth_rmse_m"].value
    )


def _qualify_partition(
    partition: Literal["development", "threshold_calibration"],
    *,
    gate: MotionAlignmentQualificationGate,
    profile: LidarAlignmentProfile,
    profile_file_sha256: str,
    parameters: TrajectoryGateParameters,
    split_manifest_path: Path,
) -> dict[str, object]:
    partition_gate = gate.partitions[partition]
    assignments = sensor_map_family_assignments(split_manifest_path, partition)
    expected_ids = tuple(
        f"{partition_gate.family_prefix}{index:03d}"
        for index in range(1, partition_gate.family_count + 1)
    )
    if tuple(item[0] for item in assignments) != expected_ids:
        raise ValueError(f"{partition} alignment membership is not exact")
    clean_pass_count = 0
    maximum_clean_truth_rmse_m = 0.0
    clean_truth_point_count = 0
    clean_retained_point_count = 0
    missing_clean_truth_frame_count = 0
    passing_clean_truth_frame_count = 0
    exact_point_time_frame_count = 0
    total_clean_frame_count = 0
    matching_case_count = 0
    outcomes: list[dict[str, object]] = []
    gap_checks: list[bool] = []
    gap_outcomes: list[dict[str, object]] = []
    observability_checks: list[bool] = []
    mask_checks: list[bool] = []
    maximum_retained_voxel_frames = 0
    for family_id, _scenario, seed in assignments:
        fixture = generate_analytic_alignment_fixture(family_id, seed)
        clean = _analyze(
            fixture.frames,
            samples=fixture.reference_samples,
            rig_from_lidar=fixture.rig_from_lidar,
            parameters=parameters,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            source_sha256=fixture.source_sha256,
        )
        clean_pass_count += int(clean.state is AlignmentState.PASS)
        maximum_retained_voxel_frames = max(
            maximum_retained_voxel_frames,
            clean.statistics.retained_voxel_frame_upper_bound,
        )
        for frame in clean.frames:
            total_clean_frame_count += 1
            clean_truth_point_count += frame.analytic_truth_point_count
            clean_retained_point_count += frame.retained_static_or_unknown_point_count
            missing_clean_truth_frame_count += int(frame.analytic_truth_rmse_m is None)
            passing_clean_truth_frame_count += int(
                _clean_truth_frame_passed(frame, gate)
            )
            exact_point_time_frame_count += int(
                frame.input_point_count == gate.fixture.points_per_frame
                and frame.retained_static_or_unknown_point_count
                == gate.fixture.points_per_frame
                and frame.per_point_pose_evaluation_count
                == gate.fixture.points_per_frame
                and frame.distinct_measurement_time_count
                == gate.fixture.distinct_measurement_times_per_frame
            )
            if frame.analytic_truth_rmse_m is not None:
                maximum_clean_truth_rmse_m = max(
                    maximum_clean_truth_rmse_m,
                    frame.analytic_truth_rmse_m,
                )
        for case in gate.cases:
            faulted = _fault_inputs(fixture, case)
            report = _analyze(
                faulted.frames,
                samples=faulted.reference_samples,
                rig_from_lidar=faulted.rig_from_lidar,
                parameters=parameters,
                profile=profile,
                profile_file_sha256=profile_file_sha256,
                source_sha256=faulted.truth.derived_sha256,
            )
            matched = report.state is AlignmentState(case.expected_state)
            matching_case_count += int(matched)
            outcomes.append(
                {
                    "source_group_id": family_id,
                    "case_id": case.case_id,
                    "operator": case.operator,
                    "severity": case.severity,
                    "value": case.value,
                    "expected_state": case.expected_state,
                    "observed_state": report.state,
                    "outcome_passed": matched,
                    "truth": faulted.truth.model_dump(mode="json"),
                    "minimum_pair_occupancy_jaccard": min(
                        item.occupancy_jaccard
                        for item in report.pairs
                        if item.occupancy_jaccard is not None
                    ),
                    "maximum_pair_surface_thickness_m": max(
                        item.shared_surface_thickness_m
                        for item in report.pairs
                        if item.shared_surface_thickness_m is not None
                    ),
                }
            )
        gap = _analyze(
            fixture.frames,
            samples=_gap_samples(fixture.reference_samples),
            rig_from_lidar=fixture.rig_from_lidar,
            parameters=parameters,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            source_sha256=_canonical_hash(
                {"source_sha256": fixture.source_sha256, "control": "gap"}
            ),
        )
        observed_unknown_frames = tuple(
            item.frame_index
            for item in gap.frames
            if item.support is AlignmentSupport.UNKNOWN_TRAJECTORY
        )
        observed_unknown_pairs = tuple(
            (item.left_frame_index, item.right_frame_index)
            for item in gap.pairs
            if item.state is AlignmentState.UNKNOWN
        )
        observed_pass_pairs = tuple(
            (item.left_frame_index, item.right_frame_index)
            for item in gap.pairs
            if item.support is AlignmentSupport.SUPPORTED
            and item.state is AlignmentState.PASS
        )
        gap_passed = (
            gap.state is AlignmentState.UNKNOWN
            and observed_unknown_frames == (gate.gap_control.unsupported_frame_index,)
            and observed_unknown_pairs == gate.gap_control.affected_pairs
            and observed_pass_pairs == gate.gap_control.unaffected_pass_pairs
        )
        gap_checks.append(gap_passed)
        gap_outcomes.append(
            {
                "source_group_id": family_id,
                "expected_unknown_frames": [gate.gap_control.unsupported_frame_index],
                "observed_unknown_frames": observed_unknown_frames,
                "expected_unknown_pairs": gate.gap_control.affected_pairs,
                "observed_unknown_pairs": observed_unknown_pairs,
                "expected_unaffected_pass_pairs": (
                    gate.gap_control.unaffected_pass_pairs
                ),
                "observed_unaffected_pass_pairs": observed_pass_pairs,
                "outcome_passed": gap_passed,
            }
        )
        sparse = _analyze(
            _sparse_frames(fixture.frames),
            samples=fixture.reference_samples,
            rig_from_lidar=fixture.rig_from_lidar,
            parameters=parameters,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            source_sha256=_canonical_hash(
                {"source_sha256": fixture.source_sha256, "control": "sparse"}
            ),
        )
        stationary = _analyze(
            fixture.frames,
            samples=_stationary_samples(fixture.reference_samples),
            rig_from_lidar=fixture.rig_from_lidar,
            parameters=parameters,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            source_sha256=_canonical_hash(
                {"source_sha256": fixture.source_sha256, "control": "stationary"}
            ),
        )
        observability_checks.extend(
            (
                sparse.state is AlignmentState.UNKNOWN
                and all(
                    item.support is AlignmentSupport.UNKNOWN_OBSERVABILITY
                    for item in sparse.pairs
                ),
                stationary.state is AlignmentState.UNKNOWN
                and all(
                    item.support is AlignmentSupport.UNKNOWN_OBSERVABILITY
                    for item in stationary.pairs
                ),
            )
        )
        masked = _analyze(
            _masked_frames(fixture.frames),
            samples=fixture.reference_samples,
            rig_from_lidar=fixture.rig_from_lidar,
            parameters=parameters,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            source_sha256=_canonical_hash(
                {"source_sha256": fixture.source_sha256, "control": "masks"}
            ),
        )
        mask_checks.extend(
            frame.excluded_dynamic_point_count == 4
            and frame.excluded_near_ego_point_count == 1
            and frame.input_point_count == gate.fixture.points_per_frame + 1
            and frame.per_point_pose_evaluation_count
            == gate.fixture.points_per_frame - 4
            for frame in masked.frames
        )
    group_count = len(assignments)
    case_count = len(outcomes)
    clean_state_fraction = clean_pass_count / group_count
    clean_truth_coverage_fraction = clean_truth_point_count / clean_retained_point_count
    case_expected_state_fraction = matching_case_count / case_count
    per_point_fraction = exact_point_time_frame_count / total_clean_frame_count
    gap_fraction = sum(gap_checks) / len(gap_checks)
    observability_fraction = sum(observability_checks) / len(observability_checks)
    mask_fraction = sum(mask_checks) / len(mask_checks)
    gate_passed = (
        clean_state_fraction == gate.gates["clean_state_fraction"].value
        and maximum_clean_truth_rmse_m
        <= gate.gates["clean_analytic_truth_rmse_m"].value
        and missing_clean_truth_frame_count == 0
        and passing_clean_truth_frame_count == total_clean_frame_count
        and clean_truth_coverage_fraction
        == gate.gates["clean_analytic_truth_coverage_fraction"].value
        and case_expected_state_fraction
        == gate.gates["case_expected_state_fraction"].value
        and per_point_fraction == gate.gates["per_point_pose_evaluation_fraction"].value
        and gap_fraction == gate.gates["unsupported_gap_unknown_fraction"].value
        and observability_fraction
        == gate.gates["unobservable_control_unknown_fraction"].value
        and mask_fraction == gate.gates["mask_expected_outcome_fraction"].value
        and maximum_retained_voxel_frames
        <= gate.gates["retained_voxel_frame_upper_bound"].value
    )
    return {
        "partition": partition,
        "claim_status": partition_gate.claim_status,
        "source_group_count": group_count,
        "clean_state_fraction": clean_state_fraction,
        "maximum_clean_analytic_truth_rmse_m": maximum_clean_truth_rmse_m,
        "clean_analytic_truth_coverage_fraction": clean_truth_coverage_fraction,
        "missing_clean_truth_frame_count": missing_clean_truth_frame_count,
        "clean_truth_frame_pass_fraction": (
            passing_clean_truth_frame_count / total_clean_frame_count
        ),
        "case_count": case_count,
        "case_expected_state_fraction": case_expected_state_fraction,
        "per_point_pose_evaluation_fraction": per_point_fraction,
        "unsupported_gap_unknown_fraction": gap_fraction,
        "unobservable_control_unknown_fraction": observability_fraction,
        "mask_expected_outcome_fraction": mask_fraction,
        "retained_voxel_frame_upper_bound": maximum_retained_voxel_frames,
        "gate_passed": gate_passed,
        "gap_outcomes": gap_outcomes,
        "outcomes": outcomes,
    }


def _authenticate_authorities(
    gate: MotionAlignmentQualificationGate,
    *,
    profile_path: Path,
    trajectory_gate_path: Path,
    lidar_integrity_gate_path: Path,
    split_manifest_path: Path,
    numerical_charter_path: Path,
    representative_fault_matrix_path: Path,
) -> None:
    expected = {
        profile_path: gate.authorities.profile_file_sha256,
        trajectory_gate_path: gate.authorities.trajectory_gate_file_sha256,
        lidar_integrity_gate_path: gate.authorities.lidar_integrity_gate_file_sha256,
        split_manifest_path: gate.authorities.split_manifest_file_sha256,
        numerical_charter_path: gate.authorities.numerical_charter_file_sha256,
        representative_fault_matrix_path: (
            gate.authorities.representative_fault_matrix_file_sha256
        ),
    }
    for path, expected_sha256 in expected.items():
        if _file_sha256(path) != expected_sha256:
            raise ValueError(f"{path.name} does not match the frozen M4.2 authority")


def qualify_motion_alignment(
    *,
    gate_path: Path,
    profile_path: Path,
    trajectory_gate_path: Path,
    lidar_integrity_gate_path: Path,
    split_manifest_path: Path,
    numerical_charter_path: Path,
    representative_fault_matrix_path: Path,
) -> dict[str, object]:
    """Run the exact analytic development and calibration qualification."""

    gate, gate_file_sha256 = load_motion_alignment_gate(gate_path)
    _authenticate_authorities(
        gate,
        profile_path=profile_path,
        trajectory_gate_path=trajectory_gate_path,
        lidar_integrity_gate_path=lidar_integrity_gate_path,
        split_manifest_path=split_manifest_path,
        numerical_charter_path=numerical_charter_path,
        representative_fault_matrix_path=representative_fault_matrix_path,
    )
    profile, profile_file_sha256 = load_lidar_alignment_profile(profile_path)
    if profile.immutable_sha256 != gate.authorities.profile_immutable_sha256:
        raise ValueError("motion-alignment profile differs from the frozen gate")
    trajectory_gate = load_trajectory_gate(trajectory_gate_path)
    if (
        trajectory_gate.immutable_sha256
        != gate.authorities.trajectory_gate_immutable_sha256
    ):
        raise ValueError("trajectory authority differs from the frozen M4.2 gate")
    lidar_gate, _lidar_gate_file_sha256 = load_lidar_qualification_gate(
        lidar_integrity_gate_path
    )
    if (
        lidar_gate.immutable_sha256
        != gate.authorities.lidar_integrity_gate_immutable_sha256
    ):
        raise ValueError("LiDAR integrity authority differs from the M4.2 gate")
    partitions = [
        _qualify_partition(
            partition,
            gate=gate,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            parameters=trajectory_gate.parameters,
            split_manifest_path=split_manifest_path,
        )
        for partition in ("development", "threshold_calibration")
    ]
    accepted = all(cast(bool, item["gate_passed"]) for item in partitions)
    return {
        "schema_version": "cartosentry.motion-alignment-qualification-report.v1",
        "gate_id": gate.gate_id,
        "gate_version": gate.gate_version,
        "accepted": accepted,
        "claim_scope": gate.claim_scope,
        "hashes": {
            **gate.authorities.model_dump(mode="json"),
            "gate_immutable_sha256": gate.immutable_sha256,
            "gate_file_sha256": gate_file_sha256,
        },
        "fixture": gate.fixture.model_dump(mode="json"),
        "gap_control": gate.gap_control.model_dump(mode="json"),
        "partitions": partitions,
    }


__all__ = [
    "GATE_IMMUTABLE_SHA256",
    "AlignmentFaultOperator",
    "AlignmentFaultSeverity",
    "MotionAlignmentQualificationGate",
    "load_motion_alignment_gate",
    "qualify_motion_alignment",
]
