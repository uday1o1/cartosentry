#include "cartosentry/ingest/boreas_inspector.hpp"

#include <catch2/catch_test_macros.hpp>

#include <cstdint>
#include <limits>
#include <string>

using cartosentry::ingest::BoreasFormatError;
using cartosentry::ingest::parse_decimal_seconds_to_nanoseconds;

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
