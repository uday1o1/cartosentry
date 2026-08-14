"""Canonical persisted time, frame, transform, and coordinate contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from . import _core

MIN_INT64 = -(2**63)
MAX_INT64 = (2**63) - 1
Int64 = Annotated[int, Field(ge=MIN_INT64, le=MAX_INT64)]
NonnegativeInt64 = Annotated[int, Field(ge=0, le=MAX_INT64)]
NonemptyString = Annotated[str, StringConstraints(min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
FrameId = Annotated[
    str,
    StringConstraints(min_length=1, pattern=r"^[A-Za-z0-9_.:-]+$"),
]


class ContractModel(BaseModel):
    """Strict, immutable base for values that cross a persistence boundary."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )


class TimeEpoch(StrEnum):
    UNIX_UTC = "UNIX_UTC"
    GPS = "GPS"
    SENSOR_BOOT = "SENSOR_BOOT"
    HOST_MONOTONIC = "HOST_MONOTONIC"
    UNKNOWN = "UNKNOWN"


class TimeReference(StrEnum):
    EXPOSURE_START = "EXPOSURE_START"
    EXPOSURE_MIDPOINT = "EXPOSURE_MIDPOINT"
    EXPOSURE_END = "EXPOSURE_END"
    SCAN_START = "SCAN_START"
    SCAN_MIDPOINT = "SCAN_MIDPOINT"
    SCAN_END = "SCAN_END"
    SAMPLE = "SAMPLE"
    PER_POINT = "PER_POINT"
    PER_AZIMUTH = "PER_AZIMUTH"
    UNKNOWN = "UNKNOWN"


class RawTimeEncoding(StrEnum):
    DECIMAL_LEXEME = "decimal_lexeme"
    SIGNED_INTEGER = "signed_integer"
    UNSIGNED_INTEGER = "unsigned_integer"
    ENCODED_BYTES = "encoded_bytes"


class TimeRounding(StrEnum):
    EXACT = "exact"
    NEAREST_NANOSECOND_HALF_AWAY_FROM_ZERO = "nearest_nanosecond_half_away_from_zero"


class RawTime(ContractModel):
    """Lossless source timestamp representation and conversion provenance."""

    source_key: NonemptyString
    field: NonemptyString
    unit: NonemptyString
    epoch: TimeEpoch
    reference: TimeReference
    encoding: RawTimeEncoding
    decimal_lexeme: str | None = None
    integer_value: str | None = None
    encoded_bytes: str | None = None
    rounding: TimeRounding
    maximum_conversion_error_ns: Annotated[float, Field(ge=0.0)]

    @model_validator(mode="after")
    def validate_exact_representation(self) -> Self:
        representations = {
            RawTimeEncoding.DECIMAL_LEXEME: self.decimal_lexeme,
            RawTimeEncoding.SIGNED_INTEGER: self.integer_value,
            RawTimeEncoding.UNSIGNED_INTEGER: self.integer_value,
            RawTimeEncoding.ENCODED_BYTES: self.encoded_bytes,
        }
        populated = sum(
            value is not None
            for value in (self.decimal_lexeme, self.integer_value, self.encoded_bytes)
        )
        if populated != 1 or representations[self.encoding] is None:
            raise ValueError(
                "raw time must contain exactly the representation named by encoding"
            )
        if self.encoding in {
            RawTimeEncoding.SIGNED_INTEGER,
            RawTimeEncoding.UNSIGNED_INTEGER,
        }:
            assert self.integer_value is not None
            digits = self.integer_value
            if self.encoding is RawTimeEncoding.SIGNED_INTEGER:
                digits = digits.removeprefix("+").removeprefix("-")
            if not digits or not digits.isascii() or not digits.isdigit():
                raise ValueError("raw integer timestamp must be a plain integer")
            if (
                self.encoding is RawTimeEncoding.UNSIGNED_INTEGER
                and self.integer_value.startswith(("+", "-"))
            ):
                raise ValueError("raw unsigned timestamp cannot contain a sign")
        if self.encoding is RawTimeEncoding.DECIMAL_LEXEME:
            assert self.decimal_lexeme is not None
            lexeme = self.decimal_lexeme
            unsigned = lexeme.removeprefix("+").removeprefix("-")
            components = unsigned.split(".")
            if (
                len(components) > 2
                or any(not component for component in components)
                or any(
                    not component.isascii() or not component.isdigit()
                    for component in components
                )
            ):
                raise ValueError("raw decimal timestamp must be a plain decimal lexeme")
        if self.rounding is TimeRounding.EXACT:
            if self.maximum_conversion_error_ns != 0.0:
                raise ValueError("exact time conversion must record zero error")
        elif self.maximum_conversion_error_ns > 0.5:
            raise ValueError("nearest-nanosecond conversion error cannot exceed 0.5 ns")
        return self


