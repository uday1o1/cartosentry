"""Production read-only Boreas trajectory, lidar, and calibration adapter."""

from __future__ import annotations

import csv
import hashlib
import math
import struct
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Final

from cartosentry.contracts import (
    Duration,
    FrameTimes,
    GlobalCoordinate,
    RawTime,
    RawTimeEncoding,
    RigidTransform,
    TimeEpoch,
    TimePoint,
    TimeReference,
    TimeRounding,
    UnitQuaternion,
    VerticalDatum,
)
from cartosentry.identifiers import make_frame_id

from .base import (
    AdapterCapability,
    AdapterSensorDescriptor,
    AdapterSequenceMetadata,
    AdapterSourceFile,
    CalibrationView,
    CapabilityState,
    FramePayloadHandle,
    GeographicObservation,
    LidarFrameScan,
    LidarFrameView,
    LidarPointView,
    LidarPoseSample,
    RelativePointTime,
    SourceProvenance,
    TrajectorySample,
)

ADAPTER_ID: Final = "boreas-public"
ADAPTER_VERSION: Final = "1.0.0"
BOREAS_CLOCK_ID: Final = "boreas-unix-utc"
GPS_SOURCE: Final = "applanix/gps_post_process.csv"
LIDAR_POSE_SOURCE: Final = "applanix/lidar_poses.csv"
LIDAR_DIRECTORY: Final = "lidar"
GPS_HEADER: Final = (
    "GPSTime",
    "easting",
    "northing",
    "altitude",
    "vel_east",
    "vel_north",
    "vel_up",
    "roll",
    "pitch",
    "heading",
    "angvel_z",
    "angvel_y",
    "angvel_x",
    "accelz",
    "accely",
    "accelx",
    "latitude",
    "longitude",
)
LIDAR_POSE_HEADER: Final = GPS_HEADER[:13]
CALIBRATIONS: Final = (
    ("calib/T_applanix_lidar.txt", "applanix", "lidar", True),
    ("calib/T_camera_lidar.txt", "camera", "lidar", False),
    ("calib/T_radar_lidar.txt", "radar", "lidar", False),
)
LIDAR_RECORD = struct.Struct("<6f")
LIDAR_BITS = struct.Struct("<6I")
LIDAR_RECORD_BYTES: Final = LIDAR_RECORD.size
LIDAR_RECORDS_PER_CHUNK: Final = 4096
MAXIMUM_BOREAS_LIDAR_FRAME_BYTES: Final = 16 * 1024 * 1024


class BoreasAdapterError(ValueError):
    """Safe source-key-only adapter error."""

    def __init__(
        self,
        message: str,
        *,
        source_key: str = "sequence",
        record: int = 0,
        field: str = "",
    ) -> None:
        super().__init__(message)
        self.source_key = source_key
        self.record = record
        self.field = field


def _error(source_key: str, record: int, field: str, detail: str) -> BoreasAdapterError:
    location = source_key
    if record:
        location += f":record-{record}"
    if field:
        location += f":{field}"
    return BoreasAdapterError(
        f"Invalid Boreas input at {location}: {detail}",
        source_key=source_key,
        record=record,
        field=field,
    )


def decode_boreas_lidar_record(
    content: bytes | memoryview,
    *,
    source_key: str = "lidar/frame.bin",
    record_number: int = 1,
) -> tuple[tuple[float, ...], tuple[int, ...]]:
    """Decode one exact Boreas record through the production adapter boundary."""

    if len(content) != LIDAR_RECORD_BYTES:
        raise _error(
            source_key,
            record_number,
            "record_layout",
            "exactly 24 bytes required",
        )
    values = tuple(float(value) for value in LIDAR_RECORD.unpack(content))
    bits = tuple(int(value) for value in LIDAR_BITS.unpack(content))
    if not all(math.isfinite(value) for value in values):
        raise _error(
            source_key,
            record_number,
            "point",
            "all six float32 fields must be finite",
        )
    laser_id = values[4]
    if not 0.0 <= laser_id <= 127.0 or round(laser_id) != laser_id:
        raise _error(
            source_key,
            record_number,
            "laser_id",
            "integer in [0,127] required",
        )
    return values, bits


def parse_boreas_lidar_frame_bytes(
    content: bytes, *, source_key: str = "lidar/frame.bin"
) -> int:
    """Validate one bounded Boreas frame without retaining decoded points."""

    return sum(
        1
        for _index, _values, _bits in iter_boreas_lidar_records(
            (content,), source_key=source_key
        )
    )


