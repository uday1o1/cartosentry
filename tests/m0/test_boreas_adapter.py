"""End-to-end contract tests for the production-minimal Boreas adapter."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest
from cartosentry.adapters import inspect_boreas
from cartosentry.cli import app
from typer.testing import CliRunner

GPS_HEADER = (
    "GPSTime,easting,northing,altitude,vel_east,vel_north,vel_up,roll,pitch,"
    "heading,angvel_z,angvel_y,angvel_x,accelz,accely,accelx,latitude,longitude"
)
LIDAR_POSE_HEADER = (
    "GPSTime,easting,northing,altitude,vel_east,vel_north,vel_up,roll,pitch,"
    "heading,angvel_z,angvel_y,angvel_x"
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _build_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    sequence = root / "boreas-synthetic"
    (sequence / "applanix").mkdir(parents=True)
    (sequence / "calib").mkdir()
    (sequence / "lidar").mkdir()
    latitude = 43.79
    longitude = -79.47
    gps_rows = []
    for index, timestamp in enumerate(("0.950000001", "1.0", "1.049999999")):
        fields = [
            timestamp,
            str(index),
            "0",
            "100",
            *("0" for _ in range(12)),
            repr(math.radians(latitude)),
            repr(math.radians(longitude + index * 0.000001)),
        ]
        gps_rows.append(",".join(fields))
    (sequence / "applanix" / "gps_post_process.csv").write_text(
        GPS_HEADER + "\n" + "\n".join(gps_rows) + "\n", encoding="utf-8"
    )
    pose_fields = ["1000000", *("0" for _ in range(12))]
    (sequence / "applanix" / "lidar_poses.csv").write_text(
        LIDAR_POSE_HEADER + "\n" + ",".join(pose_fields) + "\n",
        encoding="utf-8",
    )
    identity = "1 0 0 0\n0 1 0 0\n0 0 1 0\n0 0 0 1\n"
    for name in (
        "T_applanix_lidar.txt",
        "T_camera_lidar.txt",
        "T_radar_lidar.txt",
    ):
        (sequence / "calib" / name).write_text(identity, encoding="utf-8")
    lidar_payload = b"".join(
        (
            struct.pack("<6f", 1.0, 2.0, 3.0, 0.5, 0.0, -0.05),
            struct.pack("<6f", 2.0, 3.0, 4.0, 0.6, 127.0, 0.05),
        )
    )
    (sequence / "lidar" / "1000000.bin").write_bytes(lidar_payload)
    route = sequence / "route.html"
    route.write_text(
        "<script>L.polyline([[43.79,-79.47],[43.79,-79.469998]])</script>",
        encoding="utf-8",
    )
    region = root / "region.json"
    _write_json(
        region,
        {
            "schema_version": 1,
            "id": "synthetic",
            "coordinate_reference_system": "EPSG:4326",
            "description": "Synthetic test boundary.",
            "coordinates": [
                [-79.48, 43.78],
                [-79.46, 43.78],
                [-79.46, 43.80],
                [-79.48, 43.80],
                [-79.48, 43.78],
            ],
        },
    )
    gate = root / "gate.json"
    _write_json(
        gate,
        {
            "schema_version": 1,
            "adapter_version": "boreas-public-v1",
            "sequence_id": "boreas-synthetic",
            "route_sample_stride_rows": 1,
            "expected_lidar_frames": 1,
            "expected_calibrations": 3,
            "expected_lidar_pose_matches": 1,
            "minimum_clip_trajectory_rows": 3,
            "minimum_route_polyline_points": 2,
            "maximum_route_crosscheck_p95_m": 0.01,
            "maximum_route_crosscheck_m": 0.01,
            "maximum_wgs84_local_roundtrip_error_m": 0.001,
            "maximum_local_coordinate_magnitude_m": 10.0,
            "maximum_local_float32_quantization_m": 0.001,
            "maximum_time_conversion_error_ns": 0.5,
            "maximum_peak_rss_bytes": 2684354560,
            "require_road_region_containment": True,
            "require_nondecreasing_lidar_timestamps": True,
            "require_finite_lidar_fields": True,
        },
    )
    return sequence, route, region, gate


def test_adapter_qualifies_exact_times_layout_and_frames(tmp_path: Path) -> None:
    sequence, route, region, gate = _build_fixture(tmp_path)

    report = inspect_boreas(
        sequence, route_html=route, road_region_path=region, gate_path=gate
    )

    assert report["state"] == "ACCEPTED"
    assert report["accepted"] is True
    normalized = report["normalized"]
    assert normalized["trajectory"]["first_time_ns"] == 950000001
    assert normalized["trajectory"]["last_time_ns"] == 1049999999
    assert normalized["trajectory"]["position_frame"] == "enu_ref"
    assert normalized["trajectory"]["pose_target_frame"] == "enu_ref"
    assert normalized["trajectory"]["pose_source_frame"] == "applanix"
    assert normalized["trajectory"]["pose_convention"] == "T_target_source"
    frame = normalized["lidar"]["frames"][0]
    assert frame["scan_midpoint_ns"] == 1000000000
    assert frame["first_point_ns"] == 949999999
    assert frame["last_point_ns"] == 1050000001
    assert (
        frame["minimum_relative_time_bits"]
        == struct.unpack("<I", struct.pack("<f", -0.05))[0]
    )
    assert (
        frame["maximum_relative_time_bits"]
        == struct.unpack("<I", struct.pack("<f", 0.05))[0]
    )
    assert normalized["lidar_poses"]["selected_frame_matches"] == 1
    assert normalized["lidar_poses"]["target_frame"] == "enu_ref"
    assert normalized["lidar_poses"]["source_frame"] == "lidar"
    assert len(normalized["calibrations"]) == 3
    assert all(
        matrix["convention"] == "T_target_source"
        for matrix in normalized["calibrations"]
    )


def test_public_cli_writes_an_accepted_path_independent_report(tmp_path: Path) -> None:
    sequence, route, region, gate = _build_fixture(tmp_path)
    output = tmp_path / "report.json"

    result = CliRunner().invoke(
        app,
        [
            "inspect-boreas",
            str(sequence),
            "--route-html",
            str(route),
            "--road-region",
            str(region),
            "--gate",
            str(gate),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    report_text = output.read_text(encoding="utf-8")
    assert str(tmp_path) not in report_text
    assert json.loads(report_text)["state"] == "ACCEPTED"


def test_malformed_csv_names_the_source_without_echoing_payload(tmp_path: Path) -> None:
    sequence, route, region, gate = _build_fixture(tmp_path)
    gps_path = sequence / "applanix" / "gps_post_process.csv"
    lines = gps_path.read_text(encoding="utf-8").splitlines()
    fields = lines[1].split(",")
    fields[1] = "TOP_SECRET_PAYLOAD"
    lines[1] = ",".join(fields)
    gps_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError) as error:
        inspect_boreas(
            sequence, route_html=route, road_region_path=region, gate_path=gate
        )

    assert "applanix/gps_post_process.csv" in str(error.value)
    assert "TOP_SECRET_PAYLOAD" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_truncated_lidar_record_is_rejected_at_the_source_key(tmp_path: Path) -> None:
    sequence, route, region, gate = _build_fixture(tmp_path)
    lidar_path = sequence / "lidar" / "1000000.bin"
    lidar_path.write_bytes(lidar_path.read_bytes()[:-1])

    with pytest.raises(ValueError, match=r"lidar/1000000[.]bin"):
        inspect_boreas(
            sequence, route_html=route, road_region_path=region, gate_path=gate
        )