class Duration(ContractModel):
    value_ns: Int64


class TimePoint(ContractModel):
    """Tagged canonical timestamp with exact signed-int64 nanoseconds."""

    value_ns: Int64
    epoch: TimeEpoch
    clock_id: NonemptyString
    reference: TimeReference
    raw: RawTime

    @model_validator(mode="after")
    def validate_raw_domain(self) -> Self:
        if self.raw.epoch is not self.epoch:
            raise ValueError("raw and canonical time epochs differ")
        if self.raw.reference is not self.reference:
            raise ValueError("raw and canonical time references differ")
        return self

    @classmethod
    def from_decimal_seconds(
        cls,
        decimal_lexeme: str,
        *,
        source_key: str,
        field: str,
        epoch: TimeEpoch,
        clock_id: str,
        reference: TimeReference,
    ) -> TimePoint:
        value_ns = _core.decimal_seconds_to_nanoseconds(decimal_lexeme)
        return cls(
            value_ns=value_ns,
            epoch=epoch,
            clock_id=clock_id,
            reference=reference,
            raw=RawTime(
                source_key=source_key,
                field=field,
                unit="s",
                epoch=epoch,
                reference=reference,
                encoding=RawTimeEncoding.DECIMAL_LEXEME,
                decimal_lexeme=decimal_lexeme,
                rounding=TimeRounding.NEAREST_NANOSECOND_HALF_AWAY_FROM_ZERO,
                maximum_conversion_error_ns=0.5,
            ),
        )

    def difference(self, start: TimePoint) -> Duration:
        value_ns = _core.checked_time_difference_ns(
            self.value_ns,
            self.epoch.value,
            self.clock_id,
            start.value_ns,
            start.epoch.value,
            start.clock_id,
        )
        return Duration(value_ns=value_ns)

    def shifted_value_ns(self, duration: Duration) -> int:
        """Return checked arithmetic without inventing derived raw provenance."""

        return _core.checked_time_add_ns(
            self.value_ns,
            self.epoch.value,
            self.clock_id,
            duration.value_ns,
        )


class FrameInterval(ContractModel):
    """Nonempty half-open capture interval in one epoch and clock."""

    capture_start: TimePoint
    capture_end: TimePoint

    @model_validator(mode="after")
    def validate_half_open_interval(self) -> Self:
        if self.capture_end.difference(self.capture_start).value_ns <= 0:
            raise ValueError("capture interval must be nonempty and half-open")
        return self


class CorrectedTime(ContractModel):
    """Clock correction that retains its original timestamp domain."""

    original: TimePoint
    corrected_value_ns: Int64
    target_epoch: TimeEpoch
    target_clock_id: NonemptyString
    correction_model_id: NonemptyString
    correction_model_sha256: Sha256
    pivot_ns: Int64
    offset_ns: Int64
    rate_ppb: float
    uncertainty_ns: NonnegativeInt64
    applicability: FrameInterval

    @model_validator(mode="after")
    def validate_applicability(self) -> Self:
        since_start = self.original.difference(self.applicability.capture_start)
        until_end = self.applicability.capture_end.difference(self.original)
        if since_start.value_ns < 0 or until_end.value_ns <= 0:
            raise ValueError("original time lies outside correction applicability")
        return self


