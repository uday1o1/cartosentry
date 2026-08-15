#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace cartosentry::bins {

enum class ArcDirection { forward, reverse };

enum class Modality { camera, gnss, imu, lidar, radar, trajectory };

struct Arc {
  std::string arc_id;
  ArcDirection direction{ArcDirection::forward};
  double length_m{};
};

struct MatchedPoint {
  std::int64_t time_ns{};
  std::optional<std::size_t> arc_index;
  std::optional<double> along_arc_offset_m;
  bool confident{};
  bool stationary{};
  double speed_mps{};
  std::optional<double> heading_rad;
};

struct MatchedPath {
  std::string road_match_id;
  std::string sequence_id;
  std::string source_group_id;
  std::vector<MatchedPoint> points;
};

struct ModalityEvidence {
  std::string evidence_id;
  std::string sequence_id;
  Modality modality{Modality::trajectory};
  std::int64_t start_time_ns{};
  std::int64_t end_time_ns{};
  bool usable{};
  double point_count{};
  std::optional<double> overlap_support_m;
  bool timestamp_supported{};
};

struct FindingInterval {
  std::string finding_id;
  std::string sequence_id;
  std::int64_t start_time_ns{};
  std::int64_t end_time_ns{};
  bool critical{};
};

struct Parameters {
  double bin_length_m{};
  std::int64_t independent_traversal_minimum_gap_ns{};
  std::size_t maximum_paths{};
  std::size_t maximum_points_per_path{};
  std::size_t maximum_total_points{};
  std::size_t maximum_generated_bins{};
  std::size_t maximum_modality_evidence_intervals{};
  std::size_t maximum_findings{};
  int distance_rounding_decimal_places{};
};

struct ModalityAggregate {
  Modality modality{Modality::trajectory};
  std::int64_t valid_duration_ns{};
  double point_support{};
  std::optional<double> mean_overlap_support_m;
  std::int64_t timestamp_supported_duration_ns{};
  std::vector<std::string> evidence_ids;
};

struct TraversalCoverage {
  std::size_t arc_index{};
  std::size_t longitudinal_bin_index{};
  std::string sequence_id;
  std::string source_group_id;
  std::size_t traversal_ordinal{};
  std::int64_t first_time_ns{};
  std::int64_t last_time_ns{};
  double entry_offset_m{};
  double exit_offset_m{};
  std::int64_t usable_duration_ns{};
  double usable_distance_m{};
  std::size_t speed_sample_count{};
  double minimum_speed_mps{};
  double mean_speed_mps{};
  double maximum_speed_mps{};
  double yaw_excitation_rad{};
  std::vector<std::string> road_match_ids;
  std::vector<ModalityAggregate> modalities;
  std::vector<std::string> finding_ids;
  std::vector<std::string> critical_finding_ids;
};

struct BinCoverage {
  std::size_t arc_index{};
  std::size_t longitudinal_bin_index{};
  double start_offset_m{};
  double end_offset_m{};
  std::int64_t usable_duration_ns{};
  double usable_distance_m{};
  std::size_t independent_traversal_count{};
  std::size_t speed_sample_count{};
  std::optional<double> minimum_speed_mps;
  std::optional<double> mean_speed_mps;
  std::optional<double> maximum_speed_mps;
  double yaw_excitation_rad{};
  std::vector<TraversalCoverage> traversals;
  std::vector<ModalityAggregate> modalities;
  std::vector<std::string> finding_ids;
  std::vector<std::string> critical_finding_ids;
};

struct FindingLocalization {
  std::string finding_id;
  std::vector<std::size_t> bin_result_indices;
};

struct AggregationResult {
  std::vector<BinCoverage> bins;
  std::vector<FindingLocalization> finding_localizations;
};

auto aggregate_directed_road_bins(
    const std::vector<Arc> &arcs, const std::vector<MatchedPath> &paths,
    const std::vector<ModalityEvidence> &modality_evidence,
    const std::vector<FindingInterval> &findings,
    const Parameters &parameters) -> AggregationResult;

} // namespace cartosentry::bins
