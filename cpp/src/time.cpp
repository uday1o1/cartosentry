#include "cartosentry/contracts/time.hpp"

#include <charconv>
#include <limits>
#include <stdexcept>
#include <string>
#include <system_error>

namespace cartosentry::contracts {
namespace {

auto parse_unsigned(std::string_view lexeme) -> std::uint64_t {
  std::uint64_t value = 0U;
  const auto result =
      std::from_chars(lexeme.data(), lexeme.data() + lexeme.size(), value);
  if (lexeme.empty() || result.ec != std::errc{} ||
      result.ptr != lexeme.data() + lexeme.size()) {
    throw std::invalid_argument("plain decimal seconds required");
  }
  return value;
}

auto checked_nanosecond_magnitude(std::uint64_t seconds,
                                  std::uint64_t fraction_ns, bool negative)
    -> std::int64_t {
  constexpr std::uint64_t kNanosecondsPerSecond = 1'000'000'000U;
  const auto positive_limit =
      static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
  const auto magnitude_limit = negative ? positive_limit + 1U : positive_limit;
  if (seconds > magnitude_limit / kNanosecondsPerSecond ||
      (seconds == magnitude_limit / kNanosecondsPerSecond &&
       fraction_ns > magnitude_limit % kNanosecondsPerSecond)) {
    throw std::overflow_error("nanosecond timestamp is out of range");
  }
  const auto magnitude = seconds * kNanosecondsPerSecond + fraction_ns;
  if (!negative) {
    return static_cast<std::int64_t>(magnitude);
  }
  if (magnitude == positive_limit + 1U) {
    return std::numeric_limits<std::int64_t>::min();
  }
  return -static_cast<std::int64_t>(magnitude);
}

auto require_clock_id(const TimePoint& point) -> void {
  if (point.clock_id.empty()) {
    throw std::invalid_argument("time point clock_id must be nonempty");
  }
}

}  // namespace

auto parse_time_epoch(std::string_view value) -> TimeEpoch {
  if (value == "UNIX_UTC") {
    return TimeEpoch::unix_utc;
  }
  if (value == "GPS") {
    return TimeEpoch::gps;
  }
  if (value == "SENSOR_BOOT") {
    return TimeEpoch::sensor_boot;
  }
  if (value == "HOST_MONOTONIC") {
    return TimeEpoch::host_monotonic;
  }
  if (value == "UNKNOWN") {
    return TimeEpoch::unknown;
  }
  throw std::invalid_argument("unsupported time epoch");
}

auto parse_time_reference(std::string_view value) -> TimeReference {
  if (value == "EXPOSURE_START") {
    return TimeReference::exposure_start;
  }
  if (value == "EXPOSURE_MIDPOINT") {
    return TimeReference::exposure_midpoint;
  }
  if (value == "EXPOSURE_END") {
    return TimeReference::exposure_end;
  }
  if (value == "SCAN_START") {
    return TimeReference::scan_start;
  }
  if (value == "SCAN_MIDPOINT") {
    return TimeReference::scan_midpoint;
  }
  if (value == "SCAN_END") {
    return TimeReference::scan_end;
  }
  if (value == "SAMPLE") {
    return TimeReference::sample;
  }
  if (value == "PER_POINT") {
    return TimeReference::per_point;
  }
  if (value == "PER_AZIMUTH") {
    return TimeReference::per_azimuth;
  }
  if (value == "UNKNOWN") {
    return TimeReference::unknown;
  }
  throw std::invalid_argument("unsupported time reference");
}

auto to_string(TimeEpoch value) -> std::string_view {
  switch (value) {
    case TimeEpoch::unix_utc:
      return "UNIX_UTC";
    case TimeEpoch::gps:
      return "GPS";
    case TimeEpoch::sensor_boot:
      return "SENSOR_BOOT";
    case TimeEpoch::host_monotonic:
      return "HOST_MONOTONIC";
    case TimeEpoch::unknown:
      return "UNKNOWN";
  }
  throw std::invalid_argument("unsupported time epoch");
}

auto to_string(TimeReference value) -> std::string_view {
  switch (value) {
    case TimeReference::exposure_start:
      return "EXPOSURE_START";
    case TimeReference::exposure_midpoint:
      return "EXPOSURE_MIDPOINT";
    case TimeReference::exposure_end:
      return "EXPOSURE_END";
    case TimeReference::scan_start:
      return "SCAN_START";
    case TimeReference::scan_midpoint:
      return "SCAN_MIDPOINT";
    case TimeReference::scan_end:
      return "SCAN_END";
    case TimeReference::sample:
      return "SAMPLE";
    case TimeReference::per_point:
      return "PER_POINT";
    case TimeReference::per_azimuth:
      return "PER_AZIMUTH";
    case TimeReference::unknown:
      return "UNKNOWN";
  }
  throw std::invalid_argument("unsupported time reference");
}

auto decimal_seconds_to_nanoseconds(std::string_view lexeme) -> std::int64_t {
  if (lexeme.empty() || lexeme.size() > kMaximumDecimalSecondsBytes) {
    throw std::invalid_argument("plain decimal seconds required");
  }
  bool negative = false;
  std::size_t cursor = 0U;
  if (lexeme[cursor] == '+' || lexeme[cursor] == '-') {
    negative = lexeme[cursor] == '-';
    ++cursor;
  }
  const auto integer_begin = cursor;
  while (cursor < lexeme.size() && lexeme[cursor] >= '0' &&
         lexeme[cursor] <= '9') {
    ++cursor;
  }
  if (cursor == integer_begin) {
    throw std::invalid_argument("plain decimal seconds required");
  }
  auto seconds = parse_unsigned(lexeme.substr(integer_begin, cursor - integer_begin));
  std::uint64_t fraction_ns = 0U;
  std::size_t fraction_digits = 0U;
  bool round_up = false;
  if (cursor < lexeme.size()) {
    if (lexeme[cursor] != '.') {
      throw std::invalid_argument("plain decimal seconds required");
    }
    ++cursor;
    const auto fraction_begin = cursor;
    while (cursor < lexeme.size() && lexeme[cursor] >= '0' &&
           lexeme[cursor] <= '9') {
      if (fraction_digits < 9U) {
        fraction_ns = fraction_ns * 10U +
                      static_cast<std::uint64_t>(lexeme[cursor] - '0');
      } else if (fraction_digits == 9U) {
        round_up = lexeme[cursor] >= '5';
      }
      ++fraction_digits;
      ++cursor;
    }
    if (cursor == fraction_begin) {
      throw std::invalid_argument("fractional digits required");
    }
  }
  if (cursor != lexeme.size()) {
    throw std::invalid_argument("plain decimal seconds required");
  }
  for (; fraction_digits < 9U; ++fraction_digits) {
    fraction_ns *= 10U;
  }
  if (round_up) {
    ++fraction_ns;
  }
  if (fraction_ns == 1'000'000'000U) {
    fraction_ns = 0U;
    if (seconds == std::numeric_limits<std::uint64_t>::max()) {
      throw std::overflow_error("nanosecond timestamp is out of range");
    }
    ++seconds;
  }
  return checked_nanosecond_magnitude(seconds, fraction_ns, negative);
}

auto comparable(const TimePoint& left, const TimePoint& right) -> bool {
  require_clock_id(left);
  require_clock_id(right);
  return left.epoch == right.epoch && left.clock_id == right.clock_id;
}

auto checked_add(TimePoint point, Duration duration) -> TimePoint {
  require_clock_id(point);
  if ((duration.value_ns > 0 &&
       point.value_ns >
           std::numeric_limits<std::int64_t>::max() - duration.value_ns) ||
      (duration.value_ns < 0 &&
       point.value_ns <
           std::numeric_limits<std::int64_t>::min() - duration.value_ns)) {
    throw std::overflow_error("nanosecond timestamp addition is out of range");
  }
  point.value_ns += duration.value_ns;
  return point;
}

auto checked_difference(const TimePoint& end, const TimePoint& start)
    -> Duration {
  if (!comparable(end, start)) {
    throw std::invalid_argument("time points have incomparable epochs or clocks");
  }
  if ((start.value_ns < 0 &&
       end.value_ns >
           std::numeric_limits<std::int64_t>::max() + start.value_ns) ||
      (start.value_ns > 0 &&
       end.value_ns <
           std::numeric_limits<std::int64_t>::min() + start.value_ns)) {
    throw std::overflow_error("nanosecond duration is out of range");
  }
  return Duration{end.value_ns - start.value_ns};
}

auto validate_frame_interval(TimePoint capture_start, TimePoint capture_end)
    -> FrameInterval {
  const auto duration = checked_difference(capture_end, capture_start);
  if (duration.value_ns <= 0) {
    throw std::invalid_argument(
        "frame interval must be nonempty and half-open [start, end)");
  }
  return FrameInterval{std::move(capture_start), std::move(capture_end)};
}

}  // namespace cartosentry::contracts
