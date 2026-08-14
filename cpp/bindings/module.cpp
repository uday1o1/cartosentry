#include "cartosentry/core/native_check.hpp"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

PYBIND11_MODULE(_core, module) {
  module.doc() = "Checked native CartoSentry foundation";
  module.def("native_self_check", &cartosentry::core::native_self_check);
  module.def("checked_translation_norm", &cartosentry::core::checked_translation_norm);
  module.def("native_build_info", [] {
    const auto info = cartosentry::core::native_build_info();
    py::dict result;
    result["project_version"] = info.project_version;
    result["compiler"] = info.compiler;
    result["se3_implementation"] = info.se3_implementation;
    result["cxx_standard"] = info.cxx_standard;
    return result;
  });
}
