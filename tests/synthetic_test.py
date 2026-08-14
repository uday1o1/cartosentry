"""Acceptance and negative-control tests for deterministic synthetic fixtures."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from cartosentry.cli import app
from cartosentry.synthetic import (
    COLUMN_PERIOD_NS,
    GENERATOR_VERSION,
    MAXIMUM_FIXTURE_SET_MANIFEST_BYTES,
    generate_fixture,
    materialize_fixture_set,
    parse_fixture_set_manifest_bytes,
    qualify_fixture_set,
    render_fixture_set,
    sensor_map_family_assignments,
)
from cartosentry.synthetic_models import (
    MotionState,
    ScenarioFeature,
    SyntheticFixture,
    SyntheticScenario,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/synthetic/v1"
SPLIT_MANIFEST = REPOSITORY_ROOT / "benchmarks/split_manifest.yaml"
CHARTER = REPOSITORY_ROOT / "benchmarks/numerical_charter.yaml"


def _fixtures() -> list[SyntheticFixture]:
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text())
    return [
        SyntheticFixture.model_validate_json(
            (FIXTURE_ROOT / item["relative_path"]).read_text()
        )
        for item in manifest["fixtures"]
    ]


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":"cartosentry.synthetic-fixture-set.v1",'
        b'"schema_version":"cartosentry.synthetic-fixture-set.v1"}',
        b"[" * 65 + b"0" + b"]" * 65,
        b'{"fixture_count":NaN}',
        b" " * (MAXIMUM_FIXTURE_SET_MANIFEST_BYTES + 1),
    ],
)
def test_fixture_set_manifest_parser_rejects_unsafe_json(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_fixture_set_manifest_bytes(content)


def test_fixture_qualification_bounds_manifest_before_comparison(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "fixtures"
    materialize_fixture_set(fixture_root, SPLIT_MANIFEST)
    (fixture_root / "manifest.json").write_bytes(
        b" " * (MAXIMUM_FIXTURE_SET_MANIFEST_BYTES + 1)
    )
    with pytest.raises(ValueError, match="size is outside"):
        qualify_fixture_set(fixture_root, SPLIT_MANIFEST, CHARTER)


def test_committed_fixture_set_passes_full_qualification() -> None:
    report = qualify_fixture_set(FIXTURE_ROOT, SPLIT_MANIFEST, CHARTER)
    assert report["accepted"] is True
    assert report["byte_deterministic"] is True
    assert report["fixtures_checked"] == 8
    assert report["scenario_coverage_complete"] is True
    assert report["time_mismatch_count"] == 0
    assert report["maximum_trajectory_lidar_world_error_m"] <= 1e-9


def test_fixture_set_covers_every_declared_v1_scenario_and_truth_feature() -> None:
    fixtures = _fixtures()
    assert {item.scenario for item in fixtures} == set(SyntheticScenario)
    assert {
        feature for item in fixtures for feature in item.truth.scenario_features
    } == set(ScenarioFeature)
    assert all(item.generator_version == GENERATOR_VERSION for item in fixtures)
    assert [item.seed for item in fixtures] == list(range(10000, 10008))
    assert [item.synthetic_family_id for item in fixtures] == [
        f"sensor-map-dev-{index:03d}" for index in range(1, 9)
    ]


def test_geometry_and_motion_controls_have_expected_structure() -> None:
    by_scenario = {item.scenario: item for item in _fixtures()}
    assert (
        len(by_scenario[SyntheticScenario.PARALLEL_ROADS].road_graph.directed_arcs) == 2
    )
    overpass_layers = {
        item.layer
        for item in by_scenario[SyntheticScenario.OVERPASS].road_graph.directed_arcs
    }
    assert overpass_layers == {0, 1}
    assert (
        by_scenario[SyntheticScenario.RAMP]
        .trajectory[-1]
        .world_from_rig.translation_m[2]
        == 2.0
    )
    off_map = by_scenario[SyntheticScenario.OFF_MAP_CONNECTION]
    assert off_map.truth.off_map_intervals
    assert any(item.motion_state is MotionState.OFF_MAP for item in off_map.trajectory)
    stopped = by_scenario[SyntheticScenario.STOP_START]
    assert any(item.motion_state is MotionState.STOPPED for item in stopped.trajectory)
    stationary = by_scenario[SyntheticScenario.STATIONARY]
    assert {item.motion_state for item in stationary.trajectory} == {
        MotionState.STOPPED
    }


def test_lidar_point_times_align_exactly_to_persisted_trajectory() -> None:
    for fixture in _fixtures():
        trajectory_times = {item.time.value_ns for item in fixture.trajectory}
        for scan in fixture.lidar_scans:
            for point in scan.points:
                assert point.relative_time_ns == (
                    point.column_index * COLUMN_PERIOD_NS - 250_000_000
                )
                assert (
                    scan.sensor_time.value_ns + point.relative_time_ns
                    in trajectory_times
                )


def test_fixed_version_and_seed_render_to_identical_bytes() -> None:
    first = render_fixture_set(SPLIT_MANIFEST)
    second = render_fixture_set(SPLIT_MANIFEST)
    assert first == second


def test_threshold_calibration_set_is_derived_exactly_from_split() -> None:
    assignments = sensor_map_family_assignments(SPLIT_MANIFEST, "threshold_calibration")
    assert len(assignments) == 12
    assert [family for family, _scenario, _seed in assignments] == [
        f"sensor-map-cal-{index:03d}" for index in range(1, 13)
    ]
    assert [seed for _family, _scenario, seed in assignments] == list(
        range(20000, 20012)
    )
    rendered = render_fixture_set(SPLIT_MANIFEST, partition="threshold_calibration")
    manifest = json.loads(rendered["manifest.json"])
    assert manifest["partition"] == "threshold_calibration"
    assert len(manifest["fixtures"]) == 12
    for record in manifest["fixtures"]:
        fixture = SyntheticFixture.model_validate_json(
            rendered[record["relative_path"]]
        )
        assert fixture.partition == "threshold_calibration"


def test_split_partition_mismatch_is_rejected(tmp_path: Path) -> None:
    split = json.loads(SPLIT_MANIFEST.read_text())
    family_set = next(
        item
        for item in split["synthetic_family_sets"]
        if item["family_set_id"] == "sensor-map-threshold-v0"
    )
    family_set["partition"] = "development"
    invalid = tmp_path / "split.json"
    invalid.write_text(json.dumps(split))
    with pytest.raises(ValueError, match="partition does not match"):
        sensor_map_family_assignments(invalid, "threshold_calibration")


def test_source_velocity_is_independent_and_persisted() -> None:
    by_scenario = {item.scenario: item for item in _fixtures()}
    stationary = by_scenario[SyntheticScenario.STATIONARY]
    assert {pose.source_velocity_world_mps for pose in stationary.trajectory} == {
        (0.0, 0.0, 0.0)
    }
    moving = by_scenario[SyntheticScenario.STRAIGHT]
    assert {pose.source_velocity_world_mps for pose in moving.trajectory} == {
        (5.0, 0.0, 0.0)
    }


@given(st.integers(min_value=0, max_value=(2**64) - 1))
@settings(max_examples=8, deadline=None)
def test_one_fixture_is_deterministic_for_every_generated_seed(seed: int) -> None:
    first = generate_fixture("property-family", SyntheticScenario.STRAIGHT, seed)
    second = generate_fixture("property-family", SyntheticScenario.STRAIGHT, seed)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_seed_changes_landmark_geometry_without_changing_motion() -> None:
    first = generate_fixture("seed-control", SyntheticScenario.STRAIGHT, 1)
    second = generate_fixture("seed-control", SyntheticScenario.STRAIGHT, 2)
    assert first.world.landmarks != second.world.landmarks
    assert first.trajectory == second.trajectory


def test_seeded_point_time_defect_fails_for_intended_reason() -> None:
    path = FIXTURE_ROOT / "fixtures/sensor-map-dev-001.json"
    valid = json.loads(path.read_text())
    assert (
        SyntheticFixture.model_validate_json(json.dumps(valid)).scenario
        is SyntheticScenario.STRAIGHT
    )
    invalid = json.loads(path.read_text())
    for point in invalid["lidar_scans"][0]["points"]:
        if point["column_index"] == 0:
            point["relative_time_ns"] += 1
    with pytest.raises(ValidationError, match="relative point time is not exact"):
        SyntheticFixture.model_validate_json(json.dumps(invalid))


def test_fixture_contract_rejects_local_or_traversing_values() -> None:
    path = FIXTURE_ROOT / "fixtures/sensor-map-dev-001.json"
    invalid = json.loads(path.read_text())
    invalid["trajectory"][0]["time"]["raw"]["source_key"] = "../private/time"
    with pytest.raises(ValueError, match="traversing path"):
        SyntheticFixture.model_validate_json(json.dumps(invalid))


def test_public_cli_generates_and_qualifies_real_fixture_files(tmp_path: Path) -> None:
    runner = CliRunner()
    generated = runner.invoke(
        app,
        [
            "generate-synthetic-fixtures",
            str(tmp_path),
            "--split-manifest",
            str(SPLIT_MANIFEST),
        ],
    )
    assert generated.exit_code == 0, generated.output
    qualified = runner.invoke(
        app,
        [
            "qualify-synthetic-fixtures",
            str(tmp_path),
            "--split-manifest",
            str(SPLIT_MANIFEST),
            "--charter",
            str(CHARTER),
        ],
    )
    assert qualified.exit_code == 0, qualified.output
    assert json.loads(qualified.stdout)["accepted"] is True


def test_stale_fixture_check_is_a_failing_negative_control(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/generate_synthetic_fixtures.py",
            "--check",
            "--output-root",
            str(tmp_path),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "manifest.json" in result.stdout
    control = subprocess.run(
        [sys.executable, "scripts/generate_synthetic_fixtures.py", "--check"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert control.returncode == 0, control.stderr


def test_committed_fixtures_are_compact_and_do_not_expand_modality_scope() -> None:
    files = list(FIXTURE_ROOT.rglob("*.json"))
    assert sum(path.stat().st_size for path in files) < 2 * 1024 * 1024
    schema_text = json.dumps(SyntheticFixture.model_json_schema()).lower()
    for excluded_modality in ("camera", "imu", "radar"):
        assert f'"{excluded_modality}"' not in schema_text
