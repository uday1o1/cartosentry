#include "cartosentry/core/native_check.hpp"
#include "cartosentry/ingest/boreas_inspector.hpp"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <array>
#include <filesystem>
#include <string>

namespace py = pybind11;

namespace {

auto geographic_bounds_to_dict(
    const cartosentry::ingest::GeographicBounds& bounds) -> py::dict {
  py::dict result;
  result["minimum_latitude_deg"] = bounds.minimum_latitude_deg;
  result["maximum_latitude_deg"] = bounds.maximum_latitude_deg;
  result["minimum_longitude_deg"] = bounds.minimum_longitude_deg;
  result["maximum_longitude_deg"] = bounds.maximum_longitude_deg;
  return result;
}

auto matrix_to_dict(const cartosentry::ingest::MatrixSummary& matrix)
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

auto lidar_frame_to_dict(
    const cartosentry::ingest::LidarFrameSummary& frame) -> py::dict {
  py::dict result;
  result["frame_id"] = frame.frame_id;
  result["source_bytes"] = frame.source_bytes;
  result["point_count"] = frame.point_count;
  result["scan_midpoint_ns"] = frame.scan_midpoint_ns;
  result["first_point_ns"] = frame.first_point_ns;
  result["last_point_ns"] = frame.last_point_ns;
  result["minimum_relative_time_seconds"] =
      frame.minimum_relative_time_seconds;
  result["maximum_relative_time_seconds"] =
      frame.maximum_relative_time_seconds;
  result["minimum_relative_time_bits"] = frame.minimum_relative_time_bits;
  result["maximum_relative_time_bits"] = frame.maximum_relative_time_bits;
  result["minimum_laser_id"] = frame.minimum_laser_id;
  result["maximum_laser_id"] = frame.maximum_laser_id;
  result["timestamps_nondecreasing"] = frame.timestamps_nondecreasing;
  result["required_fields_finite"] = frame.required_fields_finite;
  return result;
}

auto inspection_to_dict(
    const cartosentry::ingest::BoreasInspectionResult& inspection) -> py::dict {
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
  trajectory["angular_input_unit"] =
      inspection.trajectory.angular_input_unit;
  trajectory["angular_output_unit"] =
      inspection.trajectory.angular_output_unit;
  trajectory["angular_conversion"] =
      inspection.trajectory.angular_conversion;
  trajectory["vertical_datum"] = inspection.trajectory.vertical_datum;
  trajectory["row_count"] = inspection.trajectory.row_count;
  trajectory["clip_row_count"] = inspection.trajectory.clip_row_count;
  trajectory["first_time_ns"] = inspection.trajectory.first_time_ns;
  trajectory["last_time_ns"] = inspection.trajectory.last_time_ns;
  trajectory["clip_first_time_ns"] =
      inspection.trajectory.clip_first_time_ns;
  trajectory["clip_last_time_ns"] =
      inspection.trajectory.clip_last_time_ns;
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
  lidar["relative_time_reference"] =
      inspection.lidar.relative_time_reference;
  lidar["relative_time_rounding"] = inspection.lidar.relative_time_rounding;
  lidar["maximum_time_conversion_error_ns"] =
      inspection.lidar.maximum_time_conversion_error_ns;
  lidar["total_points"] = inspection.lidar.total_points;
  lidar["total_bytes"] = inspection.lidar.total_bytes;
  lidar["first_point_ns"] = inspection.lidar.first_point_ns;
  lidar["last_point_ns"] = inspection.lidar.last_point_ns;
  py::list frames;
  for (const auto& frame : inspection.lidar.frames) {
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
  for (const auto& matrix : inspection.calibrations) {
    calibrations.append(matrix_to_dict(matrix));
  }
  result["calibrations"] = std::move(calibrations);
  result["unique_input_bytes"] = inspection.unique_input_bytes;
  result["peak_rss_bytes"] = inspection.peak_rss_bytes;
  result["elapsed_seconds"] = inspection.elapsed_seconds;
  return result;
}

}  // namespace

PYBIND11_MODULE(_core, module) {
  module.doc() = "Checked native CartoSentry foundation";
  py::register_exception<cartosentry::ingest::BoreasFormatError>(
      module, "BoreasFormatError", PyExc_ValueError);
  module.def("native_self_check", &cartosentry::core::native_self_check);
  module.def("checked_translation_norm", &cartosentry::core::checked_translation_norm);
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
      [](const std::string& sequence_root, const std::string& route_html_path,
         const std::array<double, 4>& road_region,
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
}
