"""Checked Boreas public-format adapter and milestone qualification gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cartosentry import _core


class BoreasGate(BaseModel):
    """Frozen, strict thresholds for the M0.4 source-format spike."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1)
    adapter_version: str = Field(min_length=1)
    sequence_id: str = Field(min_length=1)
    route_sample_stride_rows: int = Field(gt=0)
    expected_lidar_frames: int = Field(gt=0)
    expected_calibrations: int = Field(gt=0)
    expected_lidar_pose_matches: int = Field(gt=0)
    minimum_clip_trajectory_rows: int = Field(gt=0)
    minimum_route_polyline_points: int = Field(ge=2)
    maximum_route_crosscheck_p95_m: float = Field(ge=0.0)
    maximum_route_crosscheck_m: float = Field(ge=0.0)
    maximum_wgs84_local_roundtrip_error_m: float = Field(ge=0.0)
    maximum_local_coordinate_magnitude_m: float = Field(gt=0.0)
    maximum_local_float32_quantization_m: float = Field(ge=0.0)
    maximum_time_conversion_error_ns: float = Field(ge=0.0, le=0.5)
    maximum_peak_rss_bytes: int = Field(gt=0)
    require_road_region_containment: bool
    require_nondecreasing_lidar_timestamps: bool
    require_finite_lidar_fields: bool

    @field_validator("maximum_route_crosscheck_m")
    @classmethod
    def maximum_must_cover_p95(cls, value: float, info: Any) -> float:
        """Keep the maximum residual bound no tighter than its p95 bound."""

        p95 = info.data.get("maximum_route_crosscheck_p95_m")
        if isinstance(p95, float) and value < p95:
            raise ValueError("maximum route residual must cover p95 residual")
        return value


