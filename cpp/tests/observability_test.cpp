#include "cartosentry/spikes/observability.hpp"

#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <filesystem>
#include <stdexcept>
#include <string>

namespace {

auto parameters() -> cartosentry::spikes::ObservabilityParameters {
  return cartosentry::spikes::ObservabilityParameters{
      100'000'000,        1.0,  128U, 200U, 30.0, 8.0,
      0.7853981633974483, 0.15, 1.0,  0.05,
  };
}

} // namespace

TEST_CASE("synthetic observability separates supported perturbations") {
  const auto results =
      cartosentry::spikes::run_synthetic_observability_suite(parameters());
  REQUIRE(results.size() == 5U);
  const auto turning =
      std::find_if(results.begin(), results.end(), [](const auto &result) {
        return result.scenario_id == "turning";
      });
  const auto moving =
      std::find_if(results.begin(), results.end(), [](const auto &result) {
        return result.scenario_id == "moving";
      });
  REQUIRE(turning != results.end());
  REQUIRE(moving != results.end());
  CHECK(turning->observability == "OBSERVABLE");
  CHECK(turning->point_time_shift_separated);
  CHECK(turning->trajectory_shift_separated);
  CHECK(moving->observability == "OBSERVABLE");
  CHECK(moving->point_time_shift_separated);
  CHECK(moving->trajectory_shift_separated);
}

TEST_CASE("unexcited and sparse controls do not produce passing evidence") {
  const auto results =
      cartosentry::spikes::run_synthetic_observability_suite(parameters());
  for (const auto &result : results) {
    if (result.scenario_id == "static" ||
        result.scenario_id == "sparse_structure") {
      CHECK(result.observability == "NOT_OBSERVABLE");
      CHECK_FALSE(result.point_time_shift_separated);
      CHECK_FALSE(result.trajectory_shift_separated);
    }
  }
}

TEST_CASE("tiny exact route matches independent brute force and validates") {
  const auto result = cartosentry::spikes::solve_tiny_required_route();
  CHECK(result.exact_cost == 8.0);
  CHECK(result.brute_force_cost == 8.0);
  CHECK(result.exact_matches_brute_force);
  CHECK(result.exact_route_valid);
  CHECK(result.exact_arc_path.size() == 5U);
  CHECK(result.explored_states > 0U);
}

TEST_CASE("invalid observability parameters fail before source access") {
  auto invalid = parameters();
  invalid.lidar_point_stride = 0U;
  CHECK_THROWS_AS(
      cartosentry::spikes::run_synthetic_observability_suite(invalid),
      std::invalid_argument);
  CHECK_THROWS_AS(cartosentry::spikes::run_observability_spike(
                      std::filesystem::path("absent-sequence"),
                      std::filesystem::path("absent-graph"), invalid),
                  std::invalid_argument);
}
