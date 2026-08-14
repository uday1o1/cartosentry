#include "cartosentry/ingest/boreas_inspector.hpp"

#include <cstddef>
#include <cstdint>
#include <span>

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t* data,
                                      std::size_t size) {
  const auto bytes = std::as_bytes(std::span(data, size));
  try {
    static_cast<void>(cartosentry::ingest::parse_boreas_lidar_frame(
        bytes, "1630597359058594"));
  } catch (const cartosentry::ingest::BoreasFormatError&) {
  }
  return 0;
}
