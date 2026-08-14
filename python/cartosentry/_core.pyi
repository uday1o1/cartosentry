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

class BoreasFormatError(ValueError): ...
