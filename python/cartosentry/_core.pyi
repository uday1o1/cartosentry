from typing import Any, TypedDict

class NativeBuildInfo(TypedDict):
    project_version: str
    compiler: str
    se3_implementation: str
    cxx_standard: int

def native_self_check() -> bool: ...
def checked_translation_norm(translation: tuple[float, float, float]) -> float: ...
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
