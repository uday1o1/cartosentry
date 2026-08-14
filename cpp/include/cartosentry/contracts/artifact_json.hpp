#pragma once

#include <string>
#include <string_view>

namespace cartosentry::contracts {

// Validates one portable artifact and returns deterministic compact JSON.
[[nodiscard]] auto canonicalize_artifact_json(
    std::string_view input_json, std::string_view expected_schema) -> std::string;

}  // namespace cartosentry::contracts
