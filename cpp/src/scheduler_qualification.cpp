#include "cartosentry/scheduler/qualification.hpp"

#include "cartosentry/scheduler/bounded_scheduler.hpp"

#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <functional>
#include <future>
#include <latch>
#include <optional>
#include <semaphore>
#include <stdexcept>
#include <stop_token>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace cartosentry::scheduler {
namespace {

auto successful(std::function<void()> function = [] {}) -> WorkFunction {
  return [function = std::move(function)](std::stop_token)
             -> std::optional<WorkError> {
    function();
    return std::nullopt;
  };
}

auto unit(std::string id, std::string stage, Modality modality,
          std::size_t bytes, WorkFunction execute) -> WorkUnit {
  return WorkUnit{
      .unit_id = std::move(id),
      .stage_id = std::move(stage),
      .modality = modality,
      .estimated_bytes = bytes,
      .execute = std::move(execute),
  };
}

void require_submit(BoundedScheduler& scheduler, WorkUnit work) {
  if (!scheduler.submit(std::move(work)).accepted) {
    throw std::runtime_error("scheduler qualification submission failed");
  }
}

auto deterministic_trace() -> std::vector<std::string> {
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 4,
      .resident_byte_budget = 4096,
      .deterministic = true,
  });
  std::vector<std::string> trace;
  const auto traced = [&trace](std::string id) {
    return successful([&trace, id = std::move(id)] { trace.push_back(id); });
  };
  require_submit(scheduler, unit("metadata-1", "deterministic",
                                 Modality::metadata, 32,
                                 traced("metadata-1")));
  require_submit(scheduler,
                 unit("imu-1", "deterministic", Modality::imu, 64,
                      traced("imu-1")));
  require_submit(scheduler, unit("lidar-1", "deterministic", Modality::lidar,
                                 1024, traced("lidar-1")));
  require_submit(scheduler, unit("lidar-2", "deterministic", Modality::lidar,
                                 1024, traced("lidar-2")));
  require_submit(scheduler,
                 unit("imu-2", "deterministic", Modality::imu, 64,
                      traced("imu-2")));
  require_submit(scheduler, unit("metadata-2", "deterministic",
                                 Modality::metadata, 32,
                                 traced("metadata-2")));
  const auto result = scheduler.wait();
  if (result.metrics.completed_units != trace.size()) {
    throw std::runtime_error("deterministic scheduler lost work");
  }
  return trace;
}

auto run_mixed_stress(const SchedulerQualificationParameters& parameters)
    -> SchedulerResult {
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = parameters.worker_count,
      .resident_byte_budget = parameters.resident_byte_budget,
      .deterministic = false,
  });
  std::atomic<std::size_t> completed{0};
  for (std::size_t index = 0; index < parameters.mixed_unit_count; ++index) {
    const bool is_lidar = index % parameters.lidar_stride == 0U;
    require_submit(
        scheduler,
        unit("mixed-" + std::to_string(index), "mixed",
             is_lidar ? Modality::lidar : Modality::imu,
             is_lidar ? parameters.lidar_estimated_bytes
                      : parameters.imu_estimated_bytes,
             successful([&completed] { completed.fetch_add(1U); })));
  }
  auto result = scheduler.wait();
  if (completed.load() != parameters.mixed_unit_count) {
    throw std::runtime_error("mixed scheduler stress lost work");
  }
  return result;
}

auto observe_backpressure() -> bool {
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 1,
      .resident_byte_budget = 1024,
      .deterministic = false,
  });
  std::binary_semaphore started{0};
  std::binary_semaphore release{0};
  require_submit(
      scheduler,
      unit("large", "backpressure", Modality::lidar, 800,
           successful([&started, &release] {
             started.release();
             release.acquire();
           })));
  started.acquire();
  auto blocked = std::async(std::launch::async, [&scheduler] {
    return scheduler.submit(unit("waiting", "backpressure", Modality::imu,
                                 300, successful()));
  });
  const bool observed =
      blocked.wait_for(std::chrono::milliseconds(20)) ==
      std::future_status::timeout;
  release.release();
  if (!blocked.get().accepted) {
    throw std::runtime_error("backpressured work was not accepted");
  }
  const auto result = scheduler.wait();
  return observed &&
         result.metrics.backpressure_time > std::chrono::nanoseconds::zero();
}

auto run_error_isolation() -> SchedulerResult {
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 2,
      .resident_byte_budget = 4096,
      .deterministic = true,
  });
  require_submit(
      scheduler,
      unit("value-error", "errors", Modality::metadata, 64,
           [](std::stop_token) -> std::optional<WorkError> {
             return WorkError{
                 .unit_id = "value-error",
                 .stage_id = "errors",
                 .code = WorkErrorCode::task_failure,
                 .retryable = true,
                 .detail = "synthetic checked failure",
             };
           }));
  require_submit(
      scheduler,
      unit("exception", "errors", Modality::imu, 64,
           [](std::stop_token) -> std::optional<WorkError> {
             throw std::runtime_error("private raw detail");
           }));
  require_submit(scheduler, unit("control", "errors", Modality::lidar, 1024,
                                 successful()));
  return scheduler.wait();
}

