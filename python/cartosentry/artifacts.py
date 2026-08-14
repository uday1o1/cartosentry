"""Versioned persisted artifact contracts."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from . import _core
from .contracts import (
    ContractModel,
    FrameId,
    Sha256,
    TimeEpoch,
    TimePoint,
    TimeReference,
    VerticalDatum,
)
from .identifiers import (
    assert_portable,
    make_bundle_id,
    make_finding_id,
    make_recapture_plan_id,
    make_run_id,
    make_sequence_id,
    make_stream_id,
)

NonemptyString = Annotated[str, StringConstraints(min_length=1)]
PortableKey = NonemptyString
StableId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]*-sha256-[0-9a-f]{64}$"),
]
StreamId = Annotated[
    str,
    StringConstraints(pattern=r"^stream-[a-z0-9-]+-[a-z0-9-]+-[0-9a-f]{64}$"),
]
SemanticVersion = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"),
]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]
NonnegativeFloat = Annotated[float, Field(ge=0.0)]
NonnegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]


class ArtifactModel(ContractModel):
    """Base class for versioned artifact documents."""

    schema_name: ClassVar[str]

    def portable_dict(self) -> dict[str, object]:
        value = self.model_dump(mode="json")
        assert_portable(value)
        return value


class PortableArtifactModel(ArtifactModel):
    """Artifact whose complete persisted representation is portable."""

    @model_validator(mode="after")
    def validate_no_local_values(self) -> Self:
        assert_portable(self.model_dump(mode="json"))
        return self


class SourcePartition(StrEnum):
    DEVELOPMENT = "development"
    POLICY_TUNING = "policy_tuning"
    FINAL_TEST = "final_test"


class SensorModality(StrEnum):
    CAMERA = "camera"
    GNSS = "gnss"
    IMU = "imu"
    LIDAR = "lidar"
    RADAR = "radar"
    TRAJECTORY = "trajectory"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    BLOCKING_ANALYSIS = "BLOCKING_ANALYSIS"


class Observability(StrEnum):
    OBSERVABLE = "OBSERVABLE"
    WEAK = "WEAK"
    NOT_OBSERVABLE = "NOT_OBSERVABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReadinessState(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class MeasurementUnit(StrEnum):
    NANOSECOND = "ns"
    METER = "m"
    METER_PER_SECOND = "m/s"
    METER_PER_SECOND_SQUARED = "m/s^2"
    RADIAN = "rad"
    RADIAN_PER_SECOND = "rad/s"
    DEGREE = "deg"
    HERTZ = "Hz"
    COUNT = "count"
    FRACTION = "fraction"
    BYTE = "bytes"
    SQUARE_METER = "m^2"
    INVERSE_METER = "1/m"
    DECIBEL = "dB"
    BOOLEAN = "bool"


class ThresholdOperator(StrEnum):
    LESS_THAN = "lt"
    LESS_THAN_OR_EQUAL = "le"
    GREATER_THAN = "gt"
    GREATER_THAN_OR_EQUAL = "ge"
    ABSOLUTE_LESS_THAN_OR_EQUAL = "abs_le"
    EQUAL = "eq"


class StageState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    INVALIDATED = "INVALIDATED"
    SKIPPED_NOT_APPLICABLE = "SKIPPED_NOT_APPLICABLE"


class AdapterIdentity(ContractModel):
    adapter_id: Identifier
    adapter_version: SemanticVersion
    capabilities: tuple[Identifier, ...]


class SourceFile(ContractModel):
    source_key: PortableKey
    sha256: Sha256
    byte_count: NonnegativeInt


class CalibrationIdentity(ContractModel):
    calibration_id: StableId
    sha256: Sha256
    target_frame: FrameId
    source_frame: FrameId


class SensorDescriptor(ContractModel):
    stream_id: StreamId
    modality: SensorModality
    sensor_id: Identifier
    coordinate_frame: FrameId
    calibration_ids: tuple[StableId, ...]


class TimestampMetadata(ContractModel):
    stream_id: StreamId
    epoch: TimeEpoch
    clock_id: NonemptyString
    reference: TimeReference
    raw_unit: NonemptyString


class CoordinateMetadata(ContractModel):
    global_frame: Literal["WGS84"]
    local_frame: FrameId
    rig_frame: FrameId
    vertical_datum: VerticalDatum


class SourceInterval(ContractModel):
    start: TimePoint
    end: TimePoint

    @model_validator(mode="after")
    def validate_nonempty_half_open_interval(self) -> Self:
        if self.end.difference(self.start).value_ns <= 0:
            raise ValueError("source interval must be nonempty and half open")
        return self


class DeclaredGap(ContractModel):
    stream_id: StreamId
    interval: SourceInterval
    reason: NonemptyString


class SequenceManifest(PortableArtifactModel):
    schema_name = "cartosentry.sequence-manifest.v1"

    schema_version: Literal["cartosentry.sequence-manifest.v1"]
    sequence_id: StableId
    source_identity_sha256: Sha256
    source_group_id: Identifier
    partition: SourcePartition
    adapter: AdapterIdentity
    sensors: tuple[SensorDescriptor, ...]
    source_files: tuple[SourceFile, ...]
    calibrations: tuple[CalibrationIdentity, ...]
    timestamp_metadata: tuple[TimestampMetadata, ...]
    coordinate_metadata: CoordinateMetadata
    declared_gaps: tuple[DeclaredGap, ...]

    def identity_payload(self) -> dict[str, object]:
        sensor_by_stream = {item.stream_id: item for item in self.sensors}
        return {
            "adapter": self.adapter.model_dump(mode="json"),
            "calibrations": sorted(
                (item.model_dump(mode="json") for item in self.calibrations),
                key=lambda item: str(item["calibration_id"]),
            ),
            "coordinate_metadata": self.coordinate_metadata.model_dump(mode="json"),
            "sensors": sorted(
                (
                    item.model_dump(mode="json", exclude={"stream_id"})
                    for item in self.sensors
                ),
                key=lambda item: (str(item["modality"]), str(item["sensor_id"])),
            ),
            "source_files": sorted(
                (item.model_dump(mode="json") for item in self.source_files),
                key=lambda item: str(item["source_key"]),
            ),
            "source_identity_sha256": self.source_identity_sha256,
            "timestamp_metadata": sorted(
                (
                    item.model_dump(mode="json", exclude={"stream_id"})
                    | {
                        "modality": sensor_by_stream[item.stream_id].modality.value,
                        "sensor_id": sensor_by_stream[item.stream_id].sensor_id,
                    }
                    for item in self.timestamp_metadata
                    if item.stream_id in sensor_by_stream
                ),
                key=lambda item: (str(item["modality"]), str(item["sensor_id"])),
            ),
        }

    @model_validator(mode="after")
    def validate_identity_and_references(self) -> Self:
        stream_ids = [item.stream_id for item in self.sensors]
        if len(stream_ids) != len(set(stream_ids)):
            raise ValueError("sensor stream identifiers must be unique")
        calibration_ids = [item.calibration_id for item in self.calibrations]
        if len(calibration_ids) != len(set(calibration_ids)):
            raise ValueError("calibration identifiers must be unique")
        known_calibrations = set(calibration_ids)
        for sensor in self.sensors:
            expected = make_stream_id(
                self.sequence_id, sensor.modality.value, sensor.sensor_id
            )
            if sensor.stream_id != expected:
                raise ValueError(
                    "stream_id does not match sequence and sensor identity"
                )
            if not set(sensor.calibration_ids) <= known_calibrations:
                raise ValueError("sensor references an unknown calibration")
        known_streams = set(stream_ids)
        metadata_streams = {item.stream_id for item in self.timestamp_metadata}
        if metadata_streams != known_streams:
            raise ValueError("timestamp metadata must cover every stream exactly once")
        if any(item.stream_id not in known_streams for item in self.declared_gaps):
            raise ValueError("declared gap references an unknown stream")
        source_keys = [item.source_key for item in self.source_files]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("source keys must be unique")
        if self.sequence_id != make_sequence_id(self.identity_payload()):
            raise ValueError("sequence_id does not match normalized manifest identity")
        return self


class ArtifactReference(ContractModel):
    source_key: PortableKey
    sha256: Sha256
    byte_count: NonnegativeInt
    media_type: NonemptyString


class StageRecord(ContractModel):
    state: StageState
    attempt_id: Identifier | None
    output_hashes: dict[Identifier, Sha256]


class LocalRunContext(ContractModel):
    source_roots: tuple[NonemptyString, ...]
    host_name: NonemptyString
    machine_id: NonemptyString

    @model_validator(mode="after")
    def validate_absolute_source_roots(self) -> Self:
        for value in self.source_roots:
            if not (
                value.startswith(("/", "~/", "\\\\"))
                or (len(value) >= 3 and value[1:3] in {":\\", ":/"})
            ):
                raise ValueError("local source roots must be absolute paths")
        return self


class Run(ArtifactModel):
    schema_name = "cartosentry.run.v1"

    schema_version: Literal["cartosentry.run.v1"]
    run_id: StableId
    sequence_id: StableId
    road_graph_id: StableId
    profile_id: Identifier
    engine_version: SemanticVersion
    configuration_hashes: dict[Identifier, Sha256]
    state: StageState
    stages: dict[Identifier, StageRecord]
    artifacts: tuple[ArtifactReference, ...]
    local_context: LocalRunContext | None = None

    def identity_payload(self) -> dict[str, object]:
        return {
            "configuration_hashes": dict(sorted(self.configuration_hashes.items())),
            "engine_version": self.engine_version,
            "profile_id": self.profile_id,
            "road_graph_id": self.road_graph_id,
            "sequence_id": self.sequence_id,
        }

    @model_validator(mode="after")
    def validate_run_identity(self) -> Self:
        expected = make_run_id(
            sequence_id=self.sequence_id,
            road_graph_id=self.road_graph_id,
            profile_id=self.profile_id,
            engine_version=self.engine_version,
            configuration_hashes=self.configuration_hashes,
        )
        if self.run_id != expected:
            raise ValueError("run_id does not match its semantic inputs")
        assert_portable(self.portable_dict())
        return self

    def portable_dict(self) -> dict[str, object]:
        value = self.model_dump(mode="json", exclude={"local_context"})
        assert_portable(value)
        return value


class Measurement(ContractModel):
    name: Identifier
    value: float
    unit: MeasurementUnit


class Threshold(ContractModel):
    operator: ThresholdOperator
    value: float
    unit: MeasurementUnit
    charter_key: Identifier


class EvidenceReference(ContractModel):
    source_artifact_sha256: Sha256
    source_interval: SourceInterval
    frame_ids: tuple[StableId, ...]
    derived_artifact_sha256: Sha256
    detector_version: SemanticVersion
    transformation_lineage: tuple[NonemptyString, ...]


class RootCauseHypothesis(ContractModel):
    possible_cause: NonemptyString
    supporting_evidence: tuple[EvidenceReference, ...]
    contradicting_evidence: tuple[EvidenceReference, ...]
    confirmed: Literal[False] = False


class Finding(PortableArtifactModel):
    schema_name = "cartosentry.finding.v1"

    schema_version: Literal["cartosentry.finding.v1"]
    finding_id: StableId
    detector_id: Identifier
    detector_version: SemanticVersion
    rule_id: Identifier
    severity: Severity
    observability: Observability
    readiness_effect: ReadinessState | None
    streams: tuple[StreamId, ...]
    interval: SourceInterval
    measurement: Measurement
    threshold: Threshold
    road_bin_ids: tuple[StableId, ...]
    evidence: tuple[EvidenceReference, ...]
    hypotheses: tuple[RootCauseHypothesis, ...]
    remediation: NonemptyString

    @model_validator(mode="after")
    def validate_finding_identity(self) -> Self:
        if not self.streams or len(self.streams) != len(set(self.streams)):
            raise ValueError("finding streams must be nonempty and unique")
        if self.measurement.unit is not self.threshold.unit:
            raise ValueError("measurement and threshold units differ")
        expected = make_finding_id(
            detector_id=self.detector_id,
            detector_version=self.detector_version,
            rule_id=self.rule_id,
            source_interval=self.interval.model_dump(mode="json"),
            stream_ids=self.streams,
            evidence_fingerprint=[
                item.model_dump(mode="json") for item in self.evidence
            ],
        )
        if self.finding_id != expected:
            raise ValueError("finding_id does not match evidence identity")
        return self


class WindowPolicy(StrEnum):
    FIXED_DURATION = "fixed_duration"
    FIXED_DISTANCE = "fixed_distance"
    FRAME_NEIGHBORHOOD = "frame_neighborhood"
    FULL_SEQUENCE = "full_sequence"


class AggregationRule(ContractModel):
    rule_id: Identifier
    window_policy: WindowPolicy
    window_value: NonnegativeFloat | None
    window_unit: MeasurementUnit | None
    stride_value: NonnegativeFloat | None
    stride_unit: MeasurementUnit | None

    @model_validator(mode="after")
    def validate_window_units(self) -> Self:
        if (self.window_value is None) != (self.window_unit is None):
            raise ValueError("window value and unit must appear together")
        if (self.stride_value is None) != (self.stride_unit is None):
            raise ValueError("stride value and unit must appear together")
        if self.window_policy is WindowPolicy.FULL_SEQUENCE and any(
            value is not None
            for value in (
                self.window_value,
                self.window_unit,
                self.stride_value,
                self.stride_unit,
            )
        ):
            raise ValueError("full-sequence aggregation has no window or stride")
        return self


class MandatoryRequirement(ContractModel):
    requirement_id: Identifier
    evidence_key: Identifier
    operator: ThresholdOperator
    threshold: float
    unit: MeasurementUnit
    minimum_observability: Literal["OBSERVABLE"]
    charter_key: Identifier


class CharterReference(ContractModel):
    charter_key: Identifier
    document_sha256: Sha256


class ReadinessProfile(PortableArtifactModel):
    schema_name = "cartosentry.readiness-profile.v1"

    schema_version: Literal["cartosentry.readiness-profile.v1"]
    profile_id: Identifier
    profile_version: SemanticVersion
    supported_adapter_capabilities: tuple[Identifier, ...]
    required_modalities: tuple[SensorModality, ...]
    required_detectors: tuple[Identifier, ...]
    aggregation_rules: tuple[AggregationRule, ...]
    mandatory_requirements: tuple[MandatoryRequirement, ...]
    optional_review_features: tuple[Identifier, ...]
    charter_references: tuple[CharterReference, ...]

    @model_validator(mode="after")
    def validate_profile_sets(self) -> Self:
        named_sets = {
            "adapter capabilities": self.supported_adapter_capabilities,
            "modalities": self.required_modalities,
            "detectors": self.required_detectors,
        }
        for label, values in named_sets.items():
            if not values or len(values) != len(set(values)):
                raise ValueError(f"profile {label} must be nonempty and unique")
        requirement_ids = [item.requirement_id for item in self.mandatory_requirements]
        if not requirement_ids or len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("mandatory requirement identifiers must be unique")
        referenced_keys = {item.charter_key for item in self.charter_references}
        if any(
            item.charter_key not in referenced_keys
            for item in self.mandatory_requirements
        ):
            raise ValueError("mandatory requirement has no charter reference")
        return self


class Reachability(StrEnum):
    REACHABLE = "REACHABLE"
    DEFERRED = "DEFERRED"
    UNREACHABLE = "UNREACHABLE"


class RecaptureRequirement(ContractModel):
    requirement_id: StableId
    road_bin_id: StableId
    directed_arc_id: Identifier
    start_offset_m: NonnegativeFloat
    end_offset_m: NonnegativeFloat
    required_modality: SensorModality
    traversal_direction: Literal["FORWARD", "REVERSE"]
    minimum_continuous_observation_m: Annotated[float, Field(gt=0.0)]
    sensor_warmup_m: NonnegativeFloat
    priority_weight: NonnegativeInt
    reason: NonemptyString
    reachability: Reachability

    @model_validator(mode="after")
    def validate_offsets(self) -> Self:
        if self.end_offset_m <= self.start_offset_m:
            raise ValueError("recapture interval end must exceed start")
        return self


class RouteBudget(ContractModel):
    maximum_distance_m: Annotated[float, Field(gt=0.0)] | None
    maximum_duration_ns: PositiveInt | None

    @model_validator(mode="after")
    def validate_nonempty_budget(self) -> Self:
        if self.maximum_distance_m is None and self.maximum_duration_ns is None:
            raise ValueError("route budget must declare distance or duration")
        return self


class RecapturePlan(PortableArtifactModel):
    schema_name = "cartosentry.recapture-plan.v1"

    schema_version: Literal["cartosentry.recapture-plan.v1"]
    recapture_plan_id: StableId
    run_id: StableId
    road_graph_id: StableId
    depot_node_id: Identifier
    requirements: tuple[RecaptureRequirement, ...]
    route_arc_ids: tuple[Identifier, ...]
    covered_requirement_ids: tuple[StableId, ...]
    deferred_requirement_ids: tuple[StableId, ...]
    unreachable_requirement_ids: tuple[StableId, ...]
    estimated_distance_m: NonnegativeFloat
    estimated_duration_ns: NonnegativeInt
    budget: RouteBudget | None
    validation_state: ReadinessState

    def identity_payload(self) -> dict[str, object]:
        return {
            "budget": (
                None if self.budget is None else self.budget.model_dump(mode="json")
            ),
            "depot_node_id": self.depot_node_id,
            "requirements": [
                item.model_dump(mode="json") for item in self.requirements
            ],
            "road_graph_id": self.road_graph_id,
            "route_arc_ids": list(self.route_arc_ids),
            "run_id": self.run_id,
        }

    @model_validator(mode="after")
    def validate_plan_identity_and_partition(self) -> Self:
        if self.recapture_plan_id != make_recapture_plan_id(self.identity_payload()):
            raise ValueError("recapture_plan_id does not match plan semantics")
        known = {item.requirement_id: item for item in self.requirements}
        classes = (
            self.covered_requirement_ids,
            self.deferred_requirement_ids,
            self.unreachable_requirement_ids,
        )
        flattened = [item for group in classes for item in group]
        if len(flattened) != len(set(flattened)) or set(flattened) != set(known):
            raise ValueError("plan outcome lists must partition all requirements")
        expected_unreachable = {
            item.requirement_id
            for item in self.requirements
            if item.reachability is Reachability.UNREACHABLE
        }
        if set(self.unreachable_requirement_ids) != expected_unreachable:
            raise ValueError("unreachable outcomes disagree with requirements")
        return self


class BundleInterval(ContractModel):
    source_key: PortableKey
    interval: SourceInterval
    reason: NonemptyString


class AcceptedDataBundle(PortableArtifactModel):
    schema_name = "cartosentry.accepted-data-bundle.v1"

    schema_version: Literal["cartosentry.accepted-data-bundle.v1"]
    bundle_id: StableId
    immutable: Literal[True]
    source_sequence_sha256: Sha256
    sequence_id: StableId
    profile_id: Identifier
    accepted_intervals: tuple[BundleInterval, ...]
    excluded_intervals: tuple[BundleInterval, ...]
    required_calibration_ids: tuple[StableId, ...]
    derived_artifacts: tuple[ArtifactReference, ...]
    raw_data_shards: tuple[ArtifactReference, ...]

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"bundle_id"}, exclude_none=False)

    @model_validator(mode="after")
    def validate_bundle_identity(self) -> Self:
        if self.bundle_id != make_bundle_id(self.identity_payload()):
            raise ValueError("bundle_id does not match immutable bundle contents")
        accepted = {
            (item.source_key, item.interval.start.value_ns, item.interval.end.value_ns)
            for item in self.accepted_intervals
        }
        excluded = {
            (item.source_key, item.interval.start.value_ns, item.interval.end.value_ns)
            for item in self.excluded_intervals
        }
        if accepted & excluded:
            raise ValueError("an interval cannot be both accepted and excluded")
        return self


type Artifact = (
    SequenceManifest
    | Run
    | Finding
    | ReadinessProfile
    | RecapturePlan
    | AcceptedDataBundle
)

ARTIFACT_MODEL_BY_SCHEMA: dict[str, type[ArtifactModel]] = {
    model.schema_name: model
    for model in (
        SequenceManifest,
        Run,
        Finding,
        ReadinessProfile,
        RecapturePlan,
        AcceptedDataBundle,
    )
}


def validate_artifact(value: object) -> Artifact:
    if not isinstance(value, dict):
        raise ValueError("artifact JSON root must be an object")
    schema = value.get("schema_version")
    if not isinstance(schema, str) or schema not in ARTIFACT_MODEL_BY_SCHEMA:
        raise ValueError(f"unsupported artifact schema: {schema!r}")
    model = ARTIFACT_MODEL_BY_SCHEMA[schema]
    return model.model_validate(value)  # type: ignore[return-value]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"artifact JSON contains duplicate key: {key!r}")
        result[key] = value
    return result


def validate_artifact_json(text: str) -> Artifact:
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid artifact JSON: {error.msg}") from error
    if not isinstance(value, dict):
        raise ValueError("artifact JSON root must be an object")
    schema = value.get("schema_version")
    if not isinstance(schema, str) or schema not in ARTIFACT_MODEL_BY_SCHEMA:
        raise ValueError(f"unsupported artifact schema: {schema!r}")
    normalized = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    model = ARTIFACT_MODEL_BY_SCHEMA[schema]
    return model.model_validate_json(normalized)  # type: ignore[return-value]


def canonicalize_portable_artifact(artifact: Artifact) -> str:
    payload = artifact.portable_dict()
    source = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    canonical = _core.canonicalize_artifact_json(source, artifact.schema_name)
    recovered = validate_artifact_json(canonical)
    if recovered.portable_dict() != payload:
        raise ValueError("native artifact round trip changed semantic content")
    return canonical


__all__ = [
    "ARTIFACT_MODEL_BY_SCHEMA",
    "AcceptedDataBundle",
    "Artifact",
    "ArtifactReference",
    "BundleInterval",
    "CalibrationIdentity",
    "CoordinateMetadata",
    "DeclaredGap",
    "EvidenceReference",
    "Finding",
    "LocalRunContext",
    "Measurement",
    "MeasurementUnit",
    "ReadinessProfile",
    "ReadinessState",
    "RecapturePlan",
    "RecaptureRequirement",
    "RootCauseHypothesis",
    "Run",
    "SensorDescriptor",
    "SensorModality",
    "SequenceManifest",
    "Severity",
    "SourceFile",
    "SourceInterval",
    "StageState",
    "StreamId",
    "Threshold",
    "ThresholdOperator",
    "TimestampMetadata",
    "canonicalize_portable_artifact",
    "validate_artifact",
    "validate_artifact_json",
]
