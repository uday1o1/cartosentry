#include "cartosentry/contracts/geometry.hpp"
#include "cartosentry/contracts/time.hpp"

#include <GeographicLib/Geodesic.hpp>
#include <catch2/catch_approx.hpp>
#include <catch2/catch_test_macros.hpp>

#include <array>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>

namespace contracts = cartosentry::contracts;
using Catch::Approx;

namespace {

auto sample_time(std::int64_t value_ns, contracts::TimeEpoch epoch,
                 std::string clock_id) -> contracts::TimePoint {
  return contracts::TimePoint{value_ns, epoch, std::move(clock_id),
                              contracts::TimeReference::sample};
}

auto horizontal_distance_m(const contracts::GlobalCoordinate& left,
                           const contracts::GlobalCoordinate& right) -> double {
  double distance = 0.0;
  GeographicLib::Geodesic::WGS84().Inverse(
      left.latitude_deg, left.longitude_deg, right.latitude_deg,
      right.longitude_deg, distance);
  return distance;
}

}  // namespace

TEST_CASE("decimal time conversion is exact across signed int64 boundaries") {
  CHECK(contracts::decimal_seconds_to_nanoseconds("1630597311.041161") ==
        std::int64_t{1630597311041161000});
  CHECK(contracts::decimal_seconds_to_nanoseconds("1.0000000005") ==
        std::int64_t{1000000001});
  CHECK(contracts::decimal_seconds_to_nanoseconds("-0.0000000005") == -1);
  CHECK(contracts::decimal_seconds_to_nanoseconds("9223372036.854775807") ==
        std::numeric_limits<std::int64_t>::max());
  CHECK(contracts::decimal_seconds_to_nanoseconds("-9223372036.854775808") ==
        std::numeric_limits<std::int64_t>::min());
  CHECK_THROWS_AS(
      contracts::decimal_seconds_to_nanoseconds("9223372036.854775808"),
      std::overflow_error);
}

TEST_CASE("time differences reject incomparable epochs and clocks") {
  const auto unix_sensor = sample_time(20, contracts::TimeEpoch::unix_utc,
                                       "sensor-clock");
  const auto gps_sensor =
      sample_time(10, contracts::TimeEpoch::gps, "sensor-clock");
  const auto unix_host =
      sample_time(10, contracts::TimeEpoch::unix_utc, "host-clock");
  CHECK_THROWS_AS(contracts::checked_difference(unix_sensor, gps_sensor),
                  std::invalid_argument);
  CHECK_THROWS_AS(contracts::checked_difference(unix_sensor, unix_host),
                  std::invalid_argument);
}

TEST_CASE("duration arithmetic rejects signed int64 timestamp overflow") {
  const auto maximum = sample_time(std::numeric_limits<std::int64_t>::max(),
                                   contracts::TimeEpoch::unix_utc, "clock");
  CHECK(contracts::checked_add(
            sample_time(10, contracts::TimeEpoch::unix_utc, "clock"),
            contracts::Duration{1})
            .value_ns == 11);
  CHECK_THROWS_AS(contracts::checked_add(maximum, contracts::Duration{1}),
                  std::overflow_error);
}

TEST_CASE("frame intervals are nonempty half-open intervals in one clock") {
  const auto start =
      sample_time(100, contracts::TimeEpoch::unix_utc, "lidar-clock");
  const auto end =
      sample_time(150, contracts::TimeEpoch::unix_utc, "lidar-clock");
  const auto interval = contracts::validate_frame_interval(start, end);
  CHECK(contracts::checked_difference(interval.capture_end,
                                      interval.capture_start)
            .value_ns == 50);
  CHECK_THROWS_AS(contracts::validate_frame_interval(end, start),
                  std::invalid_argument);
  CHECK_THROWS_AS(contracts::validate_frame_interval(start, start),
                  std::invalid_argument);
}

TEST_CASE("T_world_rig normalizes recoverable rig-source rotation input") {
  const auto quaternion =
      contracts::make_unit_quaternion(1.0 + 5e-7, 0.0, 0.0, 0.0);
  CHECK(quaternion.w == Approx(1.0));
  CHECK(quaternion.pre_normalization_norm_deviation == Approx(5e-7));
  CHECK_THROWS_AS(
      contracts::make_unit_quaternion(1.0 + 2e-6, 0.0, 0.0, 0.0),
      std::invalid_argument);
  CHECK_THROWS_AS(contracts::make_unit_quaternion(0.0, 0.0, 0.0, 0.0),
                  std::invalid_argument);
  const auto positive_half_turn =
      contracts::make_unit_quaternion(0.0, 0.0, 0.0, 1.0);
  const auto negative_half_turn =
      contracts::make_unit_quaternion(0.0, 0.0, 0.0, -1.0);
  CHECK(positive_half_turn.w == negative_half_turn.w);
  CHECK(positive_half_turn.x == negative_half_turn.x);
  CHECK(positive_half_turn.y == negative_half_turn.y);
  CHECK(positive_half_turn.z == negative_half_turn.z);
}

TEST_CASE("T_world_rig rejects reflection from rig source to world target") {
  constexpr std::array<double, 9> reflection{
      -1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0};
  CHECK_THROWS_AS(contracts::quaternion_from_rotation_matrix(reflection),
                  std::invalid_argument);
}

