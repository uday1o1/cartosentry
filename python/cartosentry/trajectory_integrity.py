"""Fail-closed trajectory integrity detection and frozen M3.2 qualification."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry.artifacts import Observability, Severity
from cartosentry.contracts import (
    RigidTransform,
    TimeEpoch,
    TimeReference,
    UnitQuaternion,
)
from cartosentry.manifest_boundaries import (
    MAXIMUM_ARTIFACT_JSON_BYTES,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.synthetic_models import (
    SyntheticPartition,
    SyntheticScenario,
    SyntheticTransform,
    TrajectoryPose,
)
from cartosentry.trajectory import (
    M3_1_GATE_SHA256,
    ContinuousReferenceTrajectory,
    ReferenceSample,
    ReferenceTrajectoryKind,
    TrajectoryGateParameters,
)

PROFILE_IMMUTABLE_SHA256 = (
    "cc00a382071fe3cb88411ff2d0d3b23223198ecd7a24bd559abb9e70a838708e"
)
MAXIMUM_TRAJECTORY_PROFILE_BYTES = 64 * 1024
MAXIMUM_CHARTER_BYTES = 1024 * 1024
DETECTOR_ID: Literal["trajectory-integrity-v1"] = "trajectory-integrity-v1"
DETECTOR_VERSION: Literal["1.0.0"] = "1.0.0"

Vector3 = tuple[float, float, float]
_THRESHOLD_KEYS = frozenset(
    {
        "timestamp.duplicate_count",
        "timestamp.regression_count",
        "timestamp.maximum_gap_ns",
        "trajectory.coordinate_continuity_mismatch_count",
        "trajectory.position_jump_residual_m",
        "trajectory.reference_position_residual_m",
        "trajectory.velocity_residual_mps",
        "trajectory.maximum_speed_mps",
        "trajectory.maximum_acceleration_mps2",
        "trajectory.maximum_jerk_mps3",
        "trajectory.maximum_yaw_rate_radps",
        "trajectory.freeze_observed_speed_mps",
        "trajectory.freeze_source_speed_mps",
        "trajectory.freeze_minimum_duration_ns",
    }
)
_CHARTER_GATE_KEYS = frozenset(
    {
        "structural.event_recall",
        "structural.event_precision",
        "content.supported_fault_recall",
        "content.false_critical_per_clean_sensor_hour",
        "content.event_boundary_median_stride",
    }
)


class TrajectoryRule(StrEnum):
    TIMESTAMP_REGRESSION = "timestamp_regression"
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"
    TIMESTAMP_GAP = "timestamp_gap"
    COORDINATE_CONTINUITY = "coordinate_continuity"
    POSITION_FREEZE = "position_freeze"
    POSITION_JUMP = "position_jump"
    REFERENCE_POSITION_RESIDUAL = "reference_position_residual"
    VELOCITY_RESIDUAL = "velocity_residual"
    MAXIMUM_SPEED = "maximum_speed"
    MAXIMUM_ACCELERATION = "maximum_acceleration"
    MAXIMUM_JERK = "maximum_jerk"
    MAXIMUM_YAW_RATE = "maximum_yaw_rate"


_STRUCTURAL_LOCALIZATION_RULES = frozenset(
    {
        TrajectoryRule.TIMESTAMP_REGRESSION,
        TrajectoryRule.DUPLICATE_TIMESTAMP,
        TrajectoryRule.TIMESTAMP_GAP,
        TrajectoryRule.COORDINATE_CONTINUITY,
    }
)


class ReferenceEvidenceKind(StrEnum):
    NONE = "NONE"
    DECLARED_INDEPENDENT_REFERENCE = "DECLARED_INDEPENDENT_REFERENCE"
    PAIRED_COORDINATE_SELF_CONSISTENCY = "PAIRED_COORDINATE_SELF_CONSISTENCY"


class ReferenceProvenanceKind(StrEnum):
    IMMUTABLE_PREINJECTION_SOURCE = "IMMUTABLE_PREINJECTION_SOURCE"
    EXTERNAL_REFERENCE = "EXTERNAL_REFERENCE"
    PAIRED_COORDINATE_SAME_SOURCE = "PAIRED_COORDINATE_SAME_SOURCE"


class ReferenceIndependenceBasis(StrEnum):
    PREINJECTION_HASH_DISTINCT = "PREINJECTION_HASH_DISTINCT"
    EXTERNAL_SOURCE_HASH_DISTINCT = "EXTERNAL_SOURCE_HASH_DISTINCT"
    NOT_INDEPENDENT_PAIRED_COORDINATE = "NOT_INDEPENDENT_PAIRED_COORDINATE"


class TrajectoryMeasurementUnit(StrEnum):
    COUNT = "count"
    NANOSECOND = "ns"
    METER = "m"
    METER_PER_SECOND = "m/s"
    METER_PER_SECOND_SQUARED = "m/s^2"
    METER_PER_SECOND_CUBED = "m/s^3"
    RADIAN_PER_SECOND = "rad/s"


class ThresholdDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operator: Literal["max_le", "abs_max_le", "min_ge"]
    value: Annotated[float, Field(ge=0.0)]
    unit: Literal["count", "ns", "m", "m/s", "m/s^2", "m/s^3", "rad/s"]
    responsible_metric: Annotated[str, Field(min_length=1)]
    rationale: Annotated[str, Field(min_length=1)]


class EventConsolidationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    temporal_adjacency_ns: Annotated[int, Field(ge=0)]
    paired_step_maximum_duration_ns: Annotated[int, Field(gt=0)]
    clear_windows_required: Annotated[int, Field(gt=0)]
    interval_convention: Literal["half_open"]
    primary_rule_priority: list[TrajectoryRule]


class DetectorBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    maximum_samples: Annotated[int, Field(gt=0)]
    maximum_raw_failures: Annotated[int, Field(gt=0)]
    maximum_events: Annotated[int, Field(gt=0)]


class NumericalComparisonProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relative_tolerance: Annotated[float, Field(ge=0.0, le=1e-9)]
    absolute_tolerance: Annotated[float, Field(ge=0.0, le=1e-9)]


class PartitionQualification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    family_set_id: Annotated[str, Field(min_length=1)]
    minimum_source_groups: Annotated[int, Field(gt=0)]
    claim_status: Literal["DESCRIPTIVE_ONLY", "CALIBRATION_ONLY"]


class QualificationAuthorities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    split_manifest_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    fault_matrix_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    numerical_charter_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    numerical_charter_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    trajectory_gate_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_source_group_ids: dict[str, list[str]]


class QualificationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evidence_mode: Literal["FROZEN_SYNTHETIC_EXHAUSTIVE_ENGINEERING"]
    confirmatory_claims_forbidden: Literal[True]
    clean_duration_per_source_group_ns: Annotated[int, Field(gt=0)]
    clean_duration_sample_period_ns: Annotated[int, Field(gt=0)]
    clean_duration_scenario: Literal["constant_speed_straight"]
    fault_qualification_scenario: Literal["constant_speed_straight"]
    zero_event_rate_upper_bound: Literal["exact_poisson_exposure_95"]
    partitions: dict[str, PartitionQualification]
    authorities: QualificationAuthorities
    charter_gate_keys: list[str]
    required_detectable_content_operators: list[str]
    required_nonobservable_control_operator: Literal["trajectory.position_bias"]
    stationary_false_freeze_count_maximum: Literal[0]


class TrajectoryIntegrityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    profile_id: Literal["trajectory-integrity-v1"]
    profile_version: Literal["1.0.1"]
    threshold_freeze_state: Literal["FROZEN_BEFORE_M3_2_IMPLEMENTATION"]
    qualification_freeze_state: Literal[
        "FROZEN_AFTER_PREACCEPTANCE_AUDIT_BEFORE_M3_2_ACCEPTANCE"
    ]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reference_trajectory_gate_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    thresholds: dict[str, ThresholdDefinition]
    numerical_comparison: NumericalComparisonProfile
    event_consolidation: EventConsolidationProfile
    budgets: DetectorBudgets
    qualification: QualificationProfile

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.immutable_sha256 != PROFILE_IMMUTABLE_SHA256:
            raise ValueError("trajectory profile does not match the pinned M3.2 hash")
        if self.reference_trajectory_gate_sha256 != M3_1_GATE_SHA256:
            raise ValueError("trajectory profile references an unknown M3.1 gate")
        if set(self.thresholds) != _THRESHOLD_KEYS:
            raise ValueError(
                "trajectory profile threshold keys differ from the frozen set"
            )
        if set(self.qualification.charter_gate_keys) != _CHARTER_GATE_KEYS:
            raise ValueError(
                "trajectory qualification gate keys differ from the charter"
            )
        expected_partitions = {
            "development": (
                "sensor-map-development-v0",
                8,
                "DESCRIPTIVE_ONLY",
            ),
            "threshold_calibration": (
                "sensor-map-threshold-v0",
                12,
                "CALIBRATION_ONLY",
            ),
        }
        observed_partitions = {
            key: (value.family_set_id, value.minimum_source_groups, value.claim_status)
            for key, value in self.qualification.partitions.items()
        }
        if observed_partitions != expected_partitions:
            raise ValueError(
                "trajectory qualification partitions are not frozen exactly"
            )
        expected_group_ids = {
            "development": [f"sensor-map-dev-{index:03d}" for index in range(1, 9)],
            "threshold_calibration": [
                f"sensor-map-cal-{index:03d}" for index in range(1, 13)
            ],
        }
        if (
            self.qualification.authorities.expected_source_group_ids
            != expected_group_ids
        ):
            raise ValueError("trajectory qualification source groups are not exact")
        if self.event_consolidation.primary_rule_priority != list(TrajectoryRule):
            raise ValueError("trajectory rule priority is incomplete or reordered")
        return self


class RuleSupport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    rule: TrajectoryRule
    observability: Observability
    evidence_kind: ReferenceEvidenceKind
    detail: Annotated[str, Field(min_length=1)]


class ReferencePositionSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    time_ns: int
    time_epoch: TimeEpoch
    clock_id: Annotated[str, Field(min_length=1, max_length=128)]
    time_reference: TimeReference
    target_frame: Annotated[str, Field(min_length=1, max_length=128)]
    source_frame: Annotated[str, Field(min_length=1, max_length=128)]
    position_m: tuple[float, float, float]

    @model_validator(mode="after")
    def validate_sample(self) -> Self:
        if self.target_frame == self.source_frame:
            raise ValueError("reference position frames must be distinct")
        if not all(math.isfinite(value) for value in self.position_m):
            raise ValueError("reference positions must be finite")
        return self


class ReferencePositionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["cartosentry.reference-position-evidence.v1"]
    evidence_kind: ReferenceEvidenceKind
    reference_source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    provenance_kind: ReferenceProvenanceKind
    provenance: Annotated[str, Field(min_length=1, max_length=256)]
    independence_basis: ReferenceIndependenceBasis
    unit: Literal["m"]
    samples: tuple[ReferencePositionSample, ...]
    evidence_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.evidence_kind is ReferenceEvidenceKind.NONE:
            raise ValueError("reference position evidence cannot use NONE")
        valid_contracts = {
            ReferenceEvidenceKind.DECLARED_INDEPENDENT_REFERENCE: {
                (
                    ReferenceProvenanceKind.IMMUTABLE_PREINJECTION_SOURCE,
                    ReferenceIndependenceBasis.PREINJECTION_HASH_DISTINCT,
                ),
                (
                    ReferenceProvenanceKind.EXTERNAL_REFERENCE,
                    ReferenceIndependenceBasis.EXTERNAL_SOURCE_HASH_DISTINCT,
                ),
            },
            ReferenceEvidenceKind.PAIRED_COORDINATE_SELF_CONSISTENCY: {
                (
                    ReferenceProvenanceKind.PAIRED_COORDINATE_SAME_SOURCE,
                    ReferenceIndependenceBasis.NOT_INDEPENDENT_PAIRED_COORDINATE,
                )
            },
        }
        if (self.provenance_kind, self.independence_basis) not in valid_contracts[
            self.evidence_kind
        ]:
            raise ValueError("reference evidence independence contract is inconsistent")
        if not self.samples:
            raise ValueError("reference position evidence cannot be empty")
        if any(right.time_ns <= left.time_ns for left, right in pairwise(self.samples)):
            raise ValueError("reference position evidence times must increase")
        frame_pairs = {
            (sample.target_frame, sample.source_frame) for sample in self.samples
        }
        if len(frame_pairs) != 1:
            raise ValueError(
                "reference position evidence must use one named frame pair"
            )
        payload = self.model_dump(mode="json", exclude={"evidence_sha256"})
        expected = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if self.evidence_sha256 != expected:
            raise ValueError("reference position evidence hash is invalid")
        return self


class RuleMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    rule: TrajectoryRule
    value: float
    unit: TrajectoryMeasurementUnit
    threshold_key: str
    threshold_value: float
    threshold_operator: Literal["max_le", "abs_max_le", "min_ge"]


class CompatibleCause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    label: Annotated[str, Field(min_length=1)]
    confirmed: Literal[False] = False


class TrajectoryIntegrityEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: Annotated[str, Field(pattern=r"^trajectory-event-sha256-[0-9a-f]{64}$")]
    detector_id: Literal["trajectory-integrity-v1"]
    detector_version: Literal["1.0.0"]
    start_time_ns: int
    end_time_ns: int
    start_sample_index: Annotated[int, Field(ge=0)]
    end_sample_index_exclusive: Annotated[int, Field(gt=0)]
    primary_rule: TrajectoryRule
    triggered_rules: tuple[TrajectoryRule, ...]
    severity: Severity
    observability: Observability
    measurements: tuple[RuleMeasurement, ...]
    compatible_causes: tuple[CompatibleCause, ...]

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.end_time_ns <= self.start_time_ns:
            raise ValueError("trajectory event interval must be nonempty and half open")
        if self.end_sample_index_exclusive <= self.start_sample_index:
            raise ValueError("trajectory event sample interval must be nonempty")
        if not self.triggered_rules or self.primary_rule not in self.triggered_rules:
            raise ValueError("trajectory event primary rule must be triggered")
        if not self.measurements:
            raise ValueError("trajectory event needs auditable measurements")
        return self


class TrajectoryIntegrityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["cartosentry.trajectory-integrity-report.v1"]
    detector_id: Literal["trajectory-integrity-v1"]
    detector_version: Literal["1.0.0"]
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    partition: SyntheticPartition
    profile_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reference_evidence_sha256: Annotated[
        str | None, Field(pattern=r"^[0-9a-f]{64}$")
    ] = None
    sample_count: Annotated[int, Field(gt=0)]
    structural_valid: bool
    support: tuple[RuleSupport, ...]
    events: tuple[TrajectoryIntegrityEvent, ...]


@dataclass(frozen=True)
class ParsedSyntheticTrajectory:
    samples: tuple[ReferenceSample, ...]
    partition: SyntheticPartition
    scenario: SyntheticScenario
    sample_period_ns: int
    source_sha256: str


@dataclass(frozen=True)
class _RawFailure:
    rule: TrajectoryRule
    start_time_ns: int
    end_time_ns: int
    start_index: int
    end_index_exclusive: int
    measurements: tuple[RuleMeasurement, ...]
    severity: Severity
    vector: Vector3 | None = None
    compatible_causes: tuple[CompatibleCause, ...] = ()


def _canonical_hash_without(payload: dict[str, object], field: str) -> str:
    unhashed = {key: value for key, value in payload.items() if key != field}
    content = json.dumps(
        unhashed, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def make_reference_position_evidence(
    reference_samples: Sequence[ReferenceSample],
    *,
    evidence_kind: ReferenceEvidenceKind,
    reference_source_sha256: str,
    provenance_kind: ReferenceProvenanceKind,
    provenance: str,
    independence_basis: ReferenceIndependenceBasis,
) -> ReferencePositionEvidence:
    """Bind time-aligned, named-frame reference positions to immutable evidence."""

    payload: dict[str, object] = {
        "schema_version": "cartosentry.reference-position-evidence.v1",
        "evidence_kind": evidence_kind,
        "reference_source_sha256": reference_source_sha256,
        "provenance_kind": provenance_kind,
        "provenance": provenance,
        "independence_basis": independence_basis,
        "unit": "m",
        "samples": tuple(
            {
                "time_ns": sample.time.value_ns,
                "time_epoch": sample.time.epoch,
                "clock_id": sample.time.clock_id,
                "time_reference": sample.time.reference,
                "target_frame": sample.world_from_rig.target_frame,
                "source_frame": sample.world_from_rig.source_frame,
                "position_m": sample.world_from_rig.translation_m,
            }
            for sample in reference_samples
        ),
    }
    evidence_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return ReferencePositionEvidence.model_validate(
        {**payload, "evidence_sha256": evidence_sha256}
    )


def load_trajectory_integrity_profile(
    path: Path,
) -> tuple[TrajectoryIntegrityProfile, str]:
    """Load and self-authenticate the threshold-frozen M3.2 profile."""

    content = read_bounded_regular_bytes(
        path,
        maximum_bytes=MAXIMUM_TRAJECTORY_PROFILE_BYTES,
        context="trajectory integrity profile",
    )
    decoded = decode_bounded_json(
        content,
        maximum_bytes=MAXIMUM_TRAJECTORY_PROFILE_BYTES,
        context="trajectory integrity profile",
    )
    if not isinstance(decoded, dict):
        raise ValueError("trajectory integrity profile must be an object")
    expected = decoded.get("immutable_sha256")
    if expected != _canonical_hash_without(
        cast(dict[str, object], decoded), "immutable_sha256"
    ):
        raise ValueError("trajectory integrity profile immutable hash is invalid")
    profile = TrajectoryIntegrityProfile.model_validate_json(content)
    return profile, hashlib.sha256(content).hexdigest()


def _rigid_from_synthetic(transform: SyntheticTransform) -> RigidTransform:
    values = transform.row_major_4x4
    return RigidTransform(
        target_frame=transform.target_frame,
        source_frame=transform.source_frame,
        translation_m=transform.translation_m,
        rotation=UnitQuaternion.from_rotation_matrix(
            (
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
        ),
    )


def parse_synthetic_trajectory_bytes(content: bytes) -> ParsedSyntheticTrajectory:
    """Read only the bounded trajectory surface from clean or faulted fixture bytes."""

    decoded = decode_bounded_json(
        content,
        maximum_bytes=MAXIMUM_ARTIFACT_JSON_BYTES,
        context="synthetic trajectory input",
    )
    if not isinstance(decoded, dict):
        raise ValueError("synthetic trajectory input must be an object")
    raw_trajectory = decoded.get("trajectory")
    if not isinstance(raw_trajectory, list) or not raw_trajectory:
        raise ValueError("synthetic trajectory input needs a nonempty trajectory")
    if len(raw_trajectory) > 1_000_000:
        raise ValueError("synthetic trajectory input exceeds the sample ceiling")
    raw_partition = decoded.get("partition")
    raw_scenario = decoded.get("scenario")
    raw_sample_period_ns = decoded.get("sample_period_ns")
    if (
        not isinstance(raw_partition, str)
        or not isinstance(raw_scenario, str)
        or isinstance(raw_sample_period_ns, bool)
        or not isinstance(raw_sample_period_ns, int)
        or raw_sample_period_ns <= 0
    ):
        raise ValueError("synthetic trajectory metadata is invalid")
    try:
        partition = cast(
            SyntheticPartition,
            {
                "development": "development",
                "threshold_calibration": "threshold_calibration",
                "policy_tuning": "policy_tuning",
            }[raw_partition],
        )
        scenario = SyntheticScenario(raw_scenario)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("synthetic trajectory metadata is invalid") from error
    poses = tuple(
        TrajectoryPose.model_validate_json(
            json.dumps(item, sort_keys=True, separators=(",", ":"))
        )
        for item in raw_trajectory
    )
    return ParsedSyntheticTrajectory(
        samples=tuple(
            ReferenceSample(
                time=pose.time,
                world_from_rig=_rigid_from_synthetic(pose.world_from_rig),
                source_velocity_world_mps=pose.source_velocity_world_mps,
            )
            for pose in poses
        ),
        partition=partition,
        scenario=scenario,
        sample_period_ns=raw_sample_period_ns,
        source_sha256=hashlib.sha256(content).hexdigest(),
    )


def _norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _subtract(left: Sequence[float], right: Sequence[float]) -> Vector3:
    return cast(
        Vector3,
        tuple(a - b for a, b in zip(left, right, strict=True)),
    )


def _measurement(
    profile: TrajectoryIntegrityProfile,
    rule: TrajectoryRule,
    key: str,
    value: float,
) -> RuleMeasurement:
    threshold = profile.thresholds[key]
    return RuleMeasurement(
        rule=rule,
        value=value,
        unit=TrajectoryMeasurementUnit(threshold.unit),
        threshold_key=key,
        threshold_value=threshold.value,
        threshold_operator=threshold.operator,
    )


def _exceeds_threshold(
    value: float, threshold: float, profile: TrajectoryIntegrityProfile
) -> bool:
    return value > threshold and not math.isclose(
        value,
        threshold,
        rel_tol=profile.numerical_comparison.relative_tolerance,
        abs_tol=profile.numerical_comparison.absolute_tolerance,
    )


def _event_id(payload: dict[str, object]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()
    return f"trajectory-event-sha256-{digest}"


def _interval_end(times: Sequence[int], index: int, stride_ns: int) -> int:
    if index + 1 < len(times) and times[index + 1] > times[index]:
        return times[index + 1]
    return times[index] + max(1, stride_ns)


def _safe_structural_interval(left_time_ns: int, right_time_ns: int) -> tuple[int, int]:
    start = min(left_time_ns, right_time_ns)
    end = max(left_time_ns, right_time_ns)
    return (start, end if end > start else start + 1)


def _coordinate_mismatches(samples: Sequence[ReferenceSample]) -> list[int]:
    first = samples[0]
    expected_time = (
        first.time.epoch,
        first.time.clock_id,
        first.time.reference,
    )
    expected_frames = (
        first.world_from_rig.target_frame,
        first.world_from_rig.source_frame,
    )
    geographic_present = first.geographic is not None
    return [
        index
        for index, sample in enumerate(samples)
        if (
            sample.time.epoch,
            sample.time.clock_id,
            sample.time.reference,
        )
        != expected_time
        or (
            sample.world_from_rig.target_frame,
            sample.world_from_rig.source_frame,
        )
        != expected_frames
        or (sample.geographic is not None) != geographic_present
    ]


def _pair_position_steps(
    failures: list[_RawFailure],
    jump_threshold_m: float,
    maximum_pair_duration_ns: int,
) -> list[_RawFailure]:
    jumps = [item for item in failures if item.rule is TrajectoryRule.POSITION_JUMP]
    retained = [
        item for item in failures if item.rule is not TrajectoryRule.POSITION_JUMP
    ]
    cursor = 0
    while cursor < len(jumps):
        left = jumps[cursor]
        if cursor + 1 >= len(jumps) or left.vector is None:
            retained.append(left)
            cursor += 1
            continue
        right = jumps[cursor + 1]
        if right.vector is None:
            retained.append(left)
            cursor += 1
            continue
        dot = sum(a * b for a, b in zip(left.vector, right.vector, strict=True))
        cancellation = _norm(
            tuple(a + b for a, b in zip(left.vector, right.vector, strict=True))
        )
        tolerance = max(
            jump_threshold_m * 0.25,
            0.25 * max(_norm(left.vector), _norm(right.vector)),
        )
        pair_duration_ns = right.end_time_ns - left.end_time_ns
        if (
            dot >= 0.0
            or cancellation > tolerance
            or pair_duration_ns > maximum_pair_duration_ns
        ):
            retained.append(left)
            cursor += 1
            continue
        retained.append(
            _RawFailure(
                rule=TrajectoryRule.POSITION_JUMP,
                start_time_ns=left.end_time_ns,
                end_time_ns=right.end_time_ns,
                start_index=left.end_index_exclusive,
                end_index_exclusive=right.end_index_exclusive,
                measurements=left.measurements + right.measurements,
                severity=Severity.CRITICAL,
                compatible_causes=(
                    CompatibleCause(label="bounded position step"),
                    CompatibleCause(label="reference pose discontinuity"),
                    CompatibleCause(label="coordinate-frame interpretation error"),
                ),
            )
        )
        cursor += 2
    return retained


def _consolidate_freezes(
    failures: list[_RawFailure], profile: TrajectoryIntegrityProfile
) -> list[_RawFailure]:
    minimum_duration_ns = int(
        profile.thresholds["trajectory.freeze_minimum_duration_ns"].value
    )
    freezes = sorted(
        (item for item in failures if item.rule is TrajectoryRule.POSITION_FREEZE),
        key=lambda item: (item.start_time_ns, item.start_index),
    )
    retained = [
        item for item in failures if item.rule is not TrajectoryRule.POSITION_FREEZE
    ]
    cursor = 0
    while cursor < len(freezes):
        group = [freezes[cursor]]
        cursor += 1
        while (
            cursor < len(freezes)
            and freezes[cursor].start_index <= group[-1].end_index_exclusive
        ):
            group.append(freezes[cursor])
            cursor += 1
        start = group[0]
        end = group[-1]
        if end.end_time_ns - start.start_time_ns < minimum_duration_ns:
            continue
        retained.append(
            _RawFailure(
                rule=TrajectoryRule.POSITION_FREEZE,
                start_time_ns=start.start_time_ns,
                end_time_ns=end.end_time_ns,
                start_index=start.start_index,
                end_index_exclusive=end.end_index_exclusive,
                measurements=(
                    *(
                        measurement
                        for item in group
                        for measurement in item.measurements
                    ),
                    _measurement(
                        profile,
                        TrajectoryRule.POSITION_FREEZE,
                        "trajectory.freeze_minimum_duration_ns",
                        float(end.end_time_ns - start.start_time_ns),
                    ),
                ),
                severity=Severity.CRITICAL,
                compatible_causes=(
                    CompatibleCause(label="frozen position output"),
                    CompatibleCause(label="source velocity disagreement"),
                ),
            )
        )
    return retained


def _deduplicate_measurements(
    measurements: Iterable[RuleMeasurement],
) -> tuple[RuleMeasurement, ...]:
    selected: dict[tuple[TrajectoryRule, str], RuleMeasurement] = {}
    for measurement in measurements:
        key = (measurement.rule, measurement.threshold_key)
        previous = selected.get(key)
        if previous is None or abs(measurement.value) > abs(previous.value):
            selected[key] = measurement
    return tuple(
        selected[key]
        for key in sorted(selected, key=lambda item: (item[0].value, item[1]))
    )


def _consolidate_events(
    raw: list[_RawFailure],
    profile: TrajectoryIntegrityProfile,
    source_sha256: str,
    stride_ns: int,
) -> tuple[TrajectoryIntegrityEvent, ...]:
    priority = {
        rule: index
        for index, rule in enumerate(profile.event_consolidation.primary_rule_priority)
    }
    adjacency = max(
        profile.event_consolidation.temporal_adjacency_ns,
        profile.event_consolidation.clear_windows_required * stride_ns,
    )
    ordered = sorted(
        raw,
        key=lambda item: (
            item.start_time_ns,
            item.end_time_ns,
            priority[item.rule],
        ),
    )
    groups: list[list[_RawFailure]] = []
    for failure in ordered:
        if (
            not groups
            or failure.start_time_ns
            > max(item.end_time_ns for item in groups[-1]) + adjacency
        ):
            groups.append([failure])
        else:
            groups[-1].append(failure)
    if len(groups) > profile.budgets.maximum_events:
        raise ValueError("trajectory event count exceeds the frozen budget")
    severity_order = {
        Severity.INFO: 0,
        Severity.WARNING: 1,
        Severity.CRITICAL: 2,
        Severity.BLOCKING_ANALYSIS: 3,
    }
    events: list[TrajectoryIntegrityEvent] = []
    for group in groups:
        rules = tuple(sorted({item.rule for item in group}, key=priority.__getitem__))
        primary = rules[0]
        long_position_step = any(
            item.rule is TrajectoryRule.POSITION_JUMP
            and item.end_time_ns - item.start_time_ns > 2 * stride_ns
            for item in group
        )
        if any(rule in _STRUCTURAL_LOCALIZATION_RULES for rule in rules):
            boundary_rule = primary
        elif TrajectoryRule.POSITION_FREEZE in rules:
            boundary_rule = TrajectoryRule.POSITION_FREEZE
        elif long_position_step:
            boundary_rule = TrajectoryRule.POSITION_JUMP
        elif TrajectoryRule.REFERENCE_POSITION_RESIDUAL in rules:
            boundary_rule = TrajectoryRule.REFERENCE_POSITION_RESIDUAL
        elif TrajectoryRule.VELOCITY_RESIDUAL in rules:
            boundary_rule = TrajectoryRule.VELOCITY_RESIDUAL
        else:
            boundary_rule = primary
        boundary_items = [item for item in group if item.rule is boundary_rule]
        start_time_ns = min(item.start_time_ns for item in boundary_items)
        end_time_ns = max(item.end_time_ns for item in boundary_items)
        start_index = min(item.start_index for item in boundary_items)
        end_index = max(item.end_index_exclusive for item in boundary_items)
        measurements = _deduplicate_measurements(
            measurement for item in group for measurement in item.measurements
        )
        causes = tuple(
            CompatibleCause(label=label)
            for label in sorted(
                {cause.label for item in group for cause in item.compatible_causes}
            )
        )
        severity = max(group, key=lambda item: severity_order[item.severity]).severity
        identity = {
            "detector_id": DETECTOR_ID,
            "detector_version": DETECTOR_VERSION,
            "source_sha256": source_sha256,
            "start_time_ns": start_time_ns,
            "end_time_ns": end_time_ns,
            "primary_rule": primary.value,
            "triggered_rules": [rule.value for rule in rules],
        }
        events.append(
            TrajectoryIntegrityEvent(
                event_id=_event_id(identity),
                detector_id=DETECTOR_ID,
                detector_version=DETECTOR_VERSION,
                start_time_ns=start_time_ns,
                end_time_ns=end_time_ns,
                start_sample_index=start_index,
                end_sample_index_exclusive=end_index,
                primary_rule=primary,
                triggered_rules=rules,
                severity=severity,
                observability=Observability.OBSERVABLE,
                measurements=measurements,
                compatible_causes=causes,
            )
        )
    return tuple(events)


def detect_trajectory_integrity(
    samples: Iterable[ReferenceSample],
    *,
    source_sha256: str,
    partition: SyntheticPartition,
    profile: TrajectoryIntegrityProfile,
    profile_file_sha256: str,
    trajectory_parameters: TrajectoryGateParameters,
    reference_evidence: ReferencePositionEvidence | None = None,
) -> TrajectoryIntegrityReport:
    """Detect structural and content faults without consuming fault-truth metadata."""

    source = tuple(samples)
    if not source:
        raise ValueError("trajectory integrity needs at least one sample")
    if len(source) > profile.budgets.maximum_samples:
        raise ValueError("trajectory sample count exceeds the frozen budget")
    if len(source_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha256
    ):
        raise ValueError("trajectory source hash is not a full lowercase SHA-256")
    reference_positions_m: tuple[Vector3, ...] | None = None
    reference_evidence_kind = ReferenceEvidenceKind.NONE
    if reference_evidence is not None:
        if (
            reference_evidence.evidence_kind
            is ReferenceEvidenceKind.DECLARED_INDEPENDENT_REFERENCE
            and reference_evidence.reference_source_sha256 == source_sha256
        ):
            raise ValueError("independent reference evidence cannot be self-derived")
        if len(reference_evidence.samples) != len(source):
            raise ValueError("reference position sample count does not align")
        for source_sample, evidence_sample in zip(
            source, reference_evidence.samples, strict=True
        ):
            if evidence_sample.time_ns != source_sample.time.value_ns:
                raise ValueError("reference position timestamps do not align exactly")
            if (
                evidence_sample.time_epoch is not source_sample.time.epoch
                or evidence_sample.clock_id != source_sample.time.clock_id
                or evidence_sample.time_reference is not source_sample.time.reference
            ):
                raise ValueError("reference position time domains do not align")
            if (
                evidence_sample.target_frame
                != source_sample.world_from_rig.target_frame
                or evidence_sample.source_frame
                != source_sample.world_from_rig.source_frame
            ):
                raise ValueError("reference position named frames do not align")
        reference_positions_m = tuple(
            sample.position_m for sample in reference_evidence.samples
        )
        reference_evidence_kind = reference_evidence.evidence_kind

    times = [sample.time.value_ns for sample in source]
    positive_deltas = [right - left for left, right in pairwise(times) if right > left]
    stride_ns = int(median(positive_deltas)) if positive_deltas else 1
    raw: list[_RawFailure] = []

    def append(failure: _RawFailure) -> None:
        raw.append(failure)
        if len(raw) > profile.budgets.maximum_raw_failures:
            raise ValueError("trajectory raw failure count exceeds the frozen budget")

    duplicate_threshold = profile.thresholds["timestamp.duplicate_count"]
    regression_threshold = profile.thresholds["timestamp.regression_count"]
    gap_threshold = profile.thresholds["timestamp.maximum_gap_ns"]
    structural_valid = True
    for index, (left, right) in enumerate(pairwise(times)):
        if right == left:
            structural_valid = False
            start, end = _safe_structural_interval(left, right)
            append(
                _RawFailure(
                    rule=TrajectoryRule.DUPLICATE_TIMESTAMP,
                    start_time_ns=start,
                    end_time_ns=end,
                    start_index=index,
                    end_index_exclusive=index + 1,
                    measurements=(
                        _measurement(
                            profile,
                            TrajectoryRule.DUPLICATE_TIMESTAMP,
                            "timestamp.duplicate_count",
                            1.0,
                        ),
                    ),
                    severity=Severity.BLOCKING_ANALYSIS,
                )
            )
        elif right < left:
            structural_valid = False
            start, end = _safe_structural_interval(left, right)
            append(
                _RawFailure(
                    rule=TrajectoryRule.TIMESTAMP_REGRESSION,
                    start_time_ns=start,
                    end_time_ns=end,
                    start_index=index,
                    end_index_exclusive=index + 1,
                    measurements=(
                        _measurement(
                            profile,
                            TrajectoryRule.TIMESTAMP_REGRESSION,
                            "timestamp.regression_count",
                            1.0,
                        ),
                    ),
                    severity=Severity.BLOCKING_ANALYSIS,
                )
            )
        elif right - left > gap_threshold.value:
            append(
                _RawFailure(
                    rule=TrajectoryRule.TIMESTAMP_GAP,
                    start_time_ns=left,
                    end_time_ns=right,
                    start_index=index,
                    end_index_exclusive=index + 1,
                    measurements=(
                        _measurement(
                            profile,
                            TrajectoryRule.TIMESTAMP_GAP,
                            "timestamp.maximum_gap_ns",
                            float(right - left),
                        ),
                    ),
                    severity=Severity.CRITICAL,
                )
            )
    del duplicate_threshold, regression_threshold

    mismatches = _coordinate_mismatches(source)
    if mismatches:
        structural_valid = False
        for index in mismatches:
            start = times[index]
            end = _interval_end(times, index, stride_ns)
            if end <= start:
                start, end = _safe_structural_interval(start, end)
            append(
                _RawFailure(
                    rule=TrajectoryRule.COORDINATE_CONTINUITY,
                    start_time_ns=start,
                    end_time_ns=end,
                    start_index=index,
                    end_index_exclusive=index + 1,
                    measurements=(
                        _measurement(
                            profile,
                            TrajectoryRule.COORDINATE_CONTINUITY,
                            "trajectory.coordinate_continuity_mismatch_count",
                            float(len(mismatches)),
                        ),
                    ),
                    severity=Severity.BLOCKING_ANALYSIS,
                )
            )

    source_velocity_available = all(
        sample.source_velocity_world_mps is not None for sample in source
    )
    content_observability = (
        Observability.OBSERVABLE if structural_valid else Observability.NOT_APPLICABLE
    )
    support: list[RuleSupport] = [
        RuleSupport(
            rule=TrajectoryRule.TIMESTAMP_REGRESSION,
            observability=Observability.OBSERVABLE,
            evidence_kind=ReferenceEvidenceKind.NONE,
            detail="Canonical source timestamps are inspected before interpolation.",
        ),
        RuleSupport(
            rule=TrajectoryRule.DUPLICATE_TIMESTAMP,
            observability=Observability.OBSERVABLE,
            evidence_kind=ReferenceEvidenceKind.NONE,
            detail="Canonical source timestamps are inspected before interpolation.",
        ),
        RuleSupport(
            rule=TrajectoryRule.TIMESTAMP_GAP,
            observability=Observability.OBSERVABLE,
            evidence_kind=ReferenceEvidenceKind.NONE,
            detail="Elapsed source time is compared with the frozen support ceiling.",
        ),
        RuleSupport(
            rule=TrajectoryRule.COORDINATE_CONTINUITY,
            observability=Observability.OBSERVABLE,
            evidence_kind=ReferenceEvidenceKind.NONE,
            detail="Time-domain and named transform-frame continuity are explicit.",
        ),
    ]
    for rule in (
        TrajectoryRule.POSITION_JUMP,
        TrajectoryRule.POSITION_FREEZE,
    ):
        observable = (
            content_observability
            if source_velocity_available
            else Observability.NOT_OBSERVABLE
        )
        support.append(
            RuleSupport(
                rule=rule,
                observability=observable,
                evidence_kind=(
                    ReferenceEvidenceKind.PAIRED_COORDINATE_SELF_CONSISTENCY
                    if source_velocity_available
                    else ReferenceEvidenceKind.NONE
                ),
                detail=(
                    "A paired source-velocity field supports self-consistency evidence."
                    if source_velocity_available
                    else (
                        "Source velocity is absent, so motion disagreement is not "
                        "observable."
                    )
                ),
            )
        )
    support.append(
        RuleSupport(
            rule=TrajectoryRule.REFERENCE_POSITION_RESIDUAL,
            observability=(
                content_observability
                if reference_positions_m is not None
                else Observability.NOT_OBSERVABLE
            ),
            evidence_kind=reference_evidence_kind,
            detail=(
                (
                    "A declared independent position reference supports residual "
                    "evidence."
                )
                if reference_evidence_kind
                is ReferenceEvidenceKind.DECLARED_INDEPENDENT_REFERENCE
                else (
                    "A paired coordinate field supports self-consistency evidence."
                    if reference_evidence_kind
                    is ReferenceEvidenceKind.PAIRED_COORDINATE_SELF_CONSISTENCY
                    else (
                        "No independent position reference was declared; constant "
                        "bias is not observable."
                    )
                )
            ),
        )
    )
    support.append(
        RuleSupport(
            rule=TrajectoryRule.VELOCITY_RESIDUAL,
            observability=(
                content_observability
                if source_velocity_available
                else Observability.NOT_OBSERVABLE
            ),
            evidence_kind=(
                ReferenceEvidenceKind.PAIRED_COORDINATE_SELF_CONSISTENCY
                if source_velocity_available
                else ReferenceEvidenceKind.NONE
            ),
            detail=(
                "A paired source-velocity field supports self-consistency evidence."
                if source_velocity_available
                else (
                    "Source velocity is absent, so velocity residual is not observable."
                )
            ),
        )
    )
    for rule in (
        TrajectoryRule.MAXIMUM_SPEED,
        TrajectoryRule.MAXIMUM_ACCELERATION,
        TrajectoryRule.MAXIMUM_JERK,
        TrajectoryRule.MAXIMUM_YAW_RATE,
    ):
        support.append(
            RuleSupport(
                rule=rule,
                observability=content_observability,
                evidence_kind=ReferenceEvidenceKind.NONE,
                detail=(
                    "Gap-aware robust trajectory derivatives provide supported "
                    "evidence."
                ),
            )
        )

    if structural_valid:
        positions = [sample.world_from_rig.translation_m for sample in source]
        jump_limit = profile.thresholds["trajectory.position_jump_residual_m"].value
        velocity_limit = profile.thresholds["trajectory.velocity_residual_mps"].value
        freeze_observed_limit = profile.thresholds[
            "trajectory.freeze_observed_speed_mps"
        ].value
        freeze_source_minimum = profile.thresholds[
            "trajectory.freeze_source_speed_mps"
        ].value
        if source_velocity_available:
            for index in range(len(source) - 1):
                delta_ns = times[index + 1] - times[index]
                if delta_ns > gap_threshold.value:
                    continue
                seconds = delta_ns / 1_000_000_000.0
                left_velocity = cast(Vector3, source[index].source_velocity_world_mps)
                right_velocity = cast(
                    Vector3, source[index + 1].source_velocity_world_mps
                )
                predicted = cast(
                    Vector3,
                    tuple(
                        0.5 * (left + right) * seconds
                        for left, right in zip(
                            left_velocity, right_velocity, strict=True
                        )
                    ),
                )
                observed = _subtract(positions[index + 1], positions[index])
                residual_vector = _subtract(observed, predicted)
                residual_m = _norm(residual_vector)
                if _exceeds_threshold(residual_m, jump_limit, profile):
                    append(
                        _RawFailure(
                            rule=TrajectoryRule.POSITION_JUMP,
                            start_time_ns=times[index],
                            end_time_ns=times[index + 1],
                            start_index=index,
                            end_index_exclusive=index + 1,
                            measurements=(
                                _measurement(
                                    profile,
                                    TrajectoryRule.POSITION_JUMP,
                                    "trajectory.position_jump_residual_m",
                                    residual_m,
                                ),
                            ),
                            severity=Severity.CRITICAL,
                            vector=residual_vector,
                        )
                    )
                velocity_residual = residual_m / seconds
                if _exceeds_threshold(velocity_residual, velocity_limit, profile):
                    append(
                        _RawFailure(
                            rule=TrajectoryRule.VELOCITY_RESIDUAL,
                            start_time_ns=times[index + 1],
                            end_time_ns=_interval_end(times, index + 1, stride_ns),
                            start_index=index + 1,
                            end_index_exclusive=min(len(source), index + 2),
                            measurements=(
                                _measurement(
                                    profile,
                                    TrajectoryRule.VELOCITY_RESIDUAL,
                                    "trajectory.velocity_residual_mps",
                                    velocity_residual,
                                ),
                            ),
                            severity=Severity.WARNING,
                            compatible_causes=(
                                CompatibleCause(label="position drift"),
                                CompatibleCause(label="source velocity inconsistency"),
                            ),
                        )
                    )
                observed_speed = _norm(observed) / seconds
                source_speed = min(_norm(left_velocity), _norm(right_velocity))
                if (
                    observed_speed <= freeze_observed_limit
                    and source_speed >= freeze_source_minimum
                ):
                    freeze_end_index = min(len(source) - 1, index + 2)
                    append(
                        _RawFailure(
                            rule=TrajectoryRule.POSITION_FREEZE,
                            start_time_ns=times[index],
                            end_time_ns=(
                                times[freeze_end_index]
                                if freeze_end_index > index
                                else times[index + 1]
                            ),
                            start_index=index,
                            end_index_exclusive=freeze_end_index,
                            measurements=(
                                _measurement(
                                    profile,
                                    TrajectoryRule.POSITION_FREEZE,
                                    "trajectory.freeze_observed_speed_mps",
                                    observed_speed,
                                ),
                                _measurement(
                                    profile,
                                    TrajectoryRule.POSITION_FREEZE,
                                    "trajectory.freeze_source_speed_mps",
                                    source_speed,
                                ),
                            ),
                            severity=Severity.CRITICAL,
                        )
                    )

        if reference_positions_m is not None:
            reference_limit = profile.thresholds[
                "trajectory.reference_position_residual_m"
            ].value
            for index, (position, reference_position) in enumerate(
                zip(positions, reference_positions_m, strict=True)
            ):
                residual = _norm(_subtract(position, reference_position))
                if _exceeds_threshold(residual, reference_limit, profile):
                    append(
                        _RawFailure(
                            rule=TrajectoryRule.REFERENCE_POSITION_RESIDUAL,
                            start_time_ns=times[index],
                            end_time_ns=_interval_end(times, index, stride_ns),
                            start_index=index,
                            end_index_exclusive=index + 1,
                            measurements=(
                                _measurement(
                                    profile,
                                    TrajectoryRule.REFERENCE_POSITION_RESIDUAL,
                                    "trajectory.reference_position_residual_m",
                                    residual,
                                ),
                            ),
                            severity=Severity.WARNING,
                            compatible_causes=(
                                CompatibleCause(label="constant position bias"),
                                CompatibleCause(
                                    label="local-world origin disagreement"
                                ),
                                CompatibleCause(
                                    label="coordinate-frame interpretation error"
                                ),
                            ),
                        )
                    )

        continuous = ContinuousReferenceTrajectory(
            source,
            kind=ReferenceTrajectoryKind.POSTPROCESSED,
            parameters=trajectory_parameters,
        )
        derivative_rules = (
            (
                TrajectoryRule.MAXIMUM_SPEED,
                "trajectory.maximum_speed_mps",
                lambda value: _norm(value.velocity_world_mps),
            ),
            (
                TrajectoryRule.MAXIMUM_ACCELERATION,
                "trajectory.maximum_acceleration_mps2",
                lambda value: _norm(value.acceleration_world_mps2),
            ),
            (
                TrajectoryRule.MAXIMUM_JERK,
                "trajectory.maximum_jerk_mps3",
                lambda value: _norm(value.jerk_world_mps3),
            ),
            (
                TrajectoryRule.MAXIMUM_YAW_RATE,
                "trajectory.maximum_yaw_rate_radps",
                lambda value: abs(value.yaw_rate_radps),
            ),
        )
        for index, time_ns in enumerate(times):
            evaluation = continuous.evaluate(time_ns)
            if evaluation.derivatives is None:
                continue
            for rule, key, extractor in derivative_rules:
                value = float(extractor(evaluation.derivatives))
                if _exceeds_threshold(value, profile.thresholds[key].value, profile):
                    append(
                        _RawFailure(
                            rule=rule,
                            start_time_ns=time_ns,
                            end_time_ns=_interval_end(times, index, stride_ns),
                            start_index=index,
                            end_index_exclusive=index + 1,
                            measurements=(_measurement(profile, rule, key, value),),
                            severity=Severity.WARNING,
                        )
                    )

    raw = _pair_position_steps(
        raw,
        profile.thresholds["trajectory.position_jump_residual_m"].value,
        profile.event_consolidation.paired_step_maximum_duration_ns,
    )
    raw = _consolidate_freezes(
        raw,
        profile,
    )
    events = _consolidate_events(
        raw,
        profile,
        source_sha256,
        stride_ns,
    )
    support_by_rule = {item.rule: item for item in support}
    return TrajectoryIntegrityReport(
        schema_version="cartosentry.trajectory-integrity-report.v1",
        detector_id=DETECTOR_ID,
        detector_version=DETECTOR_VERSION,
        source_sha256=source_sha256,
        partition=partition,
        profile_immutable_sha256=profile.immutable_sha256,
        profile_file_sha256=profile_file_sha256,
        reference_evidence_sha256=(
            reference_evidence.evidence_sha256
            if reference_evidence is not None
            else None
        ),
        sample_count=len(source),
        structural_valid=structural_valid,
        support=tuple(support_by_rule[rule] for rule in TrajectoryRule),
        events=events,
    )


__all__ = [
    "MAXIMUM_CHARTER_BYTES",
    "PROFILE_IMMUTABLE_SHA256",
    "CompatibleCause",
    "ParsedSyntheticTrajectory",
    "ReferenceEvidenceKind",
    "ReferenceIndependenceBasis",
    "ReferencePositionEvidence",
    "ReferencePositionSample",
    "ReferenceProvenanceKind",
    "RuleMeasurement",
    "RuleSupport",
    "TrajectoryIntegrityEvent",
    "TrajectoryIntegrityProfile",
    "TrajectoryIntegrityReport",
    "TrajectoryMeasurementUnit",
    "TrajectoryRule",
    "detect_trajectory_integrity",
    "load_trajectory_integrity_profile",
    "make_reference_position_evidence",
    "parse_synthetic_trajectory_bytes",
]
