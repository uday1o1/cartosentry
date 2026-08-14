#include "cartosentry/scheduler/bounded_scheduler.hpp"

#include <algorithm>
#include <exception>
#include <stdexcept>
#include <utility>

namespace cartosentry::scheduler {
namespace {

auto modality_index(Modality modality) -> std::size_t {
  return static_cast<std::size_t>(modality);
}

auto rejection(const WorkUnit& unit, WorkErrorCode code,
               std::string detail) -> SubmitResult {
  return SubmitResult{
      .accepted = false,
      .error = WorkError{
          .unit_id = unit.unit_id,
          .stage_id = unit.stage_id,
          .code = code,
          .retryable = false,
          .detail = std::move(detail),
      },
  };
}

}  // namespace

BoundedScheduler::BoundedScheduler(SchedulerConfig config) : config_(config) {
  if (config_.worker_count == 0U) {
    throw std::invalid_argument("scheduler worker count must be positive");
  }
  if (config_.resident_byte_budget == 0U) {
    throw std::invalid_argument("scheduler byte budget must be positive");
  }
  const std::size_t worker_count =
      config_.deterministic ? 1U : config_.worker_count;
  workers_.reserve(worker_count);
  for (std::size_t index = 0; index < worker_count; ++index) {
    workers_.emplace_back(
        [this](std::stop_token stop_token) { worker_loop(stop_token); });
  }
}

BoundedScheduler::~BoundedScheduler() {
  if (!joined_) {
    request_cancel();
    static_cast<void>(wait());
  }
}

auto BoundedScheduler::submit(WorkUnit unit) -> SubmitResult {
  if (unit.unit_id.empty() || unit.stage_id.empty() || !unit.execute) {
    return rejection(unit, WorkErrorCode::task_failure,
                     "work unit identity and callable are required");
  }
  if (unit.estimated_bytes == 0U ||
      unit.estimated_bytes > config_.resident_byte_budget) {
    return rejection(unit, WorkErrorCode::invalid_estimated_bytes,
                     "estimated bytes must fit the scheduler byte budget");
  }
  if (modality_index(unit.modality) >= modality_count) {
    return rejection(unit, WorkErrorCode::task_failure,
                     "work unit modality is invalid");
  }

  const auto wait_started = std::chrono::steady_clock::now();
  std::unique_lock lock(mutex_);
  if (config_.deterministic && accepting_ && !cancellation_requested_ &&
      resident_bytes_ >
          config_.resident_byte_budget - unit.estimated_bytes) {
    return rejection(unit, WorkErrorCode::invalid_estimated_bytes,
                     "deterministic batch exceeds the scheduler byte budget");
  }
  condition_.wait(lock, [this, &unit] {
    return cancellation_requested_ || !accepting_ ||
           resident_bytes_ <=
               config_.resident_byte_budget - unit.estimated_bytes;
  });
  const auto backpressure = std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now() - wait_started);
  metrics_.backpressure_time += backpressure;
  auto& stage = stage_metrics_[unit.stage_id];
  stage.stage_id = unit.stage_id;
  stage.backpressure_time += backpressure;
  if (cancellation_requested_) {
    return rejection(unit, WorkErrorCode::cancelled,
                     "scheduler cancellation was requested");
  }
  if (!accepting_) {
    return rejection(unit, WorkErrorCode::scheduler_closed,
                     "scheduler is closed to new work");
  }

  const std::size_t bytes = unit.estimated_bytes;
  auto& queue = queues_[modality_index(unit.modality)];
  queue.push_back(QueuedUnit{
      .submission_order = next_submission_order_++,
      .unit = std::move(unit),
  });
  resident_bytes_ += bytes;
  ++metrics_.queue_depth;
  metrics_.queued_bytes += bytes;
  metrics_.resident_bytes = resident_bytes_;
  metrics_.peak_queue_depth =
      std::max(metrics_.peak_queue_depth, metrics_.queue_depth);
  metrics_.peak_queued_bytes =
      std::max(metrics_.peak_queued_bytes, metrics_.queued_bytes);
  metrics_.peak_resident_bytes =
      std::max(metrics_.peak_resident_bytes, resident_bytes_);
  ++stage.queue_depth;
  stage.queued_bytes += bytes;
  stage.peak_queue_depth = std::max(stage.peak_queue_depth, stage.queue_depth);
  stage.peak_queued_bytes = std::max(stage.peak_queued_bytes, stage.queued_bytes);
  lock.unlock();
  condition_.notify_all();
  return SubmitResult{.accepted = true, .error = std::nullopt};
}

