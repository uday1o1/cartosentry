#pragma once

#include <cstddef>
#include <filesystem>
#include <string>
#include <vector>

namespace cartosentry::scheduler {

struct SchedulerQualificationParameters {
  std::size_t worker_count;
  std::size_t resident_byte_budget;
  std::size_t mixed_unit_count;
  std::size_t lidar_stride;
  std::size_t imu_estimated_bytes;
  std::size_t lidar_estimated_bytes;
};

struct SchedulerQualification {
  bool accepted;
  std::size_t resident_byte_budget;
  std::size_t peak_resident_bytes;
  std::size_t mixed_completed_units;
  std::size_t mixed_imu_units;
  std::size_t mixed_lidar_units;
  bool deterministic_replay_equal;
  std::vector<std::string> deterministic_execution_order;
  bool backpressure_observed;
  std::size_t isolated_failed_units;
  std::size_t isolated_completed_units;
  std::vector<std::string> structured_error_codes;
  std::size_t cancelled_units;
  std::size_t outstanding_units_after_cancel;
  std::size_t resident_bytes_after_cancel;
  bool completion_pointer_exists;
};

[[nodiscard]] auto qualify_bounded_scheduler(
    const std::filesystem::path& output_root,
    const SchedulerQualificationParameters& parameters)
    -> SchedulerQualification;

}  // namespace cartosentry::scheduler
