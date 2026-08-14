#include "cartosentry/spikes/observability.hpp"

#include "cartosentry/ingest/boreas_inspector.hpp"

#include <GeographicLib/Geocentric.hpp>
#include <GeographicLib/LocalCartesian.hpp>

#include <Eigen/Core>
#include <Eigen/Geometry>

#include <osmium/handler.hpp>
#include <osmium/handler/node_locations_for_ways.hpp>
#include <osmium/index/map/flex_mem.hpp>
#include <osmium/io/file.hpp>
#include <osmium/io/reader.hpp>
#include <osmium/io/xml_input.hpp>
#include <osmium/visitor.hpp>

#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <functional>
#include <limits>
#include <numbers>
#include <optional>
#include <queue>
#include <span>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace cartosentry::spikes {
namespace {

constexpr std::string_view kGpsSource = "applanix/gps_post_process.csv";
constexpr std::string_view kCalibrationSource = "calib/T_applanix_lidar.txt";
constexpr std::string_view kLidarSource = "lidar";
constexpr std::string_view kRoadGraphSource = "road_graph";
constexpr double kRadiansToDegrees = 180.0 / std::numbers::pi_v<double>;

struct Pose {
  Eigen::Vector3d translation;
  Eigen::Quaterniond rotation;
};

struct TrajectoryRow {
  std::int64_t time_ns{};
  Pose pose_enu_applanix;
  double latitude_deg{};
  double longitude_deg{};
  double easting_m{};
  double northing_m{};
  double speed_mps{};
  double travel_heading_rad{};
};

struct Cell {
  std::int64_t x{};
  std::int64_t y{};
  std::int64_t z{};