class FrameTimes(ContractModel):
    """Permitted source and corrected time fields for one sensor frame."""

    capture_start: TimePoint | None = None
    capture_end: TimePoint | None = None
    sensor_time: TimePoint | None = None
    host_receive_time: TimePoint | None = None
    corrected_sensor_time: CorrectedTime | None = None

    @model_validator(mode="after")
    def validate_capture_pair(self) -> Self:
        if (self.capture_start is None) != (self.capture_end is None):
            raise ValueError("capture_start and capture_end must appear together")
        if self.capture_start is not None and self.capture_end is not None:
            FrameInterval(
                capture_start=self.capture_start,
                capture_end=self.capture_end,
            )
        if (
            self.sensor_time is not None
            and self.corrected_sensor_time is not None
            and self.corrected_sensor_time.original != self.sensor_time
        ):
            raise ValueError(
                "corrected_sensor_time must retain sensor_time as original"
            )
        return self


class NamedFrame(ContractModel):
    """Right-handed coordinate frame with explicit positive axis meanings."""

    frame_id: FrameId
    handedness: Literal["RIGHT_HANDED"] = "RIGHT_HANDED"
    x_axis: NonemptyString
    y_axis: NonemptyString
    z_axis: NonemptyString

    @classmethod
    def canonical_rig(cls, frame_id: str = "rig") -> NamedFrame:
        return cls(
            frame_id=frame_id,
            x_axis="forward",
            y_axis="left",
            z_axis="up",
        )


class UnitQuaternion(ContractModel):
    """Unit quaternion persisted in w, x, y, z order."""

    w: float
    x: float
    y: float
    z: float
    serialization_order: Literal["wxyz"] = "wxyz"

    @model_validator(mode="after")
    def normalize_with_frozen_tolerance(self) -> Self:
        normalized = _core.normalize_quaternion((self.w, self.x, self.y, self.z))
        object.__setattr__(self, "w", normalized["w"])
        object.__setattr__(self, "x", normalized["x"])
        object.__setattr__(self, "y", normalized["y"])
        object.__setattr__(self, "z", normalized["z"])
        return self

    def as_wxyz(self) -> tuple[float, float, float, float]:
        return (self.w, self.x, self.y, self.z)

    @classmethod
    def from_rotation_matrix(
        cls,
        row_major_values: tuple[
            float, float, float, float, float, float, float, float, float
        ],
    ) -> UnitQuaternion:
        result = _core.quaternion_from_rotation_matrix(row_major_values)
        return cls(
            w=result["w"],
            x=result["x"],
            y=result["y"],
            z=result["z"],
        )


