#include "cartosentry/map/topology_hypotheses.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

namespace cartosentry::topology {

namespace {

constexpr double pi = 3.141592653589793238462643383279502884;

auto finite(double value) -> bool { return std::isfinite(value); }

auto distance(const Point &left, const Point &right) -> double {
  return std::hypot(right.x_m - left.x_m, right.y_m - left.y_m);
}

auto polyline_length(const std::vector<Point> &points) -> double {
  double result = 0.0;
  for (std::size_t index = 1U; index < points.size(); ++index) {
    const auto segment = distance(points[index - 1U], points[index]);
    if (!finite(segment) || result > std::numeric_limits<double>::max() - segment) {
      throw std::invalid_argument("topology polyline length is not finite");
    }
    result += segment;
  }
  return result;
}

auto resample(const std::vector<Point> &points, std::size_t point_count)
    -> std::vector<Point> {
  const auto total_length = polyline_length(points);
  if (!(total_length > 0.0)) {
    throw std::invalid_argument("topology polyline has zero length");
  }
  std::vector<double> cumulative(points.size(), 0.0);
  for (std::size_t index = 1U; index < points.size(); ++index) {
    cumulative[index] =
        cumulative[index - 1U] + distance(points[index - 1U], points[index]);
  }
  std::vector<Point> result;
  result.reserve(point_count);
  std::size_t segment_index = 1U;
  for (std::size_t sample_index = 0U; sample_index < point_count;
       ++sample_index) {
    const auto denominator = static_cast<double>(point_count - 1U);
    const auto target = total_length * static_cast<double>(sample_index) /
                        denominator;
    while (segment_index + 1U < cumulative.size() &&
           cumulative[segment_index] < target) {
      ++segment_index;
    }
    const auto left_index = segment_index - 1U;
    const auto segment_length =
        cumulative[segment_index] - cumulative[left_index];
    if (!(segment_length > 0.0)) {
      result.push_back(points[segment_index]);
      continue;
    }
    const auto fraction =
        std::clamp((target - cumulative[left_index]) / segment_length, 0.0,
                   1.0);
    result.push_back(Point{
        points[left_index].x_m +
            fraction * (points[segment_index].x_m - points[left_index].x_m),
        points[left_index].y_m +
            fraction * (points[segment_index].y_m - points[left_index].y_m)});
  }
  result.front() = points.front();
  result.back() = points.back();
  return result;
}

auto heading(const std::vector<Point> &points) -> double {
  const auto &start = points.front();
  const auto &end = points.back();
  return std::atan2(end.y_m - start.y_m, end.x_m - start.x_m);
}

auto heading_difference(double left, double right) -> double {
  return std::abs(std::remainder(left - right, 2.0 * pi));
}

auto mean_corresponding_distance(const std::vector<Point> &left,
                                 const std::vector<Point> &right) -> double {
  if (left.size() != right.size() || left.empty()) {
    throw std::invalid_argument("topology resampling produced incompatible shapes");
  }
  double total = 0.0;
  for (std::size_t index = 0U; index < left.size(); ++index) {
    total += distance(left[index], right[index]);
  }
  const auto result = total / static_cast<double>(left.size());
  if (!finite(result)) {
    throw std::invalid_argument("topology corridor distance is not finite");
  }
  return result;
}

auto compatible(const std::vector<Point> &left,
                const std::vector<Point> &right,
                const Parameters &parameters) -> bool {
  return heading_difference(heading(left), heading(right)) <=
             parameters.maximum_heading_difference_rad &&
         distance(left.front(), right.front()) <=
             parameters.maximum_cluster_endpoint_distance_m &&
         distance(left.back(), right.back()) <=
             parameters.maximum_cluster_endpoint_distance_m &&
         mean_corresponding_distance(left, right) <=
             parameters.maximum_cluster_mean_distance_m;
}

auto median(std::vector<double> values) -> double {
  if (values.empty()) {
    throw std::invalid_argument("topology corridor median has no values");
  }
  std::sort(values.begin(), values.end());
  const auto middle = values.size() / 2U;
  if (values.size() % 2U != 0U) {
    return values[middle];
  }
  return (values[middle - 1U] + values[middle]) / 2.0;
}

auto fit_corridor(const std::vector<std::size_t> &members,
                  const std::vector<std::vector<Point>> &resampled)
    -> std::vector<Point> {
  if (members.empty()) {
    throw std::invalid_argument("topology cluster has no members");
  }
  std::vector<Point> result;
  result.reserve(resampled[members.front()].size());
  for (std::size_t sample_index = 0U;
       sample_index < resampled[members.front()].size(); ++sample_index) {
    std::vector<double> x_values;
    std::vector<double> y_values;
    x_values.reserve(members.size());
    y_values.reserve(members.size());
    for (const auto member : members) {
      x_values.push_back(resampled[member][sample_index].x_m);
      y_values.push_back(resampled[member][sample_index].y_m);
    }
    result.push_back(Point{median(std::move(x_values)),
                           median(std::move(y_values))});
  }
  return result;
}

struct NearestNode {
  std::size_t index{};
  double distance_m{};
};

auto nearest_node(const Point &point, const std::vector<GraphNode> &nodes,
                  double maximum_distance_m) -> std::optional<NearestNode> {
  std::optional<NearestNode> best;
  for (std::size_t index = 0U; index < nodes.size(); ++index) {
    const auto candidate_distance = distance(point, nodes[index].position);
    if (candidate_distance > maximum_distance_m) {
      continue;
    }
    if (!best.has_value() || candidate_distance < best->distance_m ||
        (candidate_distance == best->distance_m &&
         nodes[index].node_id < nodes[best->index].node_id)) {
      best = NearestNode{index, candidate_distance};
    }
  }
  return best;
}

auto validate_parameters(const Parameters &parameters) -> void {
  if (!finite(parameters.minimum_positioning_quality) ||
      parameters.minimum_positioning_quality < 0.0 ||
      parameters.minimum_positioning_quality > 1.0 ||
      !finite(parameters.minimum_interval_length_m) ||
      !(parameters.minimum_interval_length_m > 0.0) ||
      parameters.resample_point_count < 2U ||
      !finite(parameters.maximum_cluster_mean_distance_m) ||
      !(parameters.maximum_cluster_mean_distance_m > 0.0) ||
      !finite(parameters.maximum_cluster_endpoint_distance_m) ||
      !(parameters.maximum_cluster_endpoint_distance_m > 0.0) ||
      !finite(parameters.maximum_heading_difference_rad) ||
      parameters.maximum_heading_difference_rad < 0.0 ||
      parameters.maximum_heading_difference_rad > pi ||
      parameters.minimum_independent_traversals == 0U ||
      !finite(parameters.endpoint_snap_radius_m) ||
      !(parameters.endpoint_snap_radius_m > 0.0) ||
      !finite(parameters.geometry_disagreement_mean_distance_m) ||
      !(parameters.geometry_disagreement_mean_distance_m > 0.0) ||
      !finite(parameters.graph_endpoint_tolerance_m) ||
      parameters.graph_endpoint_tolerance_m < 0.0 ||
      parameters.maximum_intervals == 0U ||
      parameters.maximum_points_per_interval < 2U ||
      parameters.maximum_total_points < 2U ||
      parameters.maximum_graph_nodes == 0U ||
      parameters.maximum_graph_arcs == 0U ||
      parameters.maximum_points_per_graph_arc < 2U ||
      parameters.maximum_total_graph_points < 2U ||
      parameters.maximum_pairwise_comparisons == 0U ||
      parameters.maximum_clusters == 0U) {
    throw std::invalid_argument("topology-mining parameters are invalid");
  }
}

auto validate_points(const std::vector<Point> &points, std::size_t maximum,
                     const char *context) -> void {
  if (points.size() < 2U || points.size() > maximum) {
    throw std::invalid_argument(std::string(context) +
                                " point count is outside its budget");
  }
  for (const auto &point : points) {
    if (!finite(point.x_m) || !finite(point.y_m)) {
      throw std::invalid_argument(std::string(context) +
                                  " contains a nonfinite point");
    }
  }
  if (!(polyline_length(points) > 0.0)) {
    throw std::invalid_argument(std::string(context) +
                                " must have positive length");
  }
}

auto checked_add(std::size_t left, std::size_t right, std::size_t maximum,
                 const char *context) -> std::size_t {
  if (right > maximum || left > maximum - right) {
    throw std::invalid_argument(std::string(context) + " exceeds its budget");
  }
  return left + right;
}

auto validate_inputs(const std::vector<OffMapInterval> &intervals,
                     const std::vector<GraphNode> &nodes,
                     const std::vector<GraphArc> &arcs,
                     const Parameters &parameters) -> void {
  if (intervals.size() > parameters.maximum_intervals ||
      nodes.size() > parameters.maximum_graph_nodes ||
      arcs.size() > parameters.maximum_graph_arcs) {
    throw std::invalid_argument("topology-mining input count exceeds its budget");
  }
  std::unordered_set<std::string> interval_ids;
  std::size_t total_points = 0U;
  for (const auto &interval : intervals) {
    if (interval.interval_id.empty() || interval.sequence_id.empty() ||
        interval.traversal_id.empty() || interval.source_group_id.empty() ||
        !interval_ids.insert(interval.interval_id).second ||
        !finite(interval.positioning_quality) ||
        interval.positioning_quality < 0.0 ||
        interval.positioning_quality > 1.0) {
      throw std::invalid_argument("topology interval identity or quality is invalid");
    }
    validate_points(interval.points, parameters.maximum_points_per_interval,
                    "topology interval");
    total_points = checked_add(total_points, interval.points.size(),
                               parameters.maximum_total_points,
                               "topology interval points");
  }

  std::unordered_set<std::string> node_ids;
  for (const auto &node : nodes) {
    if (node.node_id.empty() || !node_ids.insert(node.node_id).second ||
        !finite(node.position.x_m) || !finite(node.position.y_m)) {
      throw std::invalid_argument("topology graph node is invalid");
    }
  }
  std::unordered_set<std::string> arc_ids;
  std::size_t graph_points = 0U;
  for (const auto &arc : arcs) {
    if (arc.arc_id.empty() || !arc_ids.insert(arc.arc_id).second ||
        arc.source_node_index >= nodes.size() ||
        arc.target_node_index >= nodes.size()) {
      throw std::invalid_argument("topology graph arc identity or endpoint is invalid");
    }
    validate_points(arc.geometry, parameters.maximum_points_per_graph_arc,
                    "topology graph arc");
    graph_points = checked_add(graph_points, arc.geometry.size(),
                               parameters.maximum_total_graph_points,
                               "topology graph points");
    if (distance(arc.geometry.front(), nodes[arc.source_node_index].position) >
            parameters.graph_endpoint_tolerance_m ||
        distance(arc.geometry.back(), nodes[arc.target_node_index].position) >
            parameters.graph_endpoint_tolerance_m) {
      throw std::invalid_argument(
          "topology graph arc geometry disagrees with its endpoints");
    }
  }
}

auto pairwise_count(std::size_t count, std::size_t maximum) -> std::size_t {
  if (count < 2U) {
    return 0U;
  }
  const auto smaller = count - 1U;
  if (count > std::numeric_limits<std::size_t>::max() / smaller) {
    throw std::invalid_argument("topology pairwise work overflows size_t");
  }
  const auto result = count * smaller / 2U;
  if (result > maximum) {
    throw std::invalid_argument("topology pairwise work exceeds its budget");
  }
  return result;
}

} // namespace

auto mine_repeated_topology_disagreements(
    const std::vector<OffMapInterval> &intervals,
    const std::vector<GraphNode> &nodes, const std::vector<GraphArc> &arcs,
    const Parameters &parameters) -> MiningResult {
  validate_parameters(parameters);
  validate_inputs(intervals, nodes, arcs, parameters);

  MiningResult result;
  std::vector<std::vector<Point>> resampled(intervals.size());
  for (std::size_t index = 0U; index < intervals.size(); ++index) {
    const auto &interval = intervals[index];
    if (!interval.off_map) {
      ++result.rejected_not_off_map;
      continue;
    }
    if (!interval.positioning_observable) {
      ++result.rejected_unobservable;
      continue;
    }
    if (!interval.direction_confident) {
      ++result.rejected_direction;
      continue;
    }
    if (interval.stationary) {
      ++result.rejected_stationary;
      continue;
    }
    if (interval.positioning_quality < parameters.minimum_positioning_quality) {
      ++result.rejected_quality;
      continue;
    }
    if (polyline_length(interval.points) <
        parameters.minimum_interval_length_m) {
      ++result.rejected_short;
      continue;
    }
    const auto directed_span = distance(interval.points.front(),
                                        interval.points.back());
    if (!(directed_span > parameters.graph_endpoint_tolerance_m)) {
      ++result.rejected_direction;
      continue;
    }
    resampled[index] = resample(interval.points, parameters.resample_point_count);
    result.selected_interval_indices.push_back(index);
  }
  std::sort(result.selected_interval_indices.begin(),
            result.selected_interval_indices.end(),
            [&intervals](std::size_t left, std::size_t right) {
              return intervals[left].interval_id < intervals[right].interval_id;
            });
  static_cast<void>(pairwise_count(result.selected_interval_indices.size(),
                                   parameters.maximum_pairwise_comparisons));

  std::vector<std::vector<std::size_t>> cluster_members;
  for (const auto interval_index : result.selected_interval_indices) {
    bool assigned = false;
    for (auto &members : cluster_members) {
      const auto complete_link = std::all_of(
          members.begin(), members.end(), [&](std::size_t member_index) {
            return compatible(resampled[interval_index],
                              resampled[member_index], parameters);
          });
      if (complete_link) {
        members.push_back(interval_index);
        assigned = true;
        break;
      }
    }
    if (!assigned) {
      if (cluster_members.size() >= parameters.maximum_clusters) {
        throw std::invalid_argument("topology cluster count exceeds its budget");
      }
      cluster_members.push_back({interval_index});
    }
  }

  result.clusters.reserve(cluster_members.size());
  for (std::size_t cluster_ordinal = 0U;
       cluster_ordinal < cluster_members.size(); ++cluster_ordinal) {
    auto members = cluster_members[cluster_ordinal];
    std::sort(members.begin(), members.end(),
              [&intervals](std::size_t left, std::size_t right) {
                return intervals[left].interval_id < intervals[right].interval_id;
              });
    std::set<std::string> traversal_ids;
    for (const auto member : members) {
      traversal_ids.insert(intervals[member].traversal_id);
    }
    auto corridor = fit_corridor(members, resampled);
    const auto start =
        nearest_node(corridor.front(), nodes, parameters.endpoint_snap_radius_m);
    const auto end =
        nearest_node(corridor.back(), nodes, parameters.endpoint_snap_radius_m);
    Cluster cluster;
    cluster.cluster_ordinal = cluster_ordinal;
    cluster.interval_indices = std::move(members);
    cluster.independent_traversal_count = traversal_ids.size();
    cluster.fitted_corridor = std::move(corridor);
    if (start.has_value()) {
      cluster.start_node_index = start->index;
      cluster.start_endpoint_distance_m = start->distance_m;
    }
    if (end.has_value()) {
      cluster.end_node_index = end->index;
      cluster.end_endpoint_distance_m = end->distance_m;
    }
    result.clusters.push_back(std::move(cluster));
  }

  for (std::size_t cluster_index = 0U; cluster_index < result.clusters.size();
       ++cluster_index) {
    const auto &cluster = result.clusters[cluster_index];
    if (cluster.independent_traversal_count <
            parameters.minimum_independent_traversals ||
        !cluster.start_node_index.has_value() ||
        !cluster.end_node_index.has_value() ||
        cluster.start_node_index == cluster.end_node_index) {
      continue;
    }
    std::optional<std::size_t> best_arc;
    std::optional<double> best_error;
    for (std::size_t arc_index = 0U; arc_index < arcs.size(); ++arc_index) {
      const auto &arc = arcs[arc_index];
      if (arc.source_node_index != *cluster.start_node_index ||
          arc.target_node_index != *cluster.end_node_index) {
        continue;
      }
      const auto arc_resampled =
          resample(arc.geometry, parameters.resample_point_count);
      const auto candidate_error =
          mean_corresponding_distance(cluster.fitted_corridor, arc_resampled);
      if (!best_error.has_value() || candidate_error < *best_error ||
          (candidate_error == *best_error &&
           arc.arc_id < arcs[*best_arc].arc_id)) {
        best_arc = arc_index;
        best_error = candidate_error;
      }
    }
    const auto endpoint_error =
        std::max(*cluster.start_endpoint_distance_m,
                 *cluster.end_endpoint_distance_m);
    if (!best_arc.has_value()) {
      result.hypotheses.push_back(Hypothesis{
          HypothesisKind::missing_connection, cluster_index,
          *cluster.start_node_index, *cluster.end_node_index, std::nullopt,
          endpoint_error, std::nullopt});
      continue;
    }
    if (*best_error > parameters.geometry_disagreement_mean_distance_m) {
      result.hypotheses.push_back(Hypothesis{
          HypothesisKind::geometry_disagreement, cluster_index,
          *cluster.start_node_index, *cluster.end_node_index, best_arc,
          endpoint_error, best_error});
    }
  }
  return result;
}

} // namespace cartosentry::topology