  auto operator==(const Cell &) const -> bool = default;
};

struct CellHash {
  auto operator()(const Cell &cell) const noexcept -> std::size_t {
    std::size_t result = std::hash<std::int64_t>{}(cell.x);
    result ^= std::hash<std::int64_t>{}(cell.y) + 0x9e3779b9U + (result << 6U) +
              (result >> 2U);
    result ^= std::hash<std::int64_t>{}(cell.z) + 0x9e3779b9U + (result << 6U) +
              (result >> 2U);
    return result;
  }
};

struct DirectedArc {
  std::int64_t way_id{};
  std::size_t segment_index{};
  bool forward{};
  Eigen::Vector2d begin_m;
  Eigen::Vector2d end_m;
};

struct ImportedGraph {
  std::uint64_t node_count{};
  std::uint64_t way_count{};
  std::uint64_t excluded_way_count{};
  std::vector<DirectedArc> arcs;
};

auto input_error(std::string_view source, std::string_view reason)
    -> cartosentry::ingest::BoreasFormatError {
  return cartosentry::ingest::BoreasFormatError(
      "Invalid observability input at " + std::string(source) + ": " +
      std::string(reason));
}

void validate_parameters(const ObservabilityParameters &parameters) {
  if (parameters.injected_point_time_shift_ns <= 0 ||
      parameters.injected_trajectory_shift_m <= 0.0 ||
      parameters.lidar_point_stride == 0U ||
      parameters.map_trajectory_stride_rows == 0U ||
      parameters.candidate_search_radius_m <= 0.0 ||
      parameters.confident_lateral_distance_m <= 0.0 ||
      parameters.confident_heading_error_rad <= 0.0 ||
      parameters.confident_score_separation < 0.0 ||
      parameters.minimum_moving_speed_mps < 0.0 ||
      parameters.minimum_alignment_separation_m <= 0.0) {
    throw std::invalid_argument("observability parameters must be positive");
  }
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
  if (!fields.empty() && !fields.back().empty() &&
      fields.back().back() == '\r') {
    fields.back().remove_suffix(1U);
  }
  return fields;
}

auto parse_double(std::string_view value, std::string_view source) -> double {
  double parsed = 0.0;
  const auto result =
      std::from_chars(value.data(), value.data() + value.size(), parsed);
  if (result.ec != std::errc{} || result.ptr != value.data() + value.size() ||
      !std::isfinite(parsed)) {
    throw input_error(source, "finite decimal value required");
  }
  return parsed;
}

auto parse_signed(std::string_view value, std::string_view source)
    -> std::int64_t {
  std::int64_t parsed = 0;
  const auto result =
      std::from_chars(value.data(), value.data() + value.size(), parsed);
  if (result.ec != std::errc{} || result.ptr != value.data() + value.size()) {
    throw input_error(source, "signed integer value required");
  }
  return parsed;
}

auto compose(const Pose &target_intermediate, const Pose &intermediate_source)
    -> Pose {
  return Pose{target_intermediate.rotation * intermediate_source.translation +
                  target_intermediate.translation,
              (target_intermediate.rotation * intermediate_source.rotation)
                  .normalized()};
}

auto transform_point(const Pose &pose, const Eigen::Vector3d &point)
    -> Eigen::Vector3d {
  return pose.rotation * point + pose.translation;
}

auto interpolate_pose(std::span<const TrajectoryRow> trajectory,
                      std::int64_t time_ns) -> Pose {
  const auto upper =
      std::lower_bound(trajectory.begin(), trajectory.end(), time_ns,
                       [](const TrajectoryRow &row, std::int64_t value) {
                         return row.time_ns < value;
                       });
  if (upper == trajectory.begin()) {
    if (upper != trajectory.end() && upper->time_ns == time_ns) {
      return upper->pose_enu_applanix;
    }
    throw input_error(kGpsSource, "point time precedes trajectory support");
  }
  if (upper == trajectory.end()) {
    throw input_error(kGpsSource, "point time exceeds trajectory support");
  }
  if (upper->time_ns == time_ns) {
    return upper->pose_enu_applanix;
  }
  const auto lower = upper - 1;
  const auto interval_ns = upper->time_ns - lower->time_ns;
  if (interval_ns <= 0 || interval_ns > 100'000'000) {
    throw input_error(kGpsSource,
                      "trajectory interpolation gap is unsupported");
  }
  const double alpha = static_cast<double>(time_ns - lower->time_ns) /
                       static_cast<double>(interval_ns);
  return Pose{lower->pose_enu_applanix.translation * (1.0 - alpha) +
                  upper->pose_enu_applanix.translation * alpha,
              lower->pose_enu_applanix.rotation.slerp(
                  alpha, upper->pose_enu_applanix.rotation)};
}

auto load_trajectory(const std::filesystem::path &sequence_root)
    -> std::vector<TrajectoryRow> {
  constexpr std::string_view kHeader =
      "GPSTime,easting,northing,altitude,vel_east,vel_north,vel_up,roll,pitch,"
      "heading,angvel_z,angvel_y,angvel_x,accelz,accely,accelx,latitude,"
      "longitude";
  std::ifstream input(sequence_root / std::filesystem::path(kGpsSource));
  if (!input) {
    throw input_error(kGpsSource, "file is unavailable");
  }
  std::string line;
  if (!std::getline(input, line)) {
    throw input_error(kGpsSource, "header is missing");
  }
  if (!line.empty() && line.back() == '\r') {
    line.pop_back();
  }
  if (line != kHeader) {
    throw input_error(kGpsSource, "schema does not match");
  }

  std::vector<TrajectoryRow> trajectory;
  std::int64_t previous_time = std::numeric_limits<std::int64_t>::min();
  std::size_t row_number = 1U;
  while (std::getline(input, line)) {
    ++row_number;
    const auto fields = split_csv(line);
    if (fields.size() != 18U) {
      throw input_error(kGpsSource, "exactly 18 columns required");
    }
    TrajectoryRow row;
    row.time_ns = cartosentry::ingest::parse_decimal_seconds_to_nanoseconds(
        fields[0], kGpsSource, row_number, "GPSTime");
    if (row.time_ns < previous_time) {
      throw input_error(kGpsSource, "timestamps must be nondecreasing");
    }
    previous_time = row.time_ns;
    const double easting = parse_double(fields[1], kGpsSource);
    const double northing = parse_double(fields[2], kGpsSource);
    const double altitude = parse_double(fields[3], kGpsSource);
    const double velocity_east = parse_double(fields[4], kGpsSource);
    const double velocity_north = parse_double(fields[5], kGpsSource);
    const double roll = parse_double(fields[7], kGpsSource);
    const double pitch = parse_double(fields[8], kGpsSource);
    const double heading = parse_double(fields[9], kGpsSource);
    const double latitude_rad = parse_double(fields[16], kGpsSource);
    const double longitude_rad = parse_double(fields[17], kGpsSource);
    const Eigen::Quaterniond rotation(
        Eigen::AngleAxisd(heading, Eigen::Vector3d::UnitZ()) *
        Eigen::AngleAxisd(pitch, Eigen::Vector3d::UnitY()) *
        Eigen::AngleAxisd(roll, Eigen::Vector3d::UnitX()));
    row.pose_enu_applanix = Pose{Eigen::Vector3d(easting, northing, altitude),
                                 rotation.normalized()};
    row.latitude_deg = latitude_rad * kRadiansToDegrees;
    row.longitude_deg = longitude_rad * kRadiansToDegrees;
    row.easting_m = easting;
    row.northing_m = northing;
    row.speed_mps = std::hypot(velocity_east, velocity_north);
    row.travel_heading_rad = std::atan2(velocity_north, velocity_east);
    trajectory.push_back(row);
  }
  if (!input.eof()) {
    throw input_error(kGpsSource, "text read failed");
  }
  if (trajectory.size() < 2U) {
    throw input_error(kGpsSource, "at least two rows required");
  }
  return trajectory;
}

auto load_extrinsic(const std::filesystem::path &sequence_root) -> Pose {
  std::ifstream input(sequence_root /
                      std::filesystem::path(kCalibrationSource));
  if (!input) {
    throw input_error(kCalibrationSource, "file is unavailable");
  }
  Eigen::Matrix4d matrix = Eigen::Matrix4d::Zero();
  for (Eigen::Index row = 0; row < 4; ++row) {
    for (Eigen::Index column = 0; column < 4; ++column) {
      std::string value;
      if (!(input >> value)) {
        throw input_error(kCalibrationSource, "4 by 4 matrix required");
      }
      matrix(row, column) = parse_double(value, kCalibrationSource);
    }
  }
  std::string extra;
  if (input >> extra ||
      !matrix.row(3).isApprox(Eigen::RowVector4d(0.0, 0.0, 0.0, 1.0), 1e-12)) {
    throw input_error(kCalibrationSource, "rigid 4 by 4 matrix required");
  }
  const Eigen::Matrix3d rotation = matrix.block<3, 3>(0, 0);
  if (!(rotation.transpose() * rotation)
           .isApprox(Eigen::Matrix3d::Identity(), 1e-9) ||
      std::abs(rotation.determinant() - 1.0) > 1e-9) {
    throw input_error(kCalibrationSource, "proper rigid rotation required");
  }
  return Pose{matrix.block<3, 1>(0, 3),
              Eigen::Quaterniond(rotation).normalized()};
}

auto lidar_paths(const std::filesystem::path &sequence_root)
    -> std::vector<std::filesystem::path> {
  const auto directory = sequence_root / std::filesystem::path(kLidarSource);
  std::error_code error;
  if (!std::filesystem::is_directory(directory, error) || error) {
    throw input_error(kLidarSource, "directory is unavailable");
  }
  std::vector<std::filesystem::path> paths;
  for (std::filesystem::directory_iterator iterator(directory, error);
       !error && iterator != std::filesystem::directory_iterator{};
       iterator.increment(error)) {
    std::error_code entry_error;
    if (iterator->is_regular_file(entry_error) && !entry_error &&
        iterator->path().extension() == ".bin") {
      if (paths.size() >= cartosentry::ingest::kMaximumBoreasLidarFrames) {
        throw input_error(kLidarSource,
                          "frame count exceeds the supported limit");
      }
      paths.push_back(iterator->path());
    }
    if (entry_error) {
      throw input_error(kLidarSource, "directory entry is unavailable");
    }
  }
  if (error) {
    throw input_error(kLidarSource, "directory cannot be read");
  }
  std::sort(paths.begin(), paths.end());
  if (paths.size() < 2U) {
    throw input_error(kLidarSource, "at least two frames required");
  }
  return paths;
}

auto filename_time_ns(const std::filesystem::path &path) -> std::int64_t {
  const auto microseconds = parse_signed(path.stem().string(), kLidarSource);
  if (microseconds < 0 ||
      microseconds > std::numeric_limits<std::int64_t>::max() / 1000) {
    throw input_error(kLidarSource, "filename timestamp is out of range");
  }
  return microseconds * 1000;
}

auto cell_for(const Eigen::Vector3d &point, double width_m) -> Cell {
  return Cell{
      static_cast<std::int64_t>(std::floor(point.x() / width_m)),
      static_cast<std::int64_t>(std::floor(point.y() / width_m)),
      static_cast<std::int64_t>(std::floor(point.z() / width_m)),
  };
}

auto one_way_nearest_mean(std::span<const Eigen::Vector3d> query,
                          std::span<const Eigen::Vector3d> reference)
    -> double {
  constexpr double kCellWidthM = 1.0;
  constexpr int kCellRadius = 2;
  constexpr double kMaximumDistanceM = 3.0;
  std::unordered_map<Cell, std::vector<std::size_t>, CellHash> cells;
  cells.reserve(reference.size());
  for (std::size_t index = 0U; index < reference.size(); ++index) {
    cells[cell_for(reference[index], kCellWidthM)].push_back(index);
  }
  double sum = 0.0;
  for (const auto &point : query) {
    const auto center = cell_for(point, kCellWidthM);
    double minimum_squared = kMaximumDistanceM * kMaximumDistanceM;
    for (int dx = -kCellRadius; dx <= kCellRadius; ++dx) {
      for (int dy = -kCellRadius; dy <= kCellRadius; ++dy) {
        for (int dz = -kCellRadius; dz <= kCellRadius; ++dz) {
          const auto found =
              cells.find(Cell{center.x + dx, center.y + dy, center.z + dz});
          if (found == cells.end()) {
            continue;
          }
          for (const auto index : found->second) {
            minimum_squared = std::min(
                minimum_squared, (point - reference[index]).squaredNorm());
          }
        }
      }
    }
    sum += std::sqrt(minimum_squared);
  }
  return sum / static_cast<double>(query.size());
}

auto adjacent_alignment_mean(
    std::span<const std::vector<Eigen::Vector3d>> frames) -> double {
  if (frames.size() < 2U) {
    throw input_error(kLidarSource, "at least two point sets required");
  }
  double total = 0.0;
  std::size_t comparisons = 0U;
  for (std::size_t index = 1U; index < frames.size(); ++index) {
    if (frames[index - 1U].empty() || frames[index].empty()) {
      throw input_error(kLidarSource, "sampled point set is empty");
    }
    total += 0.5 * (one_way_nearest_mean(frames[index - 1U], frames[index]) +
                    one_way_nearest_mean(frames[index], frames[index - 1U]));
    ++comparisons;
  }
  return total / static_cast<double>(comparisons);
}

auto public_alignment(const std::filesystem::path &sequence_root,
                      std::span<const TrajectoryRow> trajectory,
                      const ObservabilityParameters &parameters)
    -> PublicAlignmentResult {
  const auto paths = lidar_paths(sequence_root);
  const auto extrinsic = load_extrinsic(sequence_root);
  std::vector<std::vector<Eigen::Vector3d>> clean_frames;
  std::vector<std::vector<Eigen::Vector3d>> time_shifted_frames;
  std::vector<std::vector<Eigen::Vector3d>> trajectory_shifted_frames;
  clean_frames.reserve(paths.size());
  time_shifted_frames.reserve(paths.size());
  trajectory_shifted_frames.reserve(paths.size());
  std::uint64_t sampled_points = 0U;
  double point_time_effect_sum_m = 0.0;
  double trajectory_effect_sum_m = 0.0;
  const auto first_support_ns = filename_time_ns(paths.front()) - 60'000'000;
  const auto last_support_ns = filename_time_ns(paths.back()) + 60'000'000;
  double minimum_speed = std::numeric_limits<double>::infinity();
  double maximum_speed = 0.0;
  std::optional<double> first_heading;
  double last_heading = 0.0;
  for (const auto &row : trajectory) {
    if (row.time_ns >= first_support_ns && row.time_ns <= last_support_ns) {
      minimum_speed = std::min(minimum_speed, row.speed_mps);
      maximum_speed = std::max(maximum_speed, row.speed_mps);
      if (!first_heading.has_value()) {
        first_heading = row.travel_heading_rad;
      }
      last_heading = row.travel_heading_rad;
    }
  }
  if (!first_heading.has_value()) {
    throw input_error(kGpsSource, "trajectory does not overlap lidar support");
  }

  for (std::size_t frame_index = 0U; frame_index < paths.size();
       ++frame_index) {
    const auto &path = paths[frame_index];
    std::error_code size_error;
    const auto source_bytes = std::filesystem::file_size(path, size_error);
    if (size_error || source_bytes == 0U ||
        source_bytes > cartosentry::ingest::kMaximumBoreasLidarFrameBytes ||
        source_bytes % cartosentry::ingest::kBoreasLidarRecordBytes != 0U) {
      throw input_error(kLidarSource, "invalid binary frame size");
    }
    std::ifstream input(path, std::ios::binary);
    if (!input) {
      throw input_error(kLidarSource, "binary frame is unavailable");
    }
    const auto midpoint_ns = filename_time_ns(path);
    std::vector<Eigen::Vector3d> clean_points;
    std::vector<Eigen::Vector3d> time_shifted_points;
    std::vector<Eigen::Vector3d> trajectory_shifted_points;
    const auto record_count = static_cast<std::size_t>(source_bytes) /
                              cartosentry::ingest::kBoreasLidarRecordBytes;
    clean_points.reserve(record_count / parameters.lidar_point_stride + 1U);
    time_shifted_points.reserve(clean_points.capacity());
    trajectory_shifted_points.reserve(clean_points.capacity());
    constexpr std::size_t kRecordsPerBuffer = 4096U;
    std::array<std::byte, cartosentry::ingest::kBoreasLidarRecordBytes *
                              kRecordsPerBuffer>
        buffer{};
    std::size_t record_index = 0U;
    while (input) {
      input.read(reinterpret_cast<char *>(buffer.data()),
                 static_cast<std::streamsize>(buffer.size()));
      const auto read_bytes = input.gcount();
      if (read_bytes < 0 || static_cast<std::size_t>(read_bytes) %
                                    cartosentry::ingest::kBoreasLidarRecordBytes !=
                                0U) {
        throw input_error(kLidarSource, "binary frame has a partial record");
      }
      const auto records = static_cast<std::size_t>(read_bytes) /
                           cartosentry::ingest::kBoreasLidarRecordBytes;
      for (std::size_t local_index = 0U; local_index < records;
           ++local_index, ++record_index) {
        cartosentry::ingest::BoreasLidarRecord decoded;
        try {
          decoded = cartosentry::ingest::decode_boreas_lidar_record(
              std::span(buffer).subspan(
                  local_index * cartosentry::ingest::kBoreasLidarRecordBytes,
                  cartosentry::ingest::kBoreasLidarRecordBytes),
              kLidarSource, record_index + 1U);
        } catch (const cartosentry::ingest::BoreasFormatError &) {
          throw input_error(kLidarSource, "binary point record is invalid");
        }
        if (record_index % parameters.lidar_point_stride != 0U) {
          continue;
        }
        const auto &values = decoded.values;
        const Eigen::Vector3d point(static_cast<double>(values[0]),
                                    static_cast<double>(values[1]),
                                    static_cast<double>(values[2]));
        const double time_offset_seconds = static_cast<double>(values[5]);
        const double range = point.norm();
        if (range < 5.0 || range > 60.0 || point.z() < -3.0 ||
            point.z() > 4.0) {
          continue;
        }
        const double offset_ns_value = time_offset_seconds * 1'000'000'000.0;
        constexpr double kInt64UpperExclusive = 9'223'372'036'854'775'808.0;
        if (offset_ns_value <
                static_cast<double>(std::numeric_limits<std::int64_t>::min()) ||
            offset_ns_value >= kInt64UpperExclusive) {
          throw input_error(kLidarSource, "point time offset is out of range");
        }
        const auto offset_ns =
            static_cast<std::int64_t>(std::llround(offset_ns_value));
        if ((offset_ns > 0 &&
             midpoint_ns > std::numeric_limits<std::int64_t>::max() - offset_ns) ||
            (offset_ns < 0 && midpoint_ns <
                                  std::numeric_limits<std::int64_t>::min() -
                                      offset_ns)) {
          throw input_error(kLidarSource, "point timestamp is out of range");
        }
        const auto point_time_ns = midpoint_ns + offset_ns;
        const auto clean_pose =
            compose(interpolate_pose(trajectory, point_time_ns), extrinsic);
        if ((parameters.injected_point_time_shift_ns > 0 &&
             point_time_ns > std::numeric_limits<std::int64_t>::max() -
                                 parameters.injected_point_time_shift_ns) ||
            (parameters.injected_point_time_shift_ns < 0 &&
             point_time_ns < std::numeric_limits<std::int64_t>::min() -
                                 parameters.injected_point_time_shift_ns)) {
          throw input_error(kLidarSource, "injected point time is out of range");
        }
        const auto time_shifted_pose = compose(
            interpolate_pose(trajectory,
                             point_time_ns +
                                 parameters.injected_point_time_shift_ns),
            extrinsic);
        auto trajectory_shifted_pose = clean_pose;
        if (frame_index >= paths.size() / 3U &&
            frame_index < (2U * paths.size()) / 3U) {
          trajectory_shifted_pose.translation.x() +=
              parameters.injected_trajectory_shift_m;
        }
        const auto clean_point = transform_point(clean_pose, point);
        const auto time_shifted_point = transform_point(time_shifted_pose, point);
        const auto trajectory_shifted_point =
            transform_point(trajectory_shifted_pose, point);
        point_time_effect_sum_m += (time_shifted_point - clean_point).norm();
        trajectory_effect_sum_m +=
            (trajectory_shifted_point - clean_point).norm();
        clean_points.push_back(clean_point);
        time_shifted_points.push_back(time_shifted_point);
        trajectory_shifted_points.push_back(trajectory_shifted_point);
        ++sampled_points;
      }
    }
    if (!input.eof() || record_index != record_count) {
      throw input_error(kLidarSource, "binary frame read failed");
    }
    if (clean_points.size() < 100U) {
      throw input_error(kLidarSource, "insufficient structured sampled points");
    }
    clean_frames.push_back(std::move(clean_points));
    time_shifted_frames.push_back(std::move(time_shifted_points));
    trajectory_shifted_frames.push_back(std::move(trajectory_shifted_points));
  }

  const double clean_alignment = adjacent_alignment_mean(clean_frames);
  const double time_shifted_alignment =
      adjacent_alignment_mean(time_shifted_frames);
  const double trajectory_shifted_alignment =
      adjacent_alignment_mean(trajectory_shifted_frames);
  const double point_time_effect_mean =
      point_time_effect_sum_m / static_cast<double>(sampled_points);
  const double trajectory_effect_mean =
      trajectory_effect_sum_m / static_cast<double>(sampled_points);
  const double heading_change = std::abs(std::remainder(
      last_heading - *first_heading, 2.0 * std::numbers::pi_v<double>));
  const bool observable_motion =
      minimum_speed >= parameters.minimum_moving_speed_mps &&
      heading_change >= 0.03;
  const bool observable_structure =
      sampled_points / static_cast<std::uint64_t>(paths.size()) >= 500U;
  return PublicAlignmentResult{
      sequence_root.filename().string(),
      "float32_relative_seconds_from_scan_midpoint",
      "T_enu_ref_lidar=T_enu_ref_applanix*T_applanix_lidar",
      paths.size(),
      sampled_points,
      minimum_speed,
      maximum_speed,
      heading_change,
      clean_alignment,
      time_shifted_alignment,
      trajectory_shifted_alignment,
      point_time_effect_mean,
      trajectory_effect_mean,
      observable_motion,
      observable_structure,
      observable_motion && observable_structure &&
          point_time_effect_mean >= parameters.minimum_alignment_separation_m,
      observable_motion && observable_structure &&
          trajectory_effect_mean >= parameters.minimum_alignment_separation_m,
  };
}

auto scenario_pose(std::string_view scenario, double time_seconds) -> Pose {
  if (scenario == "straight") {
    return Pose{Eigen::Vector3d(8.0 * time_seconds, 0.0, 0.0),
                Eigen::Quaterniond::Identity()};
  }
  if (scenario == "turning" || scenario == "sparse_structure") {
    constexpr double kRadiusM = 30.0;
    constexpr double kInitialYawRateRadPerS = 0.12;
    constexpr double kYawAccelerationRadPerS2 = 0.18;
    const double yaw =
        kInitialYawRateRadPerS * time_seconds +
        0.5 * kYawAccelerationRadPerS2 * time_seconds * time_seconds;
    return Pose{
        Eigen::Vector3d(kRadiusM * std::sin(yaw),
                        kRadiusM * (1.0 - std::cos(yaw)), 0.0),
        Eigen::Quaterniond(Eigen::AngleAxisd(yaw, Eigen::Vector3d::UnitZ()))};
  }
  if (scenario == "moving") {
    const double x = 3.0 * time_seconds + 1.5 * time_seconds * time_seconds;
    return Pose{Eigen::Vector3d(x, 0.0, 0.0), Eigen::Quaterniond::Identity()};
  }
  return Pose{Eigen::Vector3d::Zero(), Eigen::Quaterniond::Identity()};
}

auto inverse_transform_point(const Pose &pose, const Eigen::Vector3d &point)
    -> Eigen::Vector3d {
  return pose.rotation.conjugate() * (point - pose.translation);
}

auto synthetic_alignment(std::string_view scenario,
                         std::span<const Eigen::Vector3d> landmarks,
                         double time_shift_seconds, bool trajectory_shift,
                         double trajectory_shift_m) -> double {
  constexpr std::array<double, 3> kFrameTimes{0.5, 1.0, 1.5};
  std::vector<std::vector<Eigen::Vector3d>> compensated;
  compensated.reserve(kFrameTimes.size());
  for (std::size_t frame_index = 0U; frame_index < kFrameTimes.size();
       ++frame_index) {
    std::vector<Eigen::Vector3d> frame;
    frame.reserve(landmarks.size());
    for (std::size_t index = 0U; index < landmarks.size(); ++index) {
      const double fraction =
          landmarks.size() == 1U
              ? 0.5
              : static_cast<double>(index) /
                    static_cast<double>(landmarks.size() - 1U);
      const double offset_seconds = -0.05 + 0.1 * fraction;
      const double true_time = kFrameTimes[frame_index] + offset_seconds;
      const auto measurement = inverse_transform_point(
          scenario_pose(scenario, true_time), landmarks[index]);
      auto estimated_pose =
          scenario_pose(scenario, true_time + time_shift_seconds);
      if (trajectory_shift && frame_index == 1U) {
        estimated_pose.translation.x() += trajectory_shift_m;
      }
      frame.push_back(transform_point(estimated_pose, measurement));
    }
    compensated.push_back(std::move(frame));
  }
  double squared_sum = 0.0;
  std::size_t count = 0U;
  for (std::size_t frame = 1U; frame < compensated.size(); ++frame) {
    for (std::size_t point = 0U; point < compensated[frame].size(); ++point) {
      squared_sum +=
          (compensated[frame][point] - compensated[0U][point]).squaredNorm();
      ++count;
    }
  }
  return std::sqrt(squared_sum / static_cast<double>(count));
}

auto make_landmarks(std::size_t count) -> std::vector<Eigen::Vector3d> {
  std::vector<Eigen::Vector3d> landmarks;
  landmarks.reserve(count);
  for (std::size_t index = 0U; index < count; ++index) {
    const double column = static_cast<double>(index % 8U);
    const double row = static_cast<double>(index / 8U);
    landmarks.emplace_back(15.0 + 4.0 * column, -12.0 + 6.0 * row,
                           -1.0 + static_cast<double>(index % 5U) * 0.75);
  }
  return landmarks;
}

auto include_highway(std::string_view highway) -> bool {
  static const std::unordered_set<std::string_view> included{
      "motorway",      "motorway_link", "trunk",        "trunk_link",
      "primary",       "primary_link",  "secondary",    "secondary_link",
      "tertiary",      "tertiary_link", "unclassified", "residential",
      "living_street", "service",
  };
  return included.contains(highway);
}

class GraphImportHandler : public osmium::handler::Handler {
public:
  GraphImportHandler(GeographicLib::LocalCartesian &local, ImportedGraph &graph)
      : local_(local), graph_(graph) {}