class RoadRegion(BaseModel):
    """Minimal checked representation of the frozen acquisition polygon."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1)
    id: str = Field(min_length=1)
    coordinate_reference_system: str
    description: str
    coordinates: list[tuple[float, float]] = Field(min_length=4)

    @field_validator("coordinate_reference_system")
    @classmethod
    def require_wgs84(cls, value: str) -> str:
        if value != "EPSG:4326":
            raise ValueError("road region must use EPSG:4326")
        return value

    @field_validator("coordinates")
    @classmethod
    def require_closed_valid_polygon(
        cls, value: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        if value[0] != value[-1]:
            raise ValueError("road region polygon must be closed")
        if any(
            not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0) for lon, lat in value
        ):
            raise ValueError("road region coordinate is outside WGS84")
        return value

    def bounds(self) -> tuple[float, float, float, float]:
        latitudes = [latitude for _, latitude in self.coordinates]
        longitudes = [longitude for longitude, _ in self.coordinates]
        return (
            min(latitudes),
            max(latitudes),
            min(longitudes),
            max(longitudes),
        )


def _load_mapping(path: Path, source_name: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"{source_name} is unavailable or malformed") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"{source_name} must contain a mapping")
    return cast(dict[str, Any], loaded)


def _normalize_floats(value: Any) -> Any:
    if isinstance(value, float):
        rounded = round(value, 12)
        return 0.0 if rounded == 0.0 else rounded
    if isinstance(value, dict):
        return {key: _normalize_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_floats(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_floats(item) for item in value]
    return value


def _check(name: str, observed: Any, required: str, passed: bool) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "observed": observed,
        "required": required,
    }


def _evaluate_gate(
    normalized: dict[str, Any], runtime: dict[str, Any], gate: BoreasGate
) -> list[dict[str, Any]]:
    trajectory = cast(dict[str, Any], normalized["trajectory"])
    lidar = cast(dict[str, Any], normalized["lidar"])
    frames = cast(list[dict[str, Any]], lidar["frames"])
    lidar_poses = cast(dict[str, Any], normalized["lidar_poses"])
    checks = [
        _check(
            "adapter-version",
            normalized["adapter_version"],
            f"equal to {gate.adapter_version}",
            normalized["adapter_version"] == gate.adapter_version,
        ),
        _check(
            "sequence-id",
            normalized["sequence_id"],
            f"equal to {gate.sequence_id}",
            normalized["sequence_id"] == gate.sequence_id,
        ),
        _check(
            "lidar-frame-count",
            len(frames),
            f"equal to {gate.expected_lidar_frames}",
            len(frames) == gate.expected_lidar_frames,
        ),
        _check(
            "calibration-count",
            len(cast(list[Any], normalized["calibrations"])),
            f"equal to {gate.expected_calibrations}",
            len(cast(list[Any], normalized["calibrations"]))
            == gate.expected_calibrations,
        ),
        _check(
            "lidar-pose-matches",
            lidar_poses["selected_frame_matches"],
            f"equal to {gate.expected_lidar_pose_matches}",
            lidar_poses["selected_frame_matches"] == gate.expected_lidar_pose_matches,
        ),
        _check(
            "clip-trajectory-rows",
            trajectory["clip_row_count"],
            f"at least {gate.minimum_clip_trajectory_rows}",
            trajectory["clip_row_count"] >= gate.minimum_clip_trajectory_rows,
        ),
        _check(
            "route-polyline-points",
            trajectory["route_polyline_point_count"],
            f"at least {gate.minimum_route_polyline_points}",
            trajectory["route_polyline_point_count"]
            >= gate.minimum_route_polyline_points,
        ),
        _check(
            "route-crosscheck-p95-m",
            trajectory["route_crosscheck_p95_m"],
            f"at most {gate.maximum_route_crosscheck_p95_m}",
            trajectory["route_crosscheck_p95_m"] <= gate.maximum_route_crosscheck_p95_m,
        ),
        _check(
            "route-crosscheck-maximum-m",
            trajectory["route_crosscheck_maximum_m"],
            f"at most {gate.maximum_route_crosscheck_m}",
            trajectory["route_crosscheck_maximum_m"] <= gate.maximum_route_crosscheck_m,
        ),
        _check(
            "wgs84-local-roundtrip-m",
            trajectory["maximum_wgs84_local_roundtrip_error_m"],
            f"at most {gate.maximum_wgs84_local_roundtrip_error_m}",
            trajectory["maximum_wgs84_local_roundtrip_error_m"]
            <= gate.maximum_wgs84_local_roundtrip_error_m,
        ),
        _check(
            "local-coordinate-magnitude-m",
            trajectory["maximum_local_coordinate_magnitude_m"],
            f"at most {gate.maximum_local_coordinate_magnitude_m}",
            trajectory["maximum_local_coordinate_magnitude_m"]
            <= gate.maximum_local_coordinate_magnitude_m,
        ),
        _check(
            "local-float32-quantization-m",
            trajectory["maximum_local_float32_quantization_m"],
            f"at most {gate.maximum_local_float32_quantization_m}",
            trajectory["maximum_local_float32_quantization_m"]
            <= gate.maximum_local_float32_quantization_m,
        ),
        _check(
            "point-time-conversion-error-ns",
            lidar["maximum_time_conversion_error_ns"],
            f"at most {gate.maximum_time_conversion_error_ns}",
            lidar["maximum_time_conversion_error_ns"]
            <= gate.maximum_time_conversion_error_ns,
        ),
        _check(
            "peak-rss-bytes",
            runtime["peak_rss_bytes"],
            f"at most {gate.maximum_peak_rss_bytes}",
            runtime["peak_rss_bytes"] <= gate.maximum_peak_rss_bytes,
        ),
    ]
    if gate.require_road_region_containment:
        checks.append(
            _check(
                "road-region-containment",
                trajectory["road_region_contains_trajectory"],
                "true",
                trajectory["road_region_contains_trajectory"] is True,
            )
        )
    if gate.require_nondecreasing_lidar_timestamps:
        monotonic = all(frame["timestamps_nondecreasing"] for frame in frames)
        checks.append(
            _check("lidar-time-order", monotonic, "true for every frame", monotonic)
        )
    if gate.require_finite_lidar_fields:
        finite = all(frame["required_fields_finite"] for frame in frames)
        checks.append(
            _check("lidar-field-finiteness", finite, "true for every frame", finite)
        )
    return checks


def inspect_boreas(
    sequence_root: Path,
    *,
    route_html: Path,
    road_region_path: Path,
    gate_path: Path,
) -> dict[str, Any]:
    """Inspect one Boreas sequence and evaluate the frozen adapter gate."""

    gate = BoreasGate.model_validate(_load_mapping(gate_path, "adapter gate"))
    road_region = RoadRegion.model_validate(
        _load_mapping(road_region_path, "road region")
    )
    raw = _core.inspect_boreas_sequence(
        str(sequence_root),
        str(route_html),
        road_region.bounds(),
        gate.route_sample_stride_rows,
    )
    runtime = {
        "unique_input_bytes": raw.pop("unique_input_bytes"),
        "peak_rss_bytes": raw.pop("peak_rss_bytes"),
        "elapsed_seconds": raw.pop("elapsed_seconds"),
    }
    elapsed_seconds = cast(float, runtime["elapsed_seconds"])
    runtime["throughput_bytes_per_second"] = (
        cast(int, runtime["unique_input_bytes"]) / elapsed_seconds
        if elapsed_seconds > 0.0
        else 0.0
    )
    normalized = cast(dict[str, Any], _normalize_floats(raw))
    runtime = cast(dict[str, Any], _normalize_floats(runtime))
    serialized = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    checks = _evaluate_gate(normalized, runtime, gate)
    accepted = all(cast(bool, check["passed"]) for check in checks)
    return {
        "schema_version": "cartosentry.boreas-contract-report.v1",
        "state": "ACCEPTED" if accepted else "FAILED",
        "accepted": accepted,
        "normalized_sha256": hashlib.sha256(serialized).hexdigest(),
        "normalized": normalized,
        "runtime": runtime,
        "gate_checks": checks,
    }
