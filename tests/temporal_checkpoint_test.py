"""Tests for the frozen M3.5 temporal benchmark checkpoint."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from cartosentry.cli import app
from cartosentry.synthetic import generate_fixture
from cartosentry.synthetic_models import SyntheticScenario
from cartosentry.temporal_checkpoint import (
    CHECKPOINT_IMMUTABLE_SHA256,
    _authenticate_authorities,
    _clip_bounds,
    load_temporal_checkpoint,
    review_public_trajectory_clips,
)
from cartosentry.trajectory import reference_samples_from_synthetic
from cartosentry.trajectory_integrity import load_trajectory_integrity_profile
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_PATH = REPOSITORY_ROOT / "benchmarks/m3_5_temporal_checkpoint.yaml"
PROFILE_PATH = REPOSITORY_ROOT / "profiles/trajectory_integrity_v1.yaml"
TRAJECTORY_GATE_PATH = REPOSITORY_ROOT / "benchmarks/m3_1_trajectory_gate.yaml"


def _authority_paths() -> dict[str, Path]:
    return {
        "profile_path": PROFILE_PATH,
        "trajectory_gate_path": TRAJECTORY_GATE_PATH,
        "split_manifest_path": REPOSITORY_ROOT / "benchmarks/split_manifest.yaml",
        "fault_matrix_path": REPOSITORY_ROOT / "benchmarks/fault_matrix_v1.yaml",
        "numerical_charter_path": (
            REPOSITORY_ROOT / "benchmarks/numerical_charter.yaml"
        ),
        "charter_revisions_path": (
            REPOSITORY_ROOT / "benchmarks/charter_revisions.yaml"
        ),
        "data_manifest_path": REPOSITORY_ROOT / "benchmarks/data_manifest.yaml",
        "source_groups_path": REPOSITORY_ROOT / "benchmarks/source_groups.yaml",
    }


def test_checkpoint_is_self_hashed_and_exact() -> None:
    checkpoint, file_sha256 = load_temporal_checkpoint(CHECKPOINT_PATH)

    assert checkpoint.immutable_sha256 == CHECKPOINT_IMMUTABLE_SHA256
    assert len(file_sha256) == 64
    assert checkpoint.public_review.minimum_clip_count == 6
    assert {item.sequence_id for item in checkpoint.public_review.sequences} == {
        "boreas-2021-09-02-11-42",
        "boreas-2021-01-19-15-08",
    }
    assert checkpoint.threshold_change_procedure.only_partition == (
        "threshold_calibration"
    )


def test_checkpoint_mutation_is_rejected(tmp_path: Path) -> None:
    raw = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    raw["public_review"]["clip_length_samples"] += 1
    changed = tmp_path / "checkpoint.json"
    changed.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable hash"):
        load_temporal_checkpoint(changed)


def test_checkpoint_authenticates_every_frozen_authority(tmp_path: Path) -> None:
    checkpoint, _ = load_temporal_checkpoint(CHECKPOINT_PATH)
    data_manifest, source_groups = _authenticate_authorities(
        checkpoint, **_authority_paths()
    )
    assert data_manifest["manifest_version"] == "m0.4-v1"
    assert source_groups["assignment_version"] == "m0.2-v1"

    changed_manifest = tmp_path / "data-manifest.json"
    raw = json.loads(
        (REPOSITORY_ROOT / "benchmarks/data_manifest.yaml").read_text(encoding="utf-8")
    )
    raw["manifest_version"] = "changed"
    changed_manifest.write_text(json.dumps(raw), encoding="utf-8")
    paths = _authority_paths()
    paths["data_manifest_path"] = changed_manifest
    with pytest.raises(ValueError, match="data manifest does not match"):
        _authenticate_authorities(checkpoint, **paths)


@pytest.mark.parametrize(
    ("sample_count", "clip_length", "expected"),
    [
        (1000, 128, (("start", 0, 128), ("middle", 436, 564), ("end", 872, 1000))),
        (4098, 4096, (("start", 0, 4096), ("middle", 1, 4097), ("end", 2, 4098))),
    ],
)
def test_clip_bounds_are_deterministic(
    sample_count: int,
    clip_length: int,
    expected: tuple[tuple[str, int, int], ...],
) -> None:
    assert _clip_bounds(sample_count, clip_length) == expected


def test_clip_bounds_require_three_distinct_clips() -> None:
    with pytest.raises(ValueError, match="three distinct"):
        _clip_bounds(4097, 4096)


def _review_fixture(*, regression: bool) -> tuple[dict[str, object], ...]:
    checkpoint, _ = load_temporal_checkpoint(CHECKPOINT_PATH)
    review = checkpoint.public_review.model_copy(update={"clip_length_samples": 64})
    test_checkpoint = checkpoint.model_copy(update={"public_review": review})
    profile, profile_file_sha256 = load_trajectory_integrity_profile(PROFILE_PATH)
    fixture = generate_fixture(
        "m3-5-unit-review",
        SyntheticScenario.STRAIGHT,
        seed=35_000,
    )
    samples = list(reference_samples_from_synthetic(fixture))
    if regression:
        selected = samples[64]
        samples[64] = replace(
            selected,
            time=selected.time.model_copy(
                update={"value_ns": samples[63].time.value_ns - 1}
            ),
        )
    return review_public_trajectory_clips(
        samples,
        sequence=test_checkpoint.public_review.sequences[0],
        source_file_sha256="0" * 64,
        checkpoint=test_checkpoint,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        trajectory_gate_path=TRAJECTORY_GATE_PATH,
    )


def test_clean_clips_have_empty_false_critical_review_queue() -> None:
    reports = _review_fixture(regression=False)

    assert len(reports) == 3
    assert all(
        report["false_critical_review"]["unresolved_count"] == 0 for report in reports
    )
    assert all(report["structural_valid"] is True for report in reports)


def test_blocking_finding_remains_unresolved_in_review_queue() -> None:
    reports = _review_fixture(regression=True)

    affected = [
        report
        for report in reports
        if report["false_critical_review"]["unresolved_count"] > 0
    ]
    assert affected
    assert all(
        report["false_critical_review"]["disposition"]
        == "UNRESOLVED_REQUIRES_ADJUDICATION"
        for report in affected
    )


def test_public_cli_exposes_temporal_checkpoint_workflow() -> None:
    result = CliRunner().invoke(app, ["qualify-temporal-checkpoint", "--help"])

    assert result.exit_code == 0
    assert "public-data-root" in result.stdout
    assert "machine-readable" in result.stdout
    assert "checkpoint atomically" in result.stdout
