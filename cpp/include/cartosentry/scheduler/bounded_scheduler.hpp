#pragma once

#include <array>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <mutex>
#include <optional>
#include <stop_token>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace cartosentry::scheduler {

enum class Modality : std::uint8_t {
  metadata = 0,
  imu = 1,
  trajectory = 2,
  lidar = 3,
  camera = 4,
  radar = 5,
};

enum class WorkState : std::uint8_t {
  completed,
  failed,
  cancelled,
};

enum class WorkErrorCode : std::uint8_t {
  task_failure,
  unhandled_exception,
  cancelled,
  scheduler_closed,
  invalid_estimated_bytes,
};

struct WorkError {
  std::string unit_id;
  std::string stage_id;
  WorkErrorCode code;
  bool retryable;
  std::string detail;
};

using WorkFunction =
    std::function<std::optional<WorkError>(std::stop_token)>;

struct WorkUnit {
  std::string unit_id;
  std::string stage_id;
  Modality modality;
  std::size_t estimated_bytes;
  WorkFunction execute;
};

struct WorkOutcome {
  std::size_t submission_order;
  std::string unit_id;
  std::string stage_id;
  Modality modality;
  WorkState state;
  std::optional<WorkError> error;
};

struct StageMetrics {
  std::string stage_id;
  std::size_t queue_depth{};
  std::size_t queued_bytes{};
  std::size_t active_units{};
  std::size_t active_bytes{};
  std::size_t peak_queue_depth{};
  std::size_t peak_queued_bytes{};
  std::size_t peak_active_bytes{};
  std::size_t completed_units{};
  std::size_t failed_units{};
  std::size_t cancelled_units{};
  std::chrono::nanoseconds backpressure_time{};
};

struct SchedulerMetrics {
  std::size_t queue_depth{};
  std::size_t queued_bytes{};
  std::size_t active_units{};
  std::size_t active_bytes{};
  std::size_t resident_bytes{};
  std::size_t peak_queue_depth{};
  std::size_t peak_queued_bytes{};
  std::size_t peak_active_bytes{};
  std::size_t peak_resident_bytes{};
  std::size_t completed_units{};
  std::size_t failed_units{};
  std::size_t cancelled_units{};
  std::chrono::nanoseconds backpressure_time{};
  std::array<std::size_t, 6> completed_by_modality{};
  std::vector<StageMetrics> stages;
};

struct SchedulerConfig {
  std::size_t worker_count;
  std::size_t resident_byte_budget;
  bool deterministic;
};

struct SubmitResult {
  bool accepted;
  std::optional<WorkError> error;
};

struct SchedulerResult {
  bool cancellation_requested;
  SchedulerMetrics metrics;
  std::vector<WorkOutcome> outcomes;
};

class BoundedScheduler {
 public:
  explicit BoundedScheduler(SchedulerConfig config);
  ~BoundedScheduler();

  BoundedScheduler(const BoundedScheduler&) = delete;
  auto operator=(const BoundedScheduler&) -> BoundedScheduler& = delete;
  BoundedScheduler(BoundedScheduler&&) = delete;
  auto operator=(BoundedScheduler&&) -> BoundedScheduler& = delete;

  [[nodiscard]] auto submit(WorkUnit unit) -> SubmitResult;
  void close();
  void request_cancel();
  [[nodiscard]] auto metrics() const -> SchedulerMetrics;
  [[nodiscard]] auto wait() -> SchedulerResult;

 private:
  struct QueuedUnit {
    std::size_t submission_order;
    WorkUnit unit;
  };

  static constexpr std::size_t modality_count = 6;

  [[nodiscard]] auto select_next_locked() -> QueuedUnit;
  [[nodiscard]] auto has_queued_locked() const -> bool;
  [[nodiscard]] auto snapshot_metrics_locked() const -> SchedulerMetrics;
  void worker_loop(std::stop_token stop_token);
  void complete_locked(const QueuedUnit& queued,
                       std::optional<WorkError> error,
                       bool stop_requested);
  void cancel_queued_locked();

  SchedulerConfig config_;
  mutable std::mutex mutex_;
  std::condition_variable condition_;
  std::array<std::deque<QueuedUnit>, modality_count> queues_;
  std::vector<std::jthread> workers_;
  std::vector<WorkOutcome> outcomes_;
  std::map<std::string, StageMetrics> stage_metrics_;
  SchedulerMetrics metrics_;
  std::size_t resident_bytes_{};
  std::size_t next_submission_order_{};
  std::size_t next_modality_{};
  bool accepting_{true};
  bool cancellation_requested_{false};
  bool joined_{false};
};

[[nodiscard]] auto modality_name(Modality modality) -> std::string_view;
[[nodiscard]] auto work_state_name(WorkState state) -> std::string_view;
[[nodiscard]] auto work_error_code_name(WorkErrorCode code)
    -> std::string_view;

}  // namespace cartosentry::scheduler
