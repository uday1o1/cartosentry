#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace cartosentry::ingest {

inline constexpr std::size_t kBoreasLidarRecordBytes = 6U * sizeof(float);
inline constexpr std::size_t kMaximumBoreasLidarFrameBytes =
    16U * 1024U * 1024U;
inline constexpr std::size_t kMaximumBoreasLidarFrames = 100'000U;

class BoreasFormatError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

struct GeographicBounds {
  double minimum_latitude_deg{};
  double maximum_latitude_deg{};
  double minimum_longitude_deg{};
  double maximum_longitude_deg{};
};

struct MatrixSummary {
  std::string source_key;
  std::string target_frame;
  std::string source_frame;
  std::array<double, 16> row_major_values{};
  double rotation_orthonormality_error{};
  double rotation_determinant{};
};

struct LidarFrameSummary {
  std::string frame_id;
  std::uint64_t source_bytes{};
  std::uint64_t point_count{};
  std::int64_t scan_midpoint_ns{};
  std::int64_t first_point_ns{};
  std::int64_t last_point_ns{};
  double minimum_relative_time_seconds{};
  double maximum_relative_time_seconds{};
  std::uint32_t minimum_relative_time_bits{};
  std::uint32_t maximum_relative_time_bits{};
  std::uint32_t minimum_laser_id{};
  std::uint32_t maximum_laser_id{};
  bool timestamps_nondecreasing{};
  bool required_fields_finite{};
};

struct LidarFrameParseResult {
  LidarFrameSummary frame;
  double maximum_time_conversion_error_ns{};
};

struct BoreasLidarRecord {
  std::array<float, 6> values{};
  std::array<std::uint32_t, 6> bits{};
};

struct LidarSummary {
  std::string coordinate_frame;
  std::string record_layout;
  std::string byte_order;
  std::string relative_time_unit;
  std::string relative_time_reference;
  std::string relative_time_rounding;
  double maximum_time_conversion_error_ns{};
  std::uint64_t total_points{};
  std::uint64_t total_bytes{};
  std::int64_t first_point_ns{};
  std::int64_t last_point_ns{};
  std::vector<LidarFrameSummary> frames;
};

struct TrajectorySummary {
  std::string source_key;
  std::string position_frame;
  std::string pose_target_frame;
  std::string pose_source_frame;
  std::string pose_convention;
  std::string time_epoch;
  std::string time_reference;
  std::string raw_time_unit;
  std::string normalized_time_unit;
  std::string angular_input_unit;
  std::string angular_output_unit;
  std::string angular_conversion;
  std::string vertical_datum;
  std::uint64_t row_count{};
  std::uint64_t clip_row_count{};
  std::int64_t first_time_ns{};
  std::int64_t last_time_ns{};
  std::int64_t clip_first_time_ns{};
  std::int64_t clip_last_time_ns{};
  GeographicBounds wgs84_bounds;
  GeographicBounds clip_wgs84_bounds;
  std::array<double, 3> enu_minimum_m{};
  std::array<double, 3> enu_maximum_m{};
  std::array<double, 2> local_origin_deg{};
  double maximum_local_coordinate_magnitude_m{};
  double maximum_local_float32_quantization_m{};
  double maximum_global_ecef_float32_quantization_m{};
  double maximum_wgs84_local_roundtrip_error_m{};
  std::uint64_t route_crosscheck_sample_count{};
  std::uint64_t route_polyline_point_count{};
  std::uint64_t route_sample_stride_rows{};
  double route_crosscheck_p95_m{};
  double route_crosscheck_maximum_m{};
  bool road_region_contains_trajectory{};
};

struct LidarPoseSummary {
  std::string source_key;
  std::uint64_t row_count{};
  std::uint64_t selected_frame_matches{};
  std::int64_t first_time_ns{};
  std::int64_t last_time_ns{};
  std::string target_frame;
  std::string source_frame;
};

struct BoreasInspectionResult {
  std::string schema_version;
  std::string adapter_version;
  std::string sequence_id;
  TrajectorySummary trajectory;
  LidarSummary lidar;
  LidarPoseSummary lidar_poses;
  std::vector<MatrixSummary> calibrations;
  std::uint64_t unique_input_bytes{};
  std::uint64_t peak_rss_bytes{};
  double elapsed_seconds{};
};

auto parse_decimal_seconds_to_nanoseconds(
    std::string_view lexeme, std::string_view source_key, std::size_t row_number,
    std::string_view field_name) -> std::int64_t;

auto parse_boreas_lidar_frame(std::span<const std::byte> content,
                              std::string_view frame_id)
    -> LidarFrameParseResult;

auto decode_boreas_lidar_record(std::span<const std::byte> content,
                                std::string_view source_key,
                                std::size_t row_number) -> BoreasLidarRecord;

auto inspect_boreas_sequence(
    const std::filesystem::path& sequence_root,
    const std::filesystem::path& route_html_path,
    const GeographicBounds& road_region,
    std::size_t route_sample_stride_rows) -> BoreasInspectionResult;

}  // namespace cartosentry::ingest
