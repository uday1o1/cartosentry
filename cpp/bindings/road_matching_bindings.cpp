#include "road_matching_bindings.hpp"

#include "cartosentry/map/road_matching.hpp"

#include <pybind11/stl.h>

#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace py = pybind11;
namespace matching = cartosentry::matching;

namespace {

template <typename Value>
auto required(const py::dict &value, const char *key) -> Value {
  if (!value.contains(key)) {
    throw std::invalid_argument(std::string("native map input is missing ") + key);
  }
  return value[key].cast<Value>();
}

auto optional_double(const py::dict &value, const char *key)
    -> std::optional<double> {
  if (!value.contains(key) || value[key].is_none()) {
    return std::nullopt;
  }
  return value[key].cast<double>();
}

auto graph_from_python(const py::dict &value) -> matching::Graph {
  matching::Graph result;
  for (const auto item : required<py::list>(value, "arcs")) {
    const auto raw = item.cast<py::dict>();
    matching::Arc arc;
    arc.arc_id = required<std::string>(raw, "arc_id");
    arc.from_node_id = required<std::string>(raw, "from_node_id");
    arc.to_node_id = required<std::string>(raw, "to_node_id");
    arc.source_way_id = required<std::int64_t>(raw, "source_way_id");
    const auto direction = required<std::string>(raw, "direction");
    if (direction == "FORWARD") {
      arc.direction = matching::ArcDirection::forward;
    } else if (direction == "REVERSE") {
      arc.direction = matching::ArcDirection::reverse;
    } else {
      throw std::invalid_argument("native map arc direction is invalid");
    }
    arc.length_m = required<double>(raw, "length_m");
    for (const auto point_item : required<py::list>(raw, "geometry")) {
      const auto point = point_item.cast<std::vector<double>>();
      if (point.size() != 2U) {
        throw std::invalid_argument("native map point must have two values");
      }
      arc.geometry.push_back(matching::Point2{point[0], point[1]});
    }
    result.arcs.push_back(std::move(arc));
  }
  for (const auto item : required<py::list>(value, "transition_rules")) {
    const auto raw = item.cast<py::dict>();
    matching::TransitionRule rule;
    rule.from_arc_id = required<std::string>(raw, "from_arc_id");
    rule.to_arc_id = required<std::string>(raw, "to_arc_id");
    const auto state = required<std::string>(raw, "state");
    if (state == "FORBIDDEN") {
      rule.state = matching::RuleState::forbidden;
    } else if (state == "UNKNOWN_RESTRICTION") {
      rule.state = matching::RuleState::unknown_restriction;
    } else {
      throw std::invalid_argument("native map transition rule is invalid");
    }
    result.transition_rules.push_back(std::move(rule));
  }
  return result;
}

auto observations_from_python(const py::list &values)
    -> std::vector<matching::Observation> {
  std::vector<matching::Observation> result;
  result.reserve(values.size());
  for (const auto item : values) {
    const auto raw = item.cast<py::dict>();
    const auto position =
        required<std::vector<double>>(raw, "position_local_m");
    if (position.size() != 2U) {
      throw std::invalid_argument(
          "native map observation position must have two values");
    }
    matching::Observation observation;
    observation.observation_id =
        required<std::string>(raw, "observation_id");
    observation.time_ns = required<std::int64_t>(raw, "time_ns");
    observation.position = matching::Point2{position[0], position[1]};
    observation.heading_rad = optional_double(raw, "heading_rad");
    observation.speed_mps = required<double>(raw, "speed_mps");
    observation.horizontal_uncertainty_m =
        optional_double(raw, "horizontal_uncertainty_m");
    result.push_back(std::move(observation));
  }
  return result;
}

auto candidate_parameters_from_python(const py::dict &value)
    -> matching::CandidateParameters {
  return matching::CandidateParameters{
      required<double>(value, "minimum_search_radius_m"),
      required<double>(value, "default_search_radius_m"),
      required<double>(value, "maximum_search_radius_m"),
      required<double>(value, "uncertainty_radius_multiplier"),
      required<std::size_t>(value, "maximum_on_road_candidates"),
      required<int>(value, "distance_rounding_decimal_places")};
}

auto emission_parameters_from_python(const py::dict &value)
    -> matching::EmissionParameters {
  return matching::EmissionParameters{
      required<double>(value, "base_lateral_sigma_m"),
      required<double>(value, "maximum_lateral_sigma_m"),
      required<double>(value, "heading_sigma_rad"),
      required<double>(value, "heading_disabled_below_speed_mps"),
      required<double>(value, "heading_full_weight_speed_mps"),
      required<double>(value, "off_map_log_likelihood"),
      required<int>(value, "score_rounding_decimal_places")};
}

auto transition_parameters_from_python(const py::dict &value)
    -> matching::TransitionParameters {
  return matching::TransitionParameters{
      required<double>(value, "path_discrepancy_scale_m"),
      required<double>(value, "maximum_absolute_speed_mps"),
      required<double>(value, "observed_speed_excess_allowance_mps"),
      required<double>(value, "speed_excess_penalty_per_mps"),
      required<double>(value, "turn_penalty"),
      required<double>(value, "u_turn_penalty"),
      required<double>(value, "off_map_enter_log_likelihood"),
      required<double>(value, "off_map_exit_log_likelihood"),
      required<double>(value, "off_map_stay_log_likelihood"),
      required<double>(value, "maximum_graph_search_distance_m"),
      required<std::size_t>(value, "maximum_graph_search_states"),
      required<int>(value, "score_rounding_decimal_places")};
}

auto decoder_parameters_from_python(const py::dict &value)
    -> matching::DecoderParameters {
  return matching::DecoderParameters{
      required<std::size_t>(value, "beam_width"),
      required<std::size_t>(value, "hypotheses_per_terminal_candidate"),
      required<double>(value, "beam_score_delta_log_likelihood"),
      required<double>(value,
                       "ambiguity_path_separation_log_likelihood"),
      required<double>(value, "stationary_speed_threshold_mps"),
      required<double>(value, "stationary_position_tolerance_m"),
      required<std::size_t>(value, "stationary_minimum_observations"),
      required<std::size_t>(value, "maximum_sequence_observations"),
      required<int>(value, "score_rounding_decimal_places")};
}

auto candidate_to_python(const matching::Candidate &candidate,
                         const matching::Graph &graph) -> py::dict {
  py::dict result;
  result["observation_index"] = candidate.observation_index;
  if (candidate.arc_index.has_value()) {
    result["arc_index"] = *candidate.arc_index;
    result["directed_arc_id"] = graph.arcs[*candidate.arc_index].arc_id;
    result["source_way_id"] = graph.arcs[*candidate.arc_index].source_way_id;
    result["projected_position_local_m"] =
        std::vector{candidate.projected_position->x,
                    candidate.projected_position->y};
    result["lateral_distance_m"] = *candidate.lateral_distance_m;
    result["tangent_heading_rad"] = *candidate.tangent_heading_rad;
    result["along_arc_offset_m"] = *candidate.along_arc_offset_m;
  } else {
    result["arc_index"] = py::none();
    result["directed_arc_id"] = py::none();
    result["source_way_id"] = py::none();
    result["projected_position_local_m"] = py::none();
    result["lateral_distance_m"] = py::none();
    result["tangent_heading_rad"] = py::none();
    result["along_arc_offset_m"] = py::none();
  }
  result["search_radius_m"] = candidate.search_radius_m;
  py::dict emission;
  emission["lateral_sigma_m"] = candidate.emission.lateral_sigma_m;
  emission["lateral_log_likelihood"] =
      candidate.emission.lateral_log_likelihood;
  emission["heading_used"] = candidate.emission.heading_used;
  emission["heading_weight"] = candidate.emission.heading_weight;
  emission["heading_difference_rad"] =
      candidate.emission.heading_difference_rad;
  emission["heading_log_likelihood"] =
      candidate.emission.heading_log_likelihood;
  emission["off_map_log_likelihood"] =
      candidate.emission.off_map_log_likelihood;
  emission["total_log_likelihood"] =
      candidate.emission.total_log_likelihood;
  result["emission"] = std::move(emission);
  return result;
}

auto candidate_from_python(const py::dict &value) -> matching::Candidate {
  matching::Candidate result;
  result.candidate_id = required<std::string>(value, "candidate_id");
  result.observation_index =
      required<std::size_t>(value, "observation_index");
  if (!value.contains("arc_index") || value["arc_index"].is_none()) {
    result.arc_index = std::nullopt;
  } else {
    result.arc_index = value["arc_index"].cast<std::size_t>();
    result.along_arc_offset_m = required<double>(value, "along_arc_offset_m");
  }
  result.emission.total_log_likelihood =
      required<double>(value, "emission_total_log_likelihood");
  return result;
}

auto rejection_to_string(matching::TransitionRejection value) -> std::string {
  switch (value) {
  case matching::TransitionRejection::non_positive_elapsed_time:
    return "NON_POSITIVE_ELAPSED_TIME";
  case matching::TransitionRejection::forbidden_turn:
    return "FORBIDDEN_TURN";
  case matching::TransitionRejection::unknown_restriction:
    return "UNKNOWN_RESTRICTION";
  case matching::TransitionRejection::no_directed_path:
    return "NO_DIRECTED_PATH";
  case matching::TransitionRejection::graph_search_budget:
    return "GRAPH_SEARCH_BUDGET";
  case matching::TransitionRejection::implausible_absolute_speed:
    return "IMPLAUSIBLE_ABSOLUTE_SPEED";
  }
  throw std::invalid_argument("native map rejection is invalid");
}

template <typename Value>
void set_optional(py::dict &target, const char *key,
                  const std::optional<Value> &value) {
  if (value.has_value()) {
    target[key] = *value;
  } else {
    target[key] = py::none();
  }
}

auto transition_to_python(const matching::TransitionResult &transition)
    -> py::dict {
  py::dict result;
  result["possible"] = transition.possible;
  if (transition.rejection_reason.has_value()) {
    result["rejection_reason"] =
        rejection_to_string(*transition.rejection_reason);
  } else {
    result["rejection_reason"] = py::none();
  }
  result["elapsed_seconds"] = transition.elapsed_seconds;
  result["observed_displacement_m"] = transition.observed_displacement_m;
  set_optional(result, "graph_distance_m", transition.graph_distance_m);
  set_optional(result, "implied_graph_speed_mps",
               transition.implied_graph_speed_mps);
  set_optional(result, "path_discrepancy_m", transition.path_discrepancy_m);
  set_optional(result, "turn_count", transition.turn_count);
  set_optional(result, "u_turn_count", transition.u_turn_count);
  result["path_arc_ids"] = transition.path_arc_ids;
  set_optional(result, "path_log_likelihood",
               transition.path_log_likelihood);
  set_optional(result, "speed_log_likelihood",
               transition.speed_log_likelihood);
  set_optional(result, "turn_log_likelihood",
               transition.turn_log_likelihood);
  set_optional(result, "off_map_log_likelihood",
               transition.off_map_log_likelihood);
  set_optional(result, "total_log_likelihood",
               transition.total_log_likelihood);
  return result;
}

auto candidates_from_python(const py::list &batches)
    -> std::vector<std::vector<matching::Candidate>> {
  std::vector<std::vector<matching::Candidate>> result;
  result.reserve(batches.size());
  for (const auto batch_item : batches) {
    const auto batch = batch_item.cast<py::list>();
    std::vector<matching::Candidate> candidates;
    candidates.reserve(batch.size());
    for (const auto item : batch) {
      candidates.push_back(candidate_from_python(item.cast<py::dict>()));
    }
    result.push_back(std::move(candidates));
  }
  return result;
}

auto decode_to_python(const matching::DecodeResult &decoded) -> py::dict {
  py::dict result;
  result["best_candidate_indices"] = decoded.best_candidate_indices;
  result["runner_up_candidate_indices"] =
      decoded.runner_up_candidate_indices;
  result["best_total_log_likelihood"] =
      decoded.best_total_log_likelihood;
  set_optional(result, "runner_up_total_log_likelihood",
               decoded.runner_up_total_log_likelihood);
  set_optional(result, "path_separation_log_likelihood",
               decoded.path_separation_log_likelihood);
  result["ambiguous"] = decoded.ambiguous;
  result["point_ambiguous"] = decoded.point_ambiguous;
  result["stationary"] = decoded.stationary;
  py::list intervals;
  for (const auto &interval : decoded.intervals) {
    py::dict item;
    item["start_observation_index"] = interval.start_observation_index;
    item["end_observation_index_exclusive"] =
        interval.end_observation_index_exclusive;
    item["usable_distance_m"] = interval.usable_distance_m;
    set_optional(item, "path_separation_log_likelihood",
                 interval.path_separation_log_likelihood);
    intervals.append(std::move(item));
  }
  result["intervals"] = std::move(intervals);
  py::dict diagnostics;
  diagnostics["generated_candidate_counts"] =
      decoded.diagnostics.generated_candidate_counts;
  diagnostics["retained_hypothesis_counts"] =
      decoded.diagnostics.retained_hypothesis_counts;
  diagnostics["rejected_transition_counts"] =
      decoded.diagnostics.rejected_transition_counts;
  diagnostics["pruned_hypothesis_counts"] =
      decoded.diagnostics.pruned_hypothesis_counts;
  result["diagnostics"] = std::move(diagnostics);
  return result;
}

} // namespace

