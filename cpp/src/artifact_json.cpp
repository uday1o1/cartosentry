#include "cartosentry/contracts/artifact_json.hpp"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <array>
#include <cctype>
#include <cstddef>
#include <stdexcept>
#include <string>
#include <string_view>

namespace cartosentry::contracts {
namespace {

using Json = nlohmann::json;

constexpr std::size_t maximum_artifact_bytes = 16U * 1024U * 1024U;
constexpr std::size_t maximum_artifact_depth = 64U;

struct SchemaRule {
  std::string_view name;
  const std::string_view* allowed_begin;
  const std::string_view* allowed_end;
  const std::string_view* optional_begin;
  const std::string_view* optional_end;
};

constexpr std::array sequence_keys{
    std::string_view{"schema_version"},
    std::string_view{"sequence_id"},
    std::string_view{"source_identity_sha256"},
    std::string_view{"source_group_id"},
    std::string_view{"partition"},
    std::string_view{"adapter"},
    std::string_view{"sensors"},
    std::string_view{"source_files"},
    std::string_view{"calibrations"},
    std::string_view{"timestamp_metadata"},
    std::string_view{"coordinate_metadata"},
    std::string_view{"declared_gaps"},
};
constexpr std::array run_keys{
    std::string_view{"schema_version"},
    std::string_view{"run_id"},
    std::string_view{"sequence_id"},
    std::string_view{"road_graph_id"},
    std::string_view{"profile_id"},
    std::string_view{"engine_version"},
    std::string_view{"configuration_hashes"},
    std::string_view{"state"},
    std::string_view{"stages"},
    std::string_view{"artifacts"},
    std::string_view{"local_context"},
};
constexpr std::array run_optional{std::string_view{"local_context"}};
constexpr std::array finding_keys{
    std::string_view{"schema_version"},
    std::string_view{"finding_id"},
    std::string_view{"detector_id"},
    std::string_view{"detector_version"},
    std::string_view{"rule_id"},
    std::string_view{"severity"},
    std::string_view{"observability"},
    std::string_view{"readiness_effect"},
    std::string_view{"streams"},
    std::string_view{"interval"},
    std::string_view{"measurement"},
    std::string_view{"threshold"},
    std::string_view{"road_bin_ids"},
    std::string_view{"evidence"},
    std::string_view{"hypotheses"},
    std::string_view{"remediation"},
};
constexpr std::array profile_keys{
    std::string_view{"schema_version"},
    std::string_view{"profile_id"},
    std::string_view{"profile_version"},
    std::string_view{"supported_adapter_capabilities"},
    std::string_view{"required_modalities"},
    std::string_view{"required_detectors"},
    std::string_view{"aggregation_rules"},
    std::string_view{"mandatory_requirements"},
    std::string_view{"optional_review_features"},
    std::string_view{"charter_references"},
};
constexpr std::array plan_keys{
    std::string_view{"schema_version"},
    std::string_view{"recapture_plan_id"},
    std::string_view{"run_id"},
    std::string_view{"road_graph_id"},
    std::string_view{"depot_node_id"},
    std::string_view{"requirements"},
    std::string_view{"route_arc_ids"},
    std::string_view{"covered_requirement_ids"},
    std::string_view{"deferred_requirement_ids"},
    std::string_view{"unreachable_requirement_ids"},
    std::string_view{"estimated_distance_m"},
    std::string_view{"estimated_duration_ns"},
    std::string_view{"budget"},
    std::string_view{"validation_state"},
};
constexpr std::array bundle_keys{
    std::string_view{"schema_version"},
    std::string_view{"bundle_id"},
    std::string_view{"immutable"},
    std::string_view{"source_sequence_sha256"},
    std::string_view{"sequence_id"},
    std::string_view{"profile_id"},
    std::string_view{"accepted_intervals"},
    std::string_view{"excluded_intervals"},
    std::string_view{"required_calibration_ids"},
    std::string_view{"derived_artifacts"},
    std::string_view{"raw_data_shards"},
};
constexpr std::array<std::string_view, 0> no_optional{};

template <std::size_t Allowed, std::size_t Optional>
constexpr auto make_rule(std::string_view name,
                         const std::array<std::string_view, Allowed>& allowed,
                         const std::array<std::string_view, Optional>& optional)
    -> SchemaRule {
  return SchemaRule{name, allowed.data(), allowed.data() + allowed.size(),
                    optional.data(), optional.data() + optional.size()};
}

constexpr std::array schema_rules{
    make_rule("cartosentry.sequence-manifest.v1", sequence_keys, no_optional),
    make_rule("cartosentry.run.v1", run_keys, run_optional),
    make_rule("cartosentry.finding.v1", finding_keys, no_optional),
    make_rule("cartosentry.readiness-profile.v1", profile_keys, no_optional),
    make_rule("cartosentry.recapture-plan.v1", plan_keys, no_optional),
    make_rule("cartosentry.accepted-data-bundle.v1", bundle_keys, no_optional),
};

auto contains(const std::string_view* begin, const std::string_view* end,
              std::string_view key) -> bool {
  return std::find(begin, end, key) != end;
}

auto schema_rule(std::string_view name) -> const SchemaRule& {
  const auto found = std::find_if(
      schema_rules.begin(), schema_rules.end(),
      [name](const SchemaRule& rule) { return rule.name == name; });
  if (found == schema_rules.end()) {
    throw std::invalid_argument("unsupported artifact schema: " +
                                std::string{name});
  }
  return *found;
}

auto is_path_leak(std::string_view value) -> bool {
  std::string normalized{value};
  std::replace(normalized.begin(), normalized.end(), '\\', '/');
  if (normalized.starts_with("/") || normalized.starts_with("~/") ||
      normalized.starts_with("//") || normalized.starts_with("file://")) {
    return true;
  }
  if (normalized.size() >= 3U &&
      std::isalpha(static_cast<unsigned char>(normalized[0])) != 0 &&
      normalized[1] == ':' && normalized[2] == '/') {
    return true;
  }
  std::size_t begin = 0;
  while (begin <= normalized.size()) {
    const auto end = normalized.find('/', begin);
    const auto component = normalized.substr(begin, end - begin);
    if (component == "..") {
      return true;
    }
    if (end == std::string::npos) {
      break;
    }
    begin = end + 1U;
  }
  return false;
}

auto forbidden_portable_key(std::string_view key) -> bool {
  constexpr std::array forbidden{
      std::string_view{"absolute_path"}, std::string_view{"host_name"},
      std::string_view{"hostname"},      std::string_view{"local_path"},
      std::string_view{"local_context"}, std::string_view{"machine_id"},
      std::string_view{"source_root"},   std::string_view{"source_roots"},
  };
  std::string normalized{key};
  std::transform(normalized.begin(), normalized.end(), normalized.begin(),
                 [](unsigned char character) {
                   return static_cast<char>(std::tolower(character));
                 });
  return std::find(forbidden.begin(), forbidden.end(), normalized) !=
         forbidden.end();
}

void validate_portable(const Json& value, std::size_t depth) {
  if (depth > maximum_artifact_depth) {
    throw std::invalid_argument("artifact nesting exceeds the supported depth");
  }
  if (value.is_object()) {
    for (const auto& [key, item] : value.items()) {
      if (forbidden_portable_key(key)) {
        throw std::invalid_argument("portable artifact contains machine-local field: " +
                                    key);
      }
      validate_portable(item, depth + 1U);
    }
    return;
  }
  if (value.is_array()) {
    for (const auto& item : value) {
      validate_portable(item, depth + 1U);
    }
    return;
  }
  if (value.is_string() && is_path_leak(value.get_ref<const std::string&>())) {
    throw std::invalid_argument("portable artifact contains a local path");
  }
}

void validate_top_level(const Json& document, const SchemaRule& rule) {
  for (const auto& [key, unused] : document.items()) {
    static_cast<void>(unused);
    if (!contains(rule.allowed_begin, rule.allowed_end, key)) {
      throw std::invalid_argument("artifact contains unknown top-level field: " + key);
    }
  }
  for (auto item = rule.allowed_begin; item != rule.allowed_end; ++item) {
    if (!contains(rule.optional_begin, rule.optional_end, *item) &&
        !document.contains(*item)) {
      throw std::invalid_argument("artifact is missing required top-level field: " +
                                  std::string{*item});
    }
  }
}

}  // namespace

auto canonicalize_artifact_json(std::string_view input_json,
                                std::string_view expected_schema)
    -> std::string {
  if (input_json.size() > maximum_artifact_bytes) {
    throw std::invalid_argument("artifact exceeds the 16 MiB validation limit");
  }
  const auto& rule = schema_rule(expected_schema);
  Json document;
  try {
    document = Json::parse(input_json);
  } catch (const Json::exception& error) {
    throw std::invalid_argument("invalid artifact JSON: " +
                                std::string{error.what()});
  }
  if (!document.is_object()) {
    throw std::invalid_argument("artifact JSON root must be an object");
  }
  const auto schema = document.find("schema_version");
  if (schema == document.end() || !schema->is_string()) {
    throw std::invalid_argument("artifact schema_version must be a string");
  }
  if (schema->get_ref<const std::string&>() != expected_schema) {
    throw std::invalid_argument("artifact schema_version does not match expectation");
  }
  validate_top_level(document, rule);
  validate_portable(document, 0U);
  return document.dump();
}

}  // namespace cartosentry::contracts
