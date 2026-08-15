#include "cartosentry/map/topology_hypotheses.hpp"

#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace topology = cartosentry::topology;

namespace {

auto parameters() -> topology::Parameters {
  return topology::Parameters{0.9, 20.0, 11U, 2.0, 3.0, 0.3, 3U, 5.0,
                              3.0, 0.001, 100U, 100U, 1000U, 100U,
                              100U, 100U, 1000U, 10'000U, 100U};
}

auto interval(std::string id, std::string traversal, double y_offset = 0.0,
              bool forward = true) -> topology::OffMapInterval {
  std::vector<topology::Point> points{
      topology::Point{0.0, y_offset}, topology::Point{50.0, y_offset},
      topology::Point{100.0, y_offset}};
  if (!forward) {
    std::reverse(points.begin(), points.end());
  }
  return topology::OffMapInterval{
      std::move(id), "sequence", std::move(traversal), "source-group",
      true,          true,       true,                 false,
      0.99,          std::move(points)};
}

auto nodes(double y_offset = 0.0) -> std::vector<topology::GraphNode> {
  return {topology::GraphNode{"start", topology::Point{0.0, y_offset}},
          topology::GraphNode{"end", topology::Point{100.0, y_offset}}};
}

auto straight_arc(double y_offset = 0.0) -> topology::GraphArc {
  return topology::GraphArc{
      "arc", 0U, 1U,
      {topology::Point{0.0, y_offset}, topology::Point{50.0, y_offset},
       topology::Point{100.0, y_offset}}};
}

} // namespace

TEST_CASE("native repeated off-map traversals surface a missing connection") {
  const std::vector intervals{
      interval("interval-a", "pass-a", -0.2),
      interval("interval-b", "pass-b", 0.0),
      interval("interval-c", "pass-c", 0.2),
      interval("interval-window", "pass-c", 0.1)};
  const auto result = topology::mine_repeated_topology_disagreements(
      intervals, nodes(), {}, parameters());
  REQUIRE(result.clusters.size() == 1U);
  CHECK(result.clusters.front().independent_traversal_count == 3U);
  REQUIRE(result.hypotheses.size() == 1U);
  CHECK(result.hypotheses.front().kind ==
        topology::HypothesisKind::missing_connection);
  CHECK(result.hypotheses.front().start_node_index == 0U);
  CHECK(result.hypotheses.front().end_node_index == 1U);
  CHECK(result.hypotheses.front().endpoint_localization_error_m < 0.051);
}

TEST_CASE("native robust corridor detects source-geometry disagreement") {
  const std::vector intervals{
      interval("interval-a", "pass-a", -0.2),
      interval("interval-b", "pass-b", 0.0),
      interval("interval-c", "pass-c", 0.2)};
  const std::vector arcs{topology::GraphArc{
      "perturbed-arc", 0U, 1U,
      {topology::Point{0.0, 0.0}, topology::Point{50.0, 15.0},
       topology::Point{100.0, 0.0}}}};
  const auto result = topology::mine_repeated_topology_disagreements(
      intervals, nodes(), arcs, parameters());
  REQUIRE(result.hypotheses.size() == 1U);
  CHECK(result.hypotheses.front().kind ==
        topology::HypothesisKind::geometry_disagreement);
  REQUIRE(result.hypotheses.front().geometry_corridor_error_m.has_value());
  CHECK(*result.hypotheses.front().geometry_corridor_error_m > 3.0);
}

TEST_CASE("native unchanged and parallel-road controls remain review silent") {
  const std::vector unchanged{
      interval("unchanged-a", "pass-a", -0.2),
      interval("unchanged-b", "pass-b", 0.0),
      interval("unchanged-c", "pass-c", 0.2)};
  const auto unchanged_result = topology::mine_repeated_topology_disagreements(
      unchanged, nodes(), {straight_arc()}, parameters());
  CHECK(unchanged_result.hypotheses.empty());

  const std::vector parallel_nodes{
      topology::GraphNode{"lower-start", topology::Point{0.0, 0.0}},
      topology::GraphNode{"lower-end", topology::Point{100.0, 0.0}},
      topology::GraphNode{"upper-start", topology::Point{0.0, 8.0}},
      topology::GraphNode{"upper-end", topology::Point{100.0, 8.0}}};
  const std::vector parallel_arcs{
      topology::GraphArc{
          "lower", 0U, 1U,
          {topology::Point{0.0, 0.0}, topology::Point{100.0, 0.0}}},
      topology::GraphArc{
          "upper", 2U, 3U,
          {topology::Point{0.0, 8.0}, topology::Point{100.0, 8.0}}}};
  const std::vector parallel_intervals{
      interval("parallel-a", "pass-a", 7.8),
      interval("parallel-b", "pass-b", 8.0),
      interval("parallel-c", "pass-c", 8.2)};
  const auto parallel_result = topology::mine_repeated_topology_disagreements(
      parallel_intervals, parallel_nodes, parallel_arcs, parameters());
  CHECK(parallel_result.hypotheses.empty());
}

TEST_CASE("native direction-aware clustering separates reverse traversals") {
  const std::vector intervals{
      interval("forward-a", "pass-a"),
      interval("forward-b", "pass-b"),
      interval("forward-c", "pass-c"),
      interval("reverse-a", "reverse-pass-a", 0.0, false),
      interval("reverse-b", "reverse-pass-b", 0.0, false)};
  const auto result = topology::mine_repeated_topology_disagreements(
      intervals, nodes(), {}, parameters());
  REQUIRE(result.clusters.size() == 2U);
  CHECK(result.clusters[0U].independent_traversal_count == 3U);
  CHECK(result.clusters[1U].independent_traversal_count == 2U);
  REQUIRE(result.hypotheses.size() == 1U);
  CHECK(result.hypotheses.front().cluster_result_index == 0U);
}

TEST_CASE("native high-quality selection reports every exclusion reason") {
  auto not_off_map = interval("not-off-map", "pass-a");
  not_off_map.off_map = false;
  auto unobservable = interval("unobservable", "pass-b");
  unobservable.positioning_observable = false;
  auto uncertain_direction = interval("uncertain-direction", "pass-c");
  uncertain_direction.direction_confident = false;
  auto stationary = interval("stationary", "pass-d");
  stationary.stationary = true;
  auto poor_quality = interval("poor-quality", "pass-e");
  poor_quality.positioning_quality = 0.5;
  auto short_interval = interval("short", "pass-f");
  short_interval.points = {{0.0, 0.0}, {10.0, 0.0}};
  const std::vector intervals{not_off_map,       unobservable,
                              uncertain_direction, stationary,
                              poor_quality,      short_interval};
  const auto result = topology::mine_repeated_topology_disagreements(
      intervals, nodes(), {}, parameters());
  CHECK(result.selected_interval_indices.empty());
  CHECK(result.rejected_not_off_map == 1U);
  CHECK(result.rejected_unobservable == 1U);
  CHECK(result.rejected_direction == 1U);
  CHECK(result.rejected_stationary == 1U);
  CHECK(result.rejected_quality == 1U);
  CHECK(result.rejected_short == 1U);
}

TEST_CASE("native topology mining rejects hostile values and bounded-work overflow") {
  const std::vector intervals{interval("interval", "pass")};
  auto invalid = parameters();
  invalid.minimum_positioning_quality =
      std::numeric_limits<double>::quiet_NaN();
  CHECK_THROWS_AS(topology::mine_repeated_topology_disagreements(
                      intervals, nodes(), {}, invalid),
                  std::invalid_argument);

  invalid = parameters();
  invalid.maximum_total_points = 2U;
  CHECK_THROWS_AS(topology::mine_repeated_topology_disagreements(
                      intervals, nodes(), {}, invalid),
                  std::invalid_argument);

  invalid = parameters();
  invalid.maximum_pairwise_comparisons = 1U;
  const std::vector three_intervals{
      interval("a", "pass-a"), interval("b", "pass-b"),
      interval("c", "pass-c")};
  CHECK_THROWS_AS(topology::mine_repeated_topology_disagreements(
                      three_intervals, nodes(), {}, invalid),
                  std::invalid_argument);

  auto invalid_nodes = nodes();
  invalid_nodes.front().position.x_m =
      std::numeric_limits<double>::infinity();
  CHECK_THROWS_AS(topology::mine_repeated_topology_disagreements(
                      intervals, invalid_nodes, {}, parameters()),
                  std::invalid_argument);

  auto invalid_arc = straight_arc();
  invalid_arc.geometry.front().x_m = 1.0;
  CHECK_THROWS_AS(topology::mine_repeated_topology_disagreements(
                      intervals, nodes(), {invalid_arc}, parameters()),
                  std::invalid_argument);
}
