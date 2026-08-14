"""Typed deterministic V1 fault laboratory and immutable provenance manifests."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import Field, StringConstraints, model_validator

from .artifacts import SourceInterval
from .contracts import ContractModel, Sha256
from .identifiers import assert_portable, canonical_sha256, make_fault_id
from .synthetic_models import SyntheticFixture

FAULT_MATRIX_ID: Literal["cartosentry-v1-core"] = "cartosentry-v1-core"
FAULT_MANIFEST_SCHEMA: Literal["cartosentry.fault-manifest.v1"] = (
    "cartosentry.fault-manifest.v1"
)
OPERATOR_VERSION: Literal["1.0.0"] = "1.0.0"

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


class FaultOperatorId(StrEnum):
    TIMESTAMP_DISCONTINUITY = "trajectory.timestamp_discontinuity"
    POSITION_JUMP = "trajectory.position_jump"
    POINT_TIME_SHIFT = "lidar.point_time_shift"
    RING_LOSS = "lidar.ring_loss"
    AZIMUTH_SECTOR_LOSS = "lidar.azimuth_sector_loss"
    CALIBRATION_PERTURBATION = "lidar.calibration_perturbation"


class FaultSeverity(StrEnum):
    BELOW_THRESHOLD = "below_threshold"
    NEAR_THRESHOLD = "near_threshold"
    DETECTABLE = "detectable"


class ChangeKind(StrEnum):
    MODIFIED = "MODIFIED"
    REMOVED = "REMOVED"


class TimestampDiscontinuityParameters(ContractModel):
    operator_id: Literal[FaultOperatorId.TIMESTAMP_DISCONTINUITY]
    gap_ns: PositiveInt
    affected_samples: PositiveInt


class PositionJumpParameters(ContractModel):
    operator_id: Literal[FaultOperatorId.POSITION_JUMP]
    translation_m: Vector3
    duration_s: PositiveFloat

    @model_validator(mode="after")
    def validate_translation(self) -> Self:
        if not any(value != 0.0 for value in self.translation_m):
            raise ValueError("position jump translation must be nonzero")
        return self


class PointTimeShiftParameters(ContractModel):
    operator_id: Literal[FaultOperatorId.POINT_TIME_SHIFT]
    shift_ns: int
    duration_frames: PositiveInt

    @model_validator(mode="after")
    def validate_shift(self) -> Self:
        if self.shift_ns == 0:
            raise ValueError("lidar point-time shift must be nonzero")
        return self


class RingLossParameters(ContractModel):
    operator_id: Literal[FaultOperatorId.RING_LOSS]
    ring_count: Annotated[int, Field(gt=0, le=256)]
    duration_frames: PositiveInt


class AzimuthSectorLossParameters(ContractModel):
    operator_id: Literal[FaultOperatorId.AZIMUTH_SECTOR_LOSS]
    start_deg: Annotated[float, Field(ge=0.0, lt=360.0)]
    width_deg: Annotated[float, Field(gt=0.0, le=360.0)]
    duration_frames: PositiveInt


class CalibrationPerturbationParameters(ContractModel):
    operator_id: Literal[FaultOperatorId.CALIBRATION_PERTURBATION]
    translation_m: Vector3
    yaw_deg: Annotated[float, Field(ge=-180.0, le=180.0)]

    @model_validator(mode="after")
    def validate_nonzero_perturbation(self) -> Self:
        if self.yaw_deg == 0.0 and not any(
            value != 0.0 for value in self.translation_m
        ):
            raise ValueError("lidar calibration perturbation must be nonzero")
        return self


type FaultParameters = Annotated[
    TimestampDiscontinuityParameters
    | PositionJumpParameters
    | PointTimeShiftParameters
    | RingLossParameters
    | AzimuthSectorLossParameters
    | CalibrationPerturbationParameters,
    Field(discriminator="operator_id"),
]


class ChangedValue(ContractModel):
    json_pointer: PortableKey
    change_kind: ChangeKind
    source_value_sha256: Sha256
    derived_value_sha256: Sha256 | None

    @model_validator(mode="after")
    def validate_pointer(self) -> Self:
        if not self.json_pointer.startswith("/"):
            raise ValueError("changed value must use an absolute JSON Pointer")
        if "\\" in self.json_pointer or any(
            component in {".", ".."}
            for component in self.json_pointer.removeprefix("/").split("/")
        ):
            raise ValueError("changed value contains an unsafe JSON Pointer")
        if (
            self.change_kind is ChangeKind.REMOVED
            and self.derived_value_sha256 is not None
        ):
            raise ValueError("removed values cannot have a derived value hash")
        if (
            self.change_kind is ChangeKind.MODIFIED
            and self.derived_value_sha256 is None
        ):
            raise ValueError("modified values need a derived value hash")
        return self


class DerivedArtifact(ContractModel):
    artifact_key: PortableKey
    media_type: Literal["application/json"] = "application/json"
    sha256: Sha256
    byte_count: NonnegativeInt


class ProvenanceEdge(ContractModel):
    source_sha256: Sha256
    derived_sha256: Sha256
    transformation_id: StableId


class FaultManifest(ContractModel):
    schema_version: Literal["cartosentry.fault-manifest.v1"]
    fault_id: StableId
    fault_matrix_id: Literal["cartosentry-v1-core"]
    fault_matrix_sha256: Sha256
    operator_id: FaultOperatorId
    operator_version: Literal["1.0.0"]
    case_id: Identifier
    severity: FaultSeverity
    seed: NonnegativeInt
    source_fixture_id: StableId
    source_identity_sha256: Sha256
    source_family_id: Identifier
    source_group_id: Identifier
    inherited_partition: Literal["development"]
    clean_source_truth_sha256: Sha256
    target_streams: tuple[Identifier, ...]
    target_fields: tuple[PortableKey, ...]
    source_interval: SourceInterval
    parameters: FaultParameters
    resulting_artifacts: tuple[DerivedArtifact, ...]
    provenance: tuple[ProvenanceEdge, ...]
    changed_values: tuple[ChangedValue, ...]
    expected_detector_capabilities: tuple[Identifier, ...]
    expected_affected_road_bins: tuple[StableId, ...]
    injected_observable: bool

    def identity_payload(self) -> dict[str, str]:
        return {
            "fault_matrix_id": self.fault_matrix_id,
            "operator_id": self.operator_id.value,
            "case_id": self.case_id,
            "source_family_id": self.source_family_id,
            "source_identity_sha256": self.source_identity_sha256,
        }

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        expected = make_fault_id(**self.identity_payload())
        if self.fault_id != expected:
            raise ValueError("fault_id does not match the frozen identity contract")
        if self.parameters.operator_id is not self.operator_id:
            raise ValueError("parameter type does not match operator_id")
        if not self.changed_values:
            raise ValueError("fault manifest must attribute at least one changed value")
        if not self.resulting_artifacts or not self.provenance:
            raise ValueError(
                "fault manifest must record derived artifacts and provenance"
            )
        artifact_hashes = {item.sha256 for item in self.resulting_artifacts}
        if any(
            edge.source_sha256 != self.source_identity_sha256
            for edge in self.provenance
        ):
            raise ValueError("provenance edge does not begin at the immutable source")
        if any(edge.derived_sha256 not in artifact_hashes for edge in self.provenance):
            raise ValueError("provenance edge references an unknown derived artifact")
        if any(edge.transformation_id != self.fault_id for edge in self.provenance):
            raise ValueError("provenance transformation does not match fault_id")
        portable = self.model_dump(mode="json", exclude={"changed_values"})
        assert_portable(portable, location="fault manifest")
        return self


class FaultRequest(ContractModel):
    operator_id: FaultOperatorId
    case_id: Identifier
    seed: NonnegativeInt
    clean_source_truth_sha256: Sha256
    expected_affected_road_bins: tuple[StableId, ...] = ()
    injected_observable: bool = True


@dataclass(frozen=True)
class RegisteredFaultCase:
    operator_id: FaultOperatorId
    case_id: str
    severity: FaultSeverity
    parameters: FaultParameters


@dataclass(frozen=True)
class FaultRegistry:
    matrix_sha256: str
    cases: dict[tuple[FaultOperatorId, str], RegisteredFaultCase]

    def case(self, operator_id: FaultOperatorId, case_id: str) -> RegisteredFaultCase:
        try:
            return self.cases[(operator_id, case_id)]
        except KeyError as error:
            raise ValueError(
                f"case {case_id!r} is not registered for operator {operator_id.value!r}"
            ) from error


@dataclass(frozen=True)
class FaultResult:
    derivative_bytes: bytes
    manifest: FaultManifest


def _typed_parameters(operator_id: FaultOperatorId, raw: object) -> FaultParameters:
    payload = cast(dict[str, object], raw)
    tagged = {"operator_id": operator_id.value, **payload}
    serialized = json.dumps(tagged, allow_nan=False)
    if operator_id is FaultOperatorId.TIMESTAMP_DISCONTINUITY:
        return TimestampDiscontinuityParameters.model_validate_json(serialized)
    if operator_id is FaultOperatorId.POSITION_JUMP:
        return PositionJumpParameters.model_validate_json(serialized)
    if operator_id is FaultOperatorId.POINT_TIME_SHIFT:
        return PointTimeShiftParameters.model_validate_json(serialized)
    if operator_id is FaultOperatorId.RING_LOSS:
        return RingLossParameters.model_validate_json(serialized)
    if operator_id is FaultOperatorId.AZIMUTH_SECTOR_LOSS:
        return AzimuthSectorLossParameters.model_validate_json(serialized)
    return CalibrationPerturbationParameters.model_validate_json(serialized)


def load_fault_registry(matrix_path: Path) -> FaultRegistry:
    """Load and strictly bind the implementation registry to the frozen matrix."""

    content = matrix_path.read_bytes()
    matrix = json.loads(content)
    if matrix.get("fault_matrix_id") != FAULT_MATRIX_ID:
        raise ValueError("fault matrix identifier is not cartosentry-v1-core")
    implemented = {item.value for item in FaultOperatorId}
    allowlist = matrix.get("v1_operator_allowlist")
    if not isinstance(allowlist, list) or set(allowlist) != implemented:
        raise ValueError("fault matrix allowlist and implementation registry differ")
    definitions = matrix.get("operators")
    if not isinstance(definitions, list):
        raise ValueError("fault matrix operators must be a list")
    cases: dict[tuple[FaultOperatorId, str], RegisteredFaultCase] = {}
    seen_operators: set[FaultOperatorId] = set()
    for definition in definitions:
        if not isinstance(definition, dict):
            raise ValueError("fault operator definition must be an object")
        try:
            operator_id = FaultOperatorId(definition["operator_id"])
        except (KeyError, ValueError) as error:
            raise ValueError(
                "fault matrix contains an unimplemented operator"
            ) from error
        seen_operators.add(operator_id)
        raw_cases = definition.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("every fault operator needs at least one case")
        for raw_case in raw_cases:
            if not isinstance(raw_case, dict):
                raise ValueError("fault case must be an object")
            try:
                case_id = str(raw_case["case_id"])
                severity = FaultSeverity(raw_case["severity"])
                parameters = _typed_parameters(operator_id, raw_case["parameters"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    "fault matrix contains an invalid typed case"
                ) from error
            key = (operator_id, case_id)
            if key in cases:
                raise ValueError(
                    "fault matrix case identifiers must be unique per operator"
                )
            cases[key] = RegisteredFaultCase(
                operator_id=operator_id,
                case_id=case_id,
                severity=severity,
                parameters=parameters,
            )
    if seen_operators != set(FaultOperatorId):
        raise ValueError("fault matrix is missing an implemented operator")
    return FaultRegistry(matrix_sha256=hashlib.sha256(content).hexdigest(), cases=cases)


def _canonical_payload_bytes(payload: object) -> bytes:
    assert_portable(payload, location="fault source or derivative")
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _selection(seed: int, label: str, count: int) -> int:
    if count <= 0:
        raise ValueError("fault target range is empty")
    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % count


def _duration_samples(duration_s: float, sample_period_ns: int) -> int:
    duration_ns = round(duration_s * 1_000_000_000)
    if duration_ns <= 0 or not math.isclose(
        duration_s * 1_000_000_000, duration_ns, abs_tol=1e-6
    ):
        raise ValueError("position-jump duration must resolve to integer nanoseconds")
    return max(1, math.ceil(duration_ns / sample_period_ns))


def _frame_span(total_frames: int, requested: int, seed: int, label: str) -> range:
    if requested > total_frames:
        raise ValueError("fault duration exceeds the available lidar frames")
    start = _selection(seed, label, total_frames - requested + 1)
    return range(start, start + requested)


def _time_interval_for_trajectory(
    fixture: SyntheticFixture, start: int, count: int
) -> SourceInterval:
    end_index = start + count
    if end_index >= len(fixture.trajectory):
        raise ValueError("trajectory fault interval has no half-open end sample")
    return SourceInterval(
        start=fixture.trajectory[start].time,
        end=fixture.trajectory[end_index].time,
    )


def _time_interval_for_frames(
    fixture: SyntheticFixture, frames: range
) -> SourceInterval:
    return SourceInterval(
        start=fixture.lidar_scans[frames.start].capture_start,
        end=fixture.lidar_scans[frames.stop - 1].capture_end,
    )


def _apply_timestamp_discontinuity(
    payload: dict[str, object],
    fixture: SyntheticFixture,
    parameters: TimestampDiscontinuityParameters,
    seed: int,
) -> tuple[SourceInterval, tuple[str, ...], tuple[str, ...]]:
    available = len(fixture.trajectory) - parameters.affected_samples - 1
    if available <= 0:
        raise ValueError("timestamp fault exceeds the available trajectory samples")
    start = 1 + _selection(seed, "trajectory.timestamp_discontinuity", available)
    interval = _time_interval_for_trajectory(
        fixture, start, parameters.affected_samples
    )
    trajectory = cast(list[dict[str, object]], payload["trajectory"])
    for index in range(start, start + parameters.affected_samples):
        time = cast(dict[str, object], trajectory[index]["time"])
        shifted = cast(int, time["value_ns"]) + parameters.gap_ns
        time["value_ns"] = shifted
        raw = cast(dict[str, object], time["raw"])
        raw["integer_value"] = str(shifted)
    return interval, ("synthetic-trajectory",), ("trajectory.time",)


def _apply_position_jump(
    payload: dict[str, object],
    fixture: SyntheticFixture,
    parameters: PositionJumpParameters,
    seed: int,
) -> tuple[SourceInterval, tuple[str, ...], tuple[str, ...]]:
    count = _duration_samples(parameters.duration_s, fixture.sample_period_ns)
    available = len(fixture.trajectory) - count - 1
    if available <= 0:
        raise ValueError("position fault exceeds the available trajectory samples")
    start = _selection(seed, "trajectory.position_jump", available + 1)
    interval = _time_interval_for_trajectory(fixture, start, count)
    trajectory = cast(list[dict[str, object]], payload["trajectory"])
    for index in range(start, start + count):
        transform = cast(dict[str, object], trajectory[index]["world_from_rig"])
        matrix = cast(list[float], transform["row_major_4x4"])
        for matrix_index, delta in zip(
            (3, 7, 11), parameters.translation_m, strict=True
        ):
            matrix[matrix_index] = round(matrix[matrix_index] + delta, 12)
    return interval, ("synthetic-trajectory",), ("trajectory.position",)


def _apply_point_time_shift(
    payload: dict[str, object],
    fixture: SyntheticFixture,
    parameters: PointTimeShiftParameters,
    seed: int,
) -> tuple[SourceInterval, tuple[str, ...], tuple[str, ...]]:
    frames = _frame_span(
        len(fixture.lidar_scans),
        parameters.duration_frames,
        seed,
        "lidar.point_time_shift",
    )
    scans = cast(list[dict[str, object]], payload["lidar_scans"])
    changed = 0
    for frame_index in frames:
        points = cast(list[dict[str, object]], scans[frame_index]["points"])
        for point in points:
            point["relative_time_ns"] = (
                cast(int, point["relative_time_ns"]) + parameters.shift_ns
            )
            changed += 1
    if changed == 0:
        raise ValueError("point-time target contains no lidar points")
    return (
        _time_interval_for_frames(fixture, frames),
        ("synthetic-lidar",),
        ("lidar.point.relative_time_ns",),
    )


def _apply_ring_loss(
    payload: dict[str, object],
    fixture: SyntheticFixture,
    parameters: RingLossParameters,
    seed: int,
) -> tuple[SourceInterval, tuple[str, ...], tuple[str, ...]]:
    frames = _frame_span(
        len(fixture.lidar_scans),
        parameters.duration_frames,
        seed,
        "lidar.ring_loss.frames",
    )
    available_rings = sorted(
        {
            point.ring_id
            for index in frames
            for point in fixture.lidar_scans[index].points
        },
        key=lambda ring: hashlib.sha256(f"{seed}:ring:{ring}".encode()).digest(),
    )
    if parameters.ring_count > len(available_rings):
        raise ValueError("ring_count exceeds rings with returns in the target interval")
    removed_rings = set(available_rings[: parameters.ring_count])
    scans = cast(list[dict[str, object]], payload["lidar_scans"])
    removed = 0
    for frame_index in frames:
        points = cast(list[dict[str, object]], scans[frame_index]["points"])
        retained = [
            point
            for point in points
            if cast(int, point["ring_id"]) not in removed_rings
        ]
        removed += len(points) - len(retained)
        scans[frame_index]["points"] = retained
    if removed == 0:
        raise ValueError("ring-loss target contains no points from selected rings")
    return (
        _time_interval_for_frames(fixture, frames),
        ("synthetic-lidar",),
        ("lidar.point.ring_id", "lidar.point"),
    )


def _azimuth_in_sector(azimuth_rad: float, start_deg: float, width_deg: float) -> bool:
    azimuth_deg = math.degrees(azimuth_rad) % 360.0
    offset = (azimuth_deg - start_deg) % 360.0
    return offset < width_deg


def _apply_sector_loss(
    payload: dict[str, object],
    fixture: SyntheticFixture,
    parameters: AzimuthSectorLossParameters,
    seed: int,
) -> tuple[SourceInterval, tuple[str, ...], tuple[str, ...]]:
    frames = _frame_span(
        len(fixture.lidar_scans),
        parameters.duration_frames,
        seed,
        "lidar.azimuth_sector_loss",
    )
    scans = cast(list[dict[str, object]], payload["lidar_scans"])
    removed = 0
    for frame_index in frames:
        points = cast(list[dict[str, object]], scans[frame_index]["points"])
        retained = [
            point
            for point in points
            if not _azimuth_in_sector(
                cast(float, point["firing_azimuth_rad"]),
                parameters.start_deg,
                parameters.width_deg,
            )
        ]
        removed += len(points) - len(retained)
        scans[frame_index]["points"] = retained
    if removed == 0:
        raise ValueError("azimuth sector contains no lidar points")
    return (
        _time_interval_for_frames(fixture, frames),
        ("synthetic-lidar",),
        ("lidar.point.firing_azimuth_rad", "lidar.point"),
    )


def _apply_calibration_perturbation(
    payload: dict[str, object],
    fixture: SyntheticFixture,
    parameters: CalibrationPerturbationParameters,
    seed: int,
) -> tuple[SourceInterval, tuple[str, ...], tuple[str, ...]]:
    del seed
    rig = cast(dict[str, object], payload["rig"])
    transform = cast(dict[str, object], rig["rig_from_lidar"])
    matrix = cast(list[float], transform["row_major_4x4"])
    yaw = math.radians(parameters.yaw_deg)
    original_00, original_01 = matrix[0], matrix[1]
    original_10, original_11 = matrix[4], matrix[5]
    perturb_cosine = math.cos(yaw)
    perturb_sine = math.sin(yaw)
    matrix[0] = round(perturb_cosine * original_00 - perturb_sine * original_10, 12)
    matrix[1] = round(perturb_cosine * original_01 - perturb_sine * original_11, 12)
    matrix[4] = round(perturb_sine * original_00 + perturb_cosine * original_10, 12)
    matrix[5] = round(perturb_sine * original_01 + perturb_cosine * original_11, 12)
    for matrix_index, delta in zip((3, 7, 11), parameters.translation_m, strict=True):
        matrix[matrix_index] = round(matrix[matrix_index] + delta, 12)
    interval = SourceInterval(
        start=fixture.lidar_scans[0].capture_start,
        end=fixture.lidar_scans[-1].capture_end,
    )
    return (
        interval,
        ("synthetic-lidar", "synthetic-lidar-calibration"),
        ("rig.rig_from_lidar",),
    )


def _pointer_component(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _value_hash(value: object) -> str:
    return canonical_sha256(value)


def _changed_value(
    pointer: str, source: object, derived: object | None, kind: ChangeKind
) -> ChangedValue:
    return ChangedValue(
        json_pointer=pointer,
        change_kind=kind,
        source_value_sha256=_value_hash(source),
        derived_value_sha256=None
        if kind is ChangeKind.REMOVED
        else _value_hash(derived),
    )


def _semantic_changes(
    source: object, derived: object, pointer: str = ""
) -> list[ChangedValue]:
    if isinstance(source, dict) and isinstance(derived, dict):
        if source.keys() != derived.keys():
            raise ValueError("fault operators may not add or remove document fields")
        result: list[ChangedValue] = []
        for key in source:
            child = f"{pointer}/{_pointer_component(key)}"
            result.extend(_semantic_changes(source[key], derived[key], child))
        return result
    if isinstance(source, list) and isinstance(derived, list):
        if len(source) == len(derived):
            result = []
            for index, (left, right) in enumerate(zip(source, derived, strict=True)):
                result.extend(_semantic_changes(left, right, f"{pointer}/{index}"))
            return result
        result = []
        derived_index = 0
        for source_index, item in enumerate(source):
            if derived_index < len(derived) and item == derived[derived_index]:
                derived_index += 1
            else:
                result.append(
                    _changed_value(
                        f"{pointer}/{source_index}", item, None, ChangeKind.REMOVED
                    )
                )
        if derived_index != len(derived):
            raise ValueError("fault list mutation must be removal-only")
        return result
    if source != derived:
        return [_changed_value(pointer, source, derived, ChangeKind.MODIFIED)]
    return []


def _apply_registered_case(
    payload: dict[str, object],
    fixture: SyntheticFixture,
    registered: RegisteredFaultCase,
    seed: int,
) -> tuple[SourceInterval, tuple[str, ...], tuple[str, ...]]:
    parameters = registered.parameters
    if isinstance(parameters, TimestampDiscontinuityParameters):
        return _apply_timestamp_discontinuity(payload, fixture, parameters, seed)
    if isinstance(parameters, PositionJumpParameters):
        return _apply_position_jump(payload, fixture, parameters, seed)
    if isinstance(parameters, PointTimeShiftParameters):
        return _apply_point_time_shift(payload, fixture, parameters, seed)
    if isinstance(parameters, RingLossParameters):
        return _apply_ring_loss(payload, fixture, parameters, seed)
    if isinstance(parameters, AzimuthSectorLossParameters):
        return _apply_sector_loss(payload, fixture, parameters, seed)
    return _apply_calibration_perturbation(payload, fixture, parameters, seed)


_CAPABILITIES: dict[FaultOperatorId, tuple[str, ...]] = {
    FaultOperatorId.TIMESTAMP_DISCONTINUITY: ("trajectory-timestamp-integrity",),
    FaultOperatorId.POSITION_JUMP: ("trajectory-position-integrity",),
    FaultOperatorId.POINT_TIME_SHIFT: ("lidar-point-time-integrity",),
    FaultOperatorId.RING_LOSS: ("lidar-coverage-integrity",),
    FaultOperatorId.AZIMUTH_SECTOR_LOSS: ("lidar-coverage-integrity",),
    FaultOperatorId.CALIBRATION_PERTURBATION: ("lidar-calibration-integrity",),
}


def inject_fault(
    source_bytes: bytes,
    request: FaultRequest,
    registry: FaultRegistry,
) -> FaultResult:
    """Inject one allowed case after validating its entire target range."""

    fixture = SyntheticFixture.model_validate_json(source_bytes)
    source_payload = json.loads(source_bytes)
    if _canonical_payload_bytes(source_payload) != source_bytes:
        raise ValueError("fault source is not in canonical fixture byte form")
    registered = registry.case(request.operator_id, request.case_id)
    derived_payload = cast(
        dict[str, object], json.loads(json.dumps(source_payload, allow_nan=False))
    )
    interval, target_streams, target_fields = _apply_registered_case(
        derived_payload,
        fixture,
        registered,
        request.seed,
    )
    changes = tuple(_semantic_changes(source_payload, derived_payload))
    if not changes:
        raise ValueError("fault operator did not change its selected source range")
    derivative_bytes = _canonical_payload_bytes(derived_payload)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    derivative_hash = hashlib.sha256(derivative_bytes).hexdigest()
    fault_id = make_fault_id(
        fault_matrix_id=FAULT_MATRIX_ID,
        operator_id=request.operator_id.value,
        case_id=request.case_id,
        source_family_id=fixture.synthetic_family_id,
        source_identity_sha256=source_hash,
    )
    manifest = FaultManifest(
        schema_version=FAULT_MANIFEST_SCHEMA,
        fault_id=fault_id,
        fault_matrix_id=FAULT_MATRIX_ID,
        fault_matrix_sha256=registry.matrix_sha256,
        operator_id=request.operator_id,
        operator_version=OPERATOR_VERSION,
        case_id=request.case_id,
        severity=registered.severity,
        seed=request.seed,
        source_fixture_id=fixture.fixture_id,
        source_identity_sha256=source_hash,
        source_family_id=fixture.synthetic_family_id,
        source_group_id=fixture.synthetic_family_id,
        inherited_partition="development",
        clean_source_truth_sha256=request.clean_source_truth_sha256,
        target_streams=target_streams,
        target_fields=target_fields,
        source_interval=interval,
        parameters=registered.parameters,
        resulting_artifacts=(
            DerivedArtifact(
                artifact_key="derivative.json",
                sha256=derivative_hash,
                byte_count=len(derivative_bytes),
            ),
        ),
        provenance=(
            ProvenanceEdge(
                source_sha256=source_hash,
                derived_sha256=derivative_hash,
                transformation_id=fault_id,
            ),
        ),
        changed_values=changes,
        expected_detector_capabilities=_CAPABILITIES[request.operator_id],
        expected_affected_road_bins=request.expected_affected_road_bins,
        injected_observable=request.injected_observable,
    )
    return FaultResult(derivative_bytes=derivative_bytes, manifest=manifest)


def serialize_fault_manifest(manifest: FaultManifest) -> bytes:
    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def materialize_fault_result(output_root: Path, result: FaultResult) -> None:
    """Atomically publish one derivative and manifest into a new directory."""

    if output_root.exists():
        raise ValueError("fault output directory must not already exist")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent)
    )
    try:
        (staging / "derivative.json").write_bytes(result.derivative_bytes)
        (staging / "manifest.json").write_bytes(
            serialize_fault_manifest(result.manifest)
        )
        os.replace(staging, output_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_fault_result(
    source_bytes: bytes,
    derivative_bytes: bytes,
    manifest_bytes: bytes,
    registry: FaultRegistry,
) -> dict[str, object]:
    manifest = FaultManifest.model_validate_json(manifest_bytes)
    request = FaultRequest(
        operator_id=manifest.operator_id,
        case_id=manifest.case_id,
        seed=manifest.seed,
        clean_source_truth_sha256=manifest.clean_source_truth_sha256,
        expected_affected_road_bins=manifest.expected_affected_road_bins,
        injected_observable=manifest.injected_observable,
    )
    expected = inject_fault(source_bytes, request, registry)
    derivative_matches = derivative_bytes == expected.derivative_bytes
    manifest_matches = manifest == expected.manifest
    return {
        "accepted": derivative_matches and manifest_matches,
        "attributed_change_count": len(manifest.changed_values),
        "derivative_matches": derivative_matches,
        "fault_id": manifest.fault_id,
        "manifest_matches": manifest_matches,
        "operator_id": manifest.operator_id.value,
    }


__all__ = [
    "FAULT_MANIFEST_SCHEMA",
    "FAULT_MATRIX_ID",
    "AzimuthSectorLossParameters",
    "CalibrationPerturbationParameters",
    "ChangedValue",
    "FaultManifest",
    "FaultOperatorId",
    "FaultRegistry",
    "FaultRequest",
    "FaultResult",
    "FaultSeverity",
    "PointTimeShiftParameters",
    "PositionJumpParameters",
    "RingLossParameters",
    "TimestampDiscontinuityParameters",
    "inject_fault",
    "load_fault_registry",
    "materialize_fault_result",
    "serialize_fault_manifest",
    "verify_fault_result",
]