  void node(const osmium::Node &) noexcept { ++graph_.node_count; }

  void way(const osmium::Way &way) {
    const auto highway = tag_value(way, "highway");
    const auto access = tag_value(way, "access");
    const auto motor_vehicle = tag_value(way, "motor_vehicle");
    const auto area = tag_value(way, "area");
    const bool conditional =
        std::any_of(way.tags().begin(), way.tags().end(), [](const auto &tag) {
          return std::string_view(tag.key()).ends_with(":conditional");
        });
    const bool denied_access = access == "no" || access == "private" ||
                               motor_vehicle == "no" ||
                               motor_vehicle == "private";
    if (!include_highway(highway) || denied_access || conditional ||
        area == "yes" || way.nodes().size() < 2U) {
      ++graph_.excluded_way_count;
      return;
    }

    bool forward = true;
    bool reverse = true;
    const auto oneway = tag_value(way, "oneway");
    if (!oneway.empty()) {
      if (oneway == "yes" || oneway == "1" || oneway == "true") {
        reverse = false;
      } else if (oneway == "-1") {
        forward = false;
      } else if (oneway != "no" && oneway != "0" && oneway != "false") {
        ++graph_.excluded_way_count;
        return;
      }
    } else if (tag_value(way, "junction") == "roundabout") {
      reverse = false;
    }

    for (std::size_t index = 1U; index < way.nodes().size(); ++index) {
      const auto first = way.nodes()[index - 1U].location();
      const auto second = way.nodes()[index].location();
      if (!first.valid() || !second.valid()) {
        throw input_error(kRoadGraphSource,
                          "way references a node without a location");
      }
      double first_x = 0.0;
      double first_y = 0.0;
      double first_z = 0.0;
      double second_x = 0.0;
      double second_y = 0.0;
      double second_z = 0.0;
      local_.Forward(first.lat(), first.lon(), 0.0, first_x, first_y, first_z);
      local_.Forward(second.lat(), second.lon(), 0.0, second_x, second_y,
                     second_z);
      const Eigen::Vector2d begin(first_x, first_y);
      const Eigen::Vector2d end(second_x, second_y);
      if ((end - begin).norm() < 0.01) {
        continue;
      }
      if (forward) {
        graph_.arcs.push_back({way.id(), index - 1U, true, begin, end});
      }
      if (reverse) {
        graph_.arcs.push_back({way.id(), index - 1U, false, end, begin});
      }
    }
    ++graph_.way_count;
  }

private:
  static auto tag_value(const osmium::Way &way, const char *key)
      -> std::string_view {
    const char *value = way.tags()[key];
    return value == nullptr ? std::string_view{} : std::string_view(value);
  }

