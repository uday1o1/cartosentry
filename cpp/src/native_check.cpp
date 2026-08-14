#include "cartosentry/core/native_check.hpp"

#include <sophus/se3.hpp>

#include <cmath>
#include <stdexcept>

namespace cartosentry::core {

double checked_translation_norm(const std::array<double, 3>& translation) {
  for (const double coordinate : translation) {
    if (!std::isfinite(coordinate)) {
      throw std::invalid_argument("translation coordinates must be finite");
    }
  }

  Sophus::SE3d::Tangent tangent = Sophus::SE3d::Tangent::Zero();
  tangent.template head<3>() =
      Eigen::Vector3d{translation[0], translation[1], translation[2]};
  const Sophus::SE3d transform = Sophus::SE3d::exp(tangent);
  const Sophus::SE3d::Tangent recovered = transform.log();
  if (!recovered.allFinite()) {
    throw std::runtime_error("Sophus returned a nonfinite SE(3) logarithm");
  }
  return transform.translation().norm();
}

bool native_self_check() {
  constexpr std::array<double, 3> translation{3.0, 4.0, 0.0};
  return std::abs(checked_translation_norm(translation) - 5.0) < 1e-12;
}

NativeBuildInfo native_build_info() {
#if defined(__clang__)
  const std::string compiler = "clang-" __clang_version__;
#elif defined(__GNUC__)
  const std::string compiler = "gcc-" __VERSION__;
#elif defined(_MSC_VER)
  const std::string compiler = "msvc-" + std::to_string(_MSC_VER);
#else
  const std::string compiler = "unknown";
#endif
  return NativeBuildInfo{
      .project_version = "0.1.0",
      .compiler = compiler,
      .se3_implementation = "Sophus-1.0.0+Eigen-3.4.0",
      .cxx_standard = 20,
  };
}

}  // namespace cartosentry::core