auto run_cancellation(const std::filesystem::path& output_root)
    -> SchedulerResult {
  const auto attempt = output_root / "cancelled-attempt";
  std::filesystem::create_directories(attempt);
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 2,
      .resident_byte_budget = 4096,
      .deterministic = false,
  });
  std::latch active{2};
  const auto cancellable = [&attempt, &active](std::string name) {
    return [attempt, &active, name = std::move(name)](std::stop_token token)
               -> std::optional<WorkError> {
      std::ofstream partial(attempt / (name + ".partial"));
      partial << "incomplete\n";
      partial.close();
      active.count_down();
      while (!token.stop_requested()) {
        std::this_thread::yield();
      }
      return WorkError{
          .unit_id = name,
          .stage_id = "cancel",
          .code = WorkErrorCode::cancelled,
          .retryable = true,
          .detail = "cooperative stop observed",
      };
    };
  };
  require_submit(scheduler, unit("active-lidar", "cancel", Modality::lidar,
                                 1000, cancellable("active-lidar")));
  require_submit(scheduler, unit("active-imu", "cancel", Modality::imu, 1000,
                                 cancellable("active-imu")));
  require_submit(scheduler, unit("queued-lidar", "cancel", Modality::lidar,
                                 1000, successful()));
  require_submit(scheduler, unit("queued-imu", "cancel", Modality::imu, 1000,
                                 successful()));
  active.wait();
  scheduler.request_cancel();
  auto result = scheduler.wait();
  if (!result.cancellation_requested && result.metrics.failed_units == 0U &&
      result.metrics.cancelled_units == 0U) {
    std::ofstream completion(attempt / "completion.json");
    completion << "complete\n";
  }
  return result;
}

}  // namespace

auto qualify_bounded_scheduler(
    const std::filesystem::path& output_root,
    const SchedulerQualificationParameters& parameters)
    -> SchedulerQualification {
  if (std::filesystem::exists(output_root)) {
    throw std::invalid_argument("scheduler qualification output already exists");
  }
  if (parameters.worker_count == 0U ||
      parameters.resident_byte_budget == 0U ||
      parameters.mixed_unit_count == 0U || parameters.lidar_stride == 0U ||
      parameters.imu_estimated_bytes == 0U ||
      parameters.lidar_estimated_bytes == 0U ||
      parameters.imu_estimated_bytes > parameters.resident_byte_budget ||
      parameters.lidar_estimated_bytes > parameters.resident_byte_budget) {
    throw std::invalid_argument("scheduler qualification parameters are invalid");
  }
  std::filesystem::create_directories(output_root);

  const auto first_trace = deterministic_trace();
  const auto second_trace = deterministic_trace();
  const auto mixed = run_mixed_stress(parameters);
  const bool backpressure = observe_backpressure();
  const auto errors = run_error_isolation();
  const auto cancelled = run_cancellation(output_root);
  std::vector<std::string> error_codes;
  for (const auto& outcome : errors.outcomes) {
    if (outcome.error.has_value()) {
      error_codes.emplace_back(work_error_code_name(outcome.error->code));
    }
  }
  const std::size_t outstanding = cancelled.metrics.queue_depth +
                                  cancelled.metrics.active_units;
  const bool completion_exists = std::filesystem::exists(
      output_root / "cancelled-attempt" / "completion.json");
  const std::size_t expected_lidar =
      ((parameters.mixed_unit_count - 1U) / parameters.lidar_stride) + 1U;
  const std::size_t expected_imu = parameters.mixed_unit_count - expected_lidar;
  const bool accepted =
      first_trace == second_trace &&
      mixed.metrics.completed_units == parameters.mixed_unit_count &&
      mixed.metrics
              .completed_by_modality[static_cast<std::size_t>(Modality::imu)] ==
          expected_imu &&
      mixed.metrics
              .completed_by_modality[static_cast<std::size_t>(Modality::lidar)] ==
          expected_lidar &&
      mixed.metrics.peak_resident_bytes <= parameters.resident_byte_budget &&
      backpressure && errors.metrics.failed_units == 2U &&
      errors.metrics.completed_units == 1U &&
      cancelled.metrics.cancelled_units == 4U && outstanding == 0U &&
      cancelled.metrics.resident_bytes == 0U && !completion_exists;
  return SchedulerQualification{
      .accepted = accepted,
      .resident_byte_budget = parameters.resident_byte_budget,
      .peak_resident_bytes = mixed.metrics.peak_resident_bytes,
      .mixed_completed_units = mixed.metrics.completed_units,
      .mixed_imu_units =
          mixed.metrics.completed_by_modality[static_cast<std::size_t>(
              Modality::imu)],
      .mixed_lidar_units =
          mixed.metrics.completed_by_modality[static_cast<std::size_t>(
              Modality::lidar)],
      .deterministic_replay_equal = first_trace == second_trace,
      .deterministic_execution_order = first_trace,
      .backpressure_observed = backpressure,
      .isolated_failed_units = errors.metrics.failed_units,
      .isolated_completed_units = errors.metrics.completed_units,
      .structured_error_codes = std::move(error_codes),
      .cancelled_units = cancelled.metrics.cancelled_units,
      .outstanding_units_after_cancel = outstanding,
      .resident_bytes_after_cancel = cancelled.metrics.resident_bytes,
      .completion_pointer_exists = completion_exists,
  };
}

}  // namespace cartosentry::scheduler