class RigidTransform(ContractModel):
    """Named T_target_source rigid transform using homogeneous column vectors."""

    target_frame: FrameId
    source_frame: FrameId
    translation_m: tuple[float, float, float]
    rotation: UnitQuaternion
    convention: Literal["T_target_source"] = "T_target_source"
    serialization_order: Literal["translation_xyz_quaternion_wxyz"] = (
        "translation_xyz_quaternion_wxyz"
    )

    def compose(self, inner: RigidTransform) -> RigidTransform:
        result = _core.compose_rigid_transforms(
            self.target_frame,
            self.source_frame,
            self.translation_m,
            self.rotation.as_wxyz(),
            inner.target_frame,
            inner.source_frame,
            inner.translation_m,
            inner.rotation.as_wxyz(),
        )
        return self._from_native(result)

    def inverse(self) -> RigidTransform:
        return self._from_native(
            _core.invert_rigid_transform(
                self.target_frame,
                self.source_frame,
                self.translation_m,
                self.rotation.as_wxyz(),
            )
        )

    def interpolate(self, end: RigidTransform, fraction: float) -> RigidTransform:
        result = _core.interpolate_rigid_transform(
            self.target_frame,
            self.source_frame,
            self.translation_m,
            self.rotation.as_wxyz(),
            end.translation_m,
            end.rotation.as_wxyz(),
            fraction,
        )
        return self._from_native(result)

    def apply(
        self, point_source: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        result = _core.transform_point(
            self.target_frame,
            self.source_frame,
            self.translation_m,
            self.rotation.as_wxyz(),
            point_source,
        )
        return (result[0], result[1], result[2])

    @classmethod
    def _from_native(cls, value: dict[str, Any]) -> RigidTransform:
        rotation = value["rotation"]
        return cls(
            target_frame=value["target_frame"],
            source_frame=value["source_frame"],
            translation_m=tuple(value["translation_m"]),
            rotation=UnitQuaternion(
                w=rotation["w"],
                x=rotation["x"],
                y=rotation["y"],
                z=rotation["z"],
            ),
        )


class VerticalDatum(StrEnum):
    WGS84_ELLIPSOID = "WGS84_ELLIPSOID"
    UNKNOWN_VERTICAL_DATUM = "UNKNOWN_VERTICAL_DATUM"


class GlobalCoordinate(ContractModel):
    latitude_deg: Annotated[float, Field(ge=-90.0, le=90.0)]
    longitude_deg: Annotated[float, Field(ge=-180.0, le=180.0)]
    altitude_m: float | None
    vertical_datum: VerticalDatum

    @model_validator(mode="after")
    def validate_vertical_datum(self) -> Self:
        if (
            self.vertical_datum is VerticalDatum.WGS84_ELLIPSOID
            and self.altitude_m is None
        ):
            raise ValueError("WGS84 ellipsoidal coordinates require altitude_m")
        return self


class LocalCoordinate(ContractModel):
    frame: FrameId
    position_m: tuple[float, float, float]


class LocalOrigin(ContractModel):
    frame: NamedFrame
    global_coordinate: GlobalCoordinate

    @model_validator(mode="after")
    def validate_ellipsoidal_origin(self) -> Self:
        if self.global_coordinate.vertical_datum is not VerticalDatum.WGS84_ELLIPSOID:
            raise ValueError("local origin requires WGS84 ellipsoidal altitude")
        return self

    def to_local(self, point: GlobalCoordinate) -> LocalCoordinate:
        origin = self.global_coordinate
        if origin.altitude_m is None or point.altitude_m is None:
            raise ValueError("local conversion requires explicit altitude")
        if point.vertical_datum is not VerticalDatum.WGS84_ELLIPSOID:
            raise ValueError("local conversion requires WGS84 ellipsoidal altitude")
        result = _core.wgs84_to_local(
            origin.latitude_deg,
            origin.longitude_deg,
            origin.altitude_m,
            point.latitude_deg,
            point.longitude_deg,
            point.altitude_m,
            self.frame.frame_id,
        )
        return LocalCoordinate(
            frame=result["frame"], position_m=tuple(result["position_m"])
        )

    def to_global(self, point: LocalCoordinate) -> GlobalCoordinate:
        if point.frame != self.frame.frame_id:
            raise ValueError("local coordinate frame does not match its origin")
        origin = self.global_coordinate
        if origin.altitude_m is None:
            raise ValueError("local conversion requires explicit altitude")
        result = _core.local_to_wgs84(
            origin.latitude_deg,
            origin.longitude_deg,
            origin.altitude_m,
            point.frame,
            point.position_m,
        )
        return GlobalCoordinate(
            latitude_deg=result["latitude_deg"],
            longitude_deg=result["longitude_deg"],
            altitude_m=result["altitude_m"],
            vertical_datum=VerticalDatum(result["vertical_datum"]),
        )


__all__ = [
    "CorrectedTime",
    "Duration",
    "FrameInterval",
    "FrameTimes",
    "GlobalCoordinate",
    "LocalCoordinate",
    "LocalOrigin",
    "NamedFrame",
    "RawTime",
    "RawTimeEncoding",
    "RigidTransform",
    "TimeEpoch",
    "TimePoint",
    "TimeReference",
    "TimeRounding",
    "UnitQuaternion",
    "VerticalDatum",
]
