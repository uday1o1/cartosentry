#include "road_bins_bindings.hpp"

#include "cartosentry/map/road_bins.hpp"

#include <pybind11/stl.h>

#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace py = pybind11;
namespace bins = cartosentry::bins;

namespace {

template <typename Value>
auto required(const py::dict &value, const char *key) -> Value {
  if (!value.contains(key)) {
    throw std::invalid_argument(std::string("native road-bin input is missing ") +
                                key);
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

auto direction_from_string(const std::string &value) -> bins::ArcDirection {
  if (value == "FORWARD") {
    return bins::ArcDirection::forward;
  }
  if (value == "REVERSE") {
    return bins::ArcDirection::reverse;
  }
  throw std::invalid_argument("native road-bin arc direction is invalid");
}

auto modality_from_string(const std::string &value) -> bins::Modality {
  if (value == "camera") {
    return bins::Modality::camera;
  }
  if (value == "gnss") {
    return bins::Modality::gnss;
  }
  if (value == "imu") {
    return bins::Modality::imu;
  }
  if (value == "lidar") {
    return bins::Modality::lidar;
  }
  if (value == "radar") {
    return bins::Modality::radar;
  }
  if (value == "trajectory") {
    return bins::Modality::trajectory;
  }
  throw std::invalid_argument("native road-bin modality is invalid");
}

auto modality_to_string(bins::Modality value) -> std::string {
  switch (value) {
  case bins::Modality::camera:
    return "camera";
  case bins::Modality::gnss:
    return "gnss";
  case bins::Modality::imu:
    return "imu";
  case bins::Modality::lidar:
    return "lidar";
  case bins::Modality::radar:
    return "radar";
  case bins::Modality::trajectory:
    return "trajectory";
  }
  throw std::invalid_argument("native road-bin modality result is invalid");
}

auto arcs_from_python(const py::list &values) -> std::vector<bins::Arc> {
  std::vector<bins::Arc> result;
  result.reserve(values.size());
  for (const auto item : values) {
    const auto raw = item.cast<py::dict>();
    result.push_back(bins::Arc{
        required<std::string>(raw, "arc_id"),
        direction_from_string(required<std::string>(raw, "direction")),
        required<double>(raw, "length_m")});
  }
  return result;
}

auto paths_from_python(const py::list &values)
    -> std::vector<bins::MatchedPath> {
  std::vector<bins::MatchedPath> result;
  result.reserve(values.size());
  for (const auto item : values) {
    const auto raw = item.cast<py::dict>();
    bins::MatchedPath path;
    path.road_match_id = required<std::string>(raw, "road_match_id");
    path.sequence_id = required<std::string>(raw, "sequence_id");
    path.source_group_id = required<std::string>(raw, "source_group_id");
    const auto point_values = required<py::list>(raw, "points");
    path.points.reserve(point_values.size());
    for (const auto point_item : point_values) {
      const auto point_raw = point_item.cast<py::dict>();
      bins::MatchedPoint point;
      point.time_ns = required<std::int64_t>(point_raw, "time_ns");
      if (!point_raw.contains("arc_index") ||
          point_raw["arc_index"].is_none()) {
        point.arc_index = std::nullopt;
      } else {
        point.arc_index = point_raw["arc_index"].cast<std::size_t>();
      }
      point.along_arc_offset_m =
          optional_double(point_raw, "along_arc_offset_m");
      point.confident = required<bool>(point_raw, "confident");
      point.stationary = required<bool>(point_raw, "stationary");
      point.speed_mps = required<double>(point_raw, "speed_mps");
      point.heading_rad = optional_double(point_raw, "heading_rad");
      path.points.push_back(std::move(point));
    }
    result.push_back(std::move(path));
  }
  return result;
}

auto evidence_from_python(const py::list &values)
    -> std::vector<bins::ModalityEvidence> {
  std::vector<bins::ModalityEvidence> result;
  result.reserve(values.size());
  for (const auto item : values) {
    const auto raw = item.cast<py::dict>();
    result.push_back(bins::ModalityEvidence{
        required<std::string>(raw, "evidence_id"),
        required<std::string>(raw, "sequence_id"),
        modality_from_string(required<std::string>(raw, "modality")),
        required<std::int64_t>(raw, "start_time_ns"),
        required<std::int64_t>(raw, "end_time_ns"),
        required<bool>(raw, "usable"),
        required<double>(raw, "point_count"),
        optional_double(raw, "overlap_support_m"),
        required<bool>(raw, "timestamp_supported")});
  }
  return result;
}

auto findings_from_python(const py::list &values)
    -> std::vector<bins::FindingInterval> {
  std::vector<bins::FindingInterval> result;
  result.reserve(values.size());
  for (const auto item : values) {
    const auto raw = item.cast<py::dict>();
    result.push_back(bins::FindingInterval{
        required<std::string>(raw, "finding_id"),
        required<std::string>(raw, "sequence_id"),
        required<std::int64_t>(raw, "start_time_ns"),
        required<std::int64_t>(raw, "end_time_ns"),
        required<bool>(raw, "critical")});
  }
  return result;
}

auto parameters_from_python(const py::dict &value) -> bins::Parameters {
  return bins::Parameters{
      required<double>(value, "bin_length_m"),
      required<std::int64_t>(
          value, "independent_traversal_minimum_gap_ns"),
      required<std::size_t>(value, "maximum_paths"),
      required<std::size_t>(value, "maximum_points_per_path"),
      required<std::size_t>(value, "maximum_total_points"),
      required<std::size_t>(value, "maximum_generated_bins"),
      required<std::size_t>(value,
                            "maximum_modality_evidence_intervals"),
      required<std::size_t>(value, "maximum_findings"),
      required<int>(value, "distance_rounding_decimal_places")};
}

auto modality_to_python(const bins::ModalityAggregate &value) -> py::dict {
  py::dict result;
  result["modality"] = modality_to_string(value.modality);
  result["valid_duration_ns"] = value.valid_duration_ns;
  result["point_support"] = value.point_support;
  if (value.mean_overlap_support_m.has_value()) {
    result["mean_overlap_support_m"] = *value.mean_overlap_support_m;
  } else {
    result["mean_overlap_support_m"] = py::none();
  }
  result["timestamp_supported_duration_ns"] =
      value.timestamp_supported_duration_ns;
  result["evidence_ids"] = value.evidence_ids;
  return result;
}

auto traversal_to_python(const bins::TraversalCoverage &value) -> py::dict {
  py::dict result;
  result["arc_index"] = value.arc_index;
  result["longitudinal_bin_index"] = value.longitudinal_bin_index;
  result["sequence_id"] = value.sequence_id;
  result["source_group_id"] = value.source_group_id;
  result["traversal_ordinal"] = value.traversal_ordinal;
  result["first_time_ns"] = value.first_time_ns;
  result["last_time_ns"] = value.last_time_ns;
  result["entry_offset_m"] = value.entry_offset_m;
  result["exit_offset_m"] = value.exit_offset_m;
  result["usable_duration_ns"] = value.usable_duration_ns;
  result["usable_distance_m"] = value.usable_distance_m;
  result["speed_sample_count"] = value.speed_sample_count;
  result["minimum_speed_mps"] = value.minimum_speed_mps;
  result["mean_speed_mps"] = value.mean_speed_mps;
  result["maximum_speed_mps"] = value.maximum_speed_mps;
  result["yaw_excitation_rad"] = value.yaw_excitation_rad;
  result["road_match_ids"] = value.road_match_ids;
  py::list modalities;
  for (const auto &modality : value.modalities) {
    modalities.append(modality_to_python(modality));
  }
  result["modalities"] = std::move(modalities);
  result["finding_ids"] = value.finding_ids;
  result["critical_finding_ids"] = value.critical_finding_ids;
  return result;
}

auto bin_to_python(const bins::BinCoverage &value) -> py::dict {
  py::dict result;
  result["arc_index"] = value.arc_index;
  result["longitudinal_bin_index"] = value.longitudinal_bin_index;
  result["start_offset_m"] = value.start_offset_m;
  result["end_offset_m"] = value.end_offset_m;
  result["usable_duration_ns"] = value.usable_duration_ns;
  result["usable_distance_m"] = value.usable_distance_m;
  result["independent_traversal_count"] =
      value.independent_traversal_count;
  result["speed_sample_count"] = value.speed_sample_count;
  if (value.minimum_speed_mps.has_value()) {
    result["minimum_speed_mps"] = *value.minimum_speed_mps;
    result["mean_speed_mps"] = *value.mean_speed_mps;
    result["maximum_speed_mps"] = *value.maximum_speed_mps;
  } else {
    result["minimum_speed_mps"] = py::none();
    result["mean_speed_mps"] = py::none();
    result["maximum_speed_mps"] = py::none();
  }
  result["yaw_excitation_rad"] = value.yaw_excitation_rad;
  py::list traversals;
  for (const auto &traversal : value.traversals) {
    traversals.append(traversal_to_python(traversal));
  }
  result["traversals"] = std::move(traversals);
  py::list modalities;
  for (const auto &modality : value.modalities) {
    modalities.append(modality_to_python(modality));
  }
  result["modalities"] = std::move(modalities);
  result["finding_ids"] = value.finding_ids;
  result["critical_finding_ids"] = value.critical_finding_ids;
  return result;
}

auto result_to_python(const bins::AggregationResult &value) -> py::dict {
  py::dict result;
  py::list bin_values;
  for (const auto &bin : value.bins) {
    bin_values.append(bin_to_python(bin));
  }
  result["bins"] = std::move(bin_values);
  py::list localizations;
  for (const auto &localization : value.finding_localizations) {
    py::dict item;
    item["finding_id"] = localization.finding_id;
    item["bin_result_indices"] = localization.bin_result_indices;
    localizations.append(std::move(item));
  }
  result["finding_localizations"] = std::move(localizations);
  return result;
}

} // namespace

void bind_road_bins(py::module_ &module) {
  module.def(
      "aggregate_directed_road_bins",
      [](const py::list &arc_values, const py::list &path_values,
         const py::list &evidence_values, const py::list &finding_values,
         const py::dict &parameter_values) {
        const auto arcs = arcs_from_python(arc_values);
        const auto paths = paths_from_python(path_values);
        const auto evidence = evidence_from_python(evidence_values);
        const auto findings = findings_from_python(finding_values);
        const auto parameters = parameters_from_python(parameter_values);
        bins::AggregationResult result;
        {
          py::gil_scoped_release release;
          result = bins::aggregate_directed_road_bins(
              arcs, paths, evidence, findings, parameters);
        }
        return result_to_python(result);
      },
      py::arg("arcs"), py::arg("paths"), py::arg("modality_evidence"),
      py::arg("findings"), py::arg("parameters"));
}
