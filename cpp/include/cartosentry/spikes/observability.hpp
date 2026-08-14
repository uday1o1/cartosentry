#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace cartosentry::spikes {

struct ObservabilityParameters {
  std::int64_t injected_point_time_shift_ns{};
  double injected_trajectory_shift_m{};
  std::size_t lidar_point_stride{};
  std::size_t map_trajectory_stride_rows{};
  double candidate_search_radius_m{};
  double confident_lateral_distance_m{};
  double confident_heading_error_rad{};
  double confident_score_separation{};
  double minimum_moving_speed_mps{};
  double minimum_alignment_separation_m{};
};

struct SyntheticScenarioResult {
  std::string scenario_id;
  std::string observability;
  bool moving{};
  bool structured{};
  double clean_alignment_rmse_m{};
  double point_time_shift_alignment_rmse_m{};
  double trajectory_shift_alignment_rmse_m{};
  bool point_time_shift_separated{};
  bool trajectory_shift_separated{};
};

struct PublicAlignmentResult {
  std::string sequence_id;
  std::string point_time_source;
  std::string trajectory_pose_convention;
  std::uint64_t lidar_frames{};
  std::uint64_t sampled_points{};
  double minimum_speed_mps{};
  double maximum_speed_mps{};
  double heading_change_rad{};
  double clean_alignment_mean_m{};
  double point_time_shift_alignment_mean_m{};
  double trajectory_shift_alignment_mean_m{};
  double point_time_transform_effect_mean_m{};
  double trajectory_transform_effect_mean_m{};
  bool observable_motion{};
  bool observable_structure{};
  bool point_time_shift_separated{};
  bool trajectory_shift_separated{};
};

struct PublicMapMatchResult {
  std::string graph_import_profile;
  std::string distance_coverage_method;
  std::uint64_t imported_nodes{};
  std::uint64_t imported_ways{};
  std::uint64_t imported_directed_arcs{};
  std::uint64_t excluded_ways{};
  std::uint64_t moving_observations{};
  std::uint64_t confident_observations{};
  double candidate_moving_distance_m{};
  double confident_moving_distance_m{};
  double confident_distance_fraction{};
  double confident_lateral_p95_m{};
};

struct TinyRouteResult {
  std::vector<std::string> exact_arc_path;
  double exact_cost{};
  double brute_force_cost{};
  bool exact_route_valid{};
  bool exact_matches_brute_force{};
  std::uint64_t explored_states{};
};

struct ObservabilitySpikeResult {
  std::string schema_version;
  std::string spike_version;
  std::vector<SyntheticScenarioResult> synthetic_scenarios;
  PublicAlignmentResult public_alignment;
  PublicMapMatchResult public_map_match;
  TinyRouteResult tiny_route;
  double elapsed_seconds{};
};

auto run_synthetic_observability_suite(
    const ObservabilityParameters &parameters)
    -> std::vector<SyntheticScenarioResult>;

auto solve_tiny_required_route() -> TinyRouteResult;

auto run_observability_spike(const std::filesystem::path &sequence_root,
                             const std::filesystem::path &road_graph_path,
                             const ObservabilityParameters &parameters)
    -> ObservabilitySpikeResult;

} // namespace cartosentry::spikes
