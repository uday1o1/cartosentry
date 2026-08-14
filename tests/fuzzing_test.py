"""Frozen corpus and fuzz-runner contract tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from cartosentry.adapters.boreas_v1 import (
    MAXIMUM_BOREAS_LIDAR_FRAME_BYTES,
    parse_boreas_lidar_frame_bytes,
)
from cartosentry.artifacts import validate_artifact_bytes
from cartosentry.faults import parse_fault_manifest_bytes
from cartosentry.ingestion import (
    parse_frame_index_line,
    parse_ingestion_budget_bytes,
)
from cartosentry.manifest_boundaries import (
    MAXIMUM_ARTIFACT_JSON_BYTES,
    MAXIMUM_FRAME_INDEX_LINE_BYTES,
    MAXIMUM_INGESTION_BUDGET_BYTES,
)
from cartosentry.runstate import (
    parse_attempt_manifest_bytes,
    parse_completion_pointer_bytes,
    parse_run_inputs_bytes,
)
from cartosentry.synthetic import (
    MAXIMUM_FIXTURE_SET_MANIFEST_BYTES,
    parse_fixture_set_manifest_bytes,
)

from scripts.run_fuzz import _accepted_fuzz_run, _load_configuration, _seed_groups
from scripts.run_fuzz_container import run_container

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
CONFIGURATION = REPOSITORY_ROOT / "benchmarks/fuzzing.yaml"


def test_frozen_fuzz_configuration_covers_every_boundary_and_seed_class() -> None:
    configuration, digest = _load_configuration(CONFIGURATION)
    assert len(digest) == 64
    assert {item.name for item in configuration.targets} == {
        "cartosentry_fuzz_artifact_json",
        "cartosentry_fuzz_boreas_lidar",
        "cartosentry_fuzz_decimal_time",
        "cartosentry_fuzz_python_boundaries",
    }
    expected_classes = set(configuration.required_seed_classes)
    for target in configuration.targets:
        seeds = _seed_groups(target.name, configuration.limits)
        not_applicable = set(target.not_applicable_seed_classes)
        assert set(seeds).isdisjoint(not_applicable)
        assert set(seeds) | not_applicable == expected_classes
        assert all(content for group in seeds.values() for content in group)
        assert max(len(content) for group in seeds.values() for content in group) <= (
            configuration.limits.fuzzer_max_input_bytes
        )
    assert configuration.limits.ingestion_budget_bytes == MAXIMUM_INGESTION_BUDGET_BYTES
    assert configuration.limits.frame_index_line_bytes == MAXIMUM_FRAME_INDEX_LINE_BYTES
    assert configuration.limits.fault_manifest_bytes == MAXIMUM_ARTIFACT_JSON_BYTES
    assert configuration.limits.run_control_bytes == 1024 * 1024
    assert (
        configuration.limits.fixture_set_manifest_bytes
        == MAXIMUM_FIXTURE_SET_MANIFEST_BYTES
    )
    assert (
        configuration.limits.boreas_lidar_frame_bytes
        == MAXIMUM_BOREAS_LIDAR_FRAME_BYTES
    )


def test_ingestion_manifest_clean_seeds_reach_the_production_models() -> None:
    configuration, _ = _load_configuration(CONFIGURATION)
    seeds = _seed_groups("cartosentry_fuzz_python_boundaries", configuration.limits)[
        "clean"
    ]
    assert parse_ingestion_budget_bytes(seeds[0][1:]).budget_id == "m2.2-v1"
    assert parse_frame_index_line(seeds[1][1:]).payload_record_count == 1


def test_every_python_manifest_clean_seed_reaches_its_production_model() -> None:
    configuration, _ = _load_configuration(CONFIGURATION)
    seeds = _seed_groups("cartosentry_fuzz_python_boundaries", configuration.limits)[
        "clean"
    ]
    parsers = {
        0: parse_ingestion_budget_bytes,
        1: parse_frame_index_line,
        2: parse_run_inputs_bytes,
        3: parse_attempt_manifest_bytes,
        4: parse_completion_pointer_bytes,
        5: parse_fault_manifest_bytes,
        6: parse_boreas_lidar_frame_bytes,
        7: validate_artifact_bytes,
        8: parse_fixture_set_manifest_bytes,
    }
    assert {seed[0] for seed in seeds} == set(parsers)
    for seed in seeds:
        parsers[seed[0]](seed[1:])


def test_fuzz_configuration_rejects_duplicate_keys(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"first","schema_version":"second"}')
    with pytest.raises(ValueError, match="duplicate key"):
        _load_configuration(duplicate)


def test_fuzz_runner_starts_without_an_installed_cartosentry_package(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = ""
    completed = subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "scripts/run_fuzz.py"), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Run the frozen sanitizer-backed" in completed.stdout


def test_container_launcher_creates_and_verifies_by_immutable_image_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_id = "sha256:" + "a" * 64
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, stdout=image_id + "\n")
        if command[:2] == ["docker", "create"]:
            assert image_id in command
            assert "mutable-tag" not in command
            return subprocess.CompletedProcess(command, 0, stdout="container-id\n")
        if "{{.Image}}" in command:
            return subprocess.CompletedProcess(command, 0, stdout=image_id + "\n")
        if "{{.State.ExitCode}}" in command:
            return subprocess.CompletedProcess(command, 0, stdout="0\n")
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr("scripts.run_fuzz_container.subprocess.run", fake_run)
    assert (
        run_container(
            image="mutable-tag",
            suite="local",
            output_root=tmp_path / "evidence",
            source_revision="revision",
        )
        == 0
    )
    assert any(command[:2] == ["docker", "start"] for command in commands)


@pytest.mark.parametrize(
    "override",
    [
        {"return_code": 1},
        {"timed_out": True},
        {"log": b"interrupted"},
        {"instrumented_counters": 9},
        {"covered_edges": 9},
        {"executed_units": 0},
        {"crash_artifact_count": 1},
    ],
)
def test_fuzz_acceptance_fails_closed(override: dict[str, object]) -> None:
    signals: dict[str, object] = {
        "return_code": 0,
        "timed_out": False,
        "log": b"DONE",
        "instrumented_counters": 10,
        "minimum_instrumented_counters": 10,
        "covered_edges": 10,
        "minimum_covered_edges": 10,
        "executed_units": 1,
        "crash_artifact_count": 0,
    }
    signals.update(override)
    assert not _accepted_fuzz_run(**signals)  # type: ignore[arg-type]


def test_fuzz_acceptance_requires_every_success_signal() -> None:
    assert _accepted_fuzz_run(
        return_code=0,
        timed_out=False,
        log=b"DONE",
        instrumented_counters=10,
        minimum_instrumented_counters=10,
        covered_edges=10,
        minimum_covered_edges=10,
        executed_units=1,
        crash_artifact_count=0,
    )