def iter_boreas_lidar_records(
    chunks: Iterable[bytes], *, source_key: str = "lidar/frame.bin"
) -> Iterator[tuple[int, tuple[float, ...], tuple[int, ...]]]:
    """Decode one bounded frame from arbitrary chunks through the production path."""

    total_bytes = 0
    record_index = 0
    pending = b""
    for chunk in chunks:
        total_bytes += len(chunk)
        if total_bytes > MAXIMUM_BOREAS_LIDAR_FRAME_BYTES:
            raise _error(
                source_key,
                record_index + 1,
                "record_layout",
                "byte count must be a bounded nonzero multiple of 24",
            )
        block = pending + chunk
        complete_bytes = len(block) - (len(block) % LIDAR_RECORD_BYTES)
        view = memoryview(block)
        for offset in range(0, complete_bytes, LIDAR_RECORD_BYTES):
            values, bits = decode_boreas_lidar_record(
                view[offset : offset + LIDAR_RECORD_BYTES],
                source_key=source_key,
                record_number=record_index + 1,
            )
            yield record_index, values, bits
            record_index += 1
        pending = bytes(view[complete_bytes:])
    if total_bytes == 0 or pending:
        raise _error(
            source_key,
            record_index + 1,
            "record_layout",
            "byte count must be a bounded nonzero multiple of 24",
        )


def _float(lexeme: str, source_key: str, record: int, field: str) -> float:
    try:
        value = float(lexeme)
    except ValueError as error:
        raise _error(source_key, record, field, "finite number required") from error
    if not math.isfinite(value):
        raise _error(source_key, record, field, "finite number required")
    return value


def _rotation_from_rph(roll: float, pitch: float, heading: float) -> UnitQuaternion:
    sine_roll, cosine_roll = math.sin(roll), math.cos(roll)
    sine_pitch, cosine_pitch = math.sin(pitch), math.cos(pitch)
    sine_heading, cosine_heading = math.sin(heading), math.cos(heading)
    return UnitQuaternion.from_rotation_matrix(
        (
            cosine_heading * cosine_pitch,
            cosine_heading * sine_pitch * sine_roll - sine_heading * cosine_roll,
            cosine_heading * sine_pitch * cosine_roll + sine_heading * sine_roll,
            sine_heading * cosine_pitch,
            sine_heading * sine_pitch * sine_roll + cosine_heading * cosine_roll,
            sine_heading * sine_pitch * cosine_roll - cosine_heading * sine_roll,
            -sine_pitch,
            cosine_pitch * sine_roll,
            cosine_pitch * cosine_roll,
        )
    )


def _decimal_time(lexeme: str, source_key: str, record: int, field: str) -> TimePoint:
    try:
        return TimePoint.from_decimal_seconds(
            lexeme,
            source_key=source_key,
            field=field,
            epoch=TimeEpoch.UNIX_UTC,
            clock_id=BOREAS_CLOCK_ID,
            reference=TimeReference.SAMPLE,
        )
    except (OverflowError, ValueError) as error:
        raise _error(
            source_key, record, field, "plain decimal Unix seconds required"
        ) from error


def _microsecond_time(
    lexeme: str,
    source_key: str,
    record: int,
    field: str,
    reference: TimeReference,
) -> TimePoint:
    if not lexeme or not lexeme.isascii() or not lexeme.isdigit():
        raise _error(
            source_key, record, field, "unsigned integer microseconds required"
        )
    microseconds = int(lexeme)
    if microseconds > ((2**63) - 1) // 1000:
        raise _error(source_key, record, field, "timestamp is outside signed int64")
    value_ns = microseconds * 1000
    return TimePoint(
        value_ns=value_ns,
        epoch=TimeEpoch.UNIX_UTC,
        clock_id=BOREAS_CLOCK_ID,
        reference=reference,
        raw=RawTime(
            source_key=source_key,
            field=field,
            unit="us",
            epoch=TimeEpoch.UNIX_UTC,
            reference=reference,
            encoding=RawTimeEncoding.UNSIGNED_INTEGER,
            integer_value=lexeme,
            rounding=TimeRounding.EXACT,
            maximum_conversion_error_ns=0.0,
        ),
    )


