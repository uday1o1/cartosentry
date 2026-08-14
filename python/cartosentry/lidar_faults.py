"""Deterministic typed lidar fault operators for M4.1 qualification."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry.lidar_integrity import LidarFrameInput, LidarPointInput, LidarRule
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)

GATE_IMMUTABLE_SHA256 = (
    "ff7da9dd5d49d8cf3ec1df68a591fa75f03ca7a0703feda5ac3aedea6531419e"
)
MAXIMUM_GATE_BYTES = 256 * 1024


class LidarFaultOperator(StrEnum):
    SCAN_LOSS = "lidar.scan_loss"
    RING_LOSS = "lidar.ring_loss"
    SECTOR_LOSS = "lidar.sector_loss"
    DENSITY_REDUCTION = "lidar.density_reduction"
    NONFINITE = "lidar.nonfinite"
    RANGE_SCALE = "lidar.range_scale"
    POINT_TIME_CORRUPTION = "lidar.point_time_corruption"


class LidarFaultSeverity(StrEnum):
    BELOW_THRESHOLD = "below_threshold"
    NEAR_THRESHOLD = "near_threshold"
    DETECTABLE = "detectable"


class NonfiniteField(StrEnum):
    POSITION_X = "position_x"
    INTENSITY = "intensity"
    RELATIVE_TIME = "relative_time"


class NonfiniteValue(StrEnum):
    NAN = "NAN"
    POSITIVE_INFINITY = "POSITIVE_INFINITY"
    NEGATIVE_INFINITY = "NEGATIVE_INFINITY"


class DensityMode(StrEnum):
    SPATIALLY_UNIFORM = "spatially_uniform"
    SECTOR_BIASED = "sector_biased"


class PointTimeVariant(StrEnum):
    SHIFT = "shift"
    REVERSE = "reverse"
    CLAMP = "clamp"


class ScanLossParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    drop_frames: Annotated[int, Field(gt=0, le=1024)]


class RingLossParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ring_count: Annotated[int, Field(gt=0, le=4096)]
    duration_frames: Annotated[int, Field(gt=0, le=1024)]


class SectorLossParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    start_deg: Annotated[float, Field(ge=0.0, lt=360.0)]
    width_deg: Annotated[float, Field(gt=0.0, lt=360.0)]
    duration_frames: Annotated[int, Field(gt=0, le=1024)]


class DensityReductionParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    retain_fraction: Annotated[float, Field(gt=0.0, lt=1.0)]
    duration_frames: Annotated[int, Field(gt=0, le=1024)]
    mode: DensityMode


class NonfiniteParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_count: Annotated[int, Field(gt=0, le=65536)]
    field: NonfiniteField
    value: NonfiniteValue


class RangeScaleParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scale: Annotated[float, Field(gt=0.0, le=1000.0)]
    duration_frames: Annotated[int, Field(gt=0, le=1024)]


class PointTimeParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    variant: PointTimeVariant
    shift_ns: int
    duration_frames: Annotated[int, Field(gt=0, le=1024)]

    @model_validator(mode="after")
    def validate_variant(self) -> Self:
        if self.variant is not PointTimeVariant.SHIFT and self.shift_ns != 0:
            raise ValueError("non-shift point-time faults require a zero shift")
        return self


LidarFaultParameters = (
    ScanLossParameters
    | RingLossParameters
    | SectorLossParameters
    | DensityReductionParameters
    | NonfiniteParameters
    | RangeScaleParameters
    | PointTimeParameters
)


class RawLidarFaultCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    case_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")]
    severity: LidarFaultSeverity
    parameters: dict[str, object]
    expected_rule: LidarRule | None


class RawLidarFaultOperator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operator_id: LidarFaultOperator
    cases: tuple[RawLidarFaultCase, ...]


class GateAuthorities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    profile_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
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


class GateThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operator: Literal["fraction_eq", "max_le"]
    value: Annotated[float, Field(ge=0.0)]
    unit: Literal["fraction", "count", "frame", "byte"]


class LidarQualificationGate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    gate_id: Literal["m4.1-lidar-integrity-v1"]
    gate_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_AFTER_PREACCEPTANCE_AUDIT_BEFORE_M4_1_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    claim_scope: Annotated[str, Field(min_length=1)]
    matrix_status: Literal[
        "ENGINEERING_SUPPLEMENT_TO_BE_MERGED_INTO_CARTOSENTRY_V1_CORE_AT_M11_1"
    ]
    authorities: GateAuthorities
    partitions: dict[str, GatePartition]
    operators: tuple[RawLidarFaultOperator, ...]
    gates: dict[str, GateThreshold]

    @model_validator(mode="after")
    def validate_gate(self) -> Self:
        if self.immutable_sha256 != GATE_IMMUTABLE_SHA256:
            raise ValueError("lidar qualification gate identity is not pinned")
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
            raise ValueError("lidar gate partitions are not exact")
        if tuple(item.operator_id for item in self.operators) != tuple(
            LidarFaultOperator
        ):
            raise ValueError("lidar gate operators are incomplete or reordered")
        case_ids = [case.case_id for item in self.operators for case in item.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("lidar gate case identifiers must be unique")
        expected_gate_names = {
            "structural_expected_outcome_fraction",
            "supported_coverage_expected_outcome_fraction",
            "clean_false_critical_count",
            "event_boundary_error_frames",
            "public_smoke_peak_traced_bytes",
        }
        if set(self.gates) != expected_gate_names:
            raise ValueError("lidar gate metrics are not exact")
        return self


@dataclass(frozen=True)
class RegisteredLidarFaultCase:
    operator_id: LidarFaultOperator
    case_id: str
    severity: LidarFaultSeverity
    parameters: LidarFaultParameters
    expected_rule: LidarRule | None


class LidarFaultTruth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["cartosentry.lidar-fault-truth.v1"]
    operator_id: LidarFaultOperator
    case_id: Annotated[str, Field(min_length=1)]
    severity: LidarFaultSeverity
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    derived_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    start_frame_index: Annotated[int, Field(ge=0)]
    end_frame_index_exclusive: Annotated[int, Field(gt=0)]
    changed_record_count: Annotated[int, Field(gt=0)]
    expected_rule: LidarRule | None
    parameters: dict[str, object]


@dataclass(frozen=True)
class LidarFaultResult:
    frames: tuple[LidarFrameInput, ...]
    truth: LidarFaultTruth


_PARAMETER_MODELS: dict[LidarFaultOperator, type[BaseModel]] = {
    LidarFaultOperator.SCAN_LOSS: ScanLossParameters,
    LidarFaultOperator.RING_LOSS: RingLossParameters,
    LidarFaultOperator.SECTOR_LOSS: SectorLossParameters,
    LidarFaultOperator.DENSITY_REDUCTION: DensityReductionParameters,
    LidarFaultOperator.NONFINITE: NonfiniteParameters,
    LidarFaultOperator.RANGE_SCALE: RangeScaleParameters,
    LidarFaultOperator.POINT_TIME_CORRUPTION: PointTimeParameters,
}


def _canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def load_lidar_qualification_gate(
    path: Path,
) -> tuple[LidarQualificationGate, str]:
    """Load and authenticate the frozen M4.1 gate and every typed case."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="lidar qualification gate",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="lidar qualification gate",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "lidar qualification gate is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("lidar qualification gate must be an object")
    raw = cast(dict[str, object], decoded)
    expected = raw.get("immutable_sha256")
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if expected != _canonical_hash(canonical):
        raise ValueError("lidar qualification gate immutable hash is invalid")
    gate = LidarQualificationGate.model_validate_json(content)
    registered_lidar_fault_cases(gate)
    return gate, hashlib.sha256(content).hexdigest()


