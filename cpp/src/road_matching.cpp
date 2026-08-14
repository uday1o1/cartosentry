#include "cartosentry/map/road_matching.hpp"

#include <algorithm>
#include <cmath>
#include <functional>
#include <limits>
#include <map>
#include <memory>
#include <numbers>
#include <optional>
#include <queue>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <variant>
#include <vector>

namespace cartosentry::matching {
namespace {

constexpr auto nanoseconds_per_second = 1'000'000'000.0;

auto finite(double value) -> bool { return std::isfinite(value); }

auto checked_round(double value, int decimal_places) -> double {
  if (!finite(value) || decimal_places < 0 || decimal_places > 15) {
    throw std::invalid_argument("native map rounding input is invalid");
  }
  const auto factor = std::pow(10.0, static_cast<double>(decimal_places));
  const auto scaled = value * factor;
  if (!finite(scaled)) {
    throw std::invalid_argument("native map rounding input overflows");
  }
  const auto lower = std::floor(scaled);
  const auto fraction = scaled - lower;
  const auto half_tie_tolerance = std::min(
      0.25, 8.0 * std::numeric_limits<double>::epsilon() *
                std::max(1.0, std::abs(scaled)));
  if (std::abs(fraction - 0.5) <= half_tie_tolerance) {
    const auto even_lower = std::fmod(std::abs(lower), 2.0) == 0.0;
    return (even_lower ? lower : lower + 1.0) / factor;
  }
  return std::floor(scaled + 0.5) / factor;
}

auto distance(Point2 left, Point2 right) -> double {
  return std::hypot(right.x - left.x, right.y - left.y);
}

auto validate_parameters(const CandidateParameters &candidate,
                         const EmissionParameters &emission) -> void {
  if (!finite(candidate.minimum_search_radius_m) ||
      !finite(candidate.default_search_radius_m) ||
      !finite(candidate.maximum_search_radius_m) ||
      !finite(candidate.uncertainty_radius_multiplier) ||
      candidate.minimum_search_radius_m <= 0.0 ||
      candidate.default_search_radius_m < candidate.minimum_search_radius_m ||
      candidate.maximum_search_radius_m < candidate.default_search_radius_m ||
      candidate.uncertainty_radius_multiplier <= 0.0 ||
      candidate.maximum_on_road_candidates == 0U ||
      candidate.distance_rounding_decimal_places < 0 ||
      candidate.distance_rounding_decimal_places > 12 ||
      !finite(emission.base_lateral_sigma_m) ||
      !finite(emission.maximum_lateral_sigma_m) ||
      !finite(emission.heading_sigma_rad) ||
      !finite(emission.heading_disabled_below_speed_mps) ||
      !finite(emission.heading_full_weight_speed_mps) ||
      !finite(emission.off_map_log_likelihood) ||
      emission.base_lateral_sigma_m <= 0.0 ||
      emission.maximum_lateral_sigma_m < emission.base_lateral_sigma_m ||
      emission.heading_sigma_rad <= 0.0 ||
      emission.heading_sigma_rad > std::numbers::pi ||
      emission.heading_disabled_below_speed_mps < 0.0 ||
      emission.heading_full_weight_speed_mps <=
          emission.heading_disabled_below_speed_mps ||
      emission.off_map_log_likelihood > 0.0 ||
      emission.score_rounding_decimal_places < 0 ||
      emission.score_rounding_decimal_places > 15) {
    throw std::invalid_argument("native map emission parameters are invalid");
  }
}

auto validate_graph(const Graph &graph) -> void {
  if (graph.arcs.empty()) {
    throw std::invalid_argument("native map graph has no arcs");
  }
  std::unordered_set<std::string> arc_ids;
  for (const auto &arc : graph.arcs) {
    if (arc.arc_id.empty() || arc.from_node_id.empty() ||
        arc.to_node_id.empty() || arc.source_way_id <= 0 ||
        !finite(arc.length_m) || arc.length_m <= 0.0 ||
        arc.geometry.size() < 2U || !arc_ids.insert(arc.arc_id).second) {
      throw std::invalid_argument("native map graph arc is invalid");
    }
    for (const auto &point : arc.geometry) {
      if (!finite(point.x) || !finite(point.y)) {
        throw std::invalid_argument("native map graph coordinate is not finite");
      }
    }
  }
  for (const auto &rule : graph.transition_rules) {
    if (!arc_ids.contains(rule.from_arc_id) ||
        !arc_ids.contains(rule.to_arc_id)) {
      throw std::invalid_argument("native map transition rule has a foreign arc");
    }
  }
}

auto validate_observations(const std::vector<Observation> &observations)
    -> void {
  if (observations.empty()) {
    throw std::invalid_argument("native map matching requires observations");
  }
  std::unordered_set<std::string> identities;
  for (const auto &observation : observations) {
    if (observation.observation_id.empty() ||
        !identities.insert(observation.observation_id).second ||
        !finite(observation.position.x) || !finite(observation.position.y) ||
        !finite(observation.speed_mps) || observation.speed_mps < 0.0 ||
        (observation.heading_rad.has_value() &&
         (!finite(*observation.heading_rad) ||
          *observation.heading_rad < -std::numbers::pi ||
          *observation.heading_rad > std::numbers::pi)) ||
        (observation.horizontal_uncertainty_m.has_value() &&
         (!finite(*observation.horizontal_uncertainty_m) ||
          *observation.horizontal_uncertainty_m <= 0.0))) {
      throw std::invalid_argument("native map observation is invalid");
    }
  }
}

auto validate_candidate_reference(const Graph &graph,
                                  const Candidate &candidate,
                                  std::size_t observation_index) -> void {
  if (candidate.candidate_id.empty() ||
      candidate.observation_index != observation_index ||
      !finite(candidate.emission.total_log_likelihood)) {
    throw std::invalid_argument("native map candidate reference is invalid");
  }
  if (!candidate.arc_index.has_value()) {
    if (candidate.along_arc_offset_m.has_value()) {
      throw std::invalid_argument("native off-map candidate has an arc offset");
    }
    return;
  }
  if (*candidate.arc_index >= graph.arcs.size() ||
      !candidate.along_arc_offset_m.has_value() ||
      !finite(*candidate.along_arc_offset_m) ||
      *candidate.along_arc_offset_m < 0.0 ||
      *candidate.along_arc_offset_m > graph.arcs[*candidate.arc_index].length_m) {
    throw std::invalid_argument("native on-road candidate is invalid");
  }
}

struct Projection {
  Point2 point;
  double lateral_distance_m{};
  double projected_geometry_distance_m{};
  double geometry_length_m{};
  double tangent_heading_rad{};
};

auto tangent_at(const std::vector<Point2> &geometry, double projected_distance)
    -> double {
  auto remaining = projected_distance;
  std::optional<Point2> fallback;
  for (std::size_t index = 1U; index < geometry.size(); ++index) {
    const auto delta = Point2{geometry[index].x - geometry[index - 1U].x,
                              geometry[index].y - geometry[index - 1U].y};
    const auto length = std::hypot(delta.x, delta.y);
    if (length == 0.0) {
      continue;
    }
    fallback = delta;
    if (remaining <= length) {
      return std::atan2(delta.y, delta.x);
    }
    remaining -= length;
  }
  if (!fallback.has_value()) {
    throw std::invalid_argument("native directed arc has no horizontal segment");
  }
  return std::atan2(fallback->y, fallback->x);
}

auto project(const Arc &arc, Point2 observation) -> Projection {
  auto geometry_length = 0.0;
  for (std::size_t index = 1U; index < arc.geometry.size(); ++index) {
    geometry_length += distance(arc.geometry[index - 1U], arc.geometry[index]);
  }
  if (!(geometry_length > 0.0) || !finite(geometry_length)) {
    throw std::invalid_argument("native directed arc has invalid geometry");
  }
  auto best_squared = std::numeric_limits<double>::infinity();
  auto best_point = arc.geometry.front();
  auto best_along = 0.0;
  auto accumulated = 0.0;
  for (std::size_t index = 1U; index < arc.geometry.size(); ++index) {
    const auto left = arc.geometry[index - 1U];
    const auto right = arc.geometry[index];
    const auto delta_x = right.x - left.x;
    const auto delta_y = right.y - left.y;
    const auto squared_length = delta_x * delta_x + delta_y * delta_y;
    if (squared_length == 0.0) {
      continue;
    }
    const auto segment_length = std::sqrt(squared_length);
    const auto unclamped = ((observation.x - left.x) * delta_x +
                            (observation.y - left.y) * delta_y) /
                           squared_length;
    const auto fraction = std::clamp(unclamped, 0.0, 1.0);
    const auto candidate =
        Point2{left.x + fraction * delta_x, left.y + fraction * delta_y};
    const auto difference_x = observation.x - candidate.x;
    const auto difference_y = observation.y - candidate.y;
    const auto squared =
        difference_x * difference_x + difference_y * difference_y;
    if (squared < best_squared) {
      best_squared = squared;
      best_point = candidate;
      best_along = accumulated + fraction * segment_length;
    }
    accumulated += segment_length;
  }
  if (!finite(best_squared)) {
    throw std::invalid_argument("native directed arc cannot be projected");
  }
  return Projection{best_point,
                    std::sqrt(best_squared),
                    best_along,
                    geometry_length,
                    tangent_at(arc.geometry, best_along)};
}

auto gaussian_log_likelihood(double residual, double sigma) -> double {
  return -0.5 * std::pow(residual / sigma, 2.0) -
         std::log(sigma * std::sqrt(2.0 * std::numbers::pi));
}

auto wrapped_heading_difference(double left, double right) -> double {
  return std::abs(std::remainder(left - right, 2.0 * std::numbers::pi));
}

auto heading_weight(double speed_mps, const EmissionParameters &parameters)
    -> double {
  if (speed_mps <= parameters.heading_disabled_below_speed_mps) {
    return 0.0;
  }
  const auto span = parameters.heading_full_weight_speed_mps -
                    parameters.heading_disabled_below_speed_mps;
  return std::min(
      1.0, (speed_mps - parameters.heading_disabled_below_speed_mps) / span);
}

auto search_radius(const Observation &observation,
                   const CandidateParameters &parameters) -> double {
  auto requested = parameters.default_search_radius_m;
  if (observation.horizontal_uncertainty_m.has_value()) {
    requested = std::max(
        requested, *observation.horizontal_uncertainty_m *
                       parameters.uncertainty_radius_multiplier);
  }
  return std::clamp(requested, parameters.minimum_search_radius_m,
                    parameters.maximum_search_radius_m);
}

auto make_on_road_candidate(std::size_t observation_index,
                            const Observation &observation,
                            std::size_t arc_index, const Arc &arc,
                            double radius_m,
                            const CandidateParameters &candidate_parameters,
                            const EmissionParameters &emission_parameters)
    -> std::optional<Candidate> {
  const auto projection = project(arc, observation.position);
  if (projection.lateral_distance_m > radius_m) {
    return std::nullopt;
  }
  const auto distance_places =
      candidate_parameters.distance_rounding_decimal_places;
  const auto score_places = emission_parameters.score_rounding_decimal_places;
  const auto lateral_distance =
      checked_round(projection.lateral_distance_m, distance_places);
  const auto along_offset = checked_round(
      std::min(arc.length_m,
               projection.projected_geometry_distance_m /
                   projection.geometry_length_m * arc.length_m),
      distance_places);
  const auto tangent =
      checked_round(projection.tangent_heading_rad, score_places);
  const auto uncertainty =
      observation.horizontal_uncertainty_m.value_or(0.0);
  const auto lateral_sigma =
      std::min(emission_parameters.maximum_lateral_sigma_m,
               std::hypot(emission_parameters.base_lateral_sigma_m,
                          uncertainty));
  const auto lateral_log =
      gaussian_log_likelihood(lateral_distance, lateral_sigma);
  const auto weight = observation.heading_rad.has_value()
                          ? heading_weight(observation.speed_mps,
                                           emission_parameters)
                          : 0.0;
  const auto heading_difference =
      weight > 0.0 && observation.heading_rad.has_value()
          ? std::optional<double>(wrapped_heading_difference(
                *observation.heading_rad, tangent))
          : std::nullopt;
  const auto heading_log =
      heading_difference.has_value()
          ? std::optional<double>(gaussian_log_likelihood(
                *heading_difference, emission_parameters.heading_sigma_rad))
          : std::nullopt;
  const auto total = lateral_log + weight * heading_log.value_or(0.0);
  Candidate result;
  result.observation_index = observation_index;
  result.arc_index = arc_index;
  result.projected_position = Point2{
      checked_round(projection.point.x, distance_places),
      checked_round(projection.point.y, distance_places)};
  result.lateral_distance_m = lateral_distance;
  result.tangent_heading_rad = tangent;
  result.along_arc_offset_m = along_offset;
  result.search_radius_m = radius_m;
  result.emission = EmissionResult{
      checked_round(lateral_sigma, score_places),
      checked_round(lateral_log, score_places),
      heading_difference.has_value(),
      checked_round(weight, score_places),
      heading_difference.has_value()
          ? std::optional<double>(std::min(
                std::numbers::pi,
                checked_round(*heading_difference, score_places)))
          : std::nullopt,
      heading_log.has_value()
          ? std::optional<double>(checked_round(*heading_log, score_places))
          : std::nullopt,
      std::nullopt,
      checked_round(total, score_places)};
  return result;
}

auto make_off_map_candidate(std::size_t observation_index, double radius_m,
                            const EmissionParameters &parameters) -> Candidate {
  Candidate result;
  result.observation_index = observation_index;
  result.search_radius_m = radius_m;
  result.emission.heading_weight = 0.0;
  result.emission.off_map_log_likelihood = parameters.off_map_log_likelihood;
  result.emission.total_log_likelihood = parameters.off_map_log_likelihood;
  return result;
}

auto blocked_transition(const Graph &graph, const std::string &from_arc_id,
                        const std::string &to_arc_id)
    -> std::optional<TransitionRejection> {
  auto unknown = false;
  for (const auto &rule : graph.transition_rules) {
    if (rule.from_arc_id != from_arc_id || rule.to_arc_id != to_arc_id) {
      continue;
    }
    if (rule.state == RuleState::forbidden) {
      return TransitionRejection::forbidden_turn;
    }
    unknown = true;
  }
  return unknown ? std::optional(TransitionRejection::unknown_restriction)
                 : std::nullopt;
}

auto count_u_turns(const Graph &graph,
                   const std::vector<std::size_t> &path) -> std::size_t {
  auto count = std::size_t{0U};
  for (std::size_t index = 1U; index < path.size(); ++index) {
    const auto &left = graph.arcs[path[index - 1U]];
    const auto &right = graph.arcs[path[index]];
    if (left.source_way_id == right.source_way_id &&
        left.direction != right.direction) {
      ++count;
    }
  }
  return count;
}

struct RouteResult {
  double distance_m{};
  std::size_t turn_count{};
  std::size_t u_turn_count{};
  std::vector<std::size_t> path;
};

struct QueueEntry {
  double distance_m{};
  std::size_t turns{};
  std::vector<std::size_t> path;
  std::string node_id;
  std::size_t incoming_arc_index{};
};

struct QueueGreater {
  auto operator()(const QueueEntry &left, const QueueEntry &right) const
      -> bool {
    return std::tie(left.distance_m, left.turns, left.path, left.node_id,
                    left.incoming_arc_index) >
           std::tie(right.distance_m, right.turns, right.path, right.node_id,
                    right.incoming_arc_index);
  }
};

struct BestRanking {
  double distance_m{};
  std::size_t turns{};
  std::vector<std::size_t> path;
};

auto route(const Graph &graph, const Candidate &previous,
           const Candidate &current, const TransitionParameters &parameters)
    -> std::variant<RouteResult, TransitionRejection> {
  if (!previous.arc_index.has_value() || !current.arc_index.has_value() ||
      !previous.along_arc_offset_m.has_value() ||
      !current.along_arc_offset_m.has_value() ||
      *previous.arc_index >= graph.arcs.size() ||
      *current.arc_index >= graph.arcs.size()) {
    throw std::invalid_argument("native on-road candidate is incomplete");
  }
  const auto previous_index = *previous.arc_index;
  const auto current_index = *current.arc_index;
  const auto &previous_arc = graph.arcs[previous_index];
  const auto &current_arc = graph.arcs[current_index];
  const auto previous_offset = *previous.along_arc_offset_m;
  const auto current_offset = *current.along_arc_offset_m;
  if (previous_index == current_index && current_offset >= previous_offset) {
    return RouteResult{current_offset - previous_offset, 0U, 0U,
                       {previous_index}};
  }
  std::unordered_map<std::string, std::vector<std::size_t>> outgoing;
  for (std::size_t index = 0U; index < graph.arcs.size(); ++index) {
    outgoing[graph.arcs[index].from_node_id].push_back(index);
  }
  const auto initial_distance = previous_arc.length_m - previous_offset;
  if (initial_distance > parameters.maximum_graph_search_distance_m) {
    return TransitionRejection::graph_search_budget;
  }
  std::priority_queue<QueueEntry, std::vector<QueueEntry>, QueueGreater> queue;
  queue.push(QueueEntry{initial_distance, 0U, {previous_index},
                        previous_arc.to_node_id, previous_index});
  std::map<std::pair<std::string, std::size_t>, BestRanking> best;
  std::set<TransitionRejection> target_rejections;
  auto visited = std::size_t{0U};
  while (!queue.empty()) {
    auto item = queue.top();
    queue.pop();
    const auto state = std::make_pair(item.node_id, item.incoming_arc_index);
    const auto ranking = BestRanking{item.distance_m, item.turns, item.path};
    const auto found = best.find(state);
    if (found != best.end() &&
        std::tie(ranking.distance_m, ranking.turns, ranking.path) >=
            std::tie(found->second.distance_m, found->second.turns,
                     found->second.path)) {
      continue;
    }
    best[state] = ranking;
    ++visited;
    if (visited > parameters.maximum_graph_search_states) {
      return TransitionRejection::graph_search_budget;
    }
    if (item.node_id == current_arc.from_node_id) {
      const auto blocked = blocked_transition(
          graph, graph.arcs[item.incoming_arc_index].arc_id,
          current_arc.arc_id);
      if (!blocked.has_value()) {
        auto result_path = item.path;
        if (result_path.back() != current_index) {
          result_path.push_back(current_index);
        }
        const auto total = item.distance_m + current_offset;
        if (total > parameters.maximum_graph_search_distance_m) {
          return TransitionRejection::graph_search_budget;
        }
        return RouteResult{total, result_path.size() - 1U,
                           count_u_turns(graph, result_path),
                           std::move(result_path)};
      }
      target_rejections.insert(*blocked);
    }
    const auto outgoing_found = outgoing.find(item.node_id);
    if (outgoing_found == outgoing.end()) {
      continue;
    }
    for (const auto next_index : outgoing_found->second) {
      const auto &next = graph.arcs[next_index];
      if (blocked_transition(graph,
                             graph.arcs[item.incoming_arc_index].arc_id,
                             next.arc_id)
              .has_value()) {
        continue;
      }
      const auto next_distance = item.distance_m + next.length_m;
      if (next_distance > parameters.maximum_graph_search_distance_m) {
        continue;
      }
      auto next_path = item.path;
      next_path.push_back(next_index);
      queue.push(QueueEntry{next_distance, item.turns + 1U,
                            std::move(next_path), next.to_node_id, next_index});
    }
  }
  if (target_rejections.contains(TransitionRejection::forbidden_turn)) {
    return TransitionRejection::forbidden_turn;
  }
  if (target_rejections.contains(
          TransitionRejection::unknown_restriction)) {
    return TransitionRejection::unknown_restriction;
  }
  return TransitionRejection::no_directed_path;
}

auto validate_transition_parameters(const TransitionParameters &parameters)
    -> void {
  if (!finite(parameters.path_discrepancy_scale_m) ||
      !finite(parameters.maximum_absolute_speed_mps) ||
      !finite(parameters.observed_speed_excess_allowance_mps) ||
      !finite(parameters.speed_excess_penalty_per_mps) ||
      !finite(parameters.turn_penalty) || !finite(parameters.u_turn_penalty) ||
      !finite(parameters.off_map_enter_log_likelihood) ||
      !finite(parameters.off_map_exit_log_likelihood) ||
      !finite(parameters.off_map_stay_log_likelihood) ||
      !finite(parameters.maximum_graph_search_distance_m) ||
      parameters.path_discrepancy_scale_m <= 0.0 ||
      parameters.maximum_absolute_speed_mps <= 0.0 ||
      parameters.observed_speed_excess_allowance_mps < 0.0 ||
      parameters.speed_excess_penalty_per_mps < 0.0 ||
      parameters.turn_penalty < 0.0 || parameters.u_turn_penalty < 0.0 ||
      parameters.off_map_enter_log_likelihood > 0.0 ||
      parameters.off_map_exit_log_likelihood > 0.0 ||
      parameters.off_map_stay_log_likelihood > 0.0 ||
      parameters.maximum_graph_search_distance_m <= 0.0 ||
      parameters.maximum_graph_search_states == 0U ||
      parameters.score_rounding_decimal_places < 0 ||
      parameters.score_rounding_decimal_places > 15) {
    throw std::invalid_argument("native map transition parameters are invalid");
  }
}

auto impossible(TransitionRejection reason, double elapsed_seconds,
                double observed_displacement_m,
                std::optional<double> graph_distance_m = std::nullopt,
                std::optional<double> implied_speed_mps = std::nullopt,
                std::vector<std::string> path = {}) -> TransitionResult {
  TransitionResult result;
  result.possible = false;
  result.rejection_reason = reason;
  result.elapsed_seconds = std::max(0.0, elapsed_seconds);
  result.observed_displacement_m = observed_displacement_m;
  result.graph_distance_m = graph_distance_m;
  result.implied_graph_speed_mps = implied_speed_mps;
  result.path_arc_ids = std::move(path);
  return result;
}

auto validate_decoder_parameters(const DecoderParameters &parameters) -> void {
  if (parameters.beam_width == 0U ||
      parameters.hypotheses_per_terminal_candidate == 0U ||
      !finite(parameters.beam_score_delta_log_likelihood) ||
      parameters.beam_score_delta_log_likelihood < 0.0 ||
      !finite(parameters.ambiguity_path_separation_log_likelihood) ||
      parameters.ambiguity_path_separation_log_likelihood < 0.0 ||
      !finite(parameters.stationary_speed_threshold_mps) ||
      parameters.stationary_speed_threshold_mps < 0.0 ||
      !finite(parameters.stationary_position_tolerance_m) ||
      parameters.stationary_position_tolerance_m < 0.0 ||
      parameters.stationary_minimum_observations < 2U ||
      parameters.maximum_sequence_observations == 0U ||
      parameters.score_rounding_decimal_places < 0 ||
      parameters.score_rounding_decimal_places > 15) {
    throw std::invalid_argument("native map decoder parameters are invalid");
  }
}

struct Hypothesis {
  std::size_t observation_index{};
  std::size_t candidate_index{};
  double total_score{};
  std::size_t lexicographic_rank{};
  std::shared_ptr<const Hypothesis> previous;
};

auto assign_lexicographic_ranks(std::vector<std::shared_ptr<Hypothesis>> &beam,
                                const std::vector<std::vector<Candidate>> &batches)
    -> void {
  auto ranked = beam;
  std::sort(ranked.begin(), ranked.end(), [&batches](const auto &left,
                                                     const auto &right) {
    const auto left_previous =
        left->previous ? left->previous->lexicographic_rank : 0U;
    const auto right_previous =
        right->previous ? right->previous->lexicographic_rank : 0U;
    const auto &left_id =
        batches[left->observation_index][left->candidate_index].candidate_id;
    const auto &right_id =
        batches[right->observation_index][right->candidate_index].candidate_id;
    return std::tie(left_previous, left_id) <
           std::tie(right_previous, right_id);
  });
  for (std::size_t index = 0U; index < ranked.size(); ++index) {
    ranked[index]->lexicographic_rank = index;
  }
}

auto reconstruct(const std::shared_ptr<const Hypothesis> &hypothesis)
    -> std::vector<std::size_t> {
  std::vector<std::size_t> reversed;
  auto current = hypothesis;
  while (current) {
    reversed.push_back(current->candidate_index);
    current = current->previous;
  }
  std::reverse(reversed.begin(), reversed.end());
  return reversed;
}

auto stationary_flags(const std::vector<Observation> &observations,
                      const DecoderParameters &parameters)
    -> std::vector<bool> {
  std::vector<bool> flags(observations.size(), false);
  auto cursor = std::size_t{0U};
  while (cursor < observations.size()) {
    if (observations[cursor].speed_mps >
        parameters.stationary_speed_threshold_mps) {
      ++cursor;
      continue;
    }
    auto end = cursor + 1U;
    while (end < observations.size() &&
           observations[end].speed_mps <=
               parameters.stationary_speed_threshold_mps) {
      ++end;
    }
    auto within_tolerance = true;
    for (auto index = cursor; index < end; ++index) {
      within_tolerance =
          within_tolerance &&
          distance(observations[cursor].position,
                   observations[index].position) <=
              parameters.stationary_position_tolerance_m;
    }
    if (end - cursor >= parameters.stationary_minimum_observations &&
        within_tolerance) {
      std::fill(flags.begin() + static_cast<std::ptrdiff_t>(cursor),
                flags.begin() + static_cast<std::ptrdiff_t>(end), true);
    }
    cursor = end;
  }
  return flags;
}

} // namespace

auto generate_candidate_batches(const Graph &graph,
                                const std::vector<Observation> &observations,
                                const CandidateParameters &candidate_parameters,
                                const EmissionParameters &emission_parameters)
    -> std::vector<std::vector<Candidate>> {
  validate_graph(graph);
  validate_observations(observations);
  validate_parameters(candidate_parameters, emission_parameters);
  std::vector<std::vector<Candidate>> result;
  result.reserve(observations.size());
  for (std::size_t observation_index = 0U;
       observation_index < observations.size(); ++observation_index) {
    const auto &observation = observations[observation_index];
    const auto radius = search_radius(observation, candidate_parameters);
    std::vector<Candidate> candidates;
    for (std::size_t arc_index = 0U; arc_index < graph.arcs.size();
         ++arc_index) {
      auto candidate = make_on_road_candidate(
          observation_index, observation, arc_index, graph.arcs[arc_index],
          radius, candidate_parameters, emission_parameters);
      if (candidate.has_value()) {
        candidates.push_back(std::move(*candidate));
      }
    }
    std::sort(candidates.begin(), candidates.end(), [&graph](const auto &left,
                                                             const auto &right) {
      const auto &left_arc = graph.arcs[*left.arc_index];
      const auto &right_arc = graph.arcs[*right.arc_index];
      if (left.emission.total_log_likelihood !=
          right.emission.total_log_likelihood) {
        return left.emission.total_log_likelihood >
               right.emission.total_log_likelihood;
      }
      if (*left.lateral_distance_m != *right.lateral_distance_m) {
        return *left.lateral_distance_m < *right.lateral_distance_m;
      }
      return left_arc.arc_id < right_arc.arc_id;
    });
    if (candidates.size() >
        candidate_parameters.maximum_on_road_candidates) {
      candidates.resize(candidate_parameters.maximum_on_road_candidates);
    }
    candidates.push_back(
        make_off_map_candidate(observation_index, radius, emission_parameters));
    result.push_back(std::move(candidates));
  }
  return result;
}

auto best_emission_candidate_index(const std::vector<Candidate> &candidates)
    -> std::size_t {
  if (candidates.empty()) {
    throw std::invalid_argument("native emission selection requires candidates");
  }
  auto selected = std::size_t{0U};
  std::unordered_set<std::string> candidate_ids;
  for (std::size_t index = 0U; index < candidates.size(); ++index) {
    const auto &candidate = candidates[index];
    if (candidate.candidate_id.empty() ||
        !candidate_ids.insert(candidate.candidate_id).second ||
        !finite(candidate.emission.total_log_likelihood)) {
      throw std::invalid_argument(
          "native emission selection candidate is invalid");
    }
    const auto &best = candidates[selected];
    if (candidate.emission.total_log_likelihood >
            best.emission.total_log_likelihood ||
        (candidate.emission.total_log_likelihood ==
             best.emission.total_log_likelihood &&
         candidate.candidate_id < best.candidate_id)) {
      selected = index;
    }
  }
  return selected;
}

static auto score_transition_impl(
    const Graph &graph, const std::vector<Observation> &observations,
    const TransitionQuery &query,
    const CandidateParameters &candidate_parameters,
    const TransitionParameters &transition_parameters)
    -> TransitionResult {
  if (candidate_parameters.distance_rounding_decimal_places < 0 ||
      candidate_parameters.distance_rounding_decimal_places > 12 ||
      query.previous_observation_index >= observations.size() ||
      query.current_observation_index >= observations.size()) {
    throw std::invalid_argument("native map transition query is invalid");
  }
  validate_candidate_reference(graph, query.previous,
                               query.previous_observation_index);
  validate_candidate_reference(graph, query.current,
                               query.current_observation_index);
  const auto &previous_observation =
      observations[query.previous_observation_index];
  const auto &current_observation = observations[query.current_observation_index];
  const auto observed_displacement =
      distance(previous_observation.position, current_observation.position);
  if (!finite(observed_displacement)) {
    throw std::invalid_argument(
        "native map transition displacement is not finite");
  }
  const auto previous_time_ns = previous_observation.time_ns;
  const auto current_time_ns = current_observation.time_ns;
  if ((previous_time_ns < 0 &&
       current_time_ns >
           std::numeric_limits<std::int64_t>::max() + previous_time_ns) ||
      (previous_time_ns > 0 &&
       current_time_ns <
           std::numeric_limits<std::int64_t>::min() + previous_time_ns)) {
    throw std::invalid_argument("native map transition elapsed time overflows");
  }
  const auto elapsed_ns = current_time_ns - previous_time_ns;
  const auto elapsed_seconds =
      static_cast<double>(elapsed_ns) / nanoseconds_per_second;
  if (elapsed_seconds <= 0.0) {
    return impossible(TransitionRejection::non_positive_elapsed_time,
                      elapsed_seconds, observed_displacement);
  }
  const auto places = transition_parameters.score_rounding_decimal_places;
  if (query.previous.off_map() || query.current.off_map()) {
    const auto off_map_log =
        query.previous.off_map() == query.current.off_map()
            ? transition_parameters.off_map_stay_log_likelihood
            : (query.previous.off_map()
                   ? transition_parameters.off_map_exit_log_likelihood
                   : transition_parameters.off_map_enter_log_likelihood);
    const auto rounded = checked_round(off_map_log, places);
    TransitionResult result;
    result.possible = true;
    result.elapsed_seconds = elapsed_seconds;
    result.observed_displacement_m = observed_displacement;
    result.off_map_log_likelihood = rounded;
    result.total_log_likelihood = rounded;
    return result;
  }
  const auto route_result =
      route(graph, query.previous, query.current, transition_parameters);
  if (std::holds_alternative<TransitionRejection>(route_result)) {
    return impossible(std::get<TransitionRejection>(route_result),
                      elapsed_seconds, observed_displacement);
  }
  const auto &matched_route = std::get<RouteResult>(route_result);
  const auto graph_distance = checked_round(
      matched_route.distance_m,
      candidate_parameters.distance_rounding_decimal_places);
  const auto implied_speed = graph_distance / elapsed_seconds;
  std::vector<std::string> path_ids;
  path_ids.reserve(matched_route.path.size());
  for (const auto index : matched_route.path) {
    path_ids.push_back(graph.arcs[index].arc_id);
  }
  if (implied_speed > transition_parameters.maximum_absolute_speed_mps) {
    return impossible(TransitionRejection::implausible_absolute_speed,
                      elapsed_seconds, observed_displacement, graph_distance,
                      implied_speed, std::move(path_ids));
  }
  const auto discrepancy =
      std::abs(graph_distance - observed_displacement);
  const auto path_log =
      -discrepancy / transition_parameters.path_discrepancy_scale_m;
  const auto supported_speed =
      std::max(previous_observation.speed_mps, current_observation.speed_mps);
  const auto speed_excess = std::max(
      0.0, implied_speed - supported_speed -
               transition_parameters.observed_speed_excess_allowance_mps);
  const auto speed_log =
      -speed_excess * transition_parameters.speed_excess_penalty_per_mps;
  const auto turn_log =
      -(static_cast<double>(matched_route.turn_count) *
            transition_parameters.turn_penalty +
        static_cast<double>(matched_route.u_turn_count) *
            transition_parameters.u_turn_penalty);
  TransitionResult result;
  result.possible = true;
  result.elapsed_seconds = elapsed_seconds;
  result.observed_displacement_m = observed_displacement;
  result.graph_distance_m = graph_distance;
  result.implied_graph_speed_mps = checked_round(implied_speed, places);
  result.path_discrepancy_m = checked_round(discrepancy, places);
  result.turn_count = matched_route.turn_count;
  result.u_turn_count = matched_route.u_turn_count;
  result.path_arc_ids = std::move(path_ids);
  result.path_log_likelihood = checked_round(path_log, places);
  result.speed_log_likelihood = checked_round(speed_log, places);
  result.turn_log_likelihood = checked_round(turn_log, places);
  result.total_log_likelihood =
      checked_round(path_log + speed_log + turn_log, places);
  return result;
}

auto score_transition(const Graph &graph,
                      const std::vector<Observation> &observations,
                      const TransitionQuery &query,
                      const CandidateParameters &candidate_parameters,
                      const TransitionParameters &transition_parameters)
    -> TransitionResult {
  validate_graph(graph);
  validate_observations(observations);
  validate_transition_parameters(transition_parameters);
  return score_transition_impl(graph, observations, query,
                               candidate_parameters,
                               transition_parameters);
}

auto score_transition_batch(
    const Graph &graph, const std::vector<Observation> &observations,
    const std::vector<TransitionQuery> &queries,
    const CandidateParameters &candidate_parameters,
    const TransitionParameters &transition_parameters)
    -> std::vector<TransitionResult> {
  validate_graph(graph);
  validate_observations(observations);
  validate_transition_parameters(transition_parameters);
  std::vector<TransitionResult> result;
  result.reserve(queries.size());
  for (const auto &query : queries) {
    result.push_back(score_transition_impl(graph, observations, query,
                                           candidate_parameters,
                                           transition_parameters));
  }
  return result;
}

auto decode_candidate_batches(
    const Graph &graph, const std::vector<Observation> &observations,
    const std::vector<std::vector<Candidate>> &candidate_batches,
    const CandidateParameters &candidate_parameters,
    const TransitionParameters &transition_parameters,
    const DecoderParameters &decoder_parameters) -> DecodeResult {
  validate_graph(graph);
  validate_observations(observations);
  validate_transition_parameters(transition_parameters);
  validate_decoder_parameters(decoder_parameters);
  if (observations.size() > decoder_parameters.maximum_sequence_observations ||
      candidate_batches.size() != observations.size()) {
    throw std::invalid_argument("native map decoder input exceeds its contract");
  }
  for (std::size_t observation_index = 0U;
       observation_index < observations.size(); ++observation_index) {
    if (observation_index > 0U &&
        observations[observation_index].time_ns <=
            observations[observation_index - 1U].time_ns) {
      throw std::invalid_argument(
          "native map decoder observations are not time ordered");
    }
    if (candidate_batches[observation_index].empty()) {
      throw std::invalid_argument("native map decoder has an empty candidate set");
    }
    std::unordered_set<std::string> candidate_ids;
    for (const auto &candidate : candidate_batches[observation_index]) {
      validate_candidate_reference(graph, candidate, observation_index);
      if (!candidate_ids.insert(candidate.candidate_id).second) {
        throw std::invalid_argument(
            "native map decoder candidate identities are not unique");
      }
    }
  }
  const auto places = decoder_parameters.score_rounding_decimal_places;
  std::vector<std::shared_ptr<Hypothesis>> beam;
  const auto &initial = candidate_batches.front();
  beam.reserve(initial.size());
  for (std::size_t candidate_index = 0U; candidate_index < initial.size();
       ++candidate_index) {
    auto hypothesis = std::make_shared<Hypothesis>();
    hypothesis->candidate_index = candidate_index;
    hypothesis->total_score = checked_round(
        initial[candidate_index].emission.total_log_likelihood, places);
    beam.push_back(std::move(hypothesis));
  }
  assign_lexicographic_ranks(beam, candidate_batches);
  const auto hypothesis_order = [](const auto &left, const auto &right) {
    if (left->total_score != right->total_score) {
      return left->total_score > right->total_score;
    }
    return left->lexicographic_rank < right->lexicographic_rank;
  };
  std::sort(beam.begin(), beam.end(), hypothesis_order);
  const auto initial_count = beam.size();
  if (beam.size() > decoder_parameters.beam_width) {
    beam.resize(decoder_parameters.beam_width);
  }
  DecodeResult result;
  result.diagnostics.generated_candidate_counts.reserve(observations.size());
  for (const auto &batch : candidate_batches) {
    result.diagnostics.generated_candidate_counts.push_back(batch.size());
  }
  result.diagnostics.retained_hypothesis_counts.push_back(beam.size());
  result.diagnostics.rejected_transition_counts.push_back(0U);
  result.diagnostics.pruned_hypothesis_counts.push_back(initial_count -
                                                         beam.size());

  for (std::size_t observation_index = 1U;
       observation_index < observations.size(); ++observation_index) {
    std::vector<std::shared_ptr<Hypothesis>> expanded;
    auto rejected = std::size_t{0U};
    for (const auto &previous : beam) {
      for (std::size_t candidate_index = 0U;
           candidate_index < candidate_batches[observation_index].size();
           ++candidate_index) {
        const auto transition = score_transition_impl(
            graph, observations,
            TransitionQuery{observation_index - 1U,
                            candidate_batches[observation_index - 1U]
                                             [previous->candidate_index],
                            observation_index,
                            candidate_batches[observation_index]
                                             [candidate_index]},
            candidate_parameters, transition_parameters);
        if (!transition.possible) {
          ++rejected;
          continue;
        }
        auto hypothesis = std::make_shared<Hypothesis>();
        hypothesis->observation_index = observation_index;
        hypothesis->candidate_index = candidate_index;
        hypothesis->total_score = checked_round(
            previous->total_score + *transition.total_log_likelihood +
                candidate_batches[observation_index][candidate_index]
                    .emission.total_log_likelihood,
            places);
        hypothesis->previous = previous;
        expanded.push_back(std::move(hypothesis));
      }
    }
    if (expanded.empty()) {
      throw std::invalid_argument(
          "native map decoder has no graph-valid path hypothesis");
    }
    assign_lexicographic_ranks(expanded, candidate_batches);
    std::sort(expanded.begin(), expanded.end(), hypothesis_order);
    std::unordered_map<std::string, std::size_t> per_terminal;
    std::vector<std::shared_ptr<Hypothesis>> terminal_pruned;
    for (const auto &hypothesis : expanded) {
      const auto &identity =
          candidate_batches[observation_index][hypothesis->candidate_index]
              .candidate_id;
      auto &count = per_terminal[identity];
      if (count >= decoder_parameters.hypotheses_per_terminal_candidate) {
        continue;
      }
      ++count;
      terminal_pruned.push_back(hypothesis);
    }
    const auto best_score = terminal_pruned.front()->total_score;
    beam.clear();
    for (const auto &hypothesis : terminal_pruned) {
      if (hypothesis->total_score <
          best_score - decoder_parameters.beam_score_delta_log_likelihood) {
        continue;
      }
      if (beam.size() == decoder_parameters.beam_width) {
        break;
      }
      beam.push_back(hypothesis);
    }
    assign_lexicographic_ranks(beam, candidate_batches);
    result.diagnostics.retained_hypothesis_counts.push_back(beam.size());
    result.diagnostics.rejected_transition_counts.push_back(rejected);
    result.diagnostics.pruned_hypothesis_counts.push_back(expanded.size() -
                                                           beam.size());
  }
  std::sort(beam.begin(), beam.end(), hypothesis_order);
  result.best_candidate_indices = reconstruct(beam.front());
  result.best_total_log_likelihood = beam.front()->total_score;
  if (beam.size() > 1U) {
    result.runner_up_candidate_indices = reconstruct(beam[1U]);
    result.runner_up_total_log_likelihood = beam[1U]->total_score;
    result.path_separation_log_likelihood = checked_round(
        beam.front()->total_score - beam[1U]->total_score, places);
  }
  result.ambiguous = result.path_separation_log_likelihood.has_value() &&
                     *result.path_separation_log_likelihood <=
                         decoder_parameters
                             .ambiguity_path_separation_log_likelihood;
  result.point_ambiguous.assign(observations.size(), false);
  if (!result.runner_up_candidate_indices.empty()) {
    for (std::size_t index = 0U; index < observations.size(); ++index) {
      const auto &best = candidate_batches[index]
                                         [result.best_candidate_indices[index]];
      const auto &runner =
          candidate_batches[index][result.runner_up_candidate_indices[index]];
      result.point_ambiguous[index] =
          result.ambiguous && best.candidate_id != runner.candidate_id;
    }
  }
  result.stationary = stationary_flags(observations, decoder_parameters);
  auto cursor = std::size_t{0U};
  while (cursor < observations.size()) {
    const auto candidate_index = result.best_candidate_indices[cursor];
    const auto &first = candidate_batches[cursor][candidate_index];
    const auto stationary = result.stationary[cursor];
    const auto ambiguous = result.point_ambiguous[cursor];
    auto end = cursor + 1U;
    while (end < observations.size()) {
      const auto &item =
          candidate_batches[end][result.best_candidate_indices[end]];
      if (item.off_map() != first.off_map() ||
          item.arc_index != first.arc_index || result.stationary[end] != stationary ||
          result.point_ambiguous[end] != ambiguous) {
        break;
      }
      ++end;
    }
    auto usable_distance = 0.0;
    if (!first.off_map() && !stationary && !ambiguous) {
      for (auto index = cursor + 1U; index < end; ++index) {
        const auto &left = candidate_batches[index - 1U]
                                           [result.best_candidate_indices[index - 1U]];
        const auto &right = candidate_batches[index]
                                            [result.best_candidate_indices[index]];
        usable_distance += std::abs(*right.along_arc_offset_m -
                                    *left.along_arc_offset_m);
      }
    }
    auto has_runner_difference = false;
    if (!result.runner_up_candidate_indices.empty()) {
      for (auto index = cursor; index < end; ++index) {
        const auto &best = candidate_batches[index]
                                           [result.best_candidate_indices[index]];
        const auto &runner =
            candidate_batches[index][result.runner_up_candidate_indices[index]];
        has_runner_difference =
            has_runner_difference || best.candidate_id != runner.candidate_id;
      }
    }
    result.intervals.push_back(DecodedInterval{
        cursor, end, checked_round(usable_distance, 6),
        has_runner_difference ? result.path_separation_log_likelihood
                              : std::nullopt});
    cursor = end;
  }
  return result;
}

} // namespace cartosentry::matching
