#include "cartosentry/map/road_bins.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <map>
#include <ranges>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace cartosentry::bins {
namespace {

struct TimeInterval {
  std::int64_t start{};
  std::int64_t end{};
};

struct Piece {
  std::size_t arc_index{};
  std::size_t bin_index{};
  std::string road_match_id;
  std::string sequence_id;
  std::string source_group_id;
  std::int64_t start_time_ns{};
  std::int64_t end_time_ns{};
  double entry_offset_m{};
  double exit_offset_m{};
  double distance_m{};
  double start_speed_mps{};
  double end_speed_mps{};
  double yaw_excitation_rad{};
  std::size_t traversal_ordinal{};
};

struct TraversalAccumulator {
  std::vector<Piece> pieces;
};

struct ModalityAccumulator {
  Modality modality{Modality::trajectory};
  std::vector<TimeInterval> valid_intervals;
  std::vector<TimeInterval> timestamp_intervals;
  double point_support{};
  double overlap_support_duration_product{};
  std::int64_t overlap_support_duration_ns{};
  std::set<std::string> evidence_ids;
};

auto checked_difference(std::int64_t end, std::int64_t start) -> std::int64_t {
  const auto result = static_cast<__int128>(end) - static_cast<__int128>(start);
  if (result < static_cast<__int128>(std::numeric_limits<std::int64_t>::min()) ||
      result > static_cast<__int128>(std::numeric_limits<std::int64_t>::max())) {
    throw std::invalid_argument("road-bin time difference overflows int64");
  }
  return static_cast<std::int64_t>(result);
}

auto checked_add(std::int64_t left, std::int64_t right) -> std::int64_t {
  const auto result = static_cast<__int128>(left) + static_cast<__int128>(right);
  if (result < static_cast<__int128>(std::numeric_limits<std::int64_t>::min()) ||
      result > static_cast<__int128>(std::numeric_limits<std::int64_t>::max())) {
    throw std::invalid_argument("road-bin duration sum overflows int64");
  }
  return static_cast<std::int64_t>(result);
}

auto checked_size_add(std::size_t left, std::size_t right) -> std::size_t {
  if (right > std::numeric_limits<std::size_t>::max() - left) {
    throw std::invalid_argument("road-bin input count overflows size_t");
  }
  return left + right;
}

auto bin_count_for_arc(const Arc &arc, const Parameters &parameters)
    -> std::size_t {
  const auto ratio = static_cast<long double>(arc.length_m) /
                     static_cast<long double>(parameters.bin_length_m);
  if (!std::isfinite(ratio) || ratio <= 0.0L ||
      ratio > static_cast<long double>(parameters.maximum_generated_bins)) {
    throw std::invalid_argument("road-bin generated-bin budget is exceeded");
  }
  return static_cast<std::size_t>(std::ceil(ratio));
}

auto rounded(double value, int decimal_places) -> double {
  const auto scale = std::pow(10.0, static_cast<double>(decimal_places));
  return std::round(value * scale) / scale;
}

auto interpolate_time(std::int64_t start, std::int64_t duration,
                      long double fraction) -> std::int64_t {
  const auto offset =
      std::round(static_cast<long double>(duration) * fraction);
  const auto result = static_cast<__int128>(start) +
                      static_cast<__int128>(offset);
  if (result < static_cast<__int128>(std::numeric_limits<std::int64_t>::min()) ||
      result > static_cast<__int128>(std::numeric_limits<std::int64_t>::max())) {
    throw std::invalid_argument("road-bin interpolated time overflows int64");
  }
  return static_cast<std::int64_t>(result);
}

auto heading_difference(double left, double right) -> double {
  constexpr double pi = 3.141592653589793238462643383279502884;
  constexpr double two_pi = 2.0 * pi;
  auto difference = std::fmod(right - left, two_pi);
  if (difference > pi) {
    difference -= two_pi;
  } else if (difference < -pi) {
    difference += two_pi;
  }
  return std::abs(difference);
}

auto merged_duration(std::vector<TimeInterval> intervals) -> std::int64_t {
  if (intervals.empty()) {
    return 0;
  }
  std::ranges::sort(intervals, {}, [](const TimeInterval &value) {
    return std::pair{value.start, value.end};
  });
  auto current_start = intervals.front().start;
  auto current_end = intervals.front().end;
  std::int64_t duration{};
  for (const auto &interval : intervals | std::views::drop(1)) {
    if (interval.start <= current_end) {
      current_end = std::max(current_end, interval.end);
      continue;
    }
    duration =
        checked_add(duration, checked_difference(current_end, current_start));
    current_start = interval.start;
    current_end = interval.end;
  }
  return checked_add(duration,
                     checked_difference(current_end, current_start));
}

auto overlap_interval(const Piece &piece, std::int64_t start,
                      std::int64_t end) -> std::optional<TimeInterval> {
  const auto overlap_start = std::max(piece.start_time_ns, start);
  const auto overlap_end = std::min(piece.end_time_ns, end);
  if (overlap_end <= overlap_start) {
    return std::nullopt;
  }
  return TimeInterval{overlap_start, overlap_end};
}

auto modality_order(Modality value) -> int {
  return static_cast<int>(value);
}

auto finalize_modality(const ModalityAccumulator &value,
                       int decimal_places) -> ModalityAggregate {
  if (!std::isfinite(value.point_support) ||
      !std::isfinite(value.overlap_support_duration_product)) {
    throw std::invalid_argument("road-bin modality aggregation is nonfinite");
  }
  ModalityAggregate result;
  result.modality = value.modality;
  result.valid_duration_ns = merged_duration(value.valid_intervals);
  result.timestamp_supported_duration_ns =
      merged_duration(value.timestamp_intervals);
  result.point_support = rounded(value.point_support, decimal_places);
  if (value.overlap_support_duration_ns > 0) {
    result.mean_overlap_support_m = rounded(
        value.overlap_support_duration_product /
            static_cast<double>(value.overlap_support_duration_ns),
        decimal_places);
  }
  result.evidence_ids.assign(value.evidence_ids.begin(),
                             value.evidence_ids.end());
  return result;
}

void validate_inputs(const std::vector<Arc> &arcs,
                     const std::vector<MatchedPath> &paths,
                     const std::vector<ModalityEvidence> &modality_evidence,
                     const std::vector<FindingInterval> &findings,
                     const Parameters &parameters) {
  if (!std::isfinite(parameters.bin_length_m) ||
      parameters.bin_length_m <= 0.0 ||
      parameters.independent_traversal_minimum_gap_ns < 0 ||
      parameters.maximum_paths == 0U ||
      parameters.maximum_points_per_path < 2U ||
      parameters.maximum_total_points < 2U ||
      parameters.maximum_generated_bins == 0U ||
      parameters.maximum_modality_evidence_intervals == 0U ||
      parameters.maximum_findings == 0U ||
      parameters.distance_rounding_decimal_places < 0 ||
      parameters.distance_rounding_decimal_places > 12) {
    throw std::invalid_argument("road-bin parameters are invalid");
  }
  if (paths.size() > parameters.maximum_paths ||
      modality_evidence.size() >
          parameters.maximum_modality_evidence_intervals ||
      findings.size() > parameters.maximum_findings) {
    throw std::invalid_argument("road-bin input exceeds its frozen budget");
  }
  std::set<std::string> arc_ids;
  std::size_t generated_bin_count{};
  for (const auto &arc : arcs) {
    if (arc.arc_id.empty() || !arc_ids.insert(arc.arc_id).second ||
        !std::isfinite(arc.length_m) || arc.length_m <= 0.0) {
      throw std::invalid_argument("road-bin arc input is invalid");
    }
    generated_bin_count = checked_size_add(
        generated_bin_count, bin_count_for_arc(arc, parameters));
    if (generated_bin_count > parameters.maximum_generated_bins) {
      throw std::invalid_argument("road-bin generated-bin budget is exceeded");
    }
  }
  std::set<std::string> road_match_ids;
  std::map<std::string, std::string> sequence_groups;
  std::size_t total_point_count{};
  for (const auto &path : paths) {
    if (path.road_match_id.empty() || path.sequence_id.empty() ||
        path.source_group_id.empty() ||
        !road_match_ids.insert(path.road_match_id).second ||
        path.points.size() > parameters.maximum_points_per_path) {
      throw std::invalid_argument("road-bin matched path is invalid");
    }
    total_point_count =
        checked_size_add(total_point_count, path.points.size());
    if (total_point_count > parameters.maximum_total_points) {
      throw std::invalid_argument("road-bin total-point budget is exceeded");
    }
    const auto [group_item, inserted] =
        sequence_groups.emplace(path.sequence_id, path.source_group_id);
    if (!inserted && group_item->second != path.source_group_id) {
      throw std::invalid_argument(
          "road-bin sequence has inconsistent source-group identity");
    }
    for (std::size_t index = 0; index < path.points.size(); ++index) {
      const auto &point = path.points[index];
      if (!std::isfinite(point.speed_mps) || point.speed_mps < 0.0 ||
          (point.heading_rad.has_value() &&
           !std::isfinite(*point.heading_rad)) ||
          (point.arc_index.has_value() !=
           point.along_arc_offset_m.has_value())) {
        throw std::invalid_argument("road-bin matched point is invalid");
      }
      if (index > 0U && point.time_ns <= path.points[index - 1U].time_ns) {
        throw std::invalid_argument(
            "road-bin matched points must be strictly time ordered");
      }
      if (point.arc_index.has_value()) {
        if (*point.arc_index >= arcs.size() ||
            !std::isfinite(*point.along_arc_offset_m) ||
            *point.along_arc_offset_m < 0.0 ||
            *point.along_arc_offset_m > arcs[*point.arc_index].length_m) {
          throw std::invalid_argument(
              "road-bin matched point offset is outside its arc");
        }
      }
    }
  }
  std::set<std::string> evidence_ids;
  for (const auto &evidence : modality_evidence) {
    if (evidence.evidence_id.empty() || evidence.sequence_id.empty() ||
        !evidence_ids.insert(evidence.evidence_id).second ||
        evidence.end_time_ns <= evidence.start_time_ns ||
        !std::isfinite(evidence.point_count) || evidence.point_count < 0.0 ||
        (evidence.overlap_support_m.has_value() &&
         (!std::isfinite(*evidence.overlap_support_m) ||
          *evidence.overlap_support_m < 0.0))) {
      throw std::invalid_argument("road-bin modality evidence is invalid");
    }
    if (!sequence_groups.contains(evidence.sequence_id)) {
      throw std::invalid_argument(
          "road-bin modality evidence references an unknown sequence");
    }
  }
  std::set<std::string> finding_ids;
  for (const auto &finding : findings) {
    if (finding.finding_id.empty() || finding.sequence_id.empty() ||
        !finding_ids.insert(finding.finding_id).second ||
        finding.end_time_ns <= finding.start_time_ns ||
        !sequence_groups.contains(finding.sequence_id)) {
      throw std::invalid_argument("road-bin finding input is invalid");
    }
  }
}

auto make_pieces(const std::vector<Arc> &arcs,
                 const std::vector<MatchedPath> &paths,
                 const Parameters &parameters) -> std::vector<Piece> {
  std::vector<Piece> pieces;
  std::set<std::tuple<std::string, std::size_t, std::int64_t, std::int64_t,
                      double, double>>
      seen;
  for (const auto &path : paths) {
    for (std::size_t point_index = 1; point_index < path.points.size();
         ++point_index) {
      const auto &left = path.points[point_index - 1U];
      const auto &right = path.points[point_index];
      if (!left.confident || !right.confident || left.stationary ||
          right.stationary || !left.arc_index.has_value() ||
          left.arc_index != right.arc_index) {
        continue;
      }
      const auto arc_index = *left.arc_index;
      const auto start_offset = *left.along_arc_offset_m;
      const auto end_offset = *right.along_arc_offset_m;
      const auto total_distance = end_offset - start_offset;
      if (total_distance <= 0.0) {
        continue;
      }
      const auto duration = checked_difference(right.time_ns, left.time_ns);
      auto cursor = start_offset;
      while (cursor < end_offset) {
        const auto raw_bin = static_cast<std::size_t>(
            std::floor(cursor / parameters.bin_length_m));
        const auto bin_count = bin_count_for_arc(arcs[arc_index], parameters);
        const auto bin_index = std::min(raw_bin, bin_count - 1U);
        const auto bin_end = std::min(
            arcs[arc_index].length_m,
            parameters.bin_length_m * static_cast<double>(bin_index + 1U));
        const auto exit = std::min(end_offset, bin_end);
        if (!(exit > cursor)) {
          throw std::invalid_argument("road-bin splitter made no progress");
        }
        const auto start_fraction = static_cast<long double>(
            (cursor - start_offset) / total_distance);
        const auto end_fraction = static_cast<long double>(
            (exit - start_offset) / total_distance);
        const auto piece_start =
            interpolate_time(left.time_ns, duration, start_fraction);
        const auto piece_end =
            interpolate_time(left.time_ns, duration, end_fraction);
        if (piece_end > piece_start) {
          const auto piece_fraction = (exit - cursor) / total_distance;
          const auto start_speed =
              left.speed_mps + (right.speed_mps - left.speed_mps) *
                                   static_cast<double>(start_fraction);
          const auto end_speed =
              left.speed_mps + (right.speed_mps - left.speed_mps) *
                                   static_cast<double>(end_fraction);
          Piece piece{
              arc_index,
              bin_index,
              path.road_match_id,
              path.sequence_id,
              path.source_group_id,
              piece_start,
              piece_end,
              cursor,
              exit,
              exit - cursor,
              start_speed,
              end_speed,
              0.0,
              0U,
          };
          if (left.heading_rad.has_value() && right.heading_rad.has_value()) {
            piece.yaw_excitation_rad =
                heading_difference(*left.heading_rad, *right.heading_rad) *
                piece_fraction;
          }
          const auto identity = std::tuple{
              piece.sequence_id, piece.arc_index, piece.start_time_ns,
              piece.end_time_ns, piece.entry_offset_m, piece.exit_offset_m};
          if (seen.insert(identity).second) {
            pieces.push_back(std::move(piece));
          }
        }
        cursor = exit;
      }
    }
  }
  std::ranges::sort(pieces, {}, [](const Piece &piece) {
    return std::tuple{piece.sequence_id, piece.arc_index, piece.start_time_ns,
                      piece.end_time_ns, piece.bin_index,
                      piece.entry_offset_m};
  });
  std::map<std::pair<std::string, std::size_t>,
           std::pair<std::size_t, std::int64_t>>
      traversal_state;
  for (auto &piece : pieces) {
    const auto key = std::pair{piece.sequence_id, piece.arc_index};
    const auto item = traversal_state.find(key);
    if (item == traversal_state.end()) {
      traversal_state.emplace(key,
                              std::pair<std::size_t, std::int64_t>{
                                  0U, piece.end_time_ns});
      piece.traversal_ordinal = 0U;
      continue;
    }
    auto &[ordinal, previous_end] = item->second;
    if (piece.start_time_ns > previous_end &&
        checked_difference(piece.start_time_ns, previous_end) >
            parameters.independent_traversal_minimum_gap_ns) {
      ++ordinal;
    }
    piece.traversal_ordinal = ordinal;
    previous_end = std::max(previous_end, piece.end_time_ns);
  }
  return pieces;
}

auto make_traversal(
    const TraversalAccumulator &accumulator,
    const std::vector<ModalityEvidence> &modality_evidence,
    const std::vector<FindingInterval> &findings, int decimal_places)
    -> TraversalCoverage {
  const auto &first = accumulator.pieces.front();
  TraversalCoverage result;
  result.arc_index = first.arc_index;
  result.longitudinal_bin_index = first.bin_index;
  result.sequence_id = first.sequence_id;
  result.source_group_id = first.source_group_id;
  result.traversal_ordinal = first.traversal_ordinal;
  result.first_time_ns = first.start_time_ns;
  result.last_time_ns = first.end_time_ns;
  result.entry_offset_m = first.entry_offset_m;
  result.exit_offset_m = first.exit_offset_m;
  result.minimum_speed_mps = std::numeric_limits<double>::infinity();
  result.maximum_speed_mps = 0.0;
  double speed_sum{};
  std::set<std::string> road_match_ids;
  for (const auto &piece : accumulator.pieces) {
    result.first_time_ns = std::min(result.first_time_ns, piece.start_time_ns);
    result.last_time_ns = std::max(result.last_time_ns, piece.end_time_ns);
    if (piece.start_time_ns < first.start_time_ns) {
      result.entry_offset_m = piece.entry_offset_m;
    }
    if (piece.end_time_ns >= result.last_time_ns) {
      result.exit_offset_m = piece.exit_offset_m;
    }
    result.usable_duration_ns = checked_add(
        result.usable_duration_ns,
        checked_difference(piece.end_time_ns, piece.start_time_ns));
    result.usable_distance_m += piece.distance_m;
    result.speed_sample_count += 2U;
    result.minimum_speed_mps =
        std::min({result.minimum_speed_mps, piece.start_speed_mps,
                  piece.end_speed_mps});
    result.maximum_speed_mps =
        std::max({result.maximum_speed_mps, piece.start_speed_mps,
                  piece.end_speed_mps});
    speed_sum += piece.start_speed_mps + piece.end_speed_mps;
    result.yaw_excitation_rad += piece.yaw_excitation_rad;
    road_match_ids.insert(piece.road_match_id);
  }
  if (!std::isfinite(result.usable_distance_m) ||
      !std::isfinite(speed_sum) ||
      !std::isfinite(result.yaw_excitation_rad)) {
    throw std::invalid_argument("road-bin traversal aggregation is nonfinite");
  }
  result.usable_distance_m =
      rounded(result.usable_distance_m, decimal_places);
  result.entry_offset_m = rounded(result.entry_offset_m, decimal_places);
  result.exit_offset_m = rounded(result.exit_offset_m, decimal_places);
  result.minimum_speed_mps =
      rounded(result.minimum_speed_mps, decimal_places);
  result.mean_speed_mps = rounded(
      speed_sum / static_cast<double>(result.speed_sample_count),
      decimal_places);
  result.maximum_speed_mps =
      rounded(result.maximum_speed_mps, decimal_places);
  result.yaw_excitation_rad =
      rounded(result.yaw_excitation_rad, decimal_places);
  result.road_match_ids.assign(road_match_ids.begin(), road_match_ids.end());

  std::map<Modality, ModalityAccumulator> modality_values;
  for (const auto &evidence : modality_evidence) {
    if (!evidence.usable || evidence.sequence_id != result.sequence_id) {
      continue;
    }
    std::vector<TimeInterval> evidence_overlaps;
    for (const auto &piece : accumulator.pieces) {
      const auto overlap = overlap_interval(
          piece, evidence.start_time_ns, evidence.end_time_ns);
      if (overlap.has_value()) {
        evidence_overlaps.push_back(*overlap);
      }
    }
    const auto duration = merged_duration(evidence_overlaps);
    if (duration <= 0) {
      continue;
    }
    auto &modality = modality_values[evidence.modality];
    modality.modality = evidence.modality;
    modality.valid_intervals.insert(modality.valid_intervals.end(),
                                    evidence_overlaps.begin(),
                                    evidence_overlaps.end());
    if (evidence.timestamp_supported) {
      modality.timestamp_intervals.insert(modality.timestamp_intervals.end(),
                                          evidence_overlaps.begin(),
                                          evidence_overlaps.end());
    }
    modality.evidence_ids.insert(evidence.evidence_id);
    const auto evidence_duration = checked_difference(
        evidence.end_time_ns, evidence.start_time_ns);
    modality.point_support +=
        evidence.point_count * static_cast<double>(duration) /
        static_cast<double>(evidence_duration);
    if (evidence.overlap_support_m.has_value()) {
      modality.overlap_support_duration_product +=
          *evidence.overlap_support_m * static_cast<double>(duration);
      modality.overlap_support_duration_ns = checked_add(
          modality.overlap_support_duration_ns, duration);
    }
  }
  for (const auto &[unused, modality] : modality_values) {
    static_cast<void>(unused);
    result.modalities.push_back(finalize_modality(modality, decimal_places));
  }
  std::ranges::sort(result.modalities, {}, [](const ModalityAggregate &value) {
    return modality_order(value.modality);
  });

  for (const auto &finding : findings) {
    if (finding.sequence_id != result.sequence_id) {
      continue;
    }
    const auto affected = std::ranges::any_of(
        accumulator.pieces, [&](const Piece &piece) {
          return overlap_interval(piece, finding.start_time_ns,
                                  finding.end_time_ns)
              .has_value();
        });
    if (affected) {
      result.finding_ids.push_back(finding.finding_id);
      if (finding.critical) {
        result.critical_finding_ids.push_back(finding.finding_id);
      }
    }
  }
  std::ranges::sort(result.finding_ids);
  std::ranges::sort(result.critical_finding_ids);
  return result;
}

} // namespace

