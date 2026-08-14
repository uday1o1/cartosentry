#pragma once

#include <cstdint>
#include <string>
#include <string_view>

namespace cartosentry::contracts {

enum class TimeEpoch {
  unix_utc,
  gps,
  sensor_boot,
  host_monotonic,
  unknown,
};

enum class TimeReference {
  exposure_start,
  exposure_midpoint,
  exposure_end,
  scan_start,
  scan_midpoint,
  scan_end,
  sample,
  per_point,
  per_azimuth,
  unknown,
};

struct TimePoint {
  std::int64_t value_ns{};
  TimeEpoch epoch{TimeEpoch::unknown};
  std::string clock_id;
  TimeReference reference{TimeReference::unknown};
};

struct Duration {
  std::int64_t value_ns{};
};

struct FrameInterval {
  TimePoint capture_start;
  TimePoint capture_end;
};

[[nodiscard]] auto parse_time_epoch(std::string_view value) -> TimeEpoch;
[[nodiscard]] auto parse_time_reference(std::string_view value) -> TimeReference;
[[nodiscard]] auto to_string(TimeEpoch value) -> std::string_view;
[[nodiscard]] auto to_string(TimeReference value) -> std::string_view;

[[nodiscard]] auto decimal_seconds_to_nanoseconds(std::string_view lexeme)
    -> std::int64_t;
[[nodiscard]] auto checked_add(TimePoint point, Duration duration) -> TimePoint;
[[nodiscard]] auto checked_difference(const TimePoint& end,
                                      const TimePoint& start) -> Duration;
[[nodiscard]] auto validate_frame_interval(TimePoint capture_start,
                                           TimePoint capture_end)
    -> FrameInterval;
[[nodiscard]] auto comparable(const TimePoint& left, const TimePoint& right)
    -> bool;

}  // namespace cartosentry::contracts