void bind_road_matching(py::module_ &module) {
  module.def(
      "select_best_road_emission_candidate",
      [](const py::list &candidate_values) {
        std::vector<matching::Candidate> candidates;
        candidates.reserve(candidate_values.size());
        for (const auto item : candidate_values) {
          const auto raw = item.cast<py::dict>();
          matching::Candidate candidate;
          candidate.candidate_id =
              required<std::string>(raw, "candidate_id");
          candidate.emission.total_log_likelihood =
              required<double>(raw, "emission_total_log_likelihood");
          candidates.push_back(std::move(candidate));
        }
        std::size_t selected{};
        {
          py::gil_scoped_release release;
          selected = matching::best_emission_candidate_index(candidates);
        }
        return selected;
      },
      py::arg("candidates"));
  module.def(
      "generate_road_candidate_batches",
      [](const py::dict &graph_value, const py::list &observation_values,
         const py::dict &candidate_parameter_values,
         const py::dict &emission_parameter_values) {
        const auto graph = graph_from_python(graph_value);
        const auto observations =
            observations_from_python(observation_values);
        const auto candidate_parameters =
            candidate_parameters_from_python(candidate_parameter_values);
        const auto emission_parameters =
            emission_parameters_from_python(emission_parameter_values);
        std::vector<std::vector<matching::Candidate>> batches;
        {
          py::gil_scoped_release release;
          batches = matching::generate_candidate_batches(
              graph, observations, candidate_parameters, emission_parameters);
        }
        py::list result;
        for (const auto &batch : batches) {
          py::list candidates;
          for (const auto &candidate : batch) {
            candidates.append(candidate_to_python(candidate, graph));
          }
          result.append(std::move(candidates));
        }
        return result;
      },
      py::arg("graph"), py::arg("observations"),
      py::arg("candidate_parameters"), py::arg("emission_parameters"));
  module.def(
      "score_road_transition_batch",
      [](const py::dict &graph_value, const py::list &observation_values,
         const py::list &query_values,
         const py::dict &candidate_parameter_values,
         const py::dict &transition_parameter_values) {
        const auto graph = graph_from_python(graph_value);
        const auto observations =
            observations_from_python(observation_values);
        const auto candidate_parameters =
            candidate_parameters_from_python(candidate_parameter_values);
        const auto transition_parameters =
            transition_parameters_from_python(transition_parameter_values);
        std::vector<matching::TransitionQuery> queries;
        queries.reserve(query_values.size());
        for (const auto item : query_values) {
          const auto raw = item.cast<py::dict>();
          queries.push_back(matching::TransitionQuery{
              required<std::size_t>(raw, "previous_observation_index"),
              candidate_from_python(
                  required<py::dict>(raw, "previous_candidate")),
              required<std::size_t>(raw, "current_observation_index"),
              candidate_from_python(
                  required<py::dict>(raw, "current_candidate"))});
        }
        std::vector<matching::TransitionResult> transitions;
        {
          py::gil_scoped_release release;
          transitions = matching::score_transition_batch(
              graph, observations, queries, candidate_parameters,
              transition_parameters);
        }
        py::list result;
        for (const auto &transition : transitions) {
          result.append(transition_to_python(transition));
        }
        return result;
      },
      py::arg("graph"), py::arg("observations"), py::arg("queries"),
      py::arg("candidate_parameters"), py::arg("transition_parameters"));
  module.def(
      "decode_road_candidate_batches",
      [](const py::dict &graph_value, const py::list &observation_values,
         const py::list &candidate_batch_values,
         const py::dict &candidate_parameter_values,
         const py::dict &transition_parameter_values,
         const py::dict &decoder_parameter_values) {
        const auto graph = graph_from_python(graph_value);
        const auto observations =
            observations_from_python(observation_values);
        const auto candidates =
            candidates_from_python(candidate_batch_values);
        const auto candidate_parameters =
            candidate_parameters_from_python(candidate_parameter_values);
        const auto transition_parameters =
            transition_parameters_from_python(transition_parameter_values);
        const auto decoder_parameters =
            decoder_parameters_from_python(decoder_parameter_values);
        matching::DecodeResult decoded;
        {
          py::gil_scoped_release release;
          decoded = matching::decode_candidate_batches(
              graph, observations, candidates, candidate_parameters,
              transition_parameters, decoder_parameters);
        }
        return decode_to_python(decoded);
      },
      py::arg("graph"), py::arg("observations"),
      py::arg("candidate_batches"), py::arg("candidate_parameters"),
      py::arg("transition_parameters"), py::arg("decoder_parameters"));
}
