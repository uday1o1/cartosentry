from typing import Any, TypedDict

class NativeBuildInfo(TypedDict):
    project_version: str
    compiler: str
    se3_implementation: str
    cxx_standard: int

def native_self_check() -> bool: ...
def checked_translation_norm(translation: tuple[float, float, float]) -> float: ...
def decimal_seconds_to_nanoseconds(decimal_lexeme: str) -> int: ...
def checked_time_difference_ns(
    end_value_ns: int,
    end_epoch: str,
    end_clock_id: str,
    start_value_ns: int,
    start_epoch: str,
    start_clock_id: str,
) -> int: ...
def checked_time_add_ns(
    value_ns: int, epoch: str, clock_id: str, duration_ns: int
) -> int: ...
def normalize_quaternion(
    quaternion_wxyz: tuple[float, float, float, float],
) -> dict[str, Any]: ...
def quaternion_from_rotation_matrix(
    row_major_values: tuple[
        float, float, float, float, float, float, float, float, float
    ],
) -> dict[str, Any]: ...
def compose_rigid_transforms(
    outer_target_frame: str,
    outer_source_frame: str,
    outer_translation_m: tuple[float, float, float],
    outer_quaternion_wxyz: tuple[float, float, float, float],
    inner_target_frame: str,
    inner_source_frame: str,
    inner_translation_m: tuple[float, float, float],
    inner_quaternion_wxyz: tuple[float, float, float, float],
) -> dict[str, Any]: ...
def invert_rigid_transform(
    target_frame: str,
    source_frame: str,
    translation_m: tuple[float, float, float],
    quaternion_wxyz: tuple[float, float, float, float],
) -> dict[str, Any]: ...
def interpolate_rigid_transform(
    target_frame: str,
    source_frame: str,
    begin_translation_m: tuple[float, float, float],
    begin_quaternion_wxyz: tuple[float, float, float, float],
    end_translation_m: tuple[float, float, float],
    end_quaternion_wxyz: tuple[float, float, float, float],
    fraction: float,
) -> dict[str, Any]: ...
def transform_point(
    target_frame: str,
    source_frame: str,
    translation_m: tuple[float, float, float],
    quaternion_wxyz: tuple[float, float, float, float],
    point_source: tuple[float, float, float],
) -> tuple[float, float, float]: ...
def wgs84_to_local(
    origin_latitude_deg: float,
    origin_longitude_deg: float,
    origin_altitude_m: float,
    latitude_deg: float,
    longitude_deg: float,
    altitude_m: float,
    local_frame: str,
) -> dict[str, Any]: ...
def local_to_wgs84(
    origin_latitude_deg: float,
    origin_longitude_deg: float,
    origin_altitude_m: float,
    local_frame: str,
    position_m: tuple[float, float, float],
) -> dict[str, Any]: ...
def native_build_info() -> NativeBuildInfo: ...
def inspect_boreas_sequence(
    sequence_root: str,
    route_html_path: str,
    road_region: tuple[float, float, float, float],
    route_sample_stride_rows: int,
) -> dict[str, Any]: ...
def run_synthetic_observability_suite(
    injected_point_time_shift_ns: int,
    injected_trajectory_shift_m: float,
    minimum_alignment_separation_m: float,
) -> list[dict[str, Any]]: ...
def solve_tiny_required_route() -> dict[str, Any]: ...
def run_observability_spike(
    sequence_root: str,
    road_graph_path: str,
    injected_point_time_shift_ns: int,
    injected_trajectory_shift_m: float,
    lidar_point_stride: int,
    map_trajectory_stride_rows: int,
    candidate_search_radius_m: float,
    confident_lateral_distance_m: float,
    confident_heading_error_rad: float,
    confident_score_separation: float,
    minimum_moving_speed_mps: float,
    minimum_alignment_separation_m: float,
) -> dict[str, Any]: ...

class BoreasFormatError(ValueError): ...
