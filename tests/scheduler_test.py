"""Public-path tests for the native bounded scheduler qualification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cartosentry.cli import app
from cartosentry.scheduler import load_scheduler_stress_suite, qualify_scheduler
from pydantic import ValidationError
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STRESS_SUITE = REPOSITORY_ROOT / "benchmarks/scheduler_stress.yaml"


def test_scheduler_qualification_exercises_real_native_path(tmp_path: Path) -> None:
    output = tmp_path / "scheduler-evidence"
    report = qualify_scheduler(output, STRESS_SUITE)
    assert report.accepted is True
    assert report.peak_resident_bytes <= report.resident_byte_budget
    assert report.mixed_completed_units == 2000
    assert report.mixed_imu_units == 1800
    assert report.mixed_lidar_units == 200
    assert report.deterministic_replay_equal is True
    assert report.deterministic_execution_order == (
        "metadata-1",
        "imu-1",
        "lidar-1",
        "metadata-2",
        "imu-2",
        "lidar-2",
    )
    assert report.backpressure_observed is True
    assert set(report.structured_error_codes) == {
        "TASK_FAILURE",
        "UNHANDLED_EXCEPTION",
    }
    assert report.isolated_failed_units == 2
    assert report.isolated_completed_units == 1
    assert report.cancelled_units == 4
    assert report.outstanding_units_after_cancel == 0
    assert report.resident_bytes_after_cancel == 0
    attempt = output / "cancelled-attempt"
    assert sorted(item.name for item in attempt.iterdir()) == [
        "active-imu.partial",
        "active-lidar.partial",
    ]
    assert not (attempt / "completion.json").exists()


def test_public_cli_reports_evidence_and_refuses_existing_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "cli-scheduler-evidence"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["qualify-scheduler", str(output), "--suite", str(STRESS_SUITE)],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["accepted"] is True
    assert report["completion_pointer_exists"] is False
    assert report["outstanding_units_after_cancel"] == 0
    repeated = runner.invoke(
        app,
        ["qualify-scheduler", str(output), "--suite", str(STRESS_SUITE)],
    )
    assert repeated.exit_code == 2
    assert "already exists" in repeated.output


def test_frozen_suite_rejects_parameter_changes(tmp_path: Path) -> None:
    changed_suite = tmp_path / "changed-suite.yaml"
    suite = json.loads(STRESS_SUITE.read_text())
    suite["mixed_unit_count"] = 1999
    changed_suite.write_text(json.dumps(suite))
    with pytest.raises(ValidationError, match="Input should be 2000"):
        load_scheduler_stress_suite(changed_suite)
