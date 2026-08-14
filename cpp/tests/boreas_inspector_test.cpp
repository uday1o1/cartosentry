#include "cartosentry/ingest/boreas_inspector.hpp"
#include "cartosentry/contracts/time.hpp"

#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <span>
#include <string>
#include <vector>

using cartosentry::ingest::BoreasFormatError;
using cartosentry::ingest::parse_boreas_lidar_frame;
using cartosentry::ingest::parse_decimal_seconds_to_nanoseconds;

namespace {

auto one_lidar_record() -> std::array<std::byte, 24> {
  const std::array values{1.0F, 2.0F, 3.0F, 0.5F, 1.0F, 0.0F};
  std::array<std::byte, 24> record{};
  std::memcpy(record.data(), values.data(), record.size());
  return record;
}

}  // namespace

TEST_CASE("decimal Unix seconds preserve the source lexeme exactly") {
  CHECK(parse_decimal_seconds_to_nanoseconds(
            "1630597311.041161", "gps.csv", 2U, "GPSTime") ==
        std::int64_t{1630597311041161000});
  CHECK(parse_decimal_seconds_to_nanoseconds("0.000000001", "gps.csv", 2U,
                                             "GPSTime") == 1);
  CHECK(parse_decimal_seconds_to_nanoseconds("-0.000000001", "gps.csv", 2U,
                                             "GPSTime") == -1);
}

TEST_CASE("subnanosecond decimal timestamps round half away from zero") {
  CHECK(parse_decimal_seconds_to_nanoseconds("1.0000000005", "gps.csv", 2U,
                                             "GPSTime") == 1000000001);
  CHECK(parse_decimal_seconds_to_nanoseconds("-1.0000000005", "gps.csv", 2U,
                                             "GPSTime") == -1000000001);
  CHECK(parse_decimal_seconds_to_nanoseconds("0.9999999995", "gps.csv", 2U,
                                             "GPSTime") == 1000000000);
  CHECK(parse_decimal_seconds_to_nanoseconds("-0.0000000005", "gps.csv", 2U,
                                             "GPSTime") == -1);
}

TEST_CASE("the full signed int64 nanosecond domain is supported") {
  CHECK(parse_decimal_seconds_to_nanoseconds(
            "9223372036.854775807", "gps.csv", 2U, "GPSTime") ==
        std::numeric_limits<std::int64_t>::max());
  CHECK(parse_decimal_seconds_to_nanoseconds(
            "-9223372036.854775808", "gps.csv", 2U, "GPSTime") ==
        std::numeric_limits<std::int64_t>::min());
  CHECK_THROWS_AS(parse_decimal_seconds_to_nanoseconds(
                      "9223372036.854775808", "gps.csv", 2U, "GPSTime"),
                  BoreasFormatError);
  CHECK_THROWS_AS(parse_decimal_seconds_to_nanoseconds(
                      std::string(
                          cartosentry::contracts::kMaximumDecimalSecondsBytes +
                              1U,
                          '1'),
                      "gps.csv", 2U, "GPSTime"),
                  BoreasFormatError);
}

TEST_CASE("format errors identify fields without reproducing raw values") {
  constexpr auto secret = "TOP_SECRET_PAYLOAD";
  try {
    static_cast<void>(parse_decimal_seconds_to_nanoseconds(
        secret, "applanix/gps_post_process.csv", 9U, "GPSTime"));
    FAIL("invalid timestamp unexpectedly parsed");
  } catch (const BoreasFormatError& error) {
    const std::string message(error.what());
    CHECK(message.find("applanix/gps_post_process.csv") != std::string::npos);
    CHECK(message.find("row 9") != std::string::npos);
    CHECK(message.find("GPSTime") != std::string::npos);
    CHECK(message.find(secret) == std::string::npos);
  }
}

TEST_CASE("Boreas lidar record parser rejects unsafe binary boundaries") {
  const auto valid = one_lidar_record();
  const auto parsed = parse_boreas_lidar_frame(valid, "1630597359058594");
  CHECK(parsed.frame.point_count == 1U);
  CHECK(parsed.frame.minimum_laser_id == 1U);
  CHECK(parsed.frame.maximum_laser_id == 1U);

  CHECK_THROWS_AS(parse_boreas_lidar_frame(
                      std::span(valid).first(valid.size() - 1U),
                      "1630597359058594"),
                  BoreasFormatError);

  auto endian_swapped = valid;
  for (std::size_t field = 0U; field < 6U; ++field) {
    std::reverse(endian_swapped.begin() + static_cast<std::ptrdiff_t>(field * 4U),
                 endian_swapped.begin() +
                     static_cast<std::ptrdiff_t>((field + 1U) * 4U));
  }
  CHECK_THROWS_AS(parse_boreas_lidar_frame(endian_swapped,
                                           "1630597359058594"),
                  BoreasFormatError);

  constexpr auto oversized_bytes =
      ((cartosentry::ingest::kMaximumBoreasLidarFrameBytes /
        cartosentry::ingest::kBoreasLidarRecordBytes) +
       1U) *
      cartosentry::ingest::kBoreasLidarRecordBytes;
  const std::vector<std::byte> oversized(oversized_bytes);
  CHECK_THROWS_AS(parse_boreas_lidar_frame(oversized, "1630597359058594"),
                  BoreasFormatError);
}
