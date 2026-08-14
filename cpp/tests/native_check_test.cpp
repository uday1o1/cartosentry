#include "cartosentry/core/native_check.hpp"

#include <catch2/catch_test_macros.hpp>

#include <array>
#include <limits>
#include <stdexcept>

TEST_CASE("the selected SE3 implementation passes its checked round trip") {
  CHECK(cartosentry::core::native_self_check());
  CHECK(cartosentry::core::checked_translation_norm({3.0, 4.0, 0.0}) == 5.0);
}

TEST_CASE("nonfinite native input is rejected") {
  const std::array<double, 3> invalid{
      std::numeric_limits<double>::quiet_NaN(), 0.0, 0.0};
  CHECK_THROWS_AS(
      cartosentry::core::checked_translation_norm(invalid), std::invalid_argument);
}

TEST_CASE("native build metadata names the frozen SE3 implementation") {
  const auto info = cartosentry::core::native_build_info();
  CHECK(info.project_version == "0.1.0");
  CHECK(info.se3_implementation == "Sophus-1.0.0+Eigen-3.4.0");
  CHECK(info.cxx_standard == 20);
  CHECK_FALSE(info.compiler.empty());
}
