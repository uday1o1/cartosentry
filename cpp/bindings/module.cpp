#include "cartosentry/core/native_check.hpp"
#include "cartosentry/contracts/artifact_json.hpp"
#include "cartosentry/contracts/geometry.hpp"
#include "cartosentry/contracts/time.hpp"
#include "cartosentry/ingest/boreas_inspector.hpp"
#include "cartosentry/spikes/observability.hpp"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <array>
#include <filesystem>
#include <string>

namespace py = pybind11;

namespace {

auto quaternion_to_dict(
    const cartosentry::contracts::UnitQuaternion &quaternion) -> py::dict {
  py::dict result;
  result["w"] = quaternion.w;
  result["x"] = quaternion.x;
  result["y"] = quaternion.y;
  result["z"] = quaternion.z;
  result["pre_normalization_norm_deviation"] =
      quaternion.pre_normalization_norm_deviation;
  return result;
}

auto make_quaternion(const std::array<double, 4> &values)
    -> cartosentry::contracts::UnitQuaternion {
  return cartosentry::contracts::make_unit_quaternion(
      values[0], values[1], values[2], values[3]);
}

auto make_transform(const std::string &target_frame,
                    const std::string &source_frame,
                    const std::array<double, 3> &translation_m,
                    const std::array<double, 4> &quaternion_wxyz)
    -> cartosentry::contracts::RigidTransform {
  return cartosentry::contracts::make_rigid_transform(
      target_frame, source_frame, translation_m,
      make_quaternion(quaternion_wxyz));
}

auto transform_to_dict(
    const cartosentry::contracts::RigidTransform &transform) -> py::dict {
  py::dict result;
  result["target_frame"] = transform.target_frame;
  result["source_frame"] = transform.source_frame;
  result["translation_m"] = transform.translation_m;
  result["rotation"] = quaternion_to_dict(transform.rotation);
  return result;
}

auto global_coordinate_to_dict(
    const cartosentry::contracts::GlobalCoordinate &coordinate) -> py::dict {
  py::dict result;
  result["latitude_deg"] = coordinate.latitude_deg;
  result["longitude_deg"] = coordinate.longitude_deg;
  if (coordinate.altitude_m.has_value()) {
    result["altitude_m"] = *coordinate.altitude_m;
  } else {
    result["altitude_m"] = py::none();
  }
  result["vertical_datum"] =
      coordinate.vertical_datum ==
              cartosentry::contracts::VerticalDatum::wgs84_ellipsoid
          ? "WGS84_ELLIPSOID"
          : "UNKNOWN_VERTICAL_DATUM";
  return result;
}

auto make_global_coordinate(double latitude_deg, double longitude_deg,
                            double altitude_m)
    -> cartosentry::contracts::GlobalCoordinate {
  return cartosentry::contracts::make_global_coordinate(
      latitude_deg, longitude_deg, altitude_m,
      cartosentry::contracts::VerticalDatum::wgs84_ellipsoid);
}

auto geographic_bounds_to_dict(
    const cartosentry::ingest::GeographicBounds &bounds) -> py::dict {
  py::dict result;
  result["minimum_latitude_deg"] = bounds.minimum_latitude_deg;
  result["maximum_latitude_deg"] = bounds.maximum_latitude_deg;
  result["minimum_longitude_deg"] = bounds.minimum_longitude_deg;
  result["maximum_longitude_deg"] = bounds.maximum_longitude_deg;
  return result;
}

auto matrix_to_dict(const cartosentry::ingest::MatrixSummary &matrix)
    -> py::dict {
  py::dict result;
  result["source_key"] = matrix.source_key;
  result["target_frame"] = matrix.target_frame;
  result["source_frame"] = matrix.source_frame;
  result["convention"] = "T_target_source";
  result["row_major_values"] = matrix.row_major_values;
  result["rotation_orthonormality_error"] =
      matrix.rotation_orthonormality_error;
  result["rotation_determinant"] = matrix.rotation_determinant;
  return result;
}

auto lidar_frame_to_dict(const cartosentry::ingest::LidarFrameSummary &frame)
    -> py::dict {
  py::dict result;
  result["frame_id"] = frame.frame_id;
  result["source_bytes"] = frame.source_bytes;
  result["point_count"] = frame.point_count;
  result["scan_midpoint_ns"] = frame.scan_midpoint_ns;
  result["first_point_ns"] = frame.first_point_ns;
  result["last_point_ns"] = frame.last_point_ns;
  result["minimum_relative_time_seconds"] = frame.minimum_relative_time_seconds;
  result["maximum_relative_time_seconds"] = frame.maximum_relative_time_seconds;
  result["minimum_relative_time_bits"] = frame.minimum_relative_time_bits;
  result["maximum_relative_time_bits"] = frame.maximum_relative_time_bits;
  result["minimum_laser_id"] = frame.minimum_laser_id;
  result["maximum_laser_id"] = frame.maximum_laser_id;
  result["timestamps_nondecreasing"] = frame.timestamps_nondecreasing;
  result["required_fields_finite"] = frame.required_fields_finite;
  return result;
}

auto inspection_to_dict(
    const cartosentry::ingest::BoreasInspectionResult &inspection) -> py::dict {
  py::dict result;
  result["schema_version"] = inspection.schema_version;
  result["adapter_version"] = inspection.adapter_version;
  result["sequence_id"] = inspection.sequence_id;

  py::dict trajectory;
  trajectory["source_key"] = inspection.trajectory.source_key;
  trajectory["position_frame"] = inspection.trajectory.position_frame;
  trajectory["pose_target_frame"] = inspection.trajectory.pose_target_frame;
  trajectory["pose_source_frame"] = inspection.trajectory.pose_source_frame;
  trajectory["pose_convention"] = inspection.trajectory.pose_convention;
  trajectory["time_epoch"] = inspection.trajectory.time_epoch;
  trajectory["time_reference"] = inspection.trajectory.time_reference;
  trajectory["raw_time_unit"] = inspection.trajectory.raw_time_unit;
  trajectory["normalized_time_unit"] =
      inspection.trajectory.normalized_time_unit;
  trajectory["angular_input_unit"] = inspection.trajectory.angular_input_unit;
  trajectory["angular_output_unit"] = inspection.trajectory.angular_output_unit;
  trajectory["angular_conversion"] = inspection.trajectory.angular_conversion;
  trajectory["vertical_datum"] = inspection.trajectory.vertical_datum;
  trajectory["row_count"] = inspection.trajectory.row_count;
  trajectory["clip_row_count"] = inspection.trajectory.clip_row_count;
  trajectory["first_time_ns"] = inspection.trajectory.first_time_ns;
  trajectory["last_time_ns"] = inspection.trajectory.last_time_ns;
  trajectory["clip_first_time_ns"] = inspection.trajectory.clip_first_time_ns;
  trajectory["clip_last_time_ns"] = inspection.trajectory.clip_last_time_ns;
  trajectory["wgs84_bounds"] =
      geographic_bounds_to_dict(inspection.trajectory.wgs84_bounds);
  trajectory["clip_wgs84_bounds"] =
      geographic_bounds_to_dict(inspection.trajectory.clip_wgs84_bounds);
  trajectory["enu_minimum_m"] = inspection.trajectory.enu_minimum_m;
  trajectory["enu_maximum_m"] = inspection.trajectory.enu_maximum_m;
  trajectory["local_origin_deg"] = inspection.trajectory.local_origin_deg;
  trajectory["maximum_local_coordinate_magnitude_m"] =
      inspection.trajectory.maximum_local_coordinate_magnitude_m;
  trajectory["maximum_local_float32_quantization_m"] =
      inspection.trajectory.maximum_local_float32_quantization_m;
  trajectory["maximum_global_ecef_float32_quantization_m"] =
      inspection.trajectory.maximum_global_ecef_float32_quantization_m;
  trajectory["maximum_wgs84_local_roundtrip_error_m"] =
      inspection.trajectory.maximum_wgs84_local_roundtrip_error_m;
  trajectory["route_crosscheck_sample_count"] =
      inspection.trajectory.route_crosscheck_sample_count;
  trajectory["route_polyline_point_count"] =
      inspection.trajectory.route_polyline_point_count;
  trajectory["route_sample_stride_rows"] =
      inspection.trajectory.route_sample_stride_rows;
  trajectory["route_crosscheck_p95_m"] =
      inspection.trajectory.route_crosscheck_p95_m;
  trajectory["route_crosscheck_maximum_m"] =
      inspection.trajectory.route_crosscheck_maximum_m;
  trajectory["road_region_contains_trajectory"] =
      inspection.trajectory.road_region_contains_trajectory;
  result["trajectory"] = std::move(trajectory);

  py::dict lidar;
  lidar["coordinate_frame"] = inspection.lidar.coordinate_frame;
  lidar["record_layout"] = inspection.lidar.record_layout;
  lidar["byte_order"] = inspection.lidar.byte_order;
  lidar["relative_time_unit"] = inspection.lidar.relative_time_unit;
  lidar["relative_time_reference"] = inspection.lidar.relative_time_reference;
  lidar["relative_time_rounding"] = inspection.lidar.relative_time_rounding;
  lidar["maximum_time_conversion_error_ns"] =
      inspection.lidar.maximum_time_conversion_error_ns;
  lidar["total_points"] = inspection.lidar.total_points;
  lidar["total_bytes"] = inspection.lidar.total_bytes;
  lidar["first_point_ns"] = inspection.lidar.first_point_ns;
  lidar["last_point_ns"] = inspection.lidar.last_point_ns;
  py::list frames;
  for (const auto &frame : inspection.lidar.frames) {
    frames.append(lidar_frame_to_dict(frame));
  }
  lidar["frames"] = std::move(frames);
  result["lidar"] = std::move(lidar);

  py::dict poses;
  poses["source_key"] = inspection.lidar_poses.source_key;
  poses["row_count"] = inspection.lidar_poses.row_count;
  poses["selected_frame_matches"] =
      inspection.lidar_poses.selected_frame_matches;
  poses["first_time_ns"] = inspection.lidar_poses.first_time_ns;
  poses["last_time_ns"] = inspection.lidar_poses.last_time_ns;
  poses["target_frame"] = inspection.lidar_poses.target_frame;
  poses["source_frame"] = inspection.lidar_poses.source_frame;
  poses["convention"] = "T_target_source";
  result["lidar_poses"] = std::move(poses);

  py::list calibrations;
  for (const auto &matrix : inspection.calibrations) {
    calibrations.append(matrix_to_dict(matrix));
  }
  result["calibrations"] = std::move(calibrations);
  result["unique_input_bytes"] = inspection.unique_input_bytes;
  result["peak_rss_bytes"] = inspection.peak_rss_bytes;
  result["elapsed_seconds"] = inspection.elapsed_seconds;
  return result;
}

auto synthetic_scenario_to_dict(
    const cartosentry::spikes::SyntheticScenarioResult &scenario) -> py::dict {
  py::dict result;
  result["scenario_id"] = scenario.scenario_id;
  result["observability"] = scenario.observability;
  result["moving"] = scenario.moving;
  result["structured"] = scenario.structured;
  result["clean_alignment_rmse_m"] = scenario.clean_alignment_rmse_m;
  result["point_time_shift_alignment_rmse_m"] =
      scenario.point_time_shift_alignment_rmse_m;
  result["trajectory_shift_alignment_rmse_m"] =
      scenario.trajectory_shift_alignment_rmse_m;
  result["point_time_shift_separated"] = scenario.point_time_shift_separated;
  result["trajectory_shift_separated"] = scenario.trajectory_shift_separated;
  return result;
}

auto synthetic_scenarios_to_list(
    const std::vector<cartosentry::spikes::SyntheticScenarioResult> &scenarios)
    -> py::list {
  py::list result;
  for (const auto &scenario : scenarios) {
    result.append(synthetic_scenario_to_dict(scenario));
  }
  return result;
}

auto tiny_route_to_dict(const cartosentry::spikes::TinyRouteResult &route)
    -> py::dict {
  py::dict result;
  result["exact_arc_path"] = route.exact_arc_path;
  result["exact_cost"] = route.exact_cost;
  result["brute_force_cost"] = route.brute_force_cost;
  result["exact_route_valid"] = route.exact_route_valid;
  result["exact_matches_brute_force"] = route.exact_matches_brute_force;
  result["explored_states"] = route.explored_states;
  return result;
}

auto observability_to_dict(
    const cartosentry::spikes::ObservabilitySpikeResult &spike) -> py::dict {
  py::dict result;
  result["schema_version"] = spike.schema_version;
  result["spike_version"] = spike.spike_version;
  result["synthetic_scenarios"] =
      synthetic_scenarios_to_list(spike.synthetic_scenarios);

  py::dict alignment;
  alignment["sequence_id"] = spike.public_alignment.sequence_id;
  alignment["point_time_source"] = spike.public_alignment.point_time_source;
  alignment["trajectory_pose_convention"] =
      spike.public_alignment.trajectory_pose_convention;
  alignment["lidar_frames"] = spike.public_alignment.lidar_frames;
  alignment["sampled_points"] = spike.public_alignment.sampled_points;
  alignment["minimum_speed_mps"] = spike.public_alignment.minimum_speed_mps;
  alignment["maximum_speed_mps"] = spike.public_alignment.maximum_speed_mps;
  alignment["heading_change_rad"] = spike.public_alignment.heading_change_rad;
  alignment["clean_alignment_mean_m"] =
      spike.public_alignment.clean_alignment_mean_m;
  alignment["point_time_shift_alignment_mean_m"] =
      spike.public_alignment.point_time_shift_alignment_mean_m;
  alignment["trajectory_shift_alignment_mean_m"] =
      spike.public_alignment.trajectory_shift_alignment_mean_m;
  alignment["point_time_transform_effect_mean_m"] =
      spike.public_alignment.point_time_transform_effect_mean_m;
  alignment["trajectory_transform_effect_mean_m"] =
      spike.public_alignment.trajectory_transform_effect_mean_m;
  alignment["observable_motion"] = spike.public_alignment.observable_motion;
  alignment["observable_structure"] =
      spike.public_alignment.observable_structure;
  alignment["point_time_shift_separated"] =
      spike.public_alignment.point_time_shift_separated;
  alignment["trajectory_shift_separated"] =
      spike.public_alignment.trajectory_shift_separated;
  result["public_alignment"] = std::move(alignment);

  py::dict map_match;
  map_match["graph_import_profile"] =
      spike.public_map_match.graph_import_profile;
  map_match["distance_coverage_method"] =
      spike.public_map_match.distance_coverage_method;
  map_match["imported_nodes"] = spike.public_map_match.imported_nodes;
  map_match["imported_ways"] = spike.public_map_match.imported_ways;
  map_match["imported_directed_arcs"] =
      spike.public_map_match.imported_directed_arcs;
  map_match["excluded_ways"] = spike.public_map_match.excluded_ways;
  map_match["moving_observations"] = spike.public_map_match.moving_observations;
  map_match["confident_observations"] =
      spike.public_map_match.confident_observations;
  map_match["candidate_moving_distance_m"] =
      spike.public_map_match.candidate_moving_distance_m;
  map_match["confident_moving_distance_m"] =
      spike.public_map_match.confident_moving_distance_m;
  map_match["confident_distance_fraction"] =
      spike.public_map_match.confident_distance_fraction;
  map_match["confident_lateral_p95_m"] =
      spike.public_map_match.confident_lateral_p95_m;
  result["public_map_match"] = std::move(map_match);
  result["tiny_route"] = tiny_route_to_dict(spike.tiny_route);
  result["elapsed_seconds"] = spike.elapsed_seconds;
  return result;
}

} // namespace

