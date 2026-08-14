#include "cartosentry/map/road_matching.hpp"

#include <catch2/catch_test_macros.hpp>

#include <cstddef>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace matching = cartosentry::matching;

namespace {

auto candidate_parameters() -> matching::CandidateParameters {
  return matching::CandidateParameters{5.0, 15.0, 75.0, 3.0, 16U, 6};
}

auto emission_parameters() -> matching::EmissionParameters {
  return matching::EmissionParameters{4.0, 30.0, 0.5235987755982988,
                                      1.0, 3.0, -5.0, 12};
}

auto transition_parameters() -> matching::TransitionParameters {
  return matching::TransitionParameters{20.0, 60.0, 15.0, 0.25, 0.2, 2.0,
                                        -2.5, -2.5, -1.0, 2000.0,
                                        100000U, 12};
}

auto decoder_parameters() -> matching::DecoderParameters {
  return matching::DecoderParameters{64U, 2U, 50.0, 1.0, 0.5,
                                     1.0, 2U, 100000U, 12};
}

auto one_arc_graph() -> matching::Graph {
  matching::Graph graph;
  graph.arcs.push_back(matching::Arc{
      "arc-forward", "node-a", "node-b", 1,
      matching::ArcDirection::forward, 100.0, {{0.0, 0.0}, {100.0, 0.0}}});
  return graph;
}

auto observation(std::string identity, std::int64_t time_ns, double x,
                 double y, double speed = 10.0) -> matching::Observation {
  return matching::Observation{std::move(identity), time_ns, {x, y}, 0.0,
                               speed, std::nullopt};
}

auto identify(std::vector<std::vector<matching::Candidate>> batches)
    -> std::vector<std::vector<matching::Candidate>> {
  for (std::size_t observation_index = 0U;
       observation_index < batches.size(); ++observation_index) {
    for (std::size_t candidate_index = 0U;
         candidate_index < batches[observation_index].size();
         ++candidate_index) {
      batches[observation_index][candidate_index].candidate_id =
          "candidate-" + std::to_string(observation_index) + "-" +
          std::to_string(candidate_index);
    }
  }
  return batches;
}

} // namespace

TEST_CASE("native road candidates retain directed projection evidence") {
  const auto graph = one_arc_graph();
  const std::vector observations{observation("observation-0", 0, 50.0, 2.0)};
  const auto batches = matching::generate_candidate_batches(
      graph, observations, candidate_parameters(), emission_parameters());
  REQUIRE(batches.size() == 1U);
  REQUIRE(batches.front().size() == 2U);
  const auto &on_road = batches.front().front();
  CHECK_FALSE(on_road.off_map());
  CHECK(on_road.arc_index == 0U);
  CHECK(on_road.projected_position->x == 50.0);
  CHECK(on_road.projected_position->y == 0.0);
  CHECK(on_road.lateral_distance_m == 2.0);
  CHECK(on_road.along_arc_offset_m == 50.0);
  CHECK(on_road.emission.heading_used);
  CHECK(batches.front().back().off_map());
  CHECK(batches.front().back().emission.total_log_likelihood == -5.0);
}

TEST_CASE("native map quantization resolves decimal half ties to even") {
  const auto graph = one_arc_graph();
  const std::vector observations{
      observation("even-lower", 0, 50.0000005, 0.0),
      observation("odd-lower", 1, 50.0000015, 0.0),
  };
  const auto batches = matching::generate_candidate_batches(
      graph, observations, candidate_parameters(), emission_parameters());
  REQUIRE(batches.size() == 2U);
  CHECK(batches[0U][0U].along_arc_offset_m == 50.0);
  CHECK(batches[1U][0U].along_arc_offset_m == 50.000002);
}

TEST_CASE("native emission selection owns deterministic identity ties") {
  auto candidates = std::vector<matching::Candidate>(3U);
  candidates[0U].candidate_id = "candidate-z";
  candidates[0U].emission.total_log_likelihood = -2.0;
  candidates[1U].candidate_id = "candidate-b";
  candidates[1U].emission.total_log_likelihood = -1.0;
  candidates[2U].candidate_id = "candidate-a";
  candidates[2U].emission.total_log_likelihood = -1.0;
  CHECK(matching::best_emission_candidate_index(candidates) == 2U);

  candidates[1U].candidate_id = "candidate-a";
  CHECK_THROWS_AS(matching::best_emission_candidate_index(candidates),
                  std::invalid_argument);
}

