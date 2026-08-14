#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace cartosentry::matching {

struct Point2 {
  double x{};
  double y{};
};

enum class ArcDirection { forward, reverse };
enum class RuleState { forbidden, unknown_restriction };

struct Arc {
  std::string arc_id;
  std::string from_node_id;
  std::string to_node_id;
  std::int64_t source_way_id{};
  ArcDirection direction{ArcDirection::forward};
  double length_m{};
  std::vector<Point2> geometry;
};

struct TransitionRule {
  std::string from_arc_id;
  std::string to_arc_id;
  RuleState state{RuleState::forbidden};
};

struct Graph {
  std::vector<Arc> arcs;
  std::vector<TransitionRule> transition_rules;
};

struct Observation {
  std::string observation_id;
  std::int64_t time_ns{};
  Point2 position;
  std::optional<double> heading_rad;
  double speed_mps{};
  std::optional<double> horizontal_uncertainty_m;
};

struct CandidateParameters {
  double minimum_search_radius_m{};
  double default_search_radius_m{};
  double maximum_search_radius_m{};
  double uncertainty_radius_multiplier{};
  std::size_t maximum_on_road_candidates{};
  int distance_rounding_decimal_places{};
};

struct EmissionParameters {
  double base_lateral_sigma_m{};
  double maximum_lateral_sigma_m{};
  double heading_sigma_rad{};
  double heading_disabled_below_speed_mps{};
  double heading_full_weight_speed_mps{};
  double off_map_log_likelihood{};
  int score_rounding_decimal_places{};
};

struct TransitionParameters {
  double path_discrepancy_scale_m{};
  double maximum_absolute_speed_mps{};
  double observed_speed_excess_allowance_mps{};
  double speed_excess_penalty_per_mps{};
  double turn_penalty{};
  double u_turn_penalty{};
  double off_map_enter_log_likelihood{};
  double off_map_exit_log_likelihood{};
  double off_map_stay_log_likelihood{};
  double maximum_graph_search_distance_m{};
  std::size_t maximum_graph_search_states{};
  int score_rounding_decimal_places{};
};

struct DecoderParameters {
  std::size_t beam_width{};
  std::size_t hypotheses_per_terminal_candidate{};
  double beam_score_delta_log_likelihood{};
  double ambiguity_path_separation_log_likelihood{};
  double stationary_speed_threshold_mps{};
  double stationary_position_tolerance_m{};
  std::size_t stationary_minimum_observations{};
  std::size_t maximum_sequence_observations{};
  int score_rounding_decimal_places{};
};

struct EmissionResult {
  std::optional<double> lateral_sigma_m;
  std::optional<double> lateral_log_likelihood;
  bool heading_used{};
  double heading_weight{};
  std::optional<double> heading_difference_rad;
  std::optional<double> heading_log_likelihood;
  std::optional<double> off_map_log_likelihood;
  double total_log_likelihood{};
};

struct Candidate {
  std::string candidate_id;
  std::size_t observation_index{};
  std::optional<std::size_t> arc_index;
  std::optional<Point2> projected_position;
  std::optional<double> lateral_distance_m;
  std::optional<double> tangent_heading_rad;
  std::optional<double> along_arc_offset_m;
  double search_radius_m{};
  EmissionResult emission;

  [[nodiscard]] auto off_map() const -> bool { return !arc_index.has_value(); }
};

enum class TransitionRejection {
  non_positive_elapsed_time,
  forbidden_turn,
  unknown_restriction,
  no_directed_path,
  graph_search_budget,
  implausible_absolute_speed,
};

struct TransitionResult {
  bool possible{};
  std::optional<TransitionRejection> rejection_reason;
  double elapsed_seconds{};
  double observed_displacement_m{};
  std::optional<double> graph_distance_m;
  std::optional<double> implied_graph_speed_mps;
  std::optional<double> path_discrepancy_m;
  std::optional<std::size_t> turn_count;
  std::optional<std::size_t> u_turn_count;
  std::vector<std::string> path_arc_ids;
  std::optional<double> path_log_likelihood;
  std::optional<double> speed_log_likelihood;
  std::optional<double> turn_log_likelihood;
  std::optional<double> off_map_log_likelihood;
  std::optional<double> total_log_likelihood;
};

struct TransitionQuery {
  std::size_t previous_observation_index{};
  Candidate previous;
  std::size_t current_observation_index{};
  Candidate current;
};

struct DecoderDiagnostics {
  std::vector<std::size_t> generated_candidate_counts;
  std::vector<std::size_t> retained_hypothesis_counts;
  std::vector<std::size_t> rejected_transition_counts;
  std::vector<std::size_t> pruned_hypothesis_counts;
};

struct DecodedInterval {
  std::size_t start_observation_index{};
  std::size_t end_observation_index_exclusive{};
  double usable_distance_m{};
  std::optional<double> path_separation_log_likelihood;
};

struct DecodeResult {
  std::vector<std::size_t> best_candidate_indices;
  std::vector<std::size_t> runner_up_candidate_indices;
  double best_total_log_likelihood{};
  std::optional<double> runner_up_total_log_likelihood;
  std::optional<double> path_separation_log_likelihood;
  bool ambiguous{};
  std::vector<bool> point_ambiguous;
  std::vector<bool> stationary;
  std::vector<DecodedInterval> intervals;
  DecoderDiagnostics diagnostics;
};

auto generate_candidate_batches(const Graph &graph,
                                const std::vector<Observation> &observations,
                                const CandidateParameters &candidate_parameters,
                                const EmissionParameters &emission_parameters)
    -> std::vector<std::vector<Candidate>>;

auto best_emission_candidate_index(const std::vector<Candidate> &candidates)
    -> std::size_t;

auto score_transition(const Graph &graph,
                      const std::vector<Observation> &observations,
                      const TransitionQuery &query,
                      const CandidateParameters &candidate_parameters,
                      const TransitionParameters &transition_parameters)
    -> TransitionResult;

auto score_transition_batch(
    const Graph &graph, const std::vector<Observation> &observations,
    const std::vector<TransitionQuery> &queries,
    const CandidateParameters &candidate_parameters,
    const TransitionParameters &transition_parameters)
    -> std::vector<TransitionResult>;

auto decode_candidate_batches(
    const Graph &graph, const std::vector<Observation> &observations,
    const std::vector<std::vector<Candidate>> &candidate_batches,
    const CandidateParameters &candidate_parameters,
    const TransitionParameters &transition_parameters,
    const DecoderParameters &decoder_parameters) -> DecodeResult;

} // namespace cartosentry::matching