PYBIND11_MODULE(_core, module) {
  module.doc() = "Checked native CartoSentry foundation";
  py::register_exception<cartosentry::ingest::BoreasFormatError>(
      module, "BoreasFormatError", PyExc_ValueError);
  module.def("native_self_check", &cartosentry::core::native_self_check);
  module.def("canonicalize_artifact_json",
             &cartosentry::contracts::canonicalize_artifact_json,
             py::arg("input_json"), py::arg("expected_schema"));
  module.def("checked_translation_norm",
             &cartosentry::core::checked_translation_norm);
  module.def("decimal_seconds_to_nanoseconds",
             &cartosentry::contracts::decimal_seconds_to_nanoseconds,
             py::arg("decimal_lexeme"));
  module.def(
      "checked_time_difference_ns",
      [](std::int64_t end_value_ns, const std::string &end_epoch,
         const std::string &end_clock_id, std::int64_t start_value_ns,
         const std::string &start_epoch, const std::string &start_clock_id) {
        return cartosentry::contracts::checked_difference(
                   cartosentry::contracts::TimePoint{
                       end_value_ns,
                       cartosentry::contracts::parse_time_epoch(end_epoch),
                       end_clock_id, cartosentry::contracts::TimeReference::unknown},
                   cartosentry::contracts::TimePoint{
                       start_value_ns,
                       cartosentry::contracts::parse_time_epoch(start_epoch),
                       start_clock_id,
                       cartosentry::contracts::TimeReference::unknown})
            .value_ns;
      },
      py::arg("end_value_ns"), py::arg("end_epoch"),
      py::arg("end_clock_id"), py::arg("start_value_ns"),
      py::arg("start_epoch"), py::arg("start_clock_id"));
  module.def(
      "checked_time_add_ns",
      [](std::int64_t value_ns, const std::string &epoch,
         const std::string &clock_id, std::int64_t duration_ns) {
        return cartosentry::contracts::checked_add(
                   cartosentry::contracts::TimePoint{
                       value_ns, cartosentry::contracts::parse_time_epoch(epoch),
                       clock_id, cartosentry::contracts::TimeReference::unknown},
                   cartosentry::contracts::Duration{duration_ns})
            .value_ns;
      },
      py::arg("value_ns"), py::arg("epoch"), py::arg("clock_id"),
      py::arg("duration_ns"));
  module.def("normalize_quaternion",
             [](const std::array<double, 4> &quaternion_wxyz) {
               return quaternion_to_dict(make_quaternion(quaternion_wxyz));
             });
  module.def("quaternion_from_rotation_matrix",
             [](const std::array<double, 9> &row_major_values) {
               return quaternion_to_dict(
                   cartosentry::contracts::quaternion_from_rotation_matrix(
                       row_major_values));
             });
  module.def(
      "compose_rigid_transforms",
      [](const std::string &outer_target_frame,
         const std::string &outer_source_frame,
         const std::array<double, 3> &outer_translation_m,
         const std::array<double, 4> &outer_quaternion_wxyz,
         const std::string &inner_target_frame,
         const std::string &inner_source_frame,
         const std::array<double, 3> &inner_translation_m,
         const std::array<double, 4> &inner_quaternion_wxyz) {
        return transform_to_dict(cartosentry::contracts::compose(
            make_transform(outer_target_frame, outer_source_frame,
                           outer_translation_m, outer_quaternion_wxyz),
            make_transform(inner_target_frame, inner_source_frame,
                           inner_translation_m, inner_quaternion_wxyz)));
      });
  module.def(
      "invert_rigid_transform",
      [](const std::string &target_frame, const std::string &source_frame,
         const std::array<double, 3> &translation_m,
         const std::array<double, 4> &quaternion_wxyz) {
        return transform_to_dict(cartosentry::contracts::inverse(make_transform(
            target_frame, source_frame, translation_m, quaternion_wxyz)));
      });
  module.def(
      "interpolate_rigid_transform",
      [](const std::string &target_frame, const std::string &source_frame,
         const std::array<double, 3> &begin_translation_m,
         const std::array<double, 4> &begin_quaternion_wxyz,
         const std::array<double, 3> &end_translation_m,
         const std::array<double, 4> &end_quaternion_wxyz, double fraction) {
        return transform_to_dict(cartosentry::contracts::interpolate(
            make_transform(target_frame, source_frame, begin_translation_m,
                           begin_quaternion_wxyz),
            make_transform(target_frame, source_frame, end_translation_m,
                           end_quaternion_wxyz),
            fraction));
      });
  module.def(
      "transform_point",
      [](const std::string &target_frame, const std::string &source_frame,
         const std::array<double, 3> &translation_m,
         const std::array<double, 4> &quaternion_wxyz,
         const std::array<double, 3> &point_source) {
        return cartosentry::contracts::transform_point(
            make_transform(target_frame, source_frame, translation_m,
                           quaternion_wxyz),
            point_source);
      });
  module.def(
      "wgs84_to_local",
      [](double origin_latitude_deg, double origin_longitude_deg,
         double origin_altitude_m, double latitude_deg, double longitude_deg,
         double altitude_m, const std::string &local_frame) {
        const auto local = cartosentry::contracts::global_to_local(
            make_global_coordinate(origin_latitude_deg, origin_longitude_deg,
                                   origin_altitude_m),
            make_global_coordinate(latitude_deg, longitude_deg, altitude_m),
            local_frame);
        py::dict result;
        result["frame"] = local.frame;
        result["position_m"] = local.position_m;
        return result;
      });
  module.def(
      "local_to_wgs84",
      [](double origin_latitude_deg, double origin_longitude_deg,
         double origin_altitude_m, const std::string &local_frame,
         const std::array<double, 3> &position_m) {
        return global_coordinate_to_dict(cartosentry::contracts::local_to_global(
            make_global_coordinate(origin_latitude_deg, origin_longitude_deg,
                                   origin_altitude_m),
            cartosentry::contracts::LocalCoordinate{local_frame, position_m}));
      });
  module.def("native_build_info", [] {
    const auto info = cartosentry::core::native_build_info();
    py::dict result;
    result["project_version"] = info.project_version;
    result["compiler"] = info.compiler;
    result["se3_implementation"] = info.se3_implementation;
    result["cxx_standard"] = info.cxx_standard;
    return result;
  });
  module.def(
      "inspect_boreas_sequence",
      [](const std::string &sequence_root, const std::string &route_html_path,
         const std::array<double, 4> &road_region,
         std::size_t route_sample_stride_rows) {
        cartosentry::ingest::BoreasInspectionResult inspection;
        {
          py::gil_scoped_release release;
          inspection = cartosentry::ingest::inspect_boreas_sequence(
              std::filesystem::path(sequence_root),
              std::filesystem::path(route_html_path),
              cartosentry::ingest::GeographicBounds{
                  road_region[0], road_region[1], road_region[2],
                  road_region[3]},
              route_sample_stride_rows);
        }
        return inspection_to_dict(inspection);
      },
      py::arg("sequence_root"), py::arg("route_html_path"),
      py::arg("road_region"), py::arg("route_sample_stride_rows"));
  module.def(
      "run_synthetic_observability_suite",
      [](std::int64_t injected_point_time_shift_ns,
         double injected_trajectory_shift_m,
         double minimum_alignment_separation_m) {
        return synthetic_scenarios_to_list(
            cartosentry::spikes::run_synthetic_observability_suite(
                cartosentry::spikes::ObservabilityParameters{
                    injected_point_time_shift_ns, injected_trajectory_shift_m,
                    1U, 1U, 1.0, 1.0, 1.0, 0.0, 0.0,
                    minimum_alignment_separation_m}));
      },
      py::arg("injected_point_time_shift_ns"),
      py::arg("injected_trajectory_shift_m"),
      py::arg("minimum_alignment_separation_m"));
  module.def("solve_tiny_required_route", [] {
    return tiny_route_to_dict(cartosentry::spikes::solve_tiny_required_route());
  });
  module.def(
      "run_observability_spike",
      [](const std::string &sequence_root, const std::string &road_graph_path,
         std::int64_t injected_point_time_shift_ns,
         double injected_trajectory_shift_m, std::size_t lidar_point_stride,
         std::size_t map_trajectory_stride_rows,
         double candidate_search_radius_m, double confident_lateral_distance_m,
         double confident_heading_error_rad, double confident_score_separation,
         double minimum_moving_speed_mps,
         double minimum_alignment_separation_m) {
        cartosentry::spikes::ObservabilitySpikeResult spike;
        {
          py::gil_scoped_release release;
          spike = cartosentry::spikes::run_observability_spike(
              std::filesystem::path(sequence_root),
              std::filesystem::path(road_graph_path),
              cartosentry::spikes::ObservabilityParameters{
                  injected_point_time_shift_ns, injected_trajectory_shift_m,
                  lidar_point_stride, map_trajectory_stride_rows,
                  candidate_search_radius_m, confident_lateral_distance_m,
                  confident_heading_error_rad, confident_score_separation,
                  minimum_moving_speed_mps, minimum_alignment_separation_m});
        }
        return observability_to_dict(spike);
      },
      py::arg("sequence_root"), py::arg("road_graph_path"),
      py::arg("injected_point_time_shift_ns"),
      py::arg("injected_trajectory_shift_m"), py::arg("lidar_point_stride"),
      py::arg("map_trajectory_stride_rows"),
      py::arg("candidate_search_radius_m"),
      py::arg("confident_lateral_distance_m"),
      py::arg("confident_heading_error_rad"),
      py::arg("confident_score_separation"),
      py::arg("minimum_moving_speed_mps"),
      py::arg("minimum_alignment_separation_m"));
}