void BoundedScheduler::close() {
  {
    std::lock_guard lock(mutex_);
    accepting_ = false;
  }
  condition_.notify_all();
}

void BoundedScheduler::request_cancel() {
  {
    std::lock_guard lock(mutex_);
    if (cancellation_requested_) {
      return;
    }
    cancellation_requested_ = true;
    accepting_ = false;
    cancel_queued_locked();
    for (auto& worker : workers_) {
      worker.request_stop();
    }
  }
  condition_.notify_all();
}

auto BoundedScheduler::metrics() const -> SchedulerMetrics {
  std::lock_guard lock(mutex_);
  return snapshot_metrics_locked();
}

auto BoundedScheduler::wait() -> SchedulerResult {
  close();
  {
    std::unique_lock lock(mutex_);
    condition_.wait(lock, [this] {
      return metrics_.queue_depth == 0U && metrics_.active_units == 0U;
    });
  }
  if (!joined_) {
    for (auto& worker : workers_) {
      if (worker.joinable()) {
        worker.join();
      }
    }
    joined_ = true;
  }
  std::lock_guard lock(mutex_);
  std::ranges::sort(outcomes_, {}, &WorkOutcome::submission_order);
  return SchedulerResult{
      .cancellation_requested = cancellation_requested_,
      .metrics = snapshot_metrics_locked(),
      .outcomes = outcomes_,
  };
}

auto BoundedScheduler::select_next_locked() -> QueuedUnit {
  for (std::size_t offset = 0; offset < modality_count; ++offset) {
    const std::size_t index = (next_modality_ + offset) % modality_count;
    auto& queue = queues_[index];
    if (queue.empty()) {
      continue;
    }
    QueuedUnit selected = std::move(queue.front());
    queue.pop_front();
    next_modality_ = (index + 1U) % modality_count;
    return selected;
  }
  throw std::logic_error("scheduler selected from an empty queue set");
}

auto BoundedScheduler::has_queued_locked() const -> bool {
  return std::ranges::any_of(queues_,
                             [](const auto& queue) { return !queue.empty(); });
}

auto BoundedScheduler::snapshot_metrics_locked() const -> SchedulerMetrics {
  SchedulerMetrics snapshot = metrics_;
  snapshot.resident_bytes = resident_bytes_;
  snapshot.stages.clear();
  snapshot.stages.reserve(stage_metrics_.size());
  for (const auto& [stage_id, stage] : stage_metrics_) {
    static_cast<void>(stage_id);
    snapshot.stages.push_back(stage);
  }
  return snapshot;
}

void BoundedScheduler::worker_loop(std::stop_token stop_token) {
  while (true) {
    QueuedUnit queued;
    {
      std::unique_lock lock(mutex_);
      condition_.wait(lock, [this, stop_token] {
        return stop_token.stop_requested() || !accepting_ ||
               (!config_.deterministic && has_queued_locked());
      });
      if (!has_queued_locked()) {
        if (!accepting_ || stop_token.stop_requested()) {
          condition_.notify_all();
          return;
        }
        continue;
      }
      queued = select_next_locked();
      const std::size_t bytes = queued.unit.estimated_bytes;
      --metrics_.queue_depth;
      metrics_.queued_bytes -= bytes;
      ++metrics_.active_units;
      metrics_.active_bytes += bytes;
      metrics_.peak_active_bytes =
          std::max(metrics_.peak_active_bytes, metrics_.active_bytes);
      auto& stage = stage_metrics_.at(queued.unit.stage_id);
      --stage.queue_depth;
      stage.queued_bytes -= bytes;
      ++stage.active_units;
      stage.active_bytes += bytes;
      stage.peak_active_bytes =
          std::max(stage.peak_active_bytes, stage.active_bytes);
    }

    std::optional<WorkError> error;
    try {
      error = queued.unit.execute(stop_token);
    } catch (const std::exception&) {
      error = WorkError{
          .unit_id = queued.unit.unit_id,
          .stage_id = queued.unit.stage_id,
          .code = WorkErrorCode::unhandled_exception,
          .retryable = false,
          .detail = "work function raised an exception",
      };
    } catch (...) {
      error = WorkError{
          .unit_id = queued.unit.unit_id,
          .stage_id = queued.unit.stage_id,
          .code = WorkErrorCode::unhandled_exception,
          .retryable = false,
          .detail = "work function raised a nonstandard exception",
      };
    }

    {
      std::lock_guard lock(mutex_);
      complete_locked(queued, std::move(error), stop_token.stop_requested());
    }
    condition_.notify_all();
  }
}