TEST_CASE("T_world_rig rejects nonorthonormal rig-source rotation matrices") {
  constexpr std::array<double, 9> scaled_axis{
      1.0 + 2e-9, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0};
  CHECK_THROWS_AS(contracts::quaternion_from_rotation_matrix(scaled_axis),
                  std::invalid_argument);
}

TEST_CASE("T_world_rig composed with T_rig_lidar maps lidar source to world target") {
  const auto identity = contracts::make_unit_quaternion(1.0, 0.0, 0.0, 0.0);
  const auto world_from_rig = contracts::make_rigid_transform(
      "world", "rig", {10.0, 0.0, 0.0}, identity);
  const auto rig_from_lidar = contracts::make_rigid_transform(
      "rig", "lidar", {0.0, 2.0, 0.0}, identity);
  const auto world_from_lidar =
      contracts::compose(world_from_rig, rig_from_lidar);
  CHECK(world_from_lidar.target_frame == "world");
  CHECK(world_from_lidar.source_frame == "lidar");
  const auto point_world =
      contracts::transform_point(world_from_lidar, {1.0, 1.0, 1.0});
  CHECK(point_world == std::array<double, 3>{11.0, 3.0, 1.0});
  CHECK_THROWS_AS(contracts::compose(rig_from_lidar, world_from_rig),
                  std::invalid_argument);
}

TEST_CASE("T_rig_world inverse returns world-source points to rig target") {
  const double half_sqrt = std::sqrt(0.5);
  const auto world_from_rig = contracts::make_rigid_transform(
      "world", "rig", {6'378'137.0, -4'000'000.0, 2'000'000.0},
      contracts::make_unit_quaternion(half_sqrt, 0.0, 0.0, half_sqrt));
  const std::array<double, 3> point_rig{1000.0, -2000.0, 3000.0};
  const auto point_world = contracts::transform_point(world_from_rig, point_rig);
  const auto rig_from_world = contracts::inverse(world_from_rig);
  const auto recovered = contracts::transform_point(rig_from_world, point_world);
  double maximum_error = 0.0;
  for (std::size_t index = 0; index < recovered.size(); ++index) {
    maximum_error =
        std::max(maximum_error, std::abs(recovered[index] - point_rig[index]));
  }
  CHECK(maximum_error <= 1e-9);
  CHECK(rig_from_world.target_frame == "rig");
  CHECK(rig_from_world.source_frame == "world");
}

TEST_CASE("T_world_rig interpolation keeps rig source and world target names") {
  const auto identity = contracts::make_unit_quaternion(1.0, 0.0, 0.0, 0.0);
  const auto half_turn = contracts::make_unit_quaternion(0.0, 0.0, 0.0, 1.0);
  const auto begin = contracts::make_rigid_transform(
      "world", "rig", {0.0, 0.0, 0.0}, identity);
  const auto end = contracts::make_rigid_transform(
      "world", "rig", {10.0, 0.0, 0.0}, half_turn);
  const auto midpoint = contracts::interpolate(begin, end, 0.5);
  const auto transformed =
      contracts::transform_point(midpoint, {1.0, 0.0, 0.0});
  CHECK(transformed[0] == Approx(5.0).margin(1e-12));
  CHECK(transformed[1] == Approx(1.0).margin(1e-12));
  CHECK(transformed[2] == Approx(0.0).margin(1e-12));
  CHECK_THROWS_AS(contracts::interpolate(begin, end, -0.01),
                  std::invalid_argument);
  CHECK_THROWS_AS(contracts::interpolate(begin, end, 1.01),
                  std::invalid_argument);
}

TEST_CASE("WGS84 global to local_world target round trip stays below one millimeter") {
  const auto origin = contracts::make_global_coordinate(
      43.784000, -79.472000, 183.0,
      contracts::VerticalDatum::wgs84_ellipsoid);
  const std::array<contracts::GlobalCoordinate, 4> points{
      contracts::make_global_coordinate(
          43.784000, -79.472000, 183.0,
          contracts::VerticalDatum::wgs84_ellipsoid),
      contracts::make_global_coordinate(
          43.794000, -79.462000, 210.0,
          contracts::VerticalDatum::wgs84_ellipsoid),
      contracts::make_global_coordinate(
          43.774000, -79.482000, 150.0,
          contracts::VerticalDatum::wgs84_ellipsoid),
      contracts::make_global_coordinate(
          43.900000, -79.300000, 250.0,
          contracts::VerticalDatum::wgs84_ellipsoid),
  };
  for (const auto& point : points) {
    const auto local =
        contracts::global_to_local(origin, point, "local_world");
    const auto recovered = contracts::local_to_global(origin, local);
    CHECK(horizontal_distance_m(point, recovered) <= 0.001);
  }
}

TEST_CASE("WGS84 local_world conversion rejects unknown source altitude datum") {
  const auto origin = contracts::make_global_coordinate(
      43.784, -79.472, 183.0,
      contracts::VerticalDatum::wgs84_ellipsoid);
  const auto unknown = contracts::make_global_coordinate(
      43.785, -79.471, 184.0, contracts::VerticalDatum::unknown);
  CHECK_THROWS_AS(contracts::global_to_local(origin, unknown, "local_world"),
                  std::invalid_argument);
}