  GeographicLib::LocalCartesian &local_;
  ImportedGraph &graph_;
};

auto import_graph(const std::filesystem::path &path,
                  GeographicLib::LocalCartesian &local) -> ImportedGraph {
  std::error_code size_error;
  const auto size = std::filesystem::file_size(path, size_error);
  if (size_error || size == 0U || size > 64U * 1024U * 1024U) {
    throw input_error(kRoadGraphSource, "file size is outside supported range");
  }
  std::ifstream availability_check(path);
  if (!availability_check) {
    throw input_error(kRoadGraphSource, "file is unavailable");
  }
  ImportedGraph graph;
  try {
    osmium::io::File file(path.string(), "osm");
    osmium::io::Reader reader(
        file, osmium::osm_entity_bits::node | osmium::osm_entity_bits::way,
        osmium::io::read_meta::no);
    using LocationIndex =
        osmium::index::map::FlexMem<osmium::unsigned_object_id_type,
                                    osmium::Location>;
    LocationIndex locations;
    osmium::handler::NodeLocationsForWays<LocationIndex> location_handler(
        locations);
    GraphImportHandler graph_handler(local, graph);
    osmium::apply(reader, location_handler, graph_handler);
    reader.close();
  } catch (const cartosentry::ingest::BoreasFormatError &) {
    throw;
  } catch (const std::exception &) {
    throw input_error(kRoadGraphSource, "OSM XML parse failed");
  }
  if (graph.arcs.empty()) {
    throw input_error(kRoadGraphSource, "no directed road arcs imported");
  }
  return graph;
}

auto angle_error(double first, double second) -> double {
  return std::abs(
      std::remainder(first - second, 2.0 * std::numbers::pi_v<double>));
}

auto public_map_match(std::span<const TrajectoryRow> trajectory,
                      const std::filesystem::path &road_graph_path,
                      const ObservabilityParameters &parameters)
    -> PublicMapMatchResult {
  GeographicLib::LocalCartesian local(trajectory.front().latitude_deg,
                                      trajectory.front().longitude_deg, 0.0,
                                      GeographicLib::Geocentric::WGS84());
  const auto graph = import_graph(road_graph_path, local);
  struct Observation {
    Eigen::Vector2d position_m;
    double easting_m{};
    double northing_m{};
    double speed_mps{};
    double heading_rad{};
    bool confident{};
    double lateral_m{};
  };
  std::vector<Observation> observations;
  for (std::size_t index = 0U; index < trajectory.size();
       index += parameters.map_trajectory_stride_rows) {
    const auto &row = trajectory[index];
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;
    local.Forward(row.latitude_deg, row.longitude_deg, 0.0, x, y, z);
    observations.push_back({Eigen::Vector2d(x, y), row.easting_m,
                            row.northing_m, row.speed_mps,
                            row.travel_heading_rad, false, 0.0});
  }
  if ((trajectory.size() - 1U) % parameters.map_trajectory_stride_rows != 0U) {
    const auto &row = trajectory.back();
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;
    local.Forward(row.latitude_deg, row.longitude_deg, 0.0, x, y, z);
    observations.push_back({Eigen::Vector2d(x, y), row.easting_m,
                            row.northing_m, row.speed_mps,
                            row.travel_heading_rad, false, 0.0});
  }

  std::vector<double> confident_lateral;
  std::uint64_t moving_observations = 0U;
  std::uint64_t confident_observations = 0U;
  for (auto &observation : observations) {
    if (observation.speed_mps < parameters.minimum_moving_speed_mps) {
      continue;
    }
    ++moving_observations;
    double best_score = -std::numeric_limits<double>::infinity();
    double second_score = -std::numeric_limits<double>::infinity();
    double best_lateral = std::numeric_limits<double>::infinity();
    double best_heading_error = std::numeric_limits<double>::infinity();
    for (const auto &arc : graph.arcs) {
      const Eigen::Vector2d delta = arc.end_m - arc.begin_m;
      const double length_squared = delta.squaredNorm();
      const double fraction = std::clamp(
          (observation.position_m - arc.begin_m).dot(delta) / length_squared,
          0.0, 1.0);
      const Eigen::Vector2d projected = arc.begin_m + fraction * delta;
      const double lateral = (observation.position_m - projected).norm();
      if (lateral > parameters.candidate_search_radius_m) {
        continue;
      }
      const double heading = std::atan2(delta.y(), delta.x());
      const double heading_residual =
          angle_error(observation.heading_rad, heading);
      const double score = -0.5 * std::pow(lateral / 5.0, 2.0) -
                           0.5 * std::pow(heading_residual / 0.5, 2.0);
      if (score > best_score) {
        second_score = best_score;
        best_score = score;
        best_lateral = lateral;
        best_heading_error = heading_residual;
      } else if (score > second_score) {
        second_score = score;
      }
    }
    const double separation = best_score - second_score;
    const bool strong_geometry =
        best_lateral <= 2.0 && best_heading_error <= 0.35;
    observation.confident =
        std::isfinite(best_score) &&
        best_lateral <= parameters.confident_lateral_distance_m &&
        best_heading_error <= parameters.confident_heading_error_rad &&
        (strong_geometry ||
         separation >= parameters.confident_score_separation);
    observation.lateral_m = best_lateral;
    if (observation.confident) {
      ++confident_observations;
      confident_lateral.push_back(best_lateral);
    }
  }

  double candidate_distance = 0.0;
  double confident_distance = 0.0;
  for (std::size_t index = 1U; index < observations.size(); ++index) {
    const auto &previous = observations[index - 1U];
    const auto &current = observations[index];
    if (previous.speed_mps < parameters.minimum_moving_speed_mps ||
        current.speed_mps < parameters.minimum_moving_speed_mps) {
      continue;
    }
    const double distance =
        std::hypot(current.easting_m - previous.easting_m,
                   current.northing_m - previous.northing_m);
    candidate_distance += distance;
    confident_distance +=
        0.5 * distance *
        static_cast<double>(static_cast<unsigned>(previous.confident) +
                            static_cast<unsigned>(current.confident));
  }
  if (candidate_distance <= 0.0 || confident_lateral.empty()) {
    throw input_error(kRoadGraphSource, "no confident moving support");
  }
  std::sort(confident_lateral.begin(), confident_lateral.end());
  const auto p95_index = static_cast<std::size_t>(
      std::ceil(0.95 * static_cast<double>(confident_lateral.size())) - 1.0);
  return PublicMapMatchResult{
      "m0.5-directed-candidate-v1",
      "endpoint-half-distance-v1",
      graph.node_count,
      graph.way_count,
      graph.arcs.size(),
      graph.excluded_way_count,
      moving_observations,
      confident_observations,
      candidate_distance,
      confident_distance,
      confident_distance / candidate_distance,
      confident_lateral[p95_index],
  };
}

struct TinyEdge {
  std::string id;
  std::size_t source{};
  std::size_t target{};
  double cost{};
  std::uint8_t required_bit{};
};

auto tiny_edges() -> std::vector<TinyEdge> {
  return {
      {"connector-out", 0U, 1U, 2.0, 0U},  {"required-east", 1U, 2U, 2.0, 1U},
      {"connector-home", 2U, 0U, 2.0, 0U}, {"required-spur", 1U, 3U, 1.0, 2U},
      {"spur-return", 3U, 1U, 1.0, 0U},
  };
}

auto validate_tiny_route(std::span<const std::string> path,
                         double expected_cost) -> bool {
  const auto edges = tiny_edges();
  std::size_t node = 0U;
  std::uint8_t mask = 0U;
  double cost = 0.0;
  for (const auto &id : path) {
    const auto edge = std::find_if(
        edges.begin(), edges.end(),
        [&id](const TinyEdge &candidate) { return candidate.id == id; });
    if (edge == edges.end() || edge->source != node) {
      return false;
    }
    node = edge->target;
    mask = static_cast<std::uint8_t>(mask | edge->required_bit);
    cost += edge->cost;
  }
  return node == 0U && mask == 3U && std::abs(cost - expected_cost) <= 1e-12;
}

auto brute_force_tiny_cost() -> double {
  const auto edges = tiny_edges();
  double best = std::numeric_limits<double>::infinity();
  std::function<void(std::size_t, std::uint8_t, double, std::size_t)> visit;
  visit = [&](std::size_t node, std::uint8_t mask, double cost,
              std::size_t depth) {
    if (cost >= best || depth > 12U) {
      return;
    }
    if (node == 0U && mask == 3U && depth > 0U) {
      best = cost;
      return;
    }
    for (const auto &edge : edges) {
      if (edge.source == node) {
        visit(edge.target, static_cast<std::uint8_t>(mask | edge.required_bit),
              cost + edge.cost, depth + 1U);
      }
    }
  };
  visit(0U, 0U, 0.0, 0U);
  return best;
}

} // namespace