void BoundedScheduler::complete_locked(const QueuedUnit& queued,
                                       std::optional<WorkError> error,
                                       bool stop_requested) {
  const std::size_t bytes = queued.unit.estimated_bytes;
  --metrics_.active_units;
  metrics_.active_bytes -= bytes;
  resident_bytes_ -= bytes;
  metrics_.resident_bytes = resident_bytes_;
  auto& stage = stage_metrics_.at(queued.unit.stage_id);
  --stage.active_units;
  stage.active_bytes -= bytes;

  WorkState state = WorkState::completed;
  if (error.has_value()) {
    error->unit_id = queued.unit.unit_id;
    error->stage_id = queued.unit.stage_id;
  }
  if (stop_requested ||
      (error.has_value() && error->code == WorkErrorCode::cancelled)) {
    state = WorkState::cancelled;
    ++metrics_.cancelled_units;
    ++stage.cancelled_units;
    if (!error.has_value()) {
      error = WorkError{
          .unit_id = queued.unit.unit_id,
          .stage_id = queued.unit.stage_id,
          .code = WorkErrorCode::cancelled,
          .retryable = true,
          .detail = "work stopped after cooperative cancellation",
      };
    }
  } else if (error.has_value()) {
    state = WorkState::failed;
    ++metrics_.failed_units;
    ++stage.failed_units;
  } else {
    ++metrics_.completed_units;
    ++stage.completed_units;
    ++metrics_.completed_by_modality[modality_index(queued.unit.modality)];
  }
  outcomes_.push_back(WorkOutcome{
      .submission_order = queued.submission_order,
      .unit_id = queued.unit.unit_id,
      .stage_id = queued.unit.stage_id,
      .modality = queued.unit.modality,
      .state = state,
      .error = std::move(error),
  });
}

void BoundedScheduler::cancel_queued_locked() {
  for (auto& queue : queues_) {
    while (!queue.empty()) {
      QueuedUnit queued = std::move(queue.front());
      queue.pop_front();
      const std::size_t bytes = queued.unit.estimated_bytes;
      --metrics_.queue_depth;
      metrics_.queued_bytes -= bytes;
      resident_bytes_ -= bytes;
      auto& stage = stage_metrics_.at(queued.unit.stage_id);
      --stage.queue_depth;
      stage.queued_bytes -= bytes;
      ++metrics_.cancelled_units;
      ++stage.cancelled_units;
      outcomes_.push_back(WorkOutcome{
          .submission_order = queued.submission_order,
          .unit_id = queued.unit.unit_id,
          .stage_id = queued.unit.stage_id,
          .modality = queued.unit.modality,
          .state = WorkState::cancelled,
          .error = WorkError{
              .unit_id = queued.unit.unit_id,
              .stage_id = queued.unit.stage_id,
              .code = WorkErrorCode::cancelled,
              .retryable = true,
              .detail = "queued work was cancelled before execution",
          },
      });
    }
  }
  metrics_.resident_bytes = resident_bytes_;
}

auto modality_name(Modality modality) -> std::string_view {
  constexpr std::array names{"metadata", "imu", "trajectory", "lidar",
                             "camera", "radar"};
  const std::size_t index = modality_index(modality);
  if (index >= names.size()) {
    return "invalid";
  }
  return names[index];
}

auto work_state_name(WorkState state) -> std::string_view {
  switch (state) {
    case WorkState::completed:
      return "COMPLETED";
    case WorkState::failed:
      return "FAILED";
    case WorkState::cancelled:
      return "CANCELLED";
  }
  return "INVALID";
}

auto work_error_code_name(WorkErrorCode code) -> std::string_view {
  switch (code) {
    case WorkErrorCode::task_failure:
      return "TASK_FAILURE";
    case WorkErrorCode::unhandled_exception:
      return "UNHANDLED_EXCEPTION";
    case WorkErrorCode::cancelled:
      return "CANCELLED";
    case WorkErrorCode::scheduler_closed:
      return "SCHEDULER_CLOSED";
    case WorkErrorCode::invalid_estimated_bytes:
      return "INVALID_ESTIMATED_BYTES";
  }
  return "INVALID";
}

}  // namespace cartosentry::scheduler
