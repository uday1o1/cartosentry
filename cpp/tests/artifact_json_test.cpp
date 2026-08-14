#include "cartosentry/contracts/artifact_json.hpp"

#include <catch2/catch_test_macros.hpp>

#include <stdexcept>
#include <string>

namespace contracts = cartosentry::contracts;

namespace {

constexpr auto valid_run = R"({
  "schema_version":"cartosentry.run.v1",
  "run_id":"run-sha256-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "sequence_id":"sequence-sha256-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "road_graph_id":"road-graph-sha256-cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "profile_id":"structural-preflight-v1",
  "engine_version":"0.1.0",
  "configuration_hashes":{},
  "state":"COMPLETE",
  "stages":{},
  "artifacts":[]
})";

}  // namespace

TEST_CASE("artifact JSON canonicalization preserves native semantic content") {
  const auto canonical = contracts::canonicalize_artifact_json(
      valid_run, "cartosentry.run.v1");
  CHECK(canonical.starts_with("{\"artifacts\":[]"));
  CHECK(canonical.find("\n") == std::string::npos);
  CHECK(contracts::canonicalize_artifact_json(
            canonical, "cartosentry.run.v1") == canonical);
}

TEST_CASE("artifact JSON rejects schema downgrade and unknown fields") {
  CHECK_THROWS_AS(contracts::canonicalize_artifact_json(
                      valid_run, "cartosentry.finding.v1"),
                  std::invalid_argument);
  auto mutated = std::string{valid_run};
  mutated.insert(mutated.rfind('\n'), ",\n  \"surprise\":true");
  try {
    static_cast<void>(contracts::canonicalize_artifact_json(
        mutated, "cartosentry.run.v1"));
    FAIL("unknown top-level field was accepted");
  } catch (const std::invalid_argument& error) {
    CHECK(std::string{error.what()}.find("unknown top-level field") !=
          std::string::npos);
  }
}

TEST_CASE("artifact JSON rejects duplicate keys at every object depth") {
  auto duplicate_top_level = std::string{valid_run};
  duplicate_top_level.insert(
      duplicate_top_level.rfind('\n'),
      ",\n  \"profile_id\":\"structural-preflight-v1\"");
  CHECK_THROWS_AS(contracts::canonicalize_artifact_json(
                      duplicate_top_level, "cartosentry.run.v1"),
                  std::invalid_argument);

  auto duplicate_nested = std::string{valid_run};
  duplicate_nested.replace(duplicate_nested.find("\"configuration_hashes\":{}"),
                           std::string{"\"configuration_hashes\":{}"}.size(),
                           "\"configuration_hashes\":{\"gate\":1,\"gate\":1}");
  CHECK_THROWS_AS(contracts::canonicalize_artifact_json(
                      duplicate_nested, "cartosentry.run.v1"),
                  std::invalid_argument);
}

TEST_CASE("artifact JSON rejects missing fields and portable path leakage") {
  auto missing = std::string{valid_run};
  const auto field = std::string{
      "  \"profile_id\":\"structural-preflight-v1\",\n"};
  missing.erase(missing.find(field), field.size());
  CHECK_THROWS_AS(contracts::canonicalize_artifact_json(
                      missing, "cartosentry.run.v1"),
                  std::invalid_argument);

  auto leaking = std::string{valid_run};
  const auto value = std::string{"\"structural-preflight-v1\""};
  leaking.replace(leaking.find(value), value.size(), "\"/recordings/profile\"");
  CHECK_THROWS_AS(contracts::canonicalize_artifact_json(
                      leaking, "cartosentry.run.v1"),
                  std::invalid_argument);

  auto local_context = std::string{valid_run};
  local_context.insert(local_context.rfind('\n'), ",\n  \"local_context\":null");
  CHECK_THROWS_AS(contracts::canonicalize_artifact_json(
                      local_context, "cartosentry.run.v1"),
                  std::invalid_argument);
}

TEST_CASE("artifact JSON rejects oversized and excessively deep inputs") {
  std::string oversized(16U * 1024U * 1024U + 1U, 'x');
  CHECK_THROWS_AS(contracts::canonicalize_artifact_json(
                      oversized, "cartosentry.run.v1"),
                  std::invalid_argument);

  auto deep = std::string{valid_run};
  const std::string opening(65U, '[');
  const std::string closing(65U, ']');
  deep.replace(deep.find("\"artifacts\":[]"),
               std::string{"\"artifacts\":[]"}.size(),
               "\"artifacts\":" + opening + closing);
  CHECK_THROWS_AS(contracts::canonicalize_artifact_json(
                      deep, "cartosentry.run.v1"),
                  std::invalid_argument);
}