auto run_synthetic_observability_suite(
    const ObservabilityParameters &parameters)
    -> std::vector<SyntheticScenarioResult> {
  validate_parameters(parameters);
  const double time_shift_seconds =
      static_cast<double>(parameters.injected_point_time_shift_ns) /
      1'000'000'000.0;
  const std::array<std::pair<std::string_view, std::size_t>, 5> scenarios{{
      {"straight", 32U},
      {"turning", 32U},
      {"static", 32U},
      {"sparse_structure", 3U},
      {"moving", 32U},
  }};
  std::vector<SyntheticScenarioResult> results;
  results.reserve(scenarios.size());
  for (const auto &[scenario, landmark_count] : scenarios) {
    const auto landmarks = make_landmarks(landmark_count);
    const double clean =
        synthetic_alignment(scenario, landmarks, 0.0, false,
                            parameters.injected_trajectory_shift_m);
    const double timing =
        synthetic_alignment(scenario, landmarks, time_shift_seconds, false,
                            parameters.injected_trajectory_shift_m);
    const double trajectory = synthetic_alignment(
        scenario, landmarks, 0.0, true, parameters.injected_trajectory_shift_m);
    const bool moving = scenario != "static";
    const bool structured = landmark_count >= 8U;
    std::string observability = "OBSERVABLE";
    if (!moving || !structured) {
      observability = "NOT_OBSERVABLE";
    } else if (scenario == "straight") {
      observability = "WEAK";
    }
    const bool observable = observability == "OBSERVABLE";
    results.push_back({
        std::string(scenario),
        observability,
        moving,
        structured,
        clean,
        timing,
        trajectory,
        observable &&
            timing >= clean + parameters.minimum_alignment_separation_m,
        observable &&
            trajectory >= clean + parameters.minimum_alignment_separation_m,
    });
  }
  return results;
}

