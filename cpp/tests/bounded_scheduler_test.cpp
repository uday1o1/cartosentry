#include "cartosentry/scheduler/bounded_scheduler.hpp"

#include <catch2/catch_test_macros.hpp>

#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <functional>
#include <future>
#include <latch>
#include <mutex>
#include <optional>
#include <semaphore>
#include <stdexcept>
#include <stop_token>
#include <string>
#include <thread>
#include <vector>

namespace {

using cartosentry::scheduler::BoundedScheduler;
using cartosentry::scheduler::Modality;
using cartosentry::scheduler::SchedulerConfig;
using cartosentry::scheduler::WorkError;
using cartosentry::scheduler::WorkErrorCode;
using cartosentry::scheduler::WorkState;
using cartosentry::scheduler::WorkUnit;

auto successful(std::function<void()> function = [] {}) {
  return [function = std::move(function)](std::stop_token)
             -> std::optional<WorkError> {
    function();
    return std::nullopt;
  };
}

auto unit(std::string id, Modality modality, std::size_t bytes,
          cartosentry::scheduler::WorkFunction execute,
          std::string stage = "stress") -> WorkUnit {
  return WorkUnit{
      .unit_id = std::move(id),
      .stage_id = std::move(stage),
      .modality = modality,
      .estimated_bytes = bytes,
      .execute = std::move(execute),
  };
}

}  // namespace

TEST_CASE("deterministic scheduler uses stable fair modality order") {
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 4,
      .resident_byte_budget = 4096,
      .deterministic = true,
  });
  std::mutex trace_mutex;
  std::vector<std::string> trace;
  const auto traced = [&trace_mutex, &trace](std::string id) {
    return successful([&trace_mutex, &trace, id = std::move(id)] {
      std::lock_guard lock(trace_mutex);
      trace.push_back(id);
    });
  };

  REQUIRE(scheduler.submit(unit("metadata-1", Modality::metadata, 32,
                                traced("metadata-1")))
              .accepted);
  REQUIRE(scheduler.submit(
                       unit("imu-1", Modality::imu, 64, traced("imu-1")))
              .accepted);
  REQUIRE(scheduler.submit(unit("lidar-1", Modality::lidar, 1024,
                                traced("lidar-1")))
              .accepted);
  REQUIRE(scheduler.submit(unit("lidar-2", Modality::lidar, 1024,
                                traced("lidar-2")))
              .accepted);
  REQUIRE(scheduler.submit(
                       unit("imu-2", Modality::imu, 64, traced("imu-2")))
              .accepted);
  REQUIRE(scheduler.submit(unit("metadata-2", Modality::metadata, 32,
                                traced("metadata-2")))
              .accepted);

  const auto result = scheduler.wait();
  CHECK(trace == std::vector<std::string>{"metadata-1", "imu-1", "lidar-1",
                                          "metadata-2", "imu-2", "lidar-2"});
  CHECK(result.metrics.completed_units == 6);
  CHECK(result.metrics.failed_units == 0);
  CHECK(result.metrics.cancelled_units == 0);
  CHECK(result.metrics.queue_depth == 0);
  CHECK(result.metrics.active_bytes == 0);
  CHECK(result.metrics.resident_bytes == 0);
  CHECK(result.metrics.peak_resident_bytes == 2240);
  REQUIRE(result.metrics.stages.size() == 1);
  CHECK(result.metrics.stages.front().completed_units == 6);
}

TEST_CASE("mixed tiny IMU and large lidar stress remains bounded") {
  constexpr std::size_t task_count = 2000;
  constexpr std::size_t lidar_stride = 10;
  constexpr std::size_t budget = 32 * 1024;
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 4,
      .resident_byte_budget = budget,
      .deterministic = false,
  });
  std::atomic<std::size_t> completed{0};

  for (std::size_t index = 0; index < task_count; ++index) {
    const bool is_lidar = index % lidar_stride == 0;
    const Modality modality = is_lidar ? Modality::lidar : Modality::imu;
    const std::size_t bytes = is_lidar ? 4096 : 64;
    auto result = scheduler.submit(unit(
        "mixed-" + std::to_string(index), modality, bytes,
        successful([&completed] { completed.fetch_add(1U); }), "mixed-stage"));
    REQUIRE(result.accepted);
  }
  const auto result = scheduler.wait();
  CHECK(completed.load() == task_count);
  CHECK(result.outcomes.size() == task_count);
  CHECK(result.metrics.completed_units == task_count);
  CHECK(result.metrics.completed_by_modality[1] == 1800);
  CHECK(result.metrics.completed_by_modality[3] == 200);
  CHECK(result.metrics.peak_resident_bytes <= budget);
  CHECK(result.metrics.resident_bytes == 0);
  CHECK(result.metrics.queue_depth == 0);
  CHECK(result.metrics.active_units == 0);
}

TEST_CASE("producer backpressure is measured in bytes") {
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 1,
      .resident_byte_budget = 1024,
      .deterministic = false,
  });
  std::binary_semaphore started{0};
  std::binary_semaphore release{0};
  REQUIRE(scheduler
              .submit(unit("large", Modality::lidar, 800,
                           successful([&started, &release] {
                             started.release();
                             release.acquire();
                           })))
              .accepted);
  started.acquire();
  auto blocked_submit = std::async(std::launch::async, [&scheduler] {
    return scheduler.submit(
        unit("waiting", Modality::imu, 300, successful()));
  });
  CHECK(blocked_submit.wait_for(std::chrono::milliseconds(20)) ==
        std::future_status::timeout);
  release.release();
  REQUIRE(blocked_submit.get().accepted);
  const auto result = scheduler.wait();
  CHECK(result.metrics.backpressure_time > std::chrono::nanoseconds::zero());
  CHECK(result.metrics.peak_resident_bytes <= 1024);
  CHECK(result.metrics.completed_units == 2);
}

