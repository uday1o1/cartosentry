#include "cartosentry/contracts/time.hpp"

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string_view>

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data,
                                      std::size_t size) {
  const auto input = std::string_view(reinterpret_cast<const char*>(data), size);
  try {
    static_cast<void>(
        cartosentry::contracts::decimal_seconds_to_nanoseconds(input));
  } catch (const std::invalid_argument&) {
  } catch (const std::overflow_error&) {
  }
  return 0;
}