def registered_lidar_fault_cases(
    gate: LidarQualificationGate,
) -> tuple[RegisteredLidarFaultCase, ...]:
    """Return the gate cases after operator-specific strict validation."""

    result: list[RegisteredLidarFaultCase] = []
    for operator in gate.operators:
        model = _PARAMETER_MODELS[operator.operator_id]
        for case in operator.cases:
            parameters = model.model_validate_json(
                json.dumps(
                    case.parameters,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
            result.append(
                RegisteredLidarFaultCase(
                    operator_id=operator.operator_id,
                    case_id=case.case_id,
                    severity=case.severity,
                    parameters=cast(LidarFaultParameters, parameters),
                    expected_rule=case.expected_rule,
                )
            )
    return tuple(result)


def _finite_json_float(value: float) -> float | str:
    if math.isnan(value):
        return "NAN"
    if value == math.inf:
        return "POSITIVE_INFINITY"
    if value == -math.inf:
        return "NEGATIVE_INFINITY"
    return value


def _frames_hash(frames: tuple[LidarFrameInput, ...]) -> str:
    payload = [
        {
            "frame_index": frame.frame_index,
            "source_key": frame.source_key,
            "reference_time_ns": frame.reference_time_ns,
            "capture_start_ns": frame.capture_start_ns,
            "capture_end_ns": frame.capture_end_ns,
            "points": [
                {
                    "position_m": [
                        _finite_json_float(value) for value in point.position_m
                    ],
                    "intensity": _finite_json_float(point.intensity),
                    "ring_id": point.ring_id,
                    "relative_time_ns": _finite_json_float(point.relative_time_ns),
                    "source_offset": point.source_offset,
                }
                for point in cast(tuple[LidarPointInput, ...], frame.points)
            ],
        }
        for frame in frames
    ]
    return _canonical_hash(payload)


def _fault_start(
    frame_count: int, duration: int, *, needs_following: bool = False
) -> int:
    available = frame_count - duration - (1 if needs_following else 0)
    if available < 1:
        raise ValueError("lidar sequence is too short for the selected fault")
    return min(2, available)


def _replace_points(
    frame: LidarFrameInput, points: tuple[LidarPointInput, ...]
) -> LidarFrameInput:
    return LidarFrameInput(
        frame_index=frame.frame_index,
        source_key=frame.source_key,
        reference_time_ns=frame.reference_time_ns,
        capture_start_ns=frame.capture_start_ns,
        capture_end_ns=frame.capture_end_ns,
        points=points,
    )


def _density_indices(
    points: tuple[LidarPointInput, ...],
    target: int,
    *,
    seed: int,
    frame_index: int,
    mode: DensityMode,
) -> set[int]:
    mandatory: set[int] = set()
    rings: set[int] = set()
    bins: set[int] = set()
    for index, point in enumerate(points):
        azimuth_bin = int(
            (math.atan2(point.position_m[1], point.position_m[0]) % (2 * math.pi))
            / (2 * math.pi)
            * 16
        )
        if point.ring_id not in rings or azimuth_bin not in bins:
            mandatory.add(index)
            rings.add(point.ring_id)
            bins.add(azimuth_bin)
    target = max(target, len(mandatory))

    def rank(index: int) -> tuple[float, str]:
        point = points[index]
        angle = math.atan2(point.position_m[1], point.position_m[0]) % (2 * math.pi)
        bias = angle if mode is DensityMode.SECTOR_BIASED else 0.0
        digest = hashlib.sha256(
            f"{seed}:{frame_index}:{point.source_offset}".encode("ascii")
        ).hexdigest()
        return bias, digest

    remaining = sorted(
        (index for index in range(len(points)) if index not in mandatory),
        key=rank,
    )
    return mandatory | set(remaining[: max(0, target - len(mandatory))])


def inject_lidar_fault(
    frames: tuple[LidarFrameInput, ...],
    case: RegisteredLidarFaultCase,
    *,
    source_sha256: str,
    seed: int,
) -> LidarFaultResult:
    """Apply one deterministic in-memory fault and bind it to immutable truth."""

    if len(source_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha256
    ):
        raise ValueError("lidar source hash must be a lowercase SHA-256")
    if not frames:
        raise ValueError("lidar fault injection requires frames")
    materialized = tuple(
        _replace_points(frame, tuple(frame.points)) for frame in frames
    )
    changed = 0
    parameters = case.parameters
    if isinstance(parameters, ScanLossParameters):
        start = _fault_start(
            len(materialized), parameters.drop_frames, needs_following=True
        )
        end = start + parameters.drop_frames
        changed = sum(
            len(cast(tuple[LidarPointInput, ...], frame.points))
            for frame in materialized[start:end]
        )
        output = materialized[:start] + materialized[end:]
    else:
        duration = (
            parameters.duration_frames if hasattr(parameters, "duration_frames") else 1
        )
        start = _fault_start(len(materialized), duration)
        end = start + duration
        output_list = list(materialized)
        for frame_position in range(start, end):
            frame = output_list[frame_position]
            points = cast(tuple[LidarPointInput, ...], frame.points)
            replacement: tuple[LidarPointInput, ...]
            if isinstance(parameters, RingLossParameters):
                ring_ids = tuple(sorted({point.ring_id for point in points}))
                removed = set(ring_ids[: parameters.ring_count])
                replacement = tuple(
                    point for point in points if point.ring_id not in removed
                )
            elif isinstance(parameters, SectorLossParameters):
                start_angle = math.radians(parameters.start_deg)
                width = math.radians(parameters.width_deg)

                def retained(point: LidarPointInput) -> bool:
                    angle = math.atan2(point.position_m[1], point.position_m[0]) % (
                        2 * math.pi
                    )
                    delta = (angle - start_angle) % (2 * math.pi)
                    return delta >= width

                replacement = tuple(point for point in points if retained(point))
            elif isinstance(parameters, DensityReductionParameters):
                target = max(1, math.floor(len(points) * parameters.retain_fraction))
                indices = _density_indices(
                    points,
                    target,
                    seed=seed,
                    frame_index=frame.frame_index,
                    mode=parameters.mode,
                )
                replacement = tuple(
                    point for index, point in enumerate(points) if index in indices
                )
            elif isinstance(parameters, NonfiniteParameters):
                special = {
                    NonfiniteValue.NAN: math.nan,
                    NonfiniteValue.POSITIVE_INFINITY: math.inf,
                    NonfiniteValue.NEGATIVE_INFINITY: -math.inf,
                }[parameters.value]
                changed_indices = set(range(min(parameters.record_count, len(points))))
                changed_points: list[LidarPointInput] = []
                for index, point in enumerate(points):
                    if index not in changed_indices:
                        changed_points.append(point)
                    elif parameters.field is NonfiniteField.POSITION_X:
                        changed_points.append(
                            LidarPointInput(
                                position_m=(
                                    special,
                                    point.position_m[1],
                                    point.position_m[2],
                                ),
                                intensity=point.intensity,
                                ring_id=point.ring_id,
                                relative_time_ns=point.relative_time_ns,
                                source_offset=point.source_offset,
                            )
                        )
                    elif parameters.field is NonfiniteField.INTENSITY:
                        changed_points.append(
                            LidarPointInput(
                                position_m=point.position_m,
                                intensity=special,
                                ring_id=point.ring_id,
                                relative_time_ns=point.relative_time_ns,
                                source_offset=point.source_offset,
                            )
                        )
                    else:
                        changed_points.append(
                            LidarPointInput(
                                position_m=point.position_m,
                                intensity=point.intensity,
                                ring_id=point.ring_id,
                                relative_time_ns=special,
                                source_offset=point.source_offset,
                            )
                        )
                replacement = tuple(changed_points)
            elif isinstance(parameters, RangeScaleParameters):
                replacement = tuple(
                    LidarPointInput(
                        position_m=(
                            point.position_m[0] * parameters.scale,
                            point.position_m[1] * parameters.scale,
                            point.position_m[2] * parameters.scale,
                        ),
                        intensity=point.intensity,
                        ring_id=point.ring_id,
                        relative_time_ns=point.relative_time_ns,
                        source_offset=point.source_offset,
                    )
                    for point in points
                )
            elif isinstance(parameters, PointTimeParameters):
                replacement = tuple(
                    LidarPointInput(
                        position_m=point.position_m,
                        intensity=point.intensity,
                        ring_id=point.ring_id,
                        relative_time_ns=(
                            point.relative_time_ns + parameters.shift_ns
                            if parameters.variant is PointTimeVariant.SHIFT
                            else -point.relative_time_ns
                            if parameters.variant is PointTimeVariant.REVERSE
                            else 0.0
                        ),
                        source_offset=point.source_offset,
                    )
                    for point in points
                )
            else:
                raise AssertionError("unhandled lidar fault parameter type")
            if isinstance(
                parameters,
                (RingLossParameters, SectorLossParameters, DensityReductionParameters),
            ):
                frame_changed = len(points) - len(replacement)
            else:
                frame_changed = sum(
                    left != right for left, right in zip(points, replacement)
                )
            changed += frame_changed
            output_list[frame_position] = _replace_points(frame, replacement)
        output = tuple(output_list)
    if changed <= 0:
        raise ValueError("lidar fault injection made no material change")
    derived_sha256 = _frames_hash(output)
    truth = LidarFaultTruth(
        schema_version="cartosentry.lidar-fault-truth.v1",
        operator_id=case.operator_id,
        case_id=case.case_id,
        severity=case.severity,
        source_sha256=source_sha256,
        derived_sha256=derived_sha256,
        start_frame_index=materialized[start].frame_index,
        end_frame_index_exclusive=materialized[end - 1].frame_index + 1,
        changed_record_count=changed,
        expected_rule=case.expected_rule,
        parameters=case.parameters.model_dump(mode="json"),
    )
    return LidarFaultResult(frames=output, truth=truth)


__all__ = [
    "GATE_IMMUTABLE_SHA256",
    "DensityMode",
    "LidarFaultOperator",
    "LidarFaultResult",
    "LidarFaultSeverity",
    "LidarFaultTruth",
    "LidarQualificationGate",
    "NonfiniteField",
    "NonfiniteValue",
    "PointTimeVariant",
    "RegisteredLidarFaultCase",
    "inject_lidar_fault",
    "load_lidar_qualification_gate",
    "registered_lidar_fault_cases",
]