def _relative_nanoseconds(offset_seconds: float) -> tuple[int, float]:
    unrounded = offset_seconds * 1_000_000_000.0
    if not -(2**63) <= unrounded <= (2**63) - 1:
        raise ValueError("relative point time lies outside signed int64")
    rounded = (
        math.floor(unrounded + 0.5) if unrounded >= 0.0 else math.ceil(unrounded - 0.5)
    )
    return rounded, abs(unrounded - rounded)


class BoreasAdapter:
    """Sequential immutable adapter over one locally materialized sequence."""

    def __init__(self, sequence_root: Path, *, source_group_id: str) -> None:
        try:
            root = sequence_root.resolve(strict=True)
        except OSError as error:
            raise BoreasAdapterError(
                "Invalid Boreas input at sequence: unavailable"
            ) from error
        if not root.is_dir() or not root.name:
            raise BoreasAdapterError(
                "Invalid Boreas input at sequence: directory required"
            )
        self._root = root
        self._source_group_id = source_group_id
        self._require_file(GPS_SOURCE)
        self._require_file(LIDAR_POSE_SOURCE)
        for source_key, _, _, required in CALIBRATIONS:
            if required:
                self._require_file(source_key)
        lidar_directory = self._path(LIDAR_DIRECTORY)
        if not lidar_directory.is_dir():
            raise _error(LIDAR_DIRECTORY, 0, "", "directory is unavailable")
        self._frame_source_keys = self._enumerate_frame_keys(lidar_directory)

    def _path(self, source_key: str) -> Path:
        candidate = (self._root / source_key).resolve(strict=False)
        try:
            candidate.relative_to(self._root)
        except ValueError as error:
            raise _error(
                source_key, 0, "", "source key escapes sequence root"
            ) from error
        return candidate

    def _require_file(self, source_key: str) -> Path:
        path = self._path(source_key)
        if not path.is_file():
            raise _error(source_key, 0, "", "required file is unavailable")
        return path

    def _enumerate_frame_keys(self, directory: Path) -> tuple[str, ...]:
        candidates: list[tuple[int, str]] = []
        try:
            entries = tuple(directory.iterdir())
        except OSError as error:
            raise _error(LIDAR_DIRECTORY, 0, "", "directory cannot be read") from error
        for entry in entries:
            if entry.suffix != ".bin" or not entry.is_file():
                continue
            stem = entry.stem
            if not stem.isascii() or not stem.isdigit():
                raise _error(
                    f"{LIDAR_DIRECTORY}/{entry.name}",
                    0,
                    "filename_timestamp",
                    "unsigned integer microseconds required",
                )
            source_key = f"{LIDAR_DIRECTORY}/{entry.name}"
            resolved = self._path(source_key)
            if resolved != entry.resolve():
                raise _error(source_key, 0, "", "source link is inconsistent")
            candidates.append((int(stem), source_key))
        if not candidates:
            raise _error(LIDAR_DIRECTORY, 0, "", "no binary frames found")
        candidates.sort()
        return tuple(source_key for _, source_key in candidates)

    def _capability(
        self,
        capability_id: str,
        source_key: str | None,
        *,
        supported: bool,
        detail: str,
    ) -> AdapterCapability:
        if supported:
            state = CapabilityState.AVAILABLE
        elif source_key is not None and not self._path(source_key).exists():
            state = CapabilityState.MISSING_OPTIONAL
        else:
            state = CapabilityState.UNSUPPORTED
        return AdapterCapability(
            capability_id=capability_id,
            state=state,
            source_key=source_key,
            detail=detail,
        )

    def sequence_metadata(self) -> AdapterSequenceMetadata:
        capabilities = (
            self._capability(
                "trajectory-samples",
                GPS_SOURCE,
                supported=True,
                detail="postprocessed reference trajectory",
            ),
            self._capability(
                "lidar-frames",
                LIDAR_DIRECTORY,
                supported=True,
                detail="24-byte point records with relative time",
            ),
            self._capability(
                "lidar-pose-samples",
                LIDAR_POSE_SOURCE,
                supported=True,
                detail="postprocessed lidar poses",
            ),
            self._capability(
                "camera-normalization",
                "camera",
                supported=False,
                detail="camera payload parsing is outside V1",
            ),
            self._capability(
                "radar-normalization",
                "radar",
                supported=False,
                detail="radar payload parsing is outside V1",
            ),
            self._capability(
                "imu-normalization",
                "applanix/imu_raw.csv",
                supported=False,
                detail="raw IMU normalization is a follow-on track",
            ),
            AdapterCapability(
                capability_id="trajectory-altitude-datum",
                state=CapabilityState.UNSUPPORTED,
                source_key=GPS_SOURCE,
                detail="dataset altitude datum is not established",
            ),
        )
        return AdapterSequenceMetadata(
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            source_sequence_key=self._root.name,
            source_group_id=self._source_group_id,
            partition="development",
            capabilities=capabilities,
        )

    def sensors(self) -> tuple[AdapterSensorDescriptor, ...]:
        return (
            AdapterSensorDescriptor(
                stream_key="trajectory-postprocessed",
                modality="trajectory",
                sensor_id="applanix-postprocessed",
                coordinate_frame="applanix",
                time_epoch="UNIX_UTC",
                clock_id=BOREAS_CLOCK_ID,
                reference="SAMPLE",
                required_calibration_keys=(),
            ),
            AdapterSensorDescriptor(
                stream_key="lidar-lidar",
                modality="lidar",
                sensor_id="lidar",
                coordinate_frame="lidar",
                time_epoch="UNIX_UTC",
                clock_id=BOREAS_CLOCK_ID,
                reference="SCAN_MIDPOINT",
                required_calibration_keys=("calib/T_applanix_lidar.txt",),
            ),
        )

    def _csv_rows(
        self, source_key: str, expected_header: tuple[str, ...]
    ) -> Iterator[tuple[int, tuple[str, ...]]]:
        path = self._require_file(source_key)
        try:
            with path.open("r", encoding="utf-8", newline="") as stream:
                reader = csv.reader(stream)
                try:
                    header = tuple(next(reader))
                except StopIteration as error:
                    raise _error(
                        source_key, 1, "header", "header is missing"
                    ) from error
                if header != expected_header:
                    raise _error(
                        source_key, 1, "header", "exact documented header required"
                    )
                for record_index, row in enumerate(reader):
                    record_number = record_index + 2
                    if len(row) != len(expected_header):
                        raise _error(
                            source_key,
                            record_number,
                            "row",
                            f"exactly {len(expected_header)} columns required",
                        )
                    yield record_index, tuple(row)
        except (OSError, UnicodeError, csv.Error) as error:
            raise _error(source_key, 0, "", "text read failed") from error

    def pose_samples(self) -> Iterator[TrajectorySample]:
        previous_time: int | None = None
        for record_index, fields in self._csv_rows(GPS_SOURCE, GPS_HEADER):
            record_number = record_index + 2
            time = _decimal_time(fields[0], GPS_SOURCE, record_number, "GPSTime")
            if previous_time is not None and time.value_ns < previous_time:
                raise _error(
                    GPS_SOURCE,
                    record_number,
                    "GPSTime",
                    "timestamps must be nondecreasing",
                )
            previous_time = time.value_ns
            values = tuple(
                _float(fields[index], GPS_SOURCE, record_number, GPS_HEADER[index])
                for index in range(1, len(fields))
            )
            latitude_rad, longitude_rad = values[15], values[16]
            latitude_deg = math.degrees(latitude_rad)
            longitude_deg = math.degrees(longitude_rad)
            if (
                not -90.0 <= latitude_deg <= 90.0
                or not -180.0 <= longitude_deg <= 180.0
            ):
                raise _error(
                    GPS_SOURCE,
                    record_number,
                    "latitude_longitude",
                    "converted WGS84 coordinate is out of range",
                )
            yield TrajectorySample(
                time=time,
                world_from_rig=RigidTransform(
                    target_frame="enu_ref",
                    source_frame="applanix",
                    translation_m=(values[0], values[1], values[2]),
                    rotation=_rotation_from_rph(values[6], values[7], values[8]),
                ),
                velocity_enu_mps=(values[3], values[4], values[5]),
                angular_velocity_rig_radps=(values[11], values[10], values[9]),
                acceleration_rig_mps2=(values[14], values[13], values[12]),
                geographic=GeographicObservation(
                    coordinate=GlobalCoordinate(
                        latitude_deg=latitude_deg,
                        longitude_deg=longitude_deg,
                        altitude_m=None,
                        vertical_datum=VerticalDatum.UNKNOWN_VERTICAL_DATUM,
                    ),
                    latitude_source_rad=latitude_rad,
                    longitude_source_rad=longitude_rad,
                ),
                source_altitude_m=values[2],
                provenance=SourceProvenance(
                    source_key=GPS_SOURCE,
                    record_index=record_index,
                    byte_offset=None,
                    raw_fields=fields,
                ),
            )

    def lidar_pose_samples(self) -> Iterator[LidarPoseSample]:
        previous_time: int | None = None
        for record_index, fields in self._csv_rows(
            LIDAR_POSE_SOURCE, LIDAR_POSE_HEADER
        ):
            record_number = record_index + 2
            time = _microsecond_time(
                fields[0],
                LIDAR_POSE_SOURCE,
                record_number,
                "GPSTime",
                TimeReference.SAMPLE,
            )
            if previous_time is not None and time.value_ns < previous_time:
                raise _error(
                    LIDAR_POSE_SOURCE,
                    record_number,
                    "GPSTime",
                    "timestamps must be nondecreasing",
                )
            previous_time = time.value_ns
            values = tuple(
                _float(
                    fields[index],
                    LIDAR_POSE_SOURCE,
                    record_number,
                    LIDAR_POSE_HEADER[index],
                )
                for index in range(1, len(fields))
            )
            yield LidarPoseSample(
                time=time,
                world_from_lidar=RigidTransform(
                    target_frame="enu_ref",
                    source_frame="lidar",
                    translation_m=(values[0], values[1], values[2]),
                    rotation=_rotation_from_rph(values[6], values[7], values[8]),
                ),
                velocity_enu_mps=(values[3], values[4], values[5]),
                angular_velocity_lidar_radps=(values[11], values[10], values[9]),
                provenance=SourceProvenance(
                    source_key=LIDAR_POSE_SOURCE,
                    record_index=record_index,
                    byte_offset=None,
                    raw_fields=fields,
                ),
            )

    def calibrations(self) -> Iterator[CalibrationView]:
        for source_key, target_frame, source_frame, required in CALIBRATIONS:
            path = self._path(source_key)
            if not path.is_file() and not required:
                continue
            path = self._require_file(source_key)
            try:
                rows = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as error:
                raise _error(source_key, 0, "", "text read failed") from error
            if len(rows) != 4:
                raise _error(source_key, 0, "matrix", "exactly four rows required")
            lexemes: list[str] = []
            values: list[float] = []
            for row_index, row in enumerate(rows, start=1):
                fields = row.split()
                if len(fields) != 4:
                    raise _error(
                        source_key,
                        row_index,
                        "matrix",
                        "exactly four columns required",
                    )
                lexemes.extend(fields)
                values.extend(
                    _float(field, source_key, row_index, "matrix") for field in fields
                )
            if tuple(values[12:]) != (0.0, 0.0, 0.0, 1.0):
                raise _error(source_key, 4, "matrix", "homogeneous bottom row required")
            try:
                rotation = UnitQuaternion.from_rotation_matrix(
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
                )
            except ValueError as error:
                raise _error(
                    source_key, 0, "matrix", "proper rigid rotation required"
                ) from error
            yield CalibrationView(
                calibration_key=source_key,
                source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                transform=RigidTransform(
                    target_frame=target_frame,
                    source_frame=source_frame,
                    translation_m=(values[3], values[7], values[11]),
                    rotation=rotation,
                ),
                raw_row_major_4x4_lexemes=tuple(lexemes),
                required_for_v1=required,
            )

    def source_files(self) -> Iterator[AdapterSourceFile]:
        """Yield the exact selected V1 sources in portable key order."""

        calibration_required = {item[0]: item[3] for item in CALIBRATIONS}
        source_keys = sorted(
            (
                GPS_SOURCE,
                LIDAR_POSE_SOURCE,
                *(
                    source_key
                    for source_key, _, _, required in CALIBRATIONS
                    if required or self._path(source_key).is_file()
                ),
                *self._frame_source_keys,
            )
        )
        for source_key in source_keys:
            path = self._require_file(source_key)
            try:
                snapshot = path.stat()
            except OSError as error:
                raise _error(
                    source_key, 0, "", "source metadata read failed"
                ) from error
            media_type = (
                "application-octet-stream"
                if source_key.endswith(".bin")
                else "text-csv"
                if source_key.endswith(".csv")
                else "text-plain"
            )
            yield AdapterSourceFile(
                source_key=source_key,
                byte_count=snapshot.st_size,
                modified_time_ns=snapshot.st_mtime_ns,
                device_id=snapshot.st_dev,
                file_id=snapshot.st_ino,
                required_for_v1=(
                    source_key in {GPS_SOURCE, LIDAR_POSE_SOURCE}
                    or source_key.endswith(".bin")
                    or calibration_required.get(source_key, False)
                ),
                media_type=media_type,
            )

    def source_chunks(
        self, source: AdapterSourceFile, *, chunk_bytes: int
    ) -> Iterator[bytes]:
        """Read one snapshotted source through a bounded binary buffer."""

        if chunk_bytes <= 0:
            raise ValueError("source chunk size must be positive")
        path = self._require_file(source.source_key)
        try:
            before = path.stat()
            if (
                before.st_size != source.byte_count
                or before.st_mtime_ns != source.modified_time_ns
                or before.st_dev != source.device_id
                or before.st_ino != source.file_id
            ):
                raise _error(
                    source.source_key,
                    0,
                    "source_snapshot",
                    "source changed after enumeration",
                )
            total = 0
            with path.open("rb") as stream:
                while chunk := stream.read(chunk_bytes):
                    total += len(chunk)
                    yield chunk
            after = path.stat()
        except OSError as error:
            raise _error(source.source_key, 0, "", "binary read failed") from error
        if total != source.byte_count or (
            after.st_size != source.byte_count
            or after.st_mtime_ns != source.modified_time_ns
            or after.st_dev != source.device_id
            or after.st_ino != source.file_id
        ):
            raise _error(
                source.source_key,
                0,
                "source_snapshot",
                "source changed during hashing",
            )

    def frames(self) -> Iterator[LidarFrameView]:
        for source_key in self._frame_source_keys:
            path = self._require_file(source_key)
            byte_count = path.stat().st_size
            if (
                byte_count == 0
                or byte_count > MAXIMUM_BOREAS_LIDAR_FRAME_BYTES
                or byte_count % LIDAR_RECORD_BYTES != 0
            ):
                raise _error(
                    source_key,
                    0,
                    "record_layout",
                    "byte count must be a bounded nonzero multiple of 24",
                )
            source_frame_key = path.stem
            midpoint = _microsecond_time(
                source_frame_key,
                source_key,
                0,
                "filename_timestamp",
                TimeReference.SCAN_MIDPOINT,
            )
            frame_id = make_frame_id(
                "lidar-lidar",
                source_frame_key,
                {"sensor_time": midpoint.model_dump(mode="json")},
            )
            yield LidarFrameView(
                frame_id=frame_id,
                source_frame_key=source_frame_key,
                times=FrameTimes(sensor_time=midpoint),
                payload=FramePayloadHandle(
                    source_key=source_key,
                    byte_count=byte_count,
                    record_count=byte_count // LIDAR_RECORD_BYTES,
                    record_layout=(
                        "little-endian float32[x,y,z,intensity,laser_id,time_offset]"
                    ),
                ),
            )

    def _records(
        self, frame: LidarFrameView
    ) -> Iterator[tuple[int, tuple[float, ...], tuple[int, ...]]]:
        expected_key = f"{LIDAR_DIRECTORY}/{frame.source_frame_key}.bin"
        if frame.payload.source_key != expected_key:
            raise BoreasAdapterError(
                "lidar frame handle does not belong to this adapter"
            )
        path = self._require_file(expected_key)
        if (
            frame.payload.byte_count > MAXIMUM_BOREAS_LIDAR_FRAME_BYTES
            or path.stat().st_size != frame.payload.byte_count
        ):
            raise _error(
                expected_key, 0, "byte_count", "source changed after enumeration"
            )
        record_index = 0
        try:
            with path.open("rb") as stream:
                chunks = iter(
                    lambda: stream.read(LIDAR_RECORD_BYTES * LIDAR_RECORDS_PER_CHUNK),
                    b"",
                )
                for record_index, values, bits in iter_boreas_lidar_records(
                    chunks, source_key=expected_key
                ):
                    yield record_index, values, bits
                record_index += 1
        except OSError as error:
            raise _error(expected_key, 0, "", "binary read failed") from error
        if record_index != frame.payload.record_count:
            raise _error(
                expected_key, 0, "record_layout", "point count changed during read"
            )

    def _point_time(
        self,
        frame: LidarFrameView,
        record_index: int,
        offset_seconds: float,
        time_bits: int,
    ) -> tuple[RelativePointTime, TimePoint]:
        offset_ns, conversion_error, absolute_ns = self._point_time_values(
            frame, record_index, offset_seconds
        )
        relative = RelativePointTime(
            offset_ns=offset_ns,
            raw_float32_bits_hex=f"{time_bits:08x}",
            maximum_conversion_error_ns=conversion_error,
        )
        absolute = TimePoint(
            value_ns=absolute_ns,
            epoch=TimeEpoch.UNIX_UTC,
            clock_id=BOREAS_CLOCK_ID,
            reference=TimeReference.PER_POINT,
            raw=RawTime(
                source_key=frame.payload.source_key,
                field=f"point[{record_index}].time_offset",
                unit="s",
                epoch=TimeEpoch.UNIX_UTC,
                reference=TimeReference.PER_POINT,
                encoding=RawTimeEncoding.ENCODED_BYTES,
                encoded_bytes=f"{time_bits:08x}",
                rounding=TimeRounding.NEAREST_NANOSECOND_HALF_AWAY_FROM_ZERO,
                maximum_conversion_error_ns=conversion_error,
            ),
        )
        return relative, absolute

    def _point_time_values(
        self,
        frame: LidarFrameView,
        record_index: int,
        offset_seconds: float,
    ) -> tuple[int, float, int]:
        midpoint = frame.times.sensor_time
        if midpoint is None:
            raise BoreasAdapterError("lidar frame is missing its scan midpoint")
        try:
            offset_ns, conversion_error = _relative_nanoseconds(offset_seconds)
            absolute_ns = midpoint.shifted_value_ns(Duration(value_ns=offset_ns))
        except (OverflowError, ValueError) as error:
            raise _error(
                frame.payload.source_key,
                record_index + 1,
                "time_offset",
                "absolute point time is outside signed int64",
            ) from error
        return offset_ns, conversion_error, absolute_ns

    def lidar_points(self, frame: LidarFrameView) -> Iterator[LidarPointView]:
        midpoint = frame.times.sensor_time
        if midpoint is None:
            raise BoreasAdapterError("lidar frame is missing its scan midpoint")
        for record_index, values, bits in self._records(frame):
            relative, absolute = self._point_time(
                frame, record_index, values[5], bits[5]
            )
            yield LidarPointView(
                record_index=record_index,
                byte_offset=record_index * LIDAR_RECORD_BYTES,
                position_lidar_m=(values[0], values[1], values[2]),
                intensity=values[3],
                laser_id=round(values[4]),
                relative_time=relative,
                absolute_time=absolute,
                frame_reference=midpoint,
                raw_float32_bits_hex=(
                    f"{bits[0]:08x}",
                    f"{bits[1]:08x}",
                    f"{bits[2]:08x}",
                    f"{bits[3]:08x}",
                    f"{bits[4]:08x}",
                    f"{bits[5]:08x}",
                ),
            )

    def scan_lidar_frame(self, frame: LidarFrameView) -> LidarFrameScan:
        first: tuple[int, int, float, int] | None = None
        last: tuple[int, int, float, int] | None = None
        previous_ns: int | None = None
        nondecreasing = True
        maximum_error = 0.0
        point_count = 0
        for record_index, values, bits in self._records(frame):
            _, conversion_error, absolute_ns = self._point_time_values(
                frame, record_index, values[5]
            )
            point_count += 1
            if first is None or absolute_ns < first[0]:
                first = (absolute_ns, record_index, values[5], bits[5])
            if last is None or absolute_ns > last[0]:
                last = (absolute_ns, record_index, values[5], bits[5])
            if previous_ns is not None and absolute_ns < previous_ns:
                nondecreasing = False
            previous_ns = absolute_ns
            maximum_error = max(maximum_error, conversion_error)
        if first is None or last is None or point_count == 0:
            raise _error(
                frame.payload.source_key, 0, "point", "at least one point required"
            )
        first_time = self._point_time(frame, first[1], first[2], first[3])[1]
        last_time = self._point_time(frame, last[1], last[2], last[3])[1]
        return LidarFrameScan(
            frame_id=frame.frame_id,
            point_count=point_count,
            first_point_time=first_time,
            last_point_time=last_time,
            timestamps_nondecreasing=nondecreasing,
            required_fields_finite=True,
            maximum_time_conversion_error_ns=maximum_error,
        )


