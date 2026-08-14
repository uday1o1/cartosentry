from __future__ import annotations

import math

import cartosentry
import pytest


def test_python_calls_checked_cpp_function() -> None:
    assert cartosentry.native_self_check()
    assert cartosentry.checked_translation_norm((3.0, 4.0, 0.0)) == 5.0


def test_python_binding_rejects_nonfinite_input() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        cartosentry.checked_translation_norm((math.nan, 0.0, 0.0))


def test_native_build_info_names_frozen_foundation() -> None:
    info = cartosentry.native_build_info()
    assert info["project_version"] == cartosentry.__version__
    assert info["se3_implementation"] == "Sophus-1.0.0+Eigen-3.4.0"
    assert info["cxx_standard"] == 20
    assert info["compiler"]
