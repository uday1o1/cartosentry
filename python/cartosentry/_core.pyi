from typing import TypedDict

class NativeBuildInfo(TypedDict):
    project_version: str
    compiler: str
    se3_implementation: str
    cxx_standard: int

def native_self_check() -> bool: ...
def checked_translation_norm(translation: tuple[float, float, float]) -> float: ...
def native_build_info() -> NativeBuildInfo: ...
