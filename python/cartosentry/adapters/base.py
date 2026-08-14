"""Common immutable, sequential input-adapter contract."""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import Field, StringConstraints

from cartosentry.contracts import (
    ContractModel,
    FrameTimes,
    GlobalCoordinate,
    RigidTransform,
    Sha256,
    TimePoint,
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
Vector3 = tuple[float, float, float]


class CapabilityState(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING_OPTIONAL = "MISSING_OPTIONAL"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_REQUESTED = "NOT_REQUESTED"


class AdapterCapability(ContractModel):
    capability_id: Identifier
    state: CapabilityState
    source_key: PortableKey | None
    detail: str


class AdapterSequenceMetadata(ContractModel):
    adapter_id: Literal["boreas-public"]
    adapter_version: Literal["1.0.0"]
    source_sequence_key: Identifier
    source_group_id: Identifier
    partition: Literal["development"]
    capabilities: tuple[AdapterCapability, ...]


class AdapterSensorDescriptor(ContractModel):
    stream_key: Identifier
    modality: Literal["trajectory", "lidar"]
    sensor_id: Identifier
    coordinate_frame: Identifier
    time_epoch: Literal["UNIX_UTC"]
    clock_id: Identifier
    reference: Literal["SAMPLE", "SCAN_MIDPOINT"]
    required_calibration_keys: tuple[PortableKey, ...]


class AdapterSourceFile(ContractModel):
    """One immutable source snapshot selected by an adapter."""

    source_key: PortableKey
    byte_count: NonnegativeInt
    modified_time_ns: NonnegativeInt
    device_id: NonnegativeInt
    file_id: NonnegativeInt
    required_for_v1: bool
    media_type: Identifier


class SourceProvenance(ContractModel):
    source_key: PortableKey
    record_index: NonnegativeInt
    byte_offset: NonnegativeInt | None
    raw_fields: tuple[str, ...]


class CalibrationView(ContractModel):
    calibration_key: PortableKey
    source_sha256: Sha256
    transform: RigidTransform
    raw_row_major_4x4_lexemes: tuple[str, ...]
    required_for_v1: bool


class GeographicObservation(ContractModel):
    coordinate: GlobalCoordinate
    latitude_source_rad: float
    longitude_source_rad: float
    conversion: Literal["degrees=radians*180/pi"] = "degrees=radians*180/pi"


class TrajectorySample(ContractModel):
    time: TimePoint
    world_from_rig: RigidTransform
    velocity_enu_mps: Vector3
    angular_velocity_rig_radps: Vector3
    acceleration_rig_mps2: Vector3
    geographic: GeographicObservation
    source_altitude_m: float
    provenance: SourceProvenance


class LidarPoseSample(ContractModel):
    time: TimePoint
    world_from_lidar: RigidTransform
    velocity_enu_mps: Vector3
    angular_velocity_lidar_radps: Vector3
    provenance: SourceProvenance


class FramePayloadHandle(ContractModel):
    source_key: PortableKey
    byte_count: PositiveInt
    record_count: PositiveInt
    record_layout: Literal[
        "little-endian float32[x,y,z,intensity,laser_id,time_offset]"
    ]


class LidarFrameView(ContractModel):
    frame_id: StableId
    stream_key: Literal["lidar-lidar"] = "lidar-lidar"
    source_frame_key: Identifier
    times: FrameTimes
    payload: FramePayloadHandle
    capture_interval_state: Literal["DERIVED_BY_POINT_SCAN"] = "DERIVED_BY_POINT_SCAN"


class RelativePointTime(ContractModel):
    offset_ns: int
    raw_float32_bits_hex: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}$")]
    unit: Literal["s"] = "s"
    reference: Literal["SCAN_MIDPOINT"] = "SCAN_MIDPOINT"
    rounding: Literal["nearest_nanosecond_half_away_from_zero"] = (
        "nearest_nanosecond_half_away_from_zero"
    )
    maximum_conversion_error_ns: Annotated[float, Field(ge=0.0, le=0.5)]


class LidarPointView(ContractModel):
    record_index: NonnegativeInt
    byte_offset: NonnegativeInt
    position_lidar_m: Vector3
    intensity: float
    laser_id: Annotated[int, Field(ge=0, le=127)]
    relative_time: RelativePointTime
    absolute_time: TimePoint
    frame_reference: TimePoint
    raw_float32_bits_hex: tuple[str, str, str, str, str, str]


class LidarFrameScan(ContractModel):
    frame_id: StableId
    point_count: PositiveInt
    first_point_time: TimePoint
    last_point_time: TimePoint
    timestamps_nondecreasing: bool
    required_fields_finite: bool
    maximum_time_conversion_error_ns: Annotated[float, Field(ge=0.0, le=0.5)]


class ReadOnlyAdapter(Protocol):
    def sequence_metadata(self) -> AdapterSequenceMetadata: ...

    def sensors(self) -> tuple[AdapterSensorDescriptor, ...]: ...

    def calibrations(self) -> Iterator[CalibrationView]: ...

    def pose_samples(self) -> Iterator[TrajectorySample]: ...

    def lidar_pose_samples(self) -> Iterator[LidarPoseSample]: ...

    def frames(self) -> Iterator[LidarFrameView]: ...

    def lidar_points(self, frame: LidarFrameView) -> Iterator[LidarPointView]: ...

    def source_files(self) -> Iterator[AdapterSourceFile]: ...

    def source_chunks(
        self, source: AdapterSourceFile, *, chunk_bytes: int
    ) -> Iterator[bytes]: ...


__all__ = [
    "AdapterCapability",
    "AdapterSensorDescriptor",
    "AdapterSequenceMetadata",
    "AdapterSourceFile",
    "CalibrationView",
    "CapabilityState",
    "FramePayloadHandle",
    "GeographicObservation",
    "LidarFrameScan",
    "LidarFrameView",
    "LidarPointView",
    "LidarPoseSample",
    "ReadOnlyAdapter",
    "RelativePointTime",
    "SourceProvenance",
    "TrajectorySample",
]
