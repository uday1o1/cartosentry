#include "cartosentry/contracts/artifact_json.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string_view>

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data,
                                      std::size_t size) {
  if (size == 0U) {
    return 0;
  }
  constexpr std::array schemas{
      std::string_view{"cartosentry.sequence-manifest.v1"},
      std::string_view{"cartosentry.run.v1"},
      std::string_view{"cartosentry.finding.v1"},
      std::string_view{"cartosentry.readiness-profile.v1"},
      std::string_view{"cartosentry.recapture-plan.v1"},
      std::string_view{"cartosentry.accepted-data-bundle.v1"},
  };
  const auto schema = schemas[data[0] % schemas.size()];
  const auto input = std::string_view(
      reinterpret_cast<const char*>(data + 1U), size - 1U);
  try {
    static_cast<void>(
        cartosentry::contracts::canonicalize_artifact_json(input, schema));
  } catch (const std::invalid_argument&) {
  }
  return 0;
}