auto aggregate_directed_road_bins(
    const std::vector<Arc> &arcs, const std::vector<MatchedPath> &paths,
    const std::vector<ModalityEvidence> &modality_evidence,
    const std::vector<FindingInterval> &findings,
    const Parameters &parameters) -> AggregationResult {
  validate_inputs(arcs, paths, modality_evidence, findings, parameters);
  const auto pieces = make_pieces(arcs, paths, parameters);
  using TraversalKey =
      std::tuple<std::size_t, std::size_t, std::string, std::size_t>;
  std::map<TraversalKey, TraversalAccumulator> traversal_values;
  for (const auto &piece : pieces) {
    traversal_values[TraversalKey{piece.arc_index, piece.bin_index,
                                  piece.sequence_id,
                                  piece.traversal_ordinal}]
        .pieces.push_back(piece);
  }

  AggregationResult result;
  std::map<std::pair<std::size_t, std::size_t>, std::size_t> bin_indices;
  for (std::size_t arc_index = 0; arc_index < arcs.size(); ++arc_index) {
    const auto bin_count = bin_count_for_arc(arcs[arc_index], parameters);
    for (std::size_t bin_index = 0; bin_index < bin_count; ++bin_index) {
      BinCoverage bin;
      bin.arc_index = arc_index;
      bin.longitudinal_bin_index = bin_index;
      bin.start_offset_m = rounded(
          parameters.bin_length_m * static_cast<double>(bin_index),
          parameters.distance_rounding_decimal_places);
      bin.end_offset_m = rounded(
          std::min(arcs[arc_index].length_m,
                   parameters.bin_length_m *
                       static_cast<double>(bin_index + 1U)),
          parameters.distance_rounding_decimal_places);
      bin_indices.emplace(std::pair{arc_index, bin_index}, result.bins.size());
      result.bins.push_back(std::move(bin));
    }
  }

  for (const auto &[key, accumulator] : traversal_values) {
    auto traversal = make_traversal(
        accumulator, modality_evidence, findings,
        parameters.distance_rounding_decimal_places);
    auto &bin = result.bins[bin_indices.at(
        std::pair{traversal.arc_index, traversal.longitudinal_bin_index})];
    bin.usable_duration_ns = checked_add(bin.usable_duration_ns,
                                         traversal.usable_duration_ns);
    bin.usable_distance_m += traversal.usable_distance_m;
    ++bin.independent_traversal_count;
    bin.speed_sample_count += traversal.speed_sample_count;
    if (!bin.minimum_speed_mps.has_value()) {
      bin.minimum_speed_mps = traversal.minimum_speed_mps;
      bin.maximum_speed_mps = traversal.maximum_speed_mps;
      bin.mean_speed_mps = 0.0;
    } else {
      bin.minimum_speed_mps =
          std::min(*bin.minimum_speed_mps, traversal.minimum_speed_mps);
      bin.maximum_speed_mps =
          std::max(*bin.maximum_speed_mps, traversal.maximum_speed_mps);
    }
    *bin.mean_speed_mps +=
        traversal.mean_speed_mps *
        static_cast<double>(traversal.speed_sample_count);
    bin.yaw_excitation_rad += traversal.yaw_excitation_rad;
    bin.traversals.push_back(std::move(traversal));
  }

  for (auto &bin : result.bins) {
    if (!std::isfinite(bin.usable_distance_m) ||
        !std::isfinite(bin.yaw_excitation_rad) ||
        (bin.mean_speed_mps.has_value() &&
         !std::isfinite(*bin.mean_speed_mps))) {
      throw std::invalid_argument("road-bin coverage aggregation is nonfinite");
    }
    bin.usable_distance_m = rounded(
        bin.usable_distance_m, parameters.distance_rounding_decimal_places);
    bin.yaw_excitation_rad = rounded(
        bin.yaw_excitation_rad, parameters.distance_rounding_decimal_places);
    if (bin.mean_speed_mps.has_value()) {
      *bin.mean_speed_mps = rounded(
          *bin.mean_speed_mps / static_cast<double>(bin.speed_sample_count),
          parameters.distance_rounding_decimal_places);
      *bin.minimum_speed_mps = rounded(
          *bin.minimum_speed_mps,
          parameters.distance_rounding_decimal_places);
      *bin.maximum_speed_mps = rounded(
          *bin.maximum_speed_mps,
          parameters.distance_rounding_decimal_places);
    }
    std::map<Modality, ModalityAccumulator> modality_values;
    std::set<std::string> finding_ids;
    std::set<std::string> critical_finding_ids;
    for (const auto &traversal : bin.traversals) {
      finding_ids.insert(traversal.finding_ids.begin(),
                         traversal.finding_ids.end());
      critical_finding_ids.insert(traversal.critical_finding_ids.begin(),
                                  traversal.critical_finding_ids.end());
      for (const auto &modality : traversal.modalities) {
        auto &value = modality_values[modality.modality];
        value.modality = modality.modality;
        if (modality.valid_duration_ns > 0) {
          value.valid_intervals.push_back(
              TimeInterval{0, modality.valid_duration_ns});
        }
        if (modality.timestamp_supported_duration_ns > 0) {
          value.timestamp_intervals.push_back(
              TimeInterval{0, modality.timestamp_supported_duration_ns});
        }
        value.point_support += modality.point_support;
        if (modality.mean_overlap_support_m.has_value()) {
          value.overlap_support_duration_product +=
              *modality.mean_overlap_support_m *
              static_cast<double>(modality.valid_duration_ns);
          value.overlap_support_duration_ns = checked_add(
              value.overlap_support_duration_ns,
              modality.valid_duration_ns);
        }
        value.evidence_ids.insert(modality.evidence_ids.begin(),
                                  modality.evidence_ids.end());
      }
    }
    for (const auto &[unused, modality] : modality_values) {
      static_cast<void>(unused);
      auto aggregate = finalize_modality(
          modality, parameters.distance_rounding_decimal_places);
      aggregate.valid_duration_ns = 0;
      aggregate.timestamp_supported_duration_ns = 0;
      for (const auto &traversal : bin.traversals) {
        const auto item = std::ranges::find(
            traversal.modalities, modality.modality,
            &ModalityAggregate::modality);
        if (item != traversal.modalities.end()) {
          aggregate.valid_duration_ns = checked_add(
              aggregate.valid_duration_ns, item->valid_duration_ns);
          aggregate.timestamp_supported_duration_ns = checked_add(
              aggregate.timestamp_supported_duration_ns,
              item->timestamp_supported_duration_ns);
        }
      }
      bin.modalities.push_back(std::move(aggregate));
    }
    std::ranges::sort(bin.modalities, {}, [](const ModalityAggregate &value) {
      return modality_order(value.modality);
    });
    bin.finding_ids.assign(finding_ids.begin(), finding_ids.end());
    bin.critical_finding_ids.assign(critical_finding_ids.begin(),
                                    critical_finding_ids.end());
  }

  for (const auto &finding : findings) {
    FindingLocalization localization;
    localization.finding_id = finding.finding_id;
    for (std::size_t index = 0; index < result.bins.size(); ++index) {
      if (std::ranges::binary_search(result.bins[index].finding_ids,
                                     finding.finding_id)) {
        localization.bin_result_indices.push_back(index);
      }
    }
    result.finding_localizations.push_back(std::move(localization));
  }
  return result;
}

} // namespace cartosentry::bins
