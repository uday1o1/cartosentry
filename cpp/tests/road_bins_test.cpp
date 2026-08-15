#include "cartosentry/map/road_bins.hpp"

#include <catch2/catch_test_macros.hpp>

#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace bins = cartosentry::bins;

namespace {

auto parameters() -> bins::Parameters {
  return bins::Parameters{20.0, 100'000'000'000, 100U, 1000U, 10'000U,
                          10'000U, 1000U, 1000U, 6};
}

auto point(std::int64_t time_ns, std::size_t arc_index, double offset_m,
           bool confident = true, bool stationary = false)
    -> bins::MatchedPoint {
  return bins::MatchedPoint{time_ns, arc_index, offset_m, confident,
                            stationary, 10.0, 0.0};
}

auto path(std::string match_id, std::string sequence_id,
          std::vector<bins::MatchedPoint> points)
    -> bins::MatchedPath {
  return bins::MatchedPath{std::move(match_id), std::move(sequence_id),
                           "source-group", std::move(points)};
}

} // namespace

TEST_CASE("native directed bins preserve boundaries and final partial length") {
  const std::vector arcs{bins::Arc{"arc-forward", bins::ArcDirection::forward,
                                  45.0}};
  const std::vector paths{path(
      "match", "sequence",
      {point(0, 0U, 0.0), point(20'000'000'000, 0U, 20.0),
       point(45'000'000'000, 0U, 45.0)})};
  const auto result = bins::aggregate_directed_road_bins(
      arcs, paths, {}, {}, parameters());
  REQUIRE(result.bins.size() == 3U);
  CHECK(result.bins[0U].start_offset_m == 0.0);
  CHECK(result.bins[0U].end_offset_m == 20.0);
  CHECK(result.bins[1U].start_offset_m == 20.0);
  CHECK(result.bins[1U].end_offset_m == 40.0);
  CHECK(result.bins[2U].start_offset_m == 40.0);
  CHECK(result.bins[2U].end_offset_m == 45.0);
  CHECK(result.bins[0U].usable_distance_m == 20.0);
  CHECK(result.bins[1U].usable_distance_m == 20.0);
  CHECK(result.bins[2U].usable_distance_m == 5.0);
  CHECK(result.bins[0U].independent_traversal_count == 1U);
  CHECK(result.bins[1U].independent_traversal_count == 1U);
  CHECK(result.bins[2U].independent_traversal_count == 1U);
}

TEST_CASE("native reverse arc and short arc retain directed coverage") {
  const std::vector arcs{bins::Arc{"arc-reverse", bins::ArcDirection::reverse,
                                  12.0}};
  const std::vector paths{path(
      "reverse-match", "reverse-sequence",
      {point(0, 0U, 0.0), point(12'000'000'000, 0U, 12.0)})};
  const auto result = bins::aggregate_directed_road_bins(
      arcs, paths, {}, {}, parameters());
  REQUIRE(result.bins.size() == 1U);
  REQUIRE(result.bins.front().traversals.size() == 1U);
  CHECK(result.bins.front().end_offset_m == 12.0);
  CHECK(result.bins.front().usable_distance_m == 12.0);
  CHECK(result.bins.front().traversals.front().entry_offset_m == 0.0);
  CHECK(result.bins.front().traversals.front().exit_offset_m == 12.0);
}

TEST_CASE("native traversal identity joins adjacent windows but separates passes") {
  const std::vector arcs{bins::Arc{"arc", bins::ArcDirection::forward, 20.0}};
  const std::vector paths{
      path("window-a", "sequence-a",
           {point(0, 0U, 0.0), point(10'000'000'000, 0U, 10.0)}),
      path("window-b", "sequence-a",
           {point(10'000'000'000, 0U, 10.0),
            point(20'000'000'000, 0U, 20.0)}),
      path("later-pass", "sequence-a",
           {point(200'000'000'000, 0U, 0.0),
            point(220'000'000'000, 0U, 20.0)}),
      path("other-sequence", "sequence-b",
           {point(0, 0U, 0.0), point(20'000'000'000, 0U, 20.0)}),
  };
  const auto result = bins::aggregate_directed_road_bins(
      arcs, paths, {}, {}, parameters());
  REQUIRE(result.bins.size() == 1U);
  CHECK(result.bins.front().independent_traversal_count == 3U);
  REQUIRE(result.bins.front().traversals.size() == 3U);
  CHECK(result.bins.front().traversals.front().road_match_ids ==
        std::vector<std::string>{"window-a", "window-b"});
}

TEST_CASE("native bins exclude ambiguous stationary off-map and against-arc motion") {
  const std::vector arcs{bins::Arc{"arc", bins::ArcDirection::forward, 20.0}};
  auto off_map_left = point(0, 0U, 0.0);
  auto off_map_right = point(1'000'000'000, 0U, 0.0);
  off_map_left.arc_index = std::nullopt;
  off_map_left.along_arc_offset_m = std::nullopt;
  off_map_right.arc_index = std::nullopt;
  off_map_right.along_arc_offset_m = std::nullopt;
  const std::vector paths{
      path("ambiguous", "ambiguous-sequence",
           {point(0, 0U, 0.0, false), point(1'000'000'000, 0U, 20.0, false)}),
      path("stationary", "stationary-sequence",
           {point(0, 0U, 0.0, true, true),
            point(1'000'000'000, 0U, 20.0, true, true)}),
      path("off-map", "off-map-sequence", {off_map_left, off_map_right}),
      path("against", "against-sequence",
           {point(0, 0U, 20.0), point(1'000'000'000, 0U, 0.0)}),
  };
  const auto result = bins::aggregate_directed_road_bins(
      arcs, paths, {}, {}, parameters());
  REQUIRE(result.bins.size() == 1U);
  CHECK(result.bins.front().usable_distance_m == 0.0);
  CHECK(result.bins.front().independent_traversal_count == 0U);
}

TEST_CASE("native temporal joins attach modality support and affected findings") {
  const std::vector arcs{bins::Arc{"arc", bins::ArcDirection::forward, 40.0}};
  const std::vector paths{path(
      "match", "sequence",
      {point(0, 0U, 0.0), point(40'000'000'000, 0U, 40.0)})};
  const std::vector evidence{bins::ModalityEvidence{
      "lidar-evidence", "sequence", bins::Modality::lidar,
      5'000'000'000, 35'000'000'000, true, 300.0, 0.2, true}};
  const std::vector findings{
      bins::FindingInterval{"critical-finding", "sequence",
                            10'000'000'000, 30'000'000'000, true},
      bins::FindingInterval{"outside-finding", "sequence",
                            50'000'000'000, 60'000'000'000, false},
  };
  const auto result = bins::aggregate_directed_road_bins(
      arcs, paths, evidence, findings, parameters());
  REQUIRE(result.bins.size() == 2U);
  for (const auto &bin : result.bins) {
    REQUIRE(bin.modalities.size() == 1U);
    CHECK(bin.modalities.front().modality == bins::Modality::lidar);
    CHECK(bin.modalities.front().valid_duration_ns == 15'000'000'000);
    CHECK(bin.modalities.front().point_support == 150.0);
    CHECK(bin.finding_ids ==
          std::vector<std::string>{"critical-finding"});
    CHECK(bin.critical_finding_ids ==
          std::vector<std::string>{"critical-finding"});
  }
  REQUIRE(result.finding_localizations.size() == 2U);
  CHECK(result.finding_localizations[0U].bin_result_indices ==
        std::vector<std::size_t>{0U, 1U});
  CHECK(result.finding_localizations[1U].bin_result_indices.empty());
}

TEST_CASE("native road bins reject hostile values and frozen-budget overflow") {
  const std::vector arcs{bins::Arc{"arc", bins::ArcDirection::forward, 20.0}};
  const std::vector paths{path(
      "match", "sequence",
      {point(0, 0U, 0.0), point(1'000'000'000, 0U, 20.0)})};
  auto invalid_parameters = parameters();
  invalid_parameters.bin_length_m =
      std::numeric_limits<double>::quiet_NaN();
  CHECK_THROWS_AS(bins::aggregate_directed_road_bins(
                      arcs, paths, {}, {}, invalid_parameters),
                  std::invalid_argument);

  invalid_parameters = parameters();
  invalid_parameters.maximum_paths = 0U;
  CHECK_THROWS_AS(bins::aggregate_directed_road_bins(
                      arcs, paths, {}, {}, invalid_parameters),
                  std::invalid_argument);

  invalid_parameters = parameters();
  invalid_parameters.maximum_total_points = 1U;
  CHECK_THROWS_AS(bins::aggregate_directed_road_bins(
                      arcs, paths, {}, {}, invalid_parameters),
                  std::invalid_argument);

  invalid_parameters = parameters();
  invalid_parameters.maximum_generated_bins = 1U;
  const std::vector long_arcs{
      bins::Arc{"long-arc", bins::ArcDirection::forward, 21.0}};
  CHECK_THROWS_AS(bins::aggregate_directed_road_bins(
                      long_arcs, paths, {}, {}, invalid_parameters),
                  std::invalid_argument);

  auto invalid_paths = paths;
  invalid_paths.front().points.back().along_arc_offset_m = 21.0;
  CHECK_THROWS_AS(bins::aggregate_directed_road_bins(
                      arcs, invalid_paths, {}, {}, parameters()),
                  std::invalid_argument);

  invalid_paths = paths;
  invalid_paths.front().points.front().time_ns =
      std::numeric_limits<std::int64_t>::min();
  invalid_paths.front().points.back().time_ns =
      std::numeric_limits<std::int64_t>::max();
  CHECK_THROWS_AS(bins::aggregate_directed_road_bins(
                      arcs, invalid_paths, {}, {}, parameters()),
                  std::invalid_argument);
}
