#include <arrow/api.h>
#include <fmt/format.h>
#include <GeographicLib/LocalCartesian.hpp>
#include <nlohmann/json.hpp>
#include <opencv2/core.hpp>
#include <spdlog/spdlog.h>
#include <sqlite3.h>
#include <yaml-cpp/yaml.h>

#include <cmath>
#include <iostream>
#include <memory>
#include <string>

namespace {

auto fail(const std::string& message) -> int {
  std::cerr << message << '\n';
  return 1;
}

}  // namespace

auto main() -> int {
  GeographicLib::LocalCartesian local(43.6532, -79.3832, 100.0);
  double east = 0.0;
  double north = 0.0;
  double up = 0.0;
  local.Forward(43.6533, -79.3831, 101.0, east, north, up);
  if (!(east > 0.0 && north > 0.0 && std::abs(up - 1.0) < 0.01)) {
    return fail("GeographicLib local Cartesian check failed");
  }

  const cv::Mat identity = cv::Mat::eye(3, 3, CV_64F);
  if (cv::countNonZero(identity) != 3) {
    return fail("OpenCV matrix check failed");
  }

  arrow::Int64Builder builder;
  if (!builder.Append(42).ok()) {
    return fail("Arrow append check failed");
  }
  std::shared_ptr<arrow::Array> values;
  if (!builder.Finish(&values).ok() || values->length() != 1) {
    return fail("Arrow array check failed");
  }

  sqlite3* database = nullptr;
  if (sqlite3_open(":memory:", &database) != SQLITE_OK) {
    return fail("SQLite open check failed");
  }
  const int sqlite_close_result = sqlite3_close(database);
  if (sqlite_close_result != SQLITE_OK) {
    return fail("SQLite close check failed");
  }

  const auto document = nlohmann::json::parse(R"({"ready":true})");
  const YAML::Node profile = YAML::Load("profile: mapping\n");
  if (!document.at("ready").get<bool>() || profile["profile"].as<std::string>() != "mapping") {
    return fail("JSON or YAML check failed");
  }

  const std::string result = fmt::format("locked-stack-{}", values->length());
  spdlog::set_level(spdlog::level::off);
  spdlog::info("{}", result);
  if (result != "locked-stack-1") {
    return fail("logging or formatting check failed");
  }

  std::cout << result << '\n';
  return 0;
}
