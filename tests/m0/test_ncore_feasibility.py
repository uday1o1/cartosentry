from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_CAPABILITIES = {
    "python",
    "dependencies",
    "platform",
    "timestamps",
    "calibration",
    "pose",
    "camera",
    "lidar",
    "radar",
    "writer_api",
}
EXPECTED_WHEEL_SHA256 = (
    "a753f81470ba1b35567cbca26794a7f9ceefe04ec306b962a52dd18dc988fe29"
)
EXPECTED_SOURCE_COMMIT = "cbd8c16f3487186e8848e707eece0045ece3a058"


def _record() -> dict[str, object]:
    loaded = yaml.safe_load(
        (ROOT / "benchmarks/ncore_feasibility.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def test_feasibility_record_is_complete() -> None:
    record = _record()

    assert record["schema_version"] == 1
    assert record["milestone"] == "M0.3b"
    assert record["decision"] in {"feasible", "blocked", "platform-limited"}
    assert record["release_scope"] == "post-v1-only"

    package = record["package"]
    assert isinstance(package, dict)
    assert package["name"] == "nvidia-ncore"
    assert package["version"] == "19.5.1"
    assert package["size_bytes"] == 115_543
    assert package["sha256"] == EXPECTED_WHEEL_SHA256
    assert package["license"] == "Apache-2.0"

    source = record["source"]
    assert isinstance(source, dict)
    assert source["commit"] == EXPECTED_SOURCE_COMMIT

    fixture = record["fixture"]
    assert isinstance(fixture, dict)
    assert fixture["external_bytes"] is False
    assert fixture["generator"] == "tools/ncore_v4_probe.py"

    environments = record["environments"]
    assert isinstance(environments, dict)
    assert set(environments) == {"macos-arm64", "linux-x86-64"}
    assert all(environment["result"] == "pass" for environment in environments.values())
    assert all(
        environment["versions"]["nvidia-ncore"] == package["version"]
        for environment in environments.values()
    )

    capabilities = record["capabilities"]
    assert isinstance(capabilities, dict)
    assert set(capabilities) == REQUIRED_CAPABILITIES
    assert all(
        capability["status"] in {"supported", "supported-with-gap", "adapter-required"}
        for capability in capabilities.values()
    )


def test_ncore_stays_out_of_v1_dependency_locks() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime_dependencies = pyproject["project"]["dependencies"]
    development_dependencies = pyproject["dependency-groups"]["dev"]

    assert not any("ncore" in dependency.lower() for dependency in runtime_dependencies)
    assert not any(
        "ncore" in dependency.lower() for dependency in development_dependencies
    )
    assert "nvidia-ncore" not in (ROOT / "uv.lock").read_text(encoding="utf-8").lower()
    assert (
        "nvidia-ncore"
        not in (ROOT / "cmake/dependencies.lock.json")
        .read_text(encoding="utf-8")
        .lower()
    )
