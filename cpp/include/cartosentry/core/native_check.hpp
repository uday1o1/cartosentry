#pragma once

#include <array>
#include <string>

namespace cartosentry::core {

struct NativeBuildInfo {
  std::string project_version;
  std::string compiler;
  std::string se3_implementation;
  int cxx_standard;
};

[[nodiscard]] double checked_translation_norm(const std::array<double, 3>& translation);
[[nodiscard]] bool native_self_check();
[[nodiscard]] NativeBuildInfo native_build_info();

}  // namespace cartosentry::core
