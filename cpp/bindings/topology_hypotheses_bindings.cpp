#include "topology_hypotheses_bindings.hpp"

#include "cartosentry/map/topology_hypotheses.hpp"

#include <pybind11/stl.h>

#include <array>
#include <cstddef>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace py = pybind11;
namespace topology = cartosentry::topology;

namespace {

template <typename Value>
auto required(const py::dict &value, const char *key) -> Value {
  if (!value.contains(key)) {
    throw std::invalid_argument(
        std::string("native topology-mining input is missing ") + key);
  }
  return value[key].cast<Value>();
}

auto point_from_python(const py::handle &value) -> topology::Point {
  const auto coordinates = value.cast<std::array<double, 2>>();
  return topology::Point{coordinates[0U], coordinates[1U]};
}

auto points_from_python(const py::list &values) -> std::vector<topology::Point> {
  std::vector<topology::Point> result;
  result.reserve(values.size());
  for (const auto value : values) {
    result.push_back(point_from_python(value));
  }
  return result;
}

auto intervals_from_python(const py::list &values)
    -> std::vector<topology::OffMapInterval> {
  std::vector<topology::OffMapInterval> result;
  result.reserve(values.size());
  for (const auto value : values) {
    const auto raw = value.cast<py::dict>();
    result.push_back(topology::OffMapInterval{
        required<std::string>(raw, "interval_id"),
        required<std::string>(raw, "sequence_id"),
        required<std::string>(raw, "traversal_id"),
        required<std::string>(raw, "source_group_id"),
        required<bool>(raw, "off_map"),
        required<bool>(raw, "positioning_observable"),
        required<bool>(raw, "direction_confident"),
        required<bool>(raw, "stationary"),
        required<double>(raw, "positioning_quality"),
        points_from_python(required<py::list>(raw, "points"))});
  }
  return result;
}

auto nodes_from_python(const py::list &values)
    -> std::vector<topology::GraphNode> {
  std::vector<topology::GraphNode> result;
  result.reserve(values.size());
  for (const auto value : values) {
    const auto raw = value.cast<py::dict>();
    result.push_back(topology::GraphNode{
        required<std::string>(raw, "node_id"),
        point_from_python(required<py::object>(raw, "position"))});
  }
  return result;
}

auto arcs_from_python(const py::list &values)
    -> std::vector<topology::GraphArc> {
  std::vector<topology::GraphArc> result;
  result.reserve(values.size());
  for (const auto value : values) {
    const auto raw = value.cast<py::dict>();
    result.push_back(topology::GraphArc{
        required<std::string>(raw, "arc_id"),
        required<std::size_t>(raw, "source_node_index"),
        required<std::size_t>(raw, "target_node_index"),
        points_from_python(required<py::list>(raw, "geometry"))});
  }
  return result;
}

auto parameters_from_python(const py::dict &raw) -> topology::Parameters {
  return topology::Parameters{
      required<double>(raw, "minimum_positioning_quality"),
      required<double>(raw, "minimum_interval_length_m"),
      required<std::size_t>(raw, "resample_point_count"),
      required<double>(raw, "maximum_cluster_mean_distance_m"),
      required<double>(raw, "maximum_cluster_endpoint_distance_m"),
      required<double>(raw, "maximum_heading_difference_rad"),
      required<std::size_t>(raw, "minimum_independent_traversals"),
      required<double>(raw, "endpoint_snap_radius_m"),
      required<double>(raw, "geometry_disagreement_mean_distance_m"),
      required<double>(raw, "graph_endpoint_tolerance_m"),
      required<std::size_t>(raw, "maximum_intervals"),
      required<std::size_t>(raw, "maximum_points_per_interval"),
      required<std::size_t>(raw, "maximum_total_points"),
      required<std::size_t>(raw, "maximum_graph_nodes"),
      required<std::size_t>(raw, "maximum_graph_arcs"),
      required<std::size_t>(raw, "maximum_points_per_graph_arc"),
      required<std::size_t>(raw, "maximum_total_graph_points"),
      required<std::size_t>(raw, "maximum_pairwise_comparisons"),
      required<std::size_t>(raw, "maximum_clusters")};
}

auto point_to_python(const topology::Point &point) -> py::tuple {
  return py::make_tuple(point.x_m, point.y_m);
}

auto optional_index_to_python(const std::optional<std::size_t> &value)
    -> py::object {
  if (value.has_value()) {
    return py::cast(*value);
  }
  return py::none();
}

auto optional_double_to_python(const std::optional<double> &value)
    -> py::object {
  if (value.has_value()) {
    return py::cast(*value);
  }
  return py::none();
}

auto kind_to_string(topology::HypothesisKind kind) -> std::string {
  switch (kind) {
  case topology::HypothesisKind::missing_connection:
    return "POSSIBLE_MISSING_CONNECTION";
  case topology::HypothesisKind::geometry_disagreement:
    return "POSSIBLE_GEOMETRY_DISAGREEMENT";
  }
  throw std::invalid_argument("native topology hypothesis kind is invalid");
}

auto result_to_python(const topology::MiningResult &value) -> py::dict {
  py::dict result;
  result["selected_interval_indices"] = value.selected_interval_indices;
  py::dict rejected;
  rejected["not_off_map"] = value.rejected_not_off_map;
  rejected["unobservable_positioning"] = value.rejected_unobservable;
  rejected["uncertain_direction"] = value.rejected_direction;
  rejected["stationary"] = value.rejected_stationary;
  rejected["insufficient_positioning_quality"] = value.rejected_quality;
  rejected["short_interval"] = value.rejected_short;
  result["rejected_interval_counts"] = std::move(rejected);
  py::list clusters;
  for (const auto &cluster : value.clusters) {
    py::dict raw;
    raw["cluster_ordinal"] = cluster.cluster_ordinal;
    raw["interval_indices"] = cluster.interval_indices;
    raw["independent_traversal_count"] =
        cluster.independent_traversal_count;
    py::list corridor;
    for (const auto &point : cluster.fitted_corridor) {
      corridor.append(point_to_python(point));
    }
    raw["fitted_corridor"] = std::move(corridor);
    raw["start_node_index"] =
        optional_index_to_python(cluster.start_node_index);
    raw["end_node_index"] = optional_index_to_python(cluster.end_node_index);
    raw["start_endpoint_distance_m"] =
        optional_double_to_python(cluster.start_endpoint_distance_m);
    raw["end_endpoint_distance_m"] =
        optional_double_to_python(cluster.end_endpoint_distance_m);
    clusters.append(std::move(raw));
  }
  result["clusters"] = std::move(clusters);
  py::list hypotheses;
  for (const auto &hypothesis : value.hypotheses) {
    py::dict raw;
    raw["kind"] = kind_to_string(hypothesis.kind);
    raw["cluster_result_index"] = hypothesis.cluster_result_index;
    raw["start_node_index"] = hypothesis.start_node_index;
    raw["end_node_index"] = hypothesis.end_node_index;
    raw["compared_arc_index"] =
        optional_index_to_python(hypothesis.compared_arc_index);
    raw["endpoint_localization_error_m"] =
        hypothesis.endpoint_localization_error_m;
    raw["geometry_corridor_error_m"] =
        optional_double_to_python(hypothesis.geometry_corridor_error_m);
    hypotheses.append(std::move(raw));
  }
  result["hypotheses"] = std::move(hypotheses);
  return result;
}

} // namespace

void bind_topology_hypotheses(py::module_ &module) {
  module.def(
      "mine_repeated_topology_disagreements",
      [](const py::list &interval_values, const py::list &node_values,
         const py::list &arc_values, const py::dict &parameter_values) {
        const auto intervals = intervals_from_python(interval_values);
        const auto nodes = nodes_from_python(node_values);
        const auto arcs = arcs_from_python(arc_values);
        const auto parameters = parameters_from_python(parameter_values);
        topology::MiningResult result;
        {
          py::gil_scoped_release release;
          result = topology::mine_repeated_topology_disagreements(
              intervals, nodes, arcs, parameters);
        }
        return result_to_python(result);
      },
      py::arg("intervals"), py::arg("nodes"), py::arg("arcs"),
      py::arg("parameters"));
}
