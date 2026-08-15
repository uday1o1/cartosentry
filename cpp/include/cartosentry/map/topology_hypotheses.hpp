#pragma once

#include <cstddef>
#include <optional>
#include <string>
#include <vector>

namespace cartosentry::topology {

struct Point {
  double x_m{};
  double y_m{};
};

struct OffMapInterval {
  std::string interval_id;
  std::string sequence_id;
  std::string traversal_id;
  std::string source_group_id;
  bool off_map{};
  bool positioning_observable{};
  bool direction_confident{};
  bool stationary{};
  double positioning_quality{};
  std::vector<Point> points;
};

struct GraphNode {
  std::string node_id;
  Point position;
};

struct GraphArc {
  std::string arc_id;
  std::size_t source_node_index{};
  std::size_t target_node_index{};
  std::vector<Point> geometry;
};

struct Parameters {
  double minimum_positioning_quality{};
  double minimum_interval_length_m{};
  std::size_t resample_point_count{};
  double maximum_cluster_mean_distance_m{};
  double maximum_cluster_endpoint_distance_m{};
  double maximum_heading_difference_rad{};
  std::size_t minimum_independent_traversals{};
  double endpoint_snap_radius_m{};
  double geometry_disagreement_mean_distance_m{};
  double graph_endpoint_tolerance_m{};
  std::size_t maximum_intervals{};
  std::size_t maximum_points_per_interval{};
  std::size_t maximum_total_points{};
  std::size_t maximum_graph_nodes{};
  std::size_t maximum_graph_arcs{};
  std::size_t maximum_points_per_graph_arc{};
  std::size_t maximum_total_graph_points{};
  std::size_t maximum_pairwise_comparisons{};
  std::size_t maximum_clusters{};
};

enum class HypothesisKind { missing_connection, geometry_disagreement };

struct Cluster {
  std::size_t cluster_ordinal{};
  std::vector<std::size_t> interval_indices;
  std::size_t independent_traversal_count{};
  std::vector<Point> fitted_corridor;
  std::optional<std::size_t> start_node_index;
  std::optional<std::size_t> end_node_index;
  std::optional<double> start_endpoint_distance_m;
  std::optional<double> end_endpoint_distance_m;
};

struct Hypothesis {
  HypothesisKind kind{HypothesisKind::missing_connection};
  std::size_t cluster_result_index{};
  std::size_t start_node_index{};
  std::size_t end_node_index{};
  std::optional<std::size_t> compared_arc_index;
  double endpoint_localization_error_m{};
  std::optional<double> geometry_corridor_error_m;
};

struct MiningResult {
  std::vector<std::size_t> selected_interval_indices;
  std::size_t rejected_not_off_map{};
  std::size_t rejected_unobservable{};
  std::size_t rejected_direction{};
  std::size_t rejected_stationary{};
  std::size_t rejected_quality{};
  std::size_t rejected_short{};
  std::vector<Cluster> clusters;
  std::vector<Hypothesis> hypotheses;
};

auto mine_repeated_topology_disagreements(
    const std::vector<OffMapInterval> &intervals,
    const std::vector<GraphNode> &nodes, const std::vector<GraphArc> &arcs,
    const Parameters &parameters) -> MiningResult;

} // namespace cartosentry::topology
