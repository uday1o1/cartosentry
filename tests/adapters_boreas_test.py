"""Golden and real-data tests for the production Boreas adapter contract."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest
from cartosentry.adapters import (
    BoreasAdapter,
    BoreasAdapterError,
    qualify_boreas_adapter,
)
from cartosentry.adapters.base import CapabilityState
from cartosentry.adapters.boreas_v1 import (
    MAXIMUM_BOREAS_LIDAR_FRAME_BYTES,
    parse_boreas_lidar_frame_bytes,
)
from cartosentry.cli import app
from cartosentry.contracts import TimeEpoch, TimeReference, VerticalDatum
from pydantic import ValidationError
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SEQUENCE = REPOSITORY_ROOT / "data/public/boreas-2021-09-02-11-42"
GPS_HEADER = (
    "GPSTime,easting,northing,altitude,vel_east,vel_north,vel_up,roll,pitch,"
    "heading,angvel_z,angvel_y,angvel_x,accelz,accely,accelx,latitude,longitude"
)
LIDAR_POSE_HEADER = (
    "GPSTime,easting,northing,altitude,vel_east,vel_north,vel_up,roll,pitch,"
    "heading,angvel_z,angvel_y,angvel_x"
)
SOURCE_GROUP = "boreas-glen-shields-family-v1"


def _build_fixture(root: Path) -> Path:
    sequence = root / "boreas-2021-09-02-11-42"
    (sequence / "applanix").mkdir(parents=True)
    (sequence / "calib").mkdir()
    (sequence / "lidar").mkdir()
    latitude = math.radians(43.79)
    longitude = math.radians(-79.47)
    gps_rows = [
        [
            "0.950000001",
            "1.0",
            "2.0",
            "100.0",
            "4.0",
            "5.0",
            "6.0",
            "0.1",
            "0.2",
            "0.3",
            "9.0",
            "8.0",
            "7.0",
            "12.0",
            "11.0",
            "10.0",
            repr(latitude),
            repr(longitude),
        ],
        [
            "1.049999999",
            "2.0",
            "3.0",
            "101.0",
            "4.5",
            "5.5",
            "6.5",
            "0.1",
            "0.2",
            "0.3",
            "9.5",
            "8.5",
            "7.5",
            "12.5",
            "11.5",
            "10.5",
            repr(latitude),
            repr(longitude + 1e-7),
        ],
    ]
    (sequence / "applanix/gps_post_process.csv").write_text(
        GPS_HEADER + "\n" + "\n".join(",".join(row) for row in gps_rows) + "\n"
    )
    pose_rows = [
        ["1000000", "1", "2", "3", "4", "5", "6", "0.1", "0.2", "0.3", "9", "8", "7"],
        ["1100000", "2", "3", "4", "5", "6", "7", "0.1", "0.2", "0.3", "10", "9", "8"],
    ]
    (sequence / "applanix/lidar_poses.csv").write_text(
        LIDAR_POSE_HEADER + "\n" + "\n".join(",".join(row) for row in pose_rows) + "\n"
    )
    identity = "1 0 0 0\n0 1 0 0\n0 0 1 0\n0 0 0 1\n"
    for name in (
        "T_applanix_lidar.txt",
        "T_camera_lidar.txt",
        "T_radar_lidar.txt",
    ):
        (sequence / "calib" / name).write_text(identity)
    points = (
        (1.0, 2.0, 3.0, 0.5, 0.0, -0.05),
        (2.0, 3.0, 4.0, 0.6, 127.0, 0.05),
    )
    (sequence / "lidar/1000000.bin").write_bytes(
        b"".join(struct.pack("<6f", *point) for point in points)
    )
    return sequence


def _adapter(sequence: Path) -> BoreasAdapter:
    return BoreasAdapter(sequence, source_group_id=SOURCE_GROUP)


def test_metadata_exposes_supported_and_optional_capabilities(tmp_path: Path) -> None:
    sequence = _build_fixture(tmp_path)
    metadata = _adapter(sequence).sequence_metadata()
    assert metadata.source_sequence_key == "boreas-2021-09-02-11-42"
    assert metadata.source_group_id == SOURCE_GROUP
    states = {item.capability_id: item.state for item in metadata.capabilities}
    assert states["trajectory-samples"] is CapabilityState.AVAILABLE
    assert states["lidar-frames"] is CapabilityState.AVAILABLE
    assert states["camera-normalization"] is CapabilityState.MISSING_OPTIONAL
    assert states["radar-normalization"] is CapabilityState.MISSING_OPTIONAL
    assert states["imu-normalization"] is CapabilityState.MISSING_OPTIONAL
    assert states["trajectory-altitude-datum"] is CapabilityState.UNSUPPORTED
    (sequence / "camera").mkdir()
    present_states = {
        item.capability_id: item.state
        for item in _adapter(sequence).sequence_metadata().capabilities
    }
    assert present_states["camera-normalization"] is CapabilityState.UNSUPPORTED
    assert [
        sensor.modality
        for sensor in _adapter(_build_fixture(tmp_path / "second")).sensors()
    ] == [
        "trajectory",
        "lidar",
    ]


def test_missing_optional_calibrations_remain_explicitly_optional(
    tmp_path: Path,
) -> None:
    sequence = _build_fixture(tmp_path)
    (sequence / "calib/T_camera_lidar.txt").unlink()
    (sequence / "calib/T_radar_lidar.txt").unlink()
    adapter = _adapter(sequence)
    calibrations = tuple(adapter.calibrations())
    assert [item.calibration_key for item in calibrations] == [
        "calib/T_applanix_lidar.txt"
    ]
    assert [item.source_key for item in adapter.source_files()] == [
        "applanix/gps_post_process.csv",
        "applanix/lidar_poses.csv",
        "calib/T_applanix_lidar.txt",
        "lidar/1000000.bin",
    ]


def test_trajectory_iterator_preserves_time_geography_motion_and_raw_row(
    tmp_path: Path,
) -> None:
    samples = list(_adapter(_build_fixture(tmp_path)).pose_samples())
    assert len(samples) == 2
    first = samples[0]
    assert first.time.value_ns == 950_000_001
    assert first.time.raw.decimal_lexeme == "0.950000001"
    assert first.time.epoch is TimeEpoch.UNIX_UTC
    assert first.time.reference is TimeReference.SAMPLE
    assert first.world_from_rig.target_frame == "enu_ref"
    assert first.world_from_rig.source_frame == "applanix"
    assert first.world_from_rig.translation_m == (1.0, 2.0, 100.0)
    assert first.velocity_enu_mps == (4.0, 5.0, 6.0)
    assert first.angular_velocity_rig_radps == (7.0, 8.0, 9.0)
    assert first.acceleration_rig_mps2 == (10.0, 11.0, 12.0)
    assert first.geographic.coordinate.latitude_deg == pytest.approx(43.79)
    assert first.geographic.coordinate.longitude_deg == pytest.approx(-79.47)
    assert first.geographic.coordinate.altitude_m is None
    assert (
        first.geographic.coordinate.vertical_datum
        is VerticalDatum.UNKNOWN_VERTICAL_DATUM
    )
    assert first.source_altitude_m == 100.0
    assert first.provenance.source_key == "applanix/gps_post_process.csv"
    assert first.provenance.raw_fields[0] == "0.950000001"


def test_lidar_pose_and_calibration_views_name_transform_direction(
    tmp_path: Path,
) -> None:
    adapter = _adapter(_build_fixture(tmp_path))
    poses = list(adapter.lidar_pose_samples())
    assert poses[0].time.value_ns == 1_000_000_000
    assert poses[0].time.raw.integer_value == "1000000"
    assert poses[0].world_from_lidar.target_frame == "enu_ref"
    assert poses[0].world_from_lidar.source_frame == "lidar"
    calibrations = list(adapter.calibrations())
    assert len(calibrations) == 3
    required = [item for item in calibrations if item.required_for_v1]
    assert len(required) == 1
    assert required[0].transform.target_frame == "applanix"
    assert required[0].transform.source_frame == "lidar"
    assert required[0].raw_row_major_4x4_lexemes == (
        "1",
        "0",
        "0",
        "0",
        "0",
        "1",
        "0",
        "0",
        "0",
        "0",
        "1",
        "0",
        "0",
        "0",
        "0",
        "1",
    )


def test_frame_and_point_iterators_preserve_float_bits_and_exact_time(
    tmp_path: Path,
) -> None:
    adapter = _adapter(_build_fixture(tmp_path))
    frame = next(adapter.frames())
    assert frame.times.capture_start is None
    assert frame.times.capture_end is None
    assert frame.capture_interval_state == "DERIVED_BY_POINT_SCAN"
    assert frame.times.sensor_time is not None
    assert frame.times.sensor_time.value_ns == 1_000_000_000
    assert frame.payload.record_count == 2
    points = list(adapter.lidar_points(frame))
    assert [item.relative_time.offset_ns for item in points] == [
        -50_000_001,
        50_000_001,
    ]
    assert [item.absolute_time.value_ns for item in points] == [
        949_999_999,
        1_050_000_001,
    ]
    expected_bits = struct.unpack("<I", struct.pack("<f", -0.05))[0]
    assert points[0].relative_time.raw_float32_bits_hex == f"{expected_bits:08x}"
    assert points[0].absolute_time.raw.encoded_bytes == f"{expected_bits:08x}"
    assert points[0].frame_reference == frame.times.sensor_time
    assert points[0].byte_offset == 0
    assert points[1].byte_offset == 24
    scan = adapter.scan_lidar_frame(frame)
    assert scan.point_count == 2
    assert scan.first_point_time.value_ns == 949_999_999
    assert scan.last_point_time.value_ns == 1_050_000_001
    assert scan.timestamps_nondecreasing is True
    assert scan.required_fields_finite is True
    assert scan.maximum_time_conversion_error_ns <= 0.5


def test_iterators_are_lazy_and_views_are_immutable(tmp_path: Path) -> None:
    sequence = _build_fixture(tmp_path)
    gps = sequence / "applanix/gps_post_process.csv"
    lines = gps.read_text().splitlines()
    fields = lines[2].split(",")
    fields[1] = "not-a-number"
    lines[2] = ",".join(fields)
    gps.write_text("\n".join(lines) + "\n")
    iterator = _adapter(sequence).pose_samples()
    first = next(iterator)
    assert first.world_from_rig.translation_m[0] == 1.0
    with pytest.raises(BoreasAdapterError, match="easting"):
        next(iterator)
    with pytest.raises(ValidationError):
        first.source_altitude_m = 0.0  # type: ignore[misc]


def test_stable_views_do_not_depend_on_local_root(tmp_path: Path) -> None:
    left = _adapter(_build_fixture(tmp_path / "left"))
    right = _adapter(_build_fixture(tmp_path / "right"))
    assert left.sequence_metadata() == right.sequence_metadata()
    assert next(left.frames()) == next(right.frames())
    assert next(left.pose_samples()) == next(right.pose_samples())
    assert list(left.calibrations()) == list(right.calibrations())


def test_invalid_lidar_payload_names_source_without_echoing_value(
    tmp_path: Path,
) -> None:
    sequence = _build_fixture(tmp_path)
    lidar = sequence / "lidar/1000000.bin"
    lidar.write_bytes(struct.pack("<6f", 1.0, 2.0, 3.0, 0.5, math.nan, -0.05))
    adapter = _adapter(sequence)
    frame = next(adapter.frames())
    with pytest.raises(BoreasAdapterError) as error:
        list(adapter.lidar_points(frame))
    assert "lidar/1000000.bin" in str(error.value)
    assert "nan" not in str(error.value).lower()
    assert str(tmp_path) not in str(error.value)


def test_python_lidar_binary_boundary_is_bounded_and_exact(tmp_path: Path) -> None:
    clean = struct.pack("<6f", 1.0, 2.0, 3.0, 0.5, 1.0, 0.0)
    assert parse_boreas_lidar_frame_bytes(clean) == 1
    with pytest.raises(BoreasAdapterError, match="bounded nonzero multiple"):
        parse_boreas_lidar_frame_bytes(clean[:-1])

    sequence = _build_fixture(tmp_path)
    lidar = sequence / "lidar/1000000.bin"
    with lidar.open("wb") as stream:
        stream.truncate(MAXIMUM_BOREAS_LIDAR_FRAME_BYTES + 1)
    with pytest.raises(BoreasAdapterError, match="bounded nonzero multiple"):
        next(_adapter(sequence).frames())


def test_tiny_end_to_end_qualification_and_public_cli(tmp_path: Path) -> None:
    sequence = _build_fixture(tmp_path)
    report = qualify_boreas_adapter(_adapter(sequence))
    assert report["accepted"] is True
    assert report["trajectory_sample_count"] == 2
    assert report["lidar_pose_sample_count"] == 2
    assert report["lidar_frames_checked"] == 1
    assert report["lidar_point_count"] == 2
    output = tmp_path / "report.json"
    result = CliRunner().invoke(
        app,
        [
            "qualify-boreas-adapter",
            str(sequence),
            "--split-manifest",
            str(REPOSITORY_ROOT / "benchmarks/split_manifest.yaml"),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    report_text = output.read_text()
    assert str(tmp_path) not in report_text
    assert json.loads(report_text)["accepted"] is True


@pytest.mark.skipif(
    not PUBLIC_SEQUENCE.is_dir(), reason="verified Boreas public-smoke data unavailable"
)
def test_actual_public_smoke_streams_and_point_times_normalize() -> None:
    report = qualify_boreas_adapter(_adapter(PUBLIC_SEQUENCE))
    assert report["accepted"] is True
    assert report["trajectory_sample_count"] == 214_719
    assert report["lidar_pose_sample_count"] == 9_967
    assert report["lidar_frames_checked"] == 10
    assert report["lidar_point_count"] == 2_131_876
    assert report["lidar_frame_midpoint_pose_matches"] == 10
    assert report["calibration_count"] == 3
    assert report["lidar_point_time_maximum_conversion_error_ns"] <= 0.5
