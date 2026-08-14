#include "cartosentry/ingest/boreas_inspector.hpp"
#include "cartosentry/contracts/time.hpp"

#include <GeographicLib/Geocentric.hpp>
#include <GeographicLib/Geodesic.hpp>
#include <GeographicLib/LocalCartesian.hpp>

#include <Eigen/Core>
#include <Eigen/LU>

#include <algorithm>
#include <array>
#include <bit>
#include <charconv>
#include <chrono>
#include <cctype>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <limits>
#include <memory>
#include <numbers>
#include <span>
#include <sstream>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

#include <sys/resource.h>

namespace cartosentry::ingest {
namespace {

constexpr std::string_view kGpsSource = "applanix/gps_post_process.csv";
constexpr std::string_view kLidarPoseSource = "applanix/lidar_poses.csv";
constexpr std::string_view kLidarSource = "lidar";
constexpr std::string_view kRouteSource = "route.html";
constexpr double kRadiansToDegrees = 180.0 / std::numbers::pi_v<double>;
constexpr std::uint64_t kMaximumGpsBytes = 256U * 1024U * 1024U;
constexpr std::uint64_t kMaximumLidarPoseBytes = 64U * 1024U * 1024U;
constexpr std::uint64_t kMaximumCalibrationBytes = 64U * 1024U;
constexpr std::uint64_t kMaximumRouteBytes = 16U * 1024U * 1024U;
constexpr std::size_t kMaximumTextLineBytes = 16U * 1024U;

struct GeographicPoint {
  double latitude_deg{};
  double longitude_deg{};
  double altitude_m{};
};

struct LocalPoint {
  double x_m{};
  double y_m{};
};

struct LidarFrameParseState {
  LidarFrameSummary frame;
  std::string source_key;
  std::int64_t previous_time{std::numeric_limits<std::int64_t>::min()};
  std::uint64_t point_index{};
  double maximum_time_conversion_error_ns{};
};

auto format_error(std::string_view source_key, std::size_t row_number,
                  std::string_view field, std::string_view reason)
    -> BoreasFormatError {
  std::ostringstream message;
  message << "Invalid Boreas input at " << source_key;
  if (row_number != 0U) {
    message << " row " << row_number;
  }
  if (!field.empty()) {
    message << " field " << field;
  }
  message << ": " << reason;
  return BoreasFormatError(message.str());
}

auto checked_file_size(const std::filesystem::path& path,
                       std::string_view source_key) -> std::uint64_t {
  std::error_code error;
  const auto size = std::filesystem::file_size(path, error);
  if (error) {
    throw format_error(source_key, 0U, "", "file is unavailable");
  }
  return size;
}

auto open_text(const std::filesystem::path& path, std::string_view source_key)
    -> std::ifstream {
  std::uint64_t maximum_bytes = 0U;
  if (source_key == kGpsSource) {
    maximum_bytes = kMaximumGpsBytes;
  } else if (source_key == kLidarPoseSource) {
    maximum_bytes = kMaximumLidarPoseBytes;
  } else if (source_key.starts_with("calib/")) {
    maximum_bytes = kMaximumCalibrationBytes;
  } else if (source_key == kRouteSource) {
    maximum_bytes = kMaximumRouteBytes;
  } else {
    throw format_error(source_key, 0U, "", "text source is not registered");
  }
  const auto source_bytes = checked_file_size(path, source_key);
  if (source_bytes == 0U || source_bytes > maximum_bytes) {
    throw format_error(source_key, 0U, "",
                       "file size is outside the supported range");
  }
  std::ifstream input(path);
  if (!input) {
    throw format_error(source_key, 0U, "", "file is unavailable");
  }
  return input;
}

auto read_bounded_line(std::ifstream& input, std::string& line,
                       std::string_view source_key, std::size_t row_number)
    -> bool {
  line.clear();
  char character = '\0';
  while (input.get(character)) {
    if (character == '\n') {
      return true;
    }
    if (line.size() >= kMaximumTextLineBytes) {
      throw format_error(source_key, row_number, "row",
                         "line exceeds the supported byte limit");
    }
    line.push_back(character);
  }
  return !line.empty();
}

auto split_csv(std::string_view line) -> std::vector<std::string_view> {
  std::vector<std::string_view> fields;
  std::size_t begin = 0U;
  while (begin <= line.size()) {
    const auto comma = line.find(',', begin);
    if (comma == std::string_view::npos) {
      fields.push_back(line.substr(begin));
      break;
    }
    fields.push_back(line.substr(begin, comma - begin));
    begin = comma + 1U;
  }
  if (!fields.empty() && !fields.back().empty() && fields.back().back() == '\r') {
    fields.back().remove_suffix(1U);
  }
  return fields;
}

auto require_header(std::ifstream& input, std::string_view expected,
                    std::string_view source_key) -> void {
  std::string header;
  if (!read_bounded_line(input, header, source_key, 1U)) {
    throw format_error(source_key, 1U, "header", "header is missing");
  }
  if (!header.empty() && header.back() == '\r') {
    header.pop_back();
  }
  if (header != expected) {
    throw format_error(source_key, 1U, "header", "schema does not match");
  }
}

auto parse_double(std::string_view lexeme, std::string_view source_key,
                  std::size_t row_number, std::string_view field) -> double {
  double value = 0.0;
  const auto result =
      std::from_chars(lexeme.data(), lexeme.data() + lexeme.size(), value);
  if (result.ec != std::errc{} || result.ptr != lexeme.data() + lexeme.size() ||
      !std::isfinite(value)) {
    throw format_error(source_key, row_number, field,
                       "finite decimal value required");
  }
  return value;
}

auto parse_unsigned(std::string_view lexeme, std::string_view source_key,
                    std::size_t row_number, std::string_view field)
    -> std::uint64_t {
  std::uint64_t value = 0U;
  const auto result =
      std::from_chars(lexeme.data(), lexeme.data() + lexeme.size(), value);
  if (lexeme.empty() || result.ec != std::errc{} ||
      result.ptr != lexeme.data() + lexeme.size()) {
    throw format_error(source_key, row_number, field,
                       "unsigned integer value required");
  }
  return value;
}

auto microseconds_to_nanoseconds(std::uint64_t microseconds,
                                 std::string_view source_key,
                                 std::size_t row_number,
                                 std::string_view field) -> std::int64_t {
  constexpr auto kScale = std::uint64_t{1000U};
  const auto limit = static_cast<std::uint64_t>(
      std::numeric_limits<std::int64_t>::max());
  if (microseconds > limit / kScale) {
    throw format_error(source_key, row_number, field,
                       "nanosecond timestamp is out of range");
  }
  return static_cast<std::int64_t>(microseconds * kScale);
}

auto checked_add(std::int64_t left, std::int64_t right,
                 std::string_view source_key, std::size_t row_number,
                 std::string_view field) -> std::int64_t {
  if ((right > 0 && left > std::numeric_limits<std::int64_t>::max() - right) ||
      (right < 0 && left < std::numeric_limits<std::int64_t>::min() - right)) {
    throw format_error(source_key, row_number, field,
                       "nanosecond timestamp is out of range");
  }
  return left + right;
}

auto checked_add_unsigned(std::uint64_t left, std::uint64_t right,
                          std::string_view source_key,
                          std::string_view field) -> std::uint64_t {
  if (right > std::numeric_limits<std::uint64_t>::max() - left) {
    throw format_error(source_key, 0U, field, "unsigned byte count overflow");
  }
  return left + right;
}

auto initialize_lidar_frame(std::string_view frame_id,
                            std::uint64_t source_bytes)
    -> LidarFrameParseState {
  const auto source_key =
      std::string(kLidarSource) + "/" + std::string(frame_id) + ".bin";
  if (source_bytes == 0U ||
      source_bytes > kMaximumBoreasLidarFrameBytes ||
      source_bytes % kBoreasLidarRecordBytes != 0U) {
    throw format_error(
        source_key, 0U, "record_layout",
        "byte count is outside the supported nonzero 24-byte record range");
  }
  const auto midpoint_us =
      parse_unsigned(frame_id, source_key, 0U, "filename_timestamp");
  const auto midpoint_ns = microseconds_to_nanoseconds(
      midpoint_us, source_key, 0U, "filename_timestamp");
  LidarFrameParseState state;
  state.source_key = source_key;
  state.frame.frame_id = frame_id;
  state.frame.source_bytes = source_bytes;
  state.frame.point_count = source_bytes / kBoreasLidarRecordBytes;
  state.frame.scan_midpoint_ns = midpoint_ns;
  state.frame.first_point_ns = std::numeric_limits<std::int64_t>::max();
  state.frame.last_point_ns = std::numeric_limits<std::int64_t>::min();
  state.frame.minimum_relative_time_seconds =
      std::numeric_limits<double>::infinity();
  state.frame.maximum_relative_time_seconds =
      -std::numeric_limits<double>::infinity();
  state.frame.minimum_laser_id = std::numeric_limits<std::uint32_t>::max();
  state.frame.timestamps_nondecreasing = true;
  state.frame.required_fields_finite = true;
  return state;
}

auto consume_lidar_records(LidarFrameParseState& state,
                           std::span<const std::byte> content) -> void {
  if (content.size() % kBoreasLidarRecordBytes != 0U) {
    throw format_error(state.source_key, 0U, "record_layout",
                       "partial record encountered");
  }
  const auto records = content.size() / kBoreasLidarRecordBytes;
  if (state.point_index > state.frame.point_count ||
      records > state.frame.point_count - state.point_index) {
    throw format_error(state.source_key, 0U, "record_layout",
                       "input exceeds the declared frame size");
  }
  for (std::size_t record = 0U; record < records; ++record) {
    ++state.point_index;
    const auto row_number = static_cast<std::size_t>(state.point_index);
    const auto record_value = decode_boreas_lidar_record(
        content.subspan(record * kBoreasLidarRecordBytes,
                        kBoreasLidarRecordBytes),
        state.source_key, row_number);
    const auto& values = record_value.values;
    const auto& bits = record_value.bits;
    const auto laser_rounded = std::round(values[4]);
    const auto laser = static_cast<std::uint32_t>(laser_rounded);
    state.frame.minimum_laser_id =
        std::min(state.frame.minimum_laser_id, laser);
    state.frame.maximum_laser_id =
        std::max(state.frame.maximum_laser_id, laser);

    const double offset_seconds = static_cast<double>(values[5]);
    const double unrounded_offset_ns = offset_seconds * 1'000'000'000.0;
    constexpr double kInt64UpperExclusive = 9'223'372'036'854'775'808.0;
    if (unrounded_offset_ns <
            static_cast<double>(std::numeric_limits<std::int64_t>::min()) ||
        unrounded_offset_ns >= kInt64UpperExclusive) {
      throw format_error(state.source_key, row_number, "time_offset",
                         "nanosecond offset is out of range");
    }
    const auto offset_ns =
        static_cast<std::int64_t>(std::llround(unrounded_offset_ns));
    state.maximum_time_conversion_error_ns =
        std::max(state.maximum_time_conversion_error_ns,
                 std::abs(unrounded_offset_ns - static_cast<double>(offset_ns)));
    const auto point_time =
        checked_add(state.frame.scan_midpoint_ns, offset_ns, state.source_key,
                    row_number, "time_offset");
    state.frame.first_point_ns =
        std::min(state.frame.first_point_ns, point_time);
    state.frame.last_point_ns =
        std::max(state.frame.last_point_ns, point_time);
    state.frame.timestamps_nondecreasing =
        state.frame.timestamps_nondecreasing &&
        point_time >= state.previous_time;
    state.previous_time = point_time;
    if (offset_seconds < state.frame.minimum_relative_time_seconds) {
      state.frame.minimum_relative_time_seconds = offset_seconds;
      state.frame.minimum_relative_time_bits = bits[5];
    }
    if (offset_seconds > state.frame.maximum_relative_time_seconds) {
      state.frame.maximum_relative_time_seconds = offset_seconds;
      state.frame.maximum_relative_time_bits = bits[5];
    }
  }
}

auto finish_lidar_frame(LidarFrameParseState state) -> LidarFrameParseResult {
  if (state.point_index != state.frame.point_count) {
    throw format_error(state.source_key, 0U, "record_layout",
                       "point count changed during read");
  }
  return {std::move(state.frame), state.maximum_time_conversion_error_ns};
}

auto quantize(double value, double scale) -> double {
  return std::round(value * scale) / scale;
}

auto initialize_bounds(double latitude_deg, double longitude_deg)
    -> GeographicBounds {
  return GeographicBounds{latitude_deg, latitude_deg, longitude_deg,
                          longitude_deg};
}

auto update_bounds(GeographicBounds& bounds, double latitude_deg,
                   double longitude_deg) -> void {
  bounds.minimum_latitude_deg =
      std::min(bounds.minimum_latitude_deg, latitude_deg);
  bounds.maximum_latitude_deg =
      std::max(bounds.maximum_latitude_deg, latitude_deg);
  bounds.minimum_longitude_deg =
      std::min(bounds.minimum_longitude_deg, longitude_deg);
  bounds.maximum_longitude_deg =
      std::max(bounds.maximum_longitude_deg, longitude_deg);
}

auto point_segment_distance(const LocalPoint& point, const LocalPoint& begin,
                            const LocalPoint& end) -> double {
  const double delta_x = end.x_m - begin.x_m;
  const double delta_y = end.y_m - begin.y_m;
  const double length_squared = (delta_x * delta_x) + (delta_y * delta_y);
  if (length_squared == 0.0) {
    return std::hypot(point.x_m - begin.x_m, point.y_m - begin.y_m);
  }
  const double projection =
      std::clamp(((point.x_m - begin.x_m) * delta_x +
                  (point.y_m - begin.y_m) * delta_y) /
                     length_squared,
                 0.0, 1.0);
  return std::hypot(point.x_m - (begin.x_m + projection * delta_x),
                    point.y_m - (begin.y_m + projection * delta_y));
}

auto parse_route_polyline(const std::filesystem::path& path)
    -> std::vector<GeographicPoint> {
  const auto source_bytes = checked_file_size(path, kRouteSource);
  if (source_bytes == 0U || source_bytes > kMaximumRouteBytes) {
    throw format_error(kRouteSource, 0U, "polyline",
                       "file size is outside the supported range");
  }
  auto input = open_text(path, kRouteSource);
  const std::string document((std::istreambuf_iterator<char>(input)),
                             std::istreambuf_iterator<char>());
  const auto marker = document.find("L.polyline");
  const auto outer = marker == std::string::npos
                         ? std::string::npos
                         : document.find("[[", marker);
  if (outer == std::string::npos) {
    throw format_error(kRouteSource, 0U, "polyline",
                       "Leaflet polyline is missing");
  }

  std::vector<GeographicPoint> points;
  std::size_t cursor = outer + 1U;
  auto skip = [&document, &cursor]() {
    while (cursor < document.size() &&
           (std::isspace(static_cast<unsigned char>(document[cursor])) != 0 ||
            document[cursor] == ',')) {
      ++cursor;
    }
  };
  while (cursor < document.size()) {
    skip();
    if (cursor >= document.size() || document[cursor] == ']') {
      break;
    }
    if (document[cursor] != '[') {
      throw format_error(kRouteSource, points.size() + 1U, "polyline",
                         "coordinate pair is malformed");
    }
    ++cursor;
    skip();
    const auto latitude_begin = cursor;
    while (cursor < document.size() && document[cursor] != ',' &&
           document[cursor] != ']') {
      ++cursor;
    }
    if (cursor >= document.size() || document[cursor] != ',') {
      throw format_error(kRouteSource, points.size() + 1U, "latitude",
                         "coordinate pair is malformed");
    }
    const auto latitude = parse_double(
        std::string_view(document).substr(latitude_begin, cursor - latitude_begin),
        kRouteSource, points.size() + 1U, "latitude");
    ++cursor;
    skip();
    const auto longitude_begin = cursor;
    while (cursor < document.size() && document[cursor] != ']') {
      ++cursor;
    }
    if (cursor >= document.size()) {
      throw format_error(kRouteSource, points.size() + 1U, "longitude",
                         "coordinate pair is malformed");
    }
    const auto longitude = parse_double(
        std::string_view(document).substr(longitude_begin,
                                          cursor - longitude_begin),
        kRouteSource, points.size() + 1U, "longitude");
    ++cursor;
    if (latitude < -90.0 || latitude > 90.0 || longitude < -180.0 ||
        longitude > 180.0) {
      throw format_error(kRouteSource, points.size() + 1U, "coordinate",
                         "WGS84 coordinate is out of range");
    }
    points.push_back({latitude, longitude, 0.0});
  }
  if (points.size() < 2U) {
    throw format_error(kRouteSource, 0U, "polyline",
                       "at least two coordinates are required");
  }
  return points;
}

auto inspect_lidar(const std::filesystem::path& lidar_directory)
    -> LidarSummary {
  if constexpr (std::endian::native != std::endian::little) {
    throw BoreasFormatError(
        "Invalid Boreas input at lidar: little-endian host required");
  }
  std::error_code directory_error;
  if (!std::filesystem::is_directory(lidar_directory, directory_error) ||
      directory_error) {
    throw format_error(kLidarSource, 0U, "", "directory is unavailable");
  }
  std::vector<std::filesystem::path> paths;
  for (std::filesystem::directory_iterator iterator(lidar_directory,
                                                     directory_error);
       !directory_error && iterator != std::filesystem::directory_iterator{};
       iterator.increment(directory_error)) {
    if (iterator->is_regular_file() && iterator->path().extension() == ".bin") {
      if (paths.size() >= kMaximumBoreasLidarFrames) {
        throw format_error(kLidarSource, 0U, "frame_count",
                           "frame count exceeds the supported limit");
      }
      paths.push_back(iterator->path());
    }
  }
  if (directory_error) {
    throw format_error(kLidarSource, 0U, "", "directory cannot be read");
  }
  std::sort(paths.begin(), paths.end());
  if (paths.empty()) {
    throw format_error(kLidarSource, 0U, "", "no binary frames found");
  }

  LidarSummary summary;
  summary.coordinate_frame = "lidar";
  summary.record_layout = "float32[x,y,z,intensity,laser_id,time_offset]";
  summary.byte_order = "little-endian";
  summary.relative_time_unit = "seconds";
  summary.relative_time_reference = "scan_midpoint";
  summary.relative_time_rounding = "nearest_nanosecond_half_away_from_zero";
  summary.first_point_ns = std::numeric_limits<std::int64_t>::max();
  summary.last_point_ns = std::numeric_limits<std::int64_t>::min();

  constexpr std::size_t kRecordsPerBuffer = 4096U;
  std::array<std::byte, kBoreasLidarRecordBytes * kRecordsPerBuffer> buffer{};
  for (std::size_t frame_index = 0U; frame_index < paths.size(); ++frame_index) {
    const auto& path = paths[frame_index];
    const auto frame_id = path.stem().string();
    const auto source_key = std::string(kLidarSource) + "/" + frame_id + ".bin";
    const auto source_bytes = checked_file_size(path, source_key);
    auto state = initialize_lidar_frame(frame_id, source_bytes);
    std::ifstream input(path, std::ios::binary);
    if (!input) {
      throw format_error(source_key, 0U, "", "file is unavailable");
    }

    while (input) {
      input.read(reinterpret_cast<char*>(buffer.data()),
                 static_cast<std::streamsize>(buffer.size()));
      const auto read_bytes = input.gcount();
      if (read_bytes < 0 || static_cast<std::size_t>(read_bytes) %
                                    kBoreasLidarRecordBytes !=
                                0U) {
        throw format_error(source_key, 0U, "record_layout",
                           "partial record encountered");
      }
      consume_lidar_records(
          state, std::span(buffer.data(), static_cast<std::size_t>(read_bytes)));
    }
    if (!input.eof()) {
      throw format_error(source_key, 0U, "", "binary read failed");
    }
    auto parsed = finish_lidar_frame(std::move(state));
    auto& frame = parsed.frame;
    summary.maximum_time_conversion_error_ns =
        std::max(summary.maximum_time_conversion_error_ns,
                 parsed.maximum_time_conversion_error_ns);
    summary.total_points = checked_add_unsigned(
        summary.total_points, frame.point_count, kLidarSource, "point_count");
    summary.total_bytes = checked_add_unsigned(
        summary.total_bytes, frame.source_bytes, kLidarSource, "byte_count");
    summary.first_point_ns = std::min(summary.first_point_ns, frame.first_point_ns);
    summary.last_point_ns = std::max(summary.last_point_ns, frame.last_point_ns);
    summary.frames.push_back(frame);
  }
  summary.maximum_time_conversion_error_ns =
      quantize(summary.maximum_time_conversion_error_ns, 1'000'000.0);
  return summary;
}

auto inspect_trajectory(const std::filesystem::path& path,
                        const LidarSummary& lidar,
                        const GeographicBounds& road_region,
                        std::size_t route_sample_stride_rows,
                        std::vector<GeographicPoint>& route_samples)
    -> TrajectorySummary {
  constexpr std::string_view kHeader =
      "GPSTime,easting,northing,altitude,vel_east,vel_north,vel_up,roll,pitch,"
      "heading,angvel_z,angvel_y,angvel_x,accelz,accely,accelx,latitude,longitude";
  auto input = open_text(path, kGpsSource);
  require_header(input, kHeader, kGpsSource);

  TrajectorySummary summary;
  summary.source_key = kGpsSource;
  summary.position_frame = "enu_ref";
  summary.pose_target_frame = "enu_ref";
  summary.pose_source_frame = "applanix";
  summary.pose_convention = "T_target_source";
  summary.time_epoch = "unix_utc";
  summary.time_reference = "gps_solution_epoch";
  summary.raw_time_unit = "decimal_seconds";
  summary.normalized_time_unit = "signed_int64_nanoseconds";
  summary.angular_input_unit = "radians";
  summary.angular_output_unit = "degrees";
  summary.angular_conversion = "degrees=radians*180/pi";
  summary.vertical_datum = "unknown_dataset_altitude";
  summary.route_sample_stride_rows = route_sample_stride_rows;
  summary.first_time_ns = std::numeric_limits<std::int64_t>::max();
  summary.last_time_ns = std::numeric_limits<std::int64_t>::min();
  summary.clip_first_time_ns = std::numeric_limits<std::int64_t>::max();
  summary.clip_last_time_ns = std::numeric_limits<std::int64_t>::min();
  summary.enu_minimum_m.fill(std::numeric_limits<double>::infinity());
  summary.enu_maximum_m.fill(-std::numeric_limits<double>::infinity());

  std::string line;
  std::int64_t previous_time = std::numeric_limits<std::int64_t>::min();
  std::unique_ptr<GeographicLib::LocalCartesian> local;
  GeographicPoint last_point;
  bool sampled_last = false;
  while (read_bounded_line(input, line, kGpsSource,
                           static_cast<std::size_t>(summary.row_count + 2U))) {
    ++summary.row_count;
    const auto row_number = static_cast<std::size_t>(summary.row_count + 1U);
    const auto fields = split_csv(line);
    if (fields.size() != 18U) {
      throw format_error(kGpsSource, row_number, "row",
                         "exactly 18 columns required");
    }
    const auto time_ns = parse_decimal_seconds_to_nanoseconds(
        fields[0], kGpsSource, row_number, "GPSTime");
    if (time_ns < previous_time) {
      throw format_error(kGpsSource, row_number, "GPSTime",
                         "timestamps must be nondecreasing");
    }
    previous_time = time_ns;
    std::array<double, 17> numeric{};
    for (std::size_t field = 1U; field < fields.size(); ++field) {
      numeric[field - 1U] = parse_double(fields[field], kGpsSource, row_number,
                                         "column");
    }
    const double latitude_deg = numeric[15] * kRadiansToDegrees;
    const double longitude_deg = numeric[16] * kRadiansToDegrees;
    if (latitude_deg < -90.0 || latitude_deg > 90.0 ||
        longitude_deg < -180.0 || longitude_deg > 180.0) {
      throw format_error(kGpsSource, row_number, "latitude_longitude",
                         "converted WGS84 coordinate is out of range");
    }
    GeographicPoint point{latitude_deg, longitude_deg, numeric[2]};
    last_point = point;
    summary.first_time_ns = std::min(summary.first_time_ns, time_ns);
    summary.last_time_ns = std::max(summary.last_time_ns, time_ns);
    if (summary.row_count == 1U) {
      summary.wgs84_bounds = initialize_bounds(latitude_deg, longitude_deg);
      summary.local_origin_deg = {latitude_deg, longitude_deg};
      local = std::make_unique<GeographicLib::LocalCartesian>(
          latitude_deg, longitude_deg, point.altitude_m,
          GeographicLib::Geocentric::WGS84());
    } else {
      update_bounds(summary.wgs84_bounds, latitude_deg, longitude_deg);
    }
    for (std::size_t axis = 0U; axis < 3U; ++axis) {
      summary.enu_minimum_m[axis] =
          std::min(summary.enu_minimum_m[axis], numeric[axis]);
      summary.enu_maximum_m[axis] =
          std::max(summary.enu_maximum_m[axis], numeric[axis]);
    }

    double local_x = 0.0;
    double local_y = 0.0;
    double local_z = 0.0;
    local->Forward(latitude_deg, longitude_deg, point.altitude_m, local_x,
                   local_y, local_z);
    summary.maximum_local_coordinate_magnitude_m =
        std::max(summary.maximum_local_coordinate_magnitude_m,
                 std::max({std::abs(local_x), std::abs(local_y),
                           std::abs(local_z)}));
    const auto local_float_error = std::max(
        {std::abs(local_x - static_cast<double>(static_cast<float>(local_x))),
         std::abs(local_y - static_cast<double>(static_cast<float>(local_y))),
         std::abs(local_z - static_cast<double>(static_cast<float>(local_z)))});
    summary.maximum_local_float32_quantization_m =
        std::max(summary.maximum_local_float32_quantization_m,
                 local_float_error);
    double ecef_x = 0.0;
    double ecef_y = 0.0;
    double ecef_z = 0.0;
    GeographicLib::Geocentric::WGS84().Forward(
        latitude_deg, longitude_deg, point.altitude_m, ecef_x, ecef_y, ecef_z);
    const auto global_float_error = std::max(
        {std::abs(ecef_x - static_cast<double>(static_cast<float>(ecef_x))),
         std::abs(ecef_y - static_cast<double>(static_cast<float>(ecef_y))),
         std::abs(ecef_z - static_cast<double>(static_cast<float>(ecef_z)))});
    summary.maximum_global_ecef_float32_quantization_m =
        std::max(summary.maximum_global_ecef_float32_quantization_m,
                 global_float_error);
    double reverse_latitude = 0.0;
    double reverse_longitude = 0.0;
    double reverse_altitude = 0.0;
    local->Reverse(local_x, local_y, local_z, reverse_latitude,
                   reverse_longitude, reverse_altitude);
    double roundtrip_error = 0.0;
    GeographicLib::Geodesic::WGS84().Inverse(
        latitude_deg, longitude_deg, reverse_latitude, reverse_longitude,
        roundtrip_error);
    roundtrip_error =
        std::hypot(roundtrip_error, reverse_altitude - point.altitude_m);
    summary.maximum_wgs84_local_roundtrip_error_m =
        std::max(summary.maximum_wgs84_local_roundtrip_error_m,
                 roundtrip_error);

    if (time_ns >= lidar.first_point_ns && time_ns <= lidar.last_point_ns) {
      ++summary.clip_row_count;
      summary.clip_first_time_ns =
          std::min(summary.clip_first_time_ns, time_ns);
      summary.clip_last_time_ns = std::max(summary.clip_last_time_ns, time_ns);
      if (summary.clip_row_count == 1U) {
        summary.clip_wgs84_bounds =
            initialize_bounds(latitude_deg, longitude_deg);
      } else {
        update_bounds(summary.clip_wgs84_bounds, latitude_deg, longitude_deg);
      }
    }
    sampled_last = ((summary.row_count - 1U) % route_sample_stride_rows) == 0U;
    if (sampled_last) {
      route_samples.push_back(point);
    }
  }
  if (!input.eof()) {
    throw format_error(kGpsSource, 0U, "", "text read failed");
  }
  if (summary.row_count == 0U) {
    throw format_error(kGpsSource, 0U, "row", "at least one row required");
  }
  if (summary.clip_row_count == 0U) {
    throw format_error(kGpsSource, 0U, "GPSTime",
                       "no trajectory rows overlap lidar point times");
  }
  if (!sampled_last) {
    route_samples.push_back(last_point);
  }
  summary.road_region_contains_trajectory =
      summary.wgs84_bounds.minimum_latitude_deg >=
          road_region.minimum_latitude_deg &&
      summary.wgs84_bounds.maximum_latitude_deg <=
          road_region.maximum_latitude_deg &&
      summary.wgs84_bounds.minimum_longitude_deg >=
          road_region.minimum_longitude_deg &&
      summary.wgs84_bounds.maximum_longitude_deg <=
          road_region.maximum_longitude_deg;
  summary.maximum_local_coordinate_magnitude_m =
      quantize(summary.maximum_local_coordinate_magnitude_m, 1'000'000.0);
  summary.maximum_local_float32_quantization_m =
      quantize(summary.maximum_local_float32_quantization_m, 1'000'000'000.0);
  summary.maximum_global_ecef_float32_quantization_m = quantize(
      summary.maximum_global_ecef_float32_quantization_m, 1'000'000'000.0);
  summary.maximum_wgs84_local_roundtrip_error_m = quantize(
      summary.maximum_wgs84_local_roundtrip_error_m, 1'000'000'000.0);
  return summary;
}

auto crosscheck_route(TrajectorySummary& trajectory,
                      std::span<const GeographicPoint> samples,
                      std::span<const GeographicPoint> route) -> void {
  GeographicLib::LocalCartesian local(
      trajectory.local_origin_deg[0], trajectory.local_origin_deg[1], 0.0,
      GeographicLib::Geocentric::WGS84());
  std::vector<LocalPoint> local_route;
  local_route.reserve(route.size());
  for (const auto& point : route) {
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;
    local.Forward(point.latitude_deg, point.longitude_deg, 0.0, x, y, z);
    local_route.push_back({x, y});
  }
  std::vector<double> residuals;
  residuals.reserve(samples.size());
  for (const auto& sample : samples) {
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;
    local.Forward(sample.latitude_deg, sample.longitude_deg, 0.0, x, y, z);
    double residual = std::numeric_limits<double>::infinity();
    for (std::size_t index = 1U; index < local_route.size(); ++index) {
      residual = std::min(residual,
                          point_segment_distance({x, y}, local_route[index - 1U],
                                                 local_route[index]));
    }
    residuals.push_back(residual);
  }
  std::sort(residuals.begin(), residuals.end());
  const auto p95_index = static_cast<std::size_t>(
      std::ceil(0.95 * static_cast<double>(residuals.size())) - 1.0);
  trajectory.route_crosscheck_sample_count = residuals.size();
  trajectory.route_polyline_point_count = route.size();
  trajectory.route_crosscheck_p95_m =
      quantize(residuals[p95_index], 1'000'000.0);
  trajectory.route_crosscheck_maximum_m =
      quantize(residuals.back(), 1'000'000.0);
}

auto inspect_lidar_poses(const std::filesystem::path& path,
                         std::span<const LidarFrameSummary> frames)
    -> LidarPoseSummary {
  constexpr std::string_view kHeader =
      "GPSTime,easting,northing,altitude,vel_east,vel_north,vel_up,roll,pitch,"
      "heading,angvel_z,angvel_y,angvel_x";
  auto input = open_text(path, kLidarPoseSource);
  require_header(input, kHeader, kLidarPoseSource);
  LidarPoseSummary summary;
  summary.source_key = kLidarPoseSource;
  summary.target_frame = "enu_ref";
  summary.source_frame = "lidar";
  summary.first_time_ns = std::numeric_limits<std::int64_t>::max();
  summary.last_time_ns = std::numeric_limits<std::int64_t>::min();
  std::vector<std::int64_t> selected;
  selected.reserve(frames.size());
  for (const auto& frame : frames) {
    selected.push_back(frame.scan_midpoint_ns);
  }
  std::sort(selected.begin(), selected.end());

  std::string line;
  std::int64_t previous_time = std::numeric_limits<std::int64_t>::min();
  while (read_bounded_line(
      input, line, kLidarPoseSource,
      static_cast<std::size_t>(summary.row_count + 2U))) {
    ++summary.row_count;
    const auto row_number = static_cast<std::size_t>(summary.row_count + 1U);
    const auto fields = split_csv(line);
    if (fields.size() != 13U) {
      throw format_error(kLidarPoseSource, row_number, "row",
                         "exactly 13 columns required");
    }
    const auto timestamp_us = parse_unsigned(fields[0], kLidarPoseSource,
                                             row_number, "GPSTime");
    const auto timestamp_ns = microseconds_to_nanoseconds(
        timestamp_us, kLidarPoseSource, row_number, "GPSTime");
    if (timestamp_ns < previous_time) {
      throw format_error(kLidarPoseSource, row_number, "GPSTime",
                         "timestamps must be nondecreasing");
    }
    previous_time = timestamp_ns;
    for (std::size_t field = 1U; field < fields.size(); ++field) {
      static_cast<void>(parse_double(fields[field], kLidarPoseSource, row_number,
                                     "column"));
    }
    summary.first_time_ns = std::min(summary.first_time_ns, timestamp_ns);
    summary.last_time_ns = std::max(summary.last_time_ns, timestamp_ns);
    if (std::binary_search(selected.begin(), selected.end(), timestamp_ns)) {
      ++summary.selected_frame_matches;
    }
  }
  if (!input.eof()) {
    throw format_error(kLidarPoseSource, 0U, "", "text read failed");
  }
  if (summary.row_count == 0U) {
    throw format_error(kLidarPoseSource, 0U, "row",
                       "at least one row required");
  }
  return summary;
}

auto inspect_calibration(const std::filesystem::path& path,
                         std::string_view source_key,
                         std::string_view target_frame,
                         std::string_view source_frame) -> MatrixSummary {
  auto input = open_text(path, source_key);
  MatrixSummary summary;
  summary.source_key = source_key;
  summary.target_frame = target_frame;
  summary.source_frame = source_frame;
  for (std::size_t row = 0U; row < 4U; ++row) {
    std::string line;
    if (!read_bounded_line(input, line, source_key, row + 1U)) {
      throw format_error(source_key, row + 1U, "matrix",
                         "exactly four rows required");
    }
    std::istringstream fields(line);
    for (std::size_t column = 0U; column < 4U; ++column) {
      std::string lexeme;
      if (!(fields >> lexeme)) {
        throw format_error(source_key, row + 1U, "matrix",
                           "exactly four columns required");
      }
      summary.row_major_values[row * 4U + column] =
          parse_double(lexeme, source_key, row + 1U, "matrix");
    }
    std::string extra;
    if (fields >> extra) {
      throw format_error(source_key, row + 1U, "matrix",
                         "exactly four columns required");
    }
  }
  std::string extra_row;
  while (read_bounded_line(input, extra_row, source_key, 5U)) {
    if (extra_row.find_first_not_of(" \t\r") != std::string::npos) {
      throw format_error(source_key, 5U, "matrix",
                         "exactly four rows required");
    }
  }
  using RowMajorMatrix4d =
      Eigen::Matrix<double, 4, 4, Eigen::RowMajor>;
  const Eigen::Map<const RowMajorMatrix4d> matrix(
      summary.row_major_values.data());
  const auto rotation = matrix.block<3, 3>(0, 0);
  summary.rotation_orthonormality_error = quantize(
      (rotation.transpose() * rotation - Eigen::Matrix3d::Identity()).norm(),
      1'000'000'000'000.0);
  summary.rotation_determinant =
      quantize(rotation.determinant(), 1'000'000'000'000.0);
  if (!matrix.allFinite() ||
      !matrix.row(3).isApprox(Eigen::RowVector4d(0.0, 0.0, 0.0, 1.0),
                              1e-12) ||
      summary.rotation_orthonormality_error > 1e-9 ||
      std::abs(summary.rotation_determinant - 1.0) > 1e-9) {
    throw format_error(source_key, 0U, "matrix",
                       "rigid T_target_source transform required");
  }
  for (auto& value : summary.row_major_values) {
    value = quantize(value, 1'000'000'000'000.0);
  }
  return summary;
}

auto peak_rss_bytes() -> std::uint64_t {
  rusage usage{};
  if (getrusage(RUSAGE_SELF, &usage) != 0) {
    throw BoreasFormatError(
        "Invalid Boreas runtime measurement: peak RSS is unavailable");
  }
#if defined(__APPLE__)
  return static_cast<std::uint64_t>(usage.ru_maxrss);
#else
  return static_cast<std::uint64_t>(usage.ru_maxrss) * 1024U;
#endif
}

}  // namespace

auto decode_boreas_lidar_record(std::span<const std::byte> content,
                                std::string_view source_key,
                                std::size_t row_number) -> BoreasLidarRecord {
  if (content.size() != kBoreasLidarRecordBytes) {
    throw format_error(source_key, row_number, "record_layout",
                       "exactly 24 bytes required");
  }
  BoreasLidarRecord result;
  for (std::size_t field = 0U; field < result.values.size(); ++field) {
    std::memcpy(&result.bits[field], content.data() + field * sizeof(float),
                sizeof(float));
    result.values[field] = std::bit_cast<float>(result.bits[field]);
  }
  if (!std::all_of(result.values.begin(), result.values.end(),
                   [](float value) { return std::isfinite(value); })) {
    throw format_error(source_key, row_number, "point",
                       "all six float32 fields must be finite");
  }
  const auto laser_rounded = std::round(result.values[4]);
  if (result.values[4] < 0.0F || result.values[4] > 127.0F ||
      laser_rounded != result.values[4]) {
    throw format_error(source_key, row_number, "laser_id",
                       "integer in [0,127] required");
  }
  return result;
}

auto parse_boreas_lidar_frame(std::span<const std::byte> content,
                              std::string_view frame_id)
    -> LidarFrameParseResult {
  auto state = initialize_lidar_frame(frame_id, content.size());
  consume_lidar_records(state, content);
  return finish_lidar_frame(std::move(state));
}

auto parse_decimal_seconds_to_nanoseconds(
    std::string_view lexeme, std::string_view source_key,
    std::size_t row_number, std::string_view field_name) -> std::int64_t {
  try {
    return cartosentry::contracts::decimal_seconds_to_nanoseconds(lexeme);
  } catch (const std::invalid_argument& error) {
    throw format_error(source_key, row_number, field_name, error.what());
  } catch (const std::overflow_error& error) {
    throw format_error(source_key, row_number, field_name, error.what());
  }
}

auto inspect_boreas_sequence(
    const std::filesystem::path& sequence_root,
    const std::filesystem::path& route_html_path,
    const GeographicBounds& road_region,
    std::size_t route_sample_stride_rows) -> BoreasInspectionResult {
  if (route_sample_stride_rows == 0U) {
    throw BoreasFormatError(
        "Invalid Boreas inspection option: route sample stride must be positive");
  }
  const auto start = std::chrono::steady_clock::now();
  BoreasInspectionResult result;
  result.schema_version = "cartosentry.boreas-inspection.v1";
  result.adapter_version = "boreas-public-v1";
  result.sequence_id = sequence_root.filename().string();
  if (result.sequence_id.empty()) {
    throw BoreasFormatError(
        "Invalid Boreas input at sequence: sequence identifier is missing");
  }

  const auto lidar_path = sequence_root / "lidar";
  const auto gps_path = sequence_root / "applanix" / "gps_post_process.csv";
  const auto lidar_poses_path = sequence_root / "applanix" / "lidar_poses.csv";
  result.lidar = inspect_lidar(lidar_path);
  std::vector<GeographicPoint> route_samples;
  result.trajectory = inspect_trajectory(gps_path, result.lidar, road_region,
                                         route_sample_stride_rows,
                                         route_samples);
  const auto route = parse_route_polyline(route_html_path);
  crosscheck_route(result.trajectory, route_samples, route);
  result.lidar_poses = inspect_lidar_poses(lidar_poses_path, result.lidar.frames);

  const std::array<std::array<std::string_view, 3>, 3> calibration_contracts{{
      {"calib/T_applanix_lidar.txt", "applanix", "lidar"},
      {"calib/T_camera_lidar.txt", "camera", "lidar"},
      {"calib/T_radar_lidar.txt", "radar", "lidar"},
  }};
  for (const auto& contract : calibration_contracts) {
    result.calibrations.push_back(inspect_calibration(
        sequence_root / std::filesystem::path(contract[0]), contract[0],
        contract[1], contract[2]));
  }

  result.unique_input_bytes = result.lidar.total_bytes;
  result.unique_input_bytes = checked_add_unsigned(
      result.unique_input_bytes, checked_file_size(gps_path, kGpsSource),
      "sequence", "unique_input_bytes");
  result.unique_input_bytes = checked_add_unsigned(
      result.unique_input_bytes,
      checked_file_size(lidar_poses_path, kLidarPoseSource), "sequence",
      "unique_input_bytes");
  result.unique_input_bytes = checked_add_unsigned(
      result.unique_input_bytes,
      checked_file_size(route_html_path, kRouteSource), "sequence",
      "unique_input_bytes");
  for (const auto& contract : calibration_contracts) {
    result.unique_input_bytes = checked_add_unsigned(
        result.unique_input_bytes,
        checked_file_size(sequence_root / std::filesystem::path(contract[0]),
                          contract[0]),
        "sequence", "unique_input_bytes");
  }
  const auto elapsed = std::chrono::duration<double>(
      std::chrono::steady_clock::now() - start);
  result.elapsed_seconds = elapsed.count();
  result.peak_rss_bytes = peak_rss_bytes();
  return result;
}

}  // namespace cartosentry::ingest