TEST_CASE("worker errors are values and unrelated work completes") {
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 3,
      .resident_byte_budget = 4096,
      .deterministic = true,
  });
  REQUIRE(scheduler
              .submit(unit(
                  "value-error", Modality::metadata, 64,
                  [](std::stop_token) -> std::optional<WorkError> {
                    return WorkError{
                        .unit_id = "value-error",
                        .stage_id = "stress",
                        .code = WorkErrorCode::task_failure,
                        .retryable = true,
                        .detail = "synthetic checked failure",
                    };
                  }))
              .accepted);
  REQUIRE(scheduler
              .submit(unit("exception", Modality::imu, 64,
                           [](std::stop_token) -> std::optional<WorkError> {
                             throw std::runtime_error("private raw detail");
                           }))
              .accepted);
  REQUIRE(scheduler
              .submit(unit("control", Modality::lidar, 1024, successful()))
              .accepted);

  const auto result = scheduler.wait();
  CHECK(result.metrics.completed_units == 1);
  CHECK(result.metrics.failed_units == 2);
  REQUIRE(result.outcomes.size() == 3);
  CHECK(result.outcomes[0].error->code == WorkErrorCode::task_failure);
  CHECK(result.outcomes[1].error->code == WorkErrorCode::unhandled_exception);
  CHECK(result.outcomes[1].error->detail == "work function raised an exception");
  CHECK(result.outcomes[2].state == WorkState::completed);
}

TEST_CASE("cooperative cancellation drains work without a completion pointer") {
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 2,
      .resident_byte_budget = 4096,
      .deterministic = false,
  });
  std::latch active{2};
  const auto cancellable = [&active](std::stop_token stop_token)
      -> std::optional<WorkError> {
    active.count_down();
    while (!stop_token.stop_requested()) {
      std::this_thread::yield();
    }
    return WorkError{
        .unit_id = "active",
        .stage_id = "cancel-stage",
        .code = WorkErrorCode::cancelled,
        .retryable = true,
        .detail = "cooperative stop observed",
    };
  };
  REQUIRE(scheduler
              .submit(unit("active-1", Modality::lidar, 1000, cancellable,
                           "cancel-stage"))
              .accepted);
  REQUIRE(scheduler
              .submit(unit("active-2", Modality::imu, 1000, cancellable,
                           "cancel-stage"))
              .accepted);
  REQUIRE(scheduler
              .submit(unit("queued-1", Modality::lidar, 1000, successful(),
                           "cancel-stage"))
              .accepted);
  REQUIRE(scheduler
              .submit(unit("queued-2", Modality::imu, 1000, successful(),
                           "cancel-stage"))
              .accepted);
  active.wait();
  scheduler.request_cancel();
  const auto result = scheduler.wait();

  const auto unique = std::chrono::steady_clock::now().time_since_epoch().count();
  const auto attempt =
      std::filesystem::temp_directory_path() /
      ("cartosentry-scheduler-cancelled-attempt-" + std::to_string(unique));
  REQUIRE(std::filesystem::create_directories(attempt));
  const auto completion = attempt / "completion.json";
  if (!result.cancellation_requested && result.metrics.failed_units == 0U &&
      result.metrics.cancelled_units == 0U) {
    std::ofstream stream(completion);
    stream << "complete\n";
  }

  CHECK(result.cancellation_requested);
  CHECK(result.metrics.cancelled_units == 4);
  CHECK(result.metrics.completed_units == 0);
  CHECK(result.metrics.resident_bytes == 0);
  CHECK(result.metrics.queue_depth == 0);
  CHECK(result.metrics.active_units == 0);
  CHECK_FALSE(std::filesystem::exists(completion));
  std::filesystem::remove_all(attempt);
}

TEST_CASE("oversized work is rejected before entering the queue") {
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 1,
      .resident_byte_budget = 1024,
      .deterministic = true,
  });
  const auto result = scheduler.submit(
      unit("oversized", Modality::lidar, 1025, successful()));
  CHECK_FALSE(result.accepted);
  REQUIRE(result.error.has_value());
  CHECK(result.error->code == WorkErrorCode::invalid_estimated_bytes);
  const auto final = scheduler.wait();
  CHECK(final.outcomes.empty());
  CHECK(final.metrics.peak_resident_bytes == 0);
}

TEST_CASE("deterministic batches reject aggregate work beyond the budget") {
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 2,
      .resident_byte_budget = 100,
      .deterministic = true,
  });
  REQUIRE(scheduler
              .submit(unit("fits", Modality::imu, 60, successful()))
              .accepted);
  const auto rejected =
      scheduler.submit(unit("over-budget", Modality::lidar, 50, successful()));
  CHECK_FALSE(rejected.accepted);
  REQUIRE(rejected.error.has_value());
  CHECK(rejected.error->code == WorkErrorCode::invalid_estimated_bytes);
  const auto final = scheduler.wait();
  CHECK(final.metrics.completed_units == 1);
  CHECK(final.metrics.resident_bytes == 0);
}

TEST_CASE("closed schedulers reject new work as a structured value") {
  BoundedScheduler scheduler(SchedulerConfig{
      .worker_count = 1,
      .resident_byte_budget = 1024,
      .deterministic = false,
  });
  scheduler.close();
  const auto rejected =
      scheduler.submit(unit("late", Modality::imu, 64, successful()));
  CHECK_FALSE(rejected.accepted);
  REQUIRE(rejected.error.has_value());
  CHECK(rejected.error->code == WorkErrorCode::scheduler_closed);
  CHECK(scheduler.wait().outcomes.empty());
}