auto solve_tiny_required_route() -> TinyRouteResult {
  const auto edges = tiny_edges();
  constexpr std::size_t kNodeCount = 4U;
  const std::size_t no_incoming = edges.size();
  constexpr std::size_t kMaskCount = 4U;
  const std::size_t state_count = kNodeCount * (edges.size() + 1U) * kMaskCount;
  auto state_index = [&](std::size_t node, std::size_t incoming,
                         std::uint8_t mask) {
    return (node * (edges.size() + 1U) + incoming) * kMaskCount + mask;
  };
  struct QueueItem {
    double cost{};
    std::size_t node{};
    std::size_t incoming{};
    std::uint8_t mask{};
  };
  const auto greater = [](const QueueItem &left, const QueueItem &right) {
    return std::tie(left.cost, left.node, left.incoming, left.mask) >
           std::tie(right.cost, right.node, right.incoming, right.mask);
  };
  std::priority_queue<QueueItem, std::vector<QueueItem>, decltype(greater)>
      queue(greater);
  std::vector<double> distances(state_count,
                                std::numeric_limits<double>::infinity());
  std::vector<std::optional<std::pair<std::size_t, std::size_t>>> predecessors(
      state_count);
  const auto start = state_index(0U, no_incoming, 0U);
  distances[start] = 0.0;
  queue.push({0.0, 0U, no_incoming, 0U});
  std::optional<std::size_t> goal;
  std::uint64_t explored = 0U;
  while (!queue.empty()) {
    const auto current = queue.top();
    queue.pop();
    const auto current_index =
        state_index(current.node, current.incoming, current.mask);
    if (current.cost != distances[current_index]) {
      continue;
    }
    ++explored;
    if (current.node == 0U && current.mask == 3U &&
        current.incoming != no_incoming) {
      goal = current_index;
      break;
    }
    for (std::size_t edge_index = 0U; edge_index < edges.size(); ++edge_index) {
      const auto &edge = edges[edge_index];
      if (edge.source != current.node) {
        continue;
      }
      const auto next_mask =
          static_cast<std::uint8_t>(current.mask | edge.required_bit);
      const auto next_index = state_index(edge.target, edge_index, next_mask);
      const double next_cost = current.cost + edge.cost;
      if (next_cost < distances[next_index]) {
        distances[next_index] = next_cost;
        predecessors[next_index] = {current_index, edge_index};
        queue.push({next_cost, edge.target, edge_index, next_mask});
      }
    }
  }
  if (!goal.has_value()) {
    throw std::runtime_error(
        "tiny required-arc route is unexpectedly unreachable");
  }
  std::vector<std::string> reverse_path;
  auto cursor = *goal;
  while (cursor != start) {
    const auto predecessor = predecessors[cursor];
    if (!predecessor.has_value()) {
      throw std::runtime_error("tiny route predecessor chain is incomplete");
    }
    reverse_path.push_back(edges[predecessor->second].id);
    cursor = predecessor->first;
  }
  std::reverse(reverse_path.begin(), reverse_path.end());
  const double exact_cost = distances[*goal];
  const double brute_force_cost = brute_force_tiny_cost();
  return TinyRouteResult{
      reverse_path,
      exact_cost,
      brute_force_cost,
      validate_tiny_route(reverse_path, exact_cost),
      std::abs(exact_cost - brute_force_cost) <= 1e-12,
      explored,
  };
}

auto run_observability_spike(const std::filesystem::path &sequence_root,
                             const std::filesystem::path &road_graph_path,
                             const ObservabilityParameters &parameters)
    -> ObservabilitySpikeResult {
  validate_parameters(parameters);
  const auto start = std::chrono::steady_clock::now();
  const auto trajectory = load_trajectory(sequence_root);
  ObservabilitySpikeResult result;
  result.schema_version = "cartosentry.observability-spike.v1";
  result.spike_version = "m0.5-observability-v1";
  result.synthetic_scenarios = run_synthetic_observability_suite(parameters);
  result.public_alignment =
      public_alignment(sequence_root, trajectory, parameters);
  result.public_map_match =
      public_map_match(trajectory, road_graph_path, parameters);
  result.tiny_route = solve_tiny_required_route();
  result.elapsed_seconds =
      std::chrono::duration<double>(std::chrono::steady_clock::now() - start)
          .count();
  return result;
}

} // namespace cartosentry::spikes