def qualify_boreas_adapter(
    adapter: BoreasAdapter, *, maximum_lidar_frames: int | None = None
) -> dict[str, object]:
    """Exercise the complete production adapter through sequential public views."""

    if maximum_lidar_frames is not None and maximum_lidar_frames <= 0:
        raise ValueError("maximum lidar frames must be positive")
    metadata = adapter.sequence_metadata()
    calibrations = tuple(adapter.calibrations())
    trajectory_count = 0
    trajectory_first_ns: int | None = None
    trajectory_last_ns: int | None = None
    for sample in adapter.pose_samples():
        trajectory_count += 1
        trajectory_first_ns = (
            sample.time.value_ns
            if trajectory_first_ns is None
            else min(trajectory_first_ns, sample.time.value_ns)
        )
        trajectory_last_ns = (
            sample.time.value_ns
            if trajectory_last_ns is None
            else max(trajectory_last_ns, sample.time.value_ns)
        )
    lidar_pose_count = 0
    lidar_pose_times: set[int] = set()
    for lidar_pose_sample in adapter.lidar_pose_samples():
        lidar_pose_count += 1
        lidar_pose_times.add(lidar_pose_sample.time.value_ns)
    all_frames = tuple(adapter.frames())
    selected_frames = (
        all_frames
        if maximum_lidar_frames is None
        else all_frames[:maximum_lidar_frames]
    )
    scans: list[LidarFrameScan] = []
    for frame in selected_frames:
        scans.append(adapter.scan_lidar_frame(frame))
    midpoint_matches = sum(
        frame.times.sensor_time is not None
        and frame.times.sensor_time.value_ns in lidar_pose_times
        for frame in selected_frames
    )
    optional_states = {
        item.capability_id: item.state.value
        for item in metadata.capabilities
        if item.state is not CapabilityState.AVAILABLE
    }
    required_calibrations = [item for item in calibrations if item.required_for_v1]
    accepted = (
        trajectory_count > 0
        and lidar_pose_count > 0
        and bool(selected_frames)
        and all(
            scan.point_count == frame.payload.record_count
            for scan, frame in zip(scans, selected_frames, strict=True)
        )
        and all(scan.required_fields_finite for scan in scans)
        and all(scan.timestamps_nondecreasing for scan in scans)
        and midpoint_matches == len(selected_frames)
        and len(required_calibrations) == 1
        and required_calibrations[0].transform.target_frame == "applanix"
        and required_calibrations[0].transform.source_frame == "lidar"
        and bool(optional_states)
    )
    return {
        "accepted": accepted,
        "adapter_id": metadata.adapter_id,
        "adapter_version": metadata.adapter_version,
        "available_lidar_frames": len(all_frames),
        "calibration_count": len(calibrations),
        "lidar_frame_midpoint_pose_matches": midpoint_matches,
        "lidar_frames_checked": len(selected_frames),
        "lidar_point_count": sum(item.point_count for item in scans),
        "lidar_point_time_maximum_conversion_error_ns": max(
            (item.maximum_time_conversion_error_ns for item in scans), default=0.0
        ),
        "lidar_pose_sample_count": lidar_pose_count,
        "optional_capability_states": optional_states,
        "source_group_id": metadata.source_group_id,
        "source_sequence_key": metadata.source_sequence_key,
        "trajectory_first_time_ns": trajectory_first_ns,
        "trajectory_last_time_ns": trajectory_last_ns,
        "trajectory_sample_count": trajectory_count,
    }


def source_group_for_sequence(sequence_key: str, split_manifest_path: Path) -> str:
    """Resolve one frozen source group without moving or inferring membership."""

    import json

    split = json.loads(split_manifest_path.read_text(encoding="utf-8"))
    matches = [
        group["source_group_id"]
        for group in split["real_source_groups"]
        if sequence_key in group["sequence_ids"]
    ]
    if len(matches) != 1:
        raise ValueError("sequence is not assigned to exactly one frozen source group")
    return str(matches[0])


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "MAXIMUM_BOREAS_LIDAR_FRAME_BYTES",
    "BoreasAdapter",
    "BoreasAdapterError",
    "decode_boreas_lidar_record",
    "iter_boreas_lidar_records",
    "parse_boreas_lidar_frame_bytes",
    "qualify_boreas_adapter",
    "source_group_for_sequence",
]