TEST_CASE("native transition scoring preserves direction and off-map state") {
  const auto graph = one_arc_graph();
  const std::vector observations{
      observation("observation-0", 0, 20.0, 0.0),
      observation("observation-1", 10'000'000'000, 50.0, 0.0)};
  const auto batches = identify(matching::generate_candidate_batches(
      graph, observations, candidate_parameters(), emission_parameters()));
  const auto forward = matching::score_transition(
      graph, observations,
      matching::TransitionQuery{0U, batches[0U][0U], 1U, batches[1U][0U]},
      candidate_parameters(), transition_parameters());
  REQUIRE(forward.possible);
  CHECK(forward.graph_distance_m == 30.0);
  CHECK(forward.turn_count == 0U);
  CHECK(forward.path_arc_ids == std::vector<std::string>{"arc-forward"});

  const auto reverse = matching::score_transition(
      graph, observations,
      matching::TransitionQuery{1U, batches[1U][0U], 0U, batches[0U][0U]},
      candidate_parameters(), transition_parameters());
  CHECK_FALSE(reverse.possible);
  CHECK(reverse.rejection_reason ==
        matching::TransitionRejection::non_positive_elapsed_time);

  const auto off_map = matching::score_transition(
      graph, observations,
      matching::TransitionQuery{0U, batches[0U].back(), 1U,
                                batches[1U].back()},
      candidate_parameters(), transition_parameters());
  REQUIRE(off_map.possible);
  CHECK(off_map.total_log_likelihood == -1.0);
}

TEST_CASE("native Viterbi decoder emits exact intervals and ambiguity") {
  auto graph = one_arc_graph();
  const std::vector observations{
      observation("observation-0", 0, 10.0, 0.0),
      observation("observation-1", 10'000'000'000, 40.0, 0.0),
      observation("observation-2", 20'000'000'000, 70.0, 0.0)};
  auto batches = identify(matching::generate_candidate_batches(
      graph, observations, candidate_parameters(), emission_parameters()));
  const auto decoded = matching::decode_candidate_batches(
      graph, observations, batches, candidate_parameters(),
      transition_parameters(), decoder_parameters());
  CHECK(decoded.best_candidate_indices ==
        std::vector<std::size_t>{0U, 0U, 0U});
  REQUIRE(decoded.intervals.size() == 1U);
  CHECK(decoded.intervals.front().usable_distance_m == 60.0);
  CHECK_FALSE(decoded.ambiguous);

  graph.arcs.push_back(matching::Arc{
      "arc-parallel", "node-c", "node-d", 2,
      matching::ArcDirection::forward, 100.0,
      {{0.0, 10.0}, {100.0, 10.0}}});
  const std::vector midpoint_observations{
      observation("midpoint-0", 0, 10.0, 5.0),
      observation("midpoint-1", 10'000'000'000, 40.0, 5.0),
      observation("midpoint-2", 20'000'000'000, 70.0, 5.0)};
  batches = identify(matching::generate_candidate_batches(
      graph, midpoint_observations, candidate_parameters(),
      emission_parameters()));
  const auto ambiguous = matching::decode_candidate_batches(
      graph, midpoint_observations, batches, candidate_parameters(),
      transition_parameters(), decoder_parameters());
  CHECK(ambiguous.ambiguous);
  CHECK(ambiguous.path_separation_log_likelihood == 0.0);
  CHECK(ambiguous.point_ambiguous ==
        std::vector<bool>{true, true, true});
  CHECK(ambiguous.intervals.size() == 1U);
  CHECK(ambiguous.intervals.front().usable_distance_m == 0.0);
}

TEST_CASE("native road matching rejects hostile numeric and sequence inputs") {
  const auto graph = one_arc_graph();
  auto observations =
      std::vector{observation("observation-0", 0, 10.0, 0.0)};
  observations.front().position.x = std::numeric_limits<double>::quiet_NaN();
  CHECK_THROWS_AS(matching::generate_candidate_batches(
                      graph, observations, candidate_parameters(),
                      emission_parameters()),
                  std::invalid_argument);

  observations = {
      observation("observation-0", 10'000'000'000, 10.0, 0.0),
      observation("observation-1", 0, 20.0, 0.0),
  };
  auto batches = identify(matching::generate_candidate_batches(
      graph, observations, candidate_parameters(), emission_parameters()));
  CHECK_THROWS_AS(matching::decode_candidate_batches(
                      graph, observations, batches, candidate_parameters(),
                      transition_parameters(), decoder_parameters()),
                  std::invalid_argument);

  auto invalid_decoder = decoder_parameters();
  invalid_decoder.maximum_sequence_observations = 1U;
  CHECK_THROWS_AS(matching::decode_candidate_batches(
                      graph, observations, batches, candidate_parameters(),
                      transition_parameters(), invalid_decoder),
                  std::invalid_argument);

  observations = {
      observation("overflow-previous", std::numeric_limits<std::int64_t>::max(),
                  10.0, 0.0),
      observation("overflow-current", std::numeric_limits<std::int64_t>::min(),
                  20.0, 0.0),
  };
  batches = identify(matching::generate_candidate_batches(
      graph, observations, candidate_parameters(), emission_parameters()));
  CHECK_THROWS_AS(
      matching::score_transition(
          graph, observations,
          matching::TransitionQuery{0U, batches[0U][0U], 1U,
                                    batches[1U][0U]},
          candidate_parameters(), transition_parameters()),
      std::invalid_argument);

  observations = {
      observation("observation-0", 0, 10.0, 0.0),
      observation("observation-1", 1'000'000'000, 20.0, 0.0),
  };
  batches = identify(matching::generate_candidate_batches(
      graph, observations, candidate_parameters(), emission_parameters()));
  auto out_of_bounds = batches[0U][0U];
  out_of_bounds.along_arc_offset_m = graph.arcs[0U].length_m + 1.0;
  CHECK_THROWS_AS(
      matching::score_transition(
          graph, observations,
          matching::TransitionQuery{0U, out_of_bounds, 1U, batches[1U][0U]},
          candidate_parameters(), transition_parameters()),
      std::invalid_argument);

  batches[0U].push_back(batches[0U][0U]);
  CHECK_THROWS_AS(matching::decode_candidate_batches(
                      graph, observations, batches, candidate_parameters(),
                      transition_parameters(), decoder_parameters()),
                  std::invalid_argument);

  observations[0U].position.x = std::numeric_limits<double>::max();
  observations[1U].position.x = -std::numeric_limits<double>::max();
  CHECK_THROWS_AS(
      matching::score_transition(
          graph, observations,
          matching::TransitionQuery{0U, batches[0U][0U], 1U,
                                    batches[1U][0U]},
          candidate_parameters(), transition_parameters()),
      std::invalid_argument);
}
