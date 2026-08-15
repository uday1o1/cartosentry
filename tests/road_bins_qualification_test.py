"""Frozen M5.4 directed-road bin qualification tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cartosentry.cli import app
from cartosentry.road_bins import PROFILE_FILE_SHA256
from cartosentry.road_bins_qualification import (
    GATE_IMMUTABLE_SHA256,
    load_road_bin_gate,
    qualify_directed_road_bins,
)
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GRAPH_PROFILE_PATH = REPOSITORY_ROOT / "profiles/graph_import_v1.yaml"
MATCH_PROFILE_PATH = REPOSITORY_ROOT / "profiles/map_matching_v1.yaml"
DECODER_PROFILE_PATH = REPOSITORY_ROOT / "profiles/map_decoder_v1.yaml"
BIN_PROFILE_PATH = REPOSITORY_ROOT / "profiles/road_binning_v1.yaml"
GATE_PATH = REPOSITORY_ROOT / "benchmarks/m5_4_road_bins_gate.yaml"
NUMERICAL_CHARTER_PATH = REPOSITORY_ROOT / "benchmarks/numerical_charter.yaml"
FIXTURE_PATH = REPOSITORY_ROOT / "tests/fixtures/road_graphs/topology_v1.osm"


def _qualify() -> dict[str, object]:
    return qualify_directed_road_bins(
        graph_profile_path=GRAPH_PROFILE_PATH,
        matching_profile_path=MATCH_PROFILE_PATH,
        decoder_profile_path=DECODER_PROFILE_PATH,
        binning_profile_path=BIN_PROFILE_PATH,
        gate_path=GATE_PATH,
        numerical_charter_path=NUMERICAL_CHARTER_PATH,
        fixture_path=FIXTURE_PATH,
    )


def test_gate_is_self_hashed_and_binds_every_authority() -> None:
    gate, file_sha256 = load_road_bin_gate(GATE_PATH)
    assert gate.immutable_sha256 == GATE_IMMUTABLE_SHA256
    assert len(file_sha256) == 64
    assert gate.authorities.road_binning_profile_file_sha256 == PROFILE_FILE_SHA256
    assert (
        gate.authorities.numerical_charter_file_sha256
        == hashlib.sha256(NUMERICAL_CHARTER_PATH.read_bytes()).hexdigest()
    )
    assert (
        gate.authorities.fixture_sha256
        == hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    )
    assert gate.synthetic_population.total_injected_events == 36


def test_qualification_is_deterministic_and_passes_every_gate() -> None:
    first = _qualify()
    second = _qualify()
    assert first == second
    assert first["accepted"] is True
    assert first["algorithm_backend"] == "C++20_NATIVE_BATCH_V1"
    assert first["support"] == {
        "independent_synthetic_family_count": 12,
        "injected_event_count": 36,
        "minimum_independent_clusters": 12,
        "minimum_injected_events": 30,
    }
    assert first["metrics"] == {
        "exact_bin_coverage_mismatch_count": 0,
        "adjacent_window_traversal_inflation_count": 0,
        "spatial_affected_bin_f1": 1.0,
        "spatial_affected_bin_f1_lower_95": 1.0,
        "true_positive_affected_bins": 72,
        "false_positive_affected_bins": 0,
        "false_negative_affected_bins": 0,
        "materialized_directed_bin_count": 218,
        "final_partial_bin_count": 26,
    }
    gates = first["gates"]
    assert isinstance(gates, dict)
    assert set(gates.values()) == {True}


def test_gate_rejects_threshold_and_authority_tampering(tmp_path: Path) -> None:
    raw = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    raw["thresholds"]["spatial_affected_bin_f1_minimum"] = 0.5
    changed_threshold = tmp_path / "changed-threshold.json"
    changed_threshold.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable hash"):
        load_road_bin_gate(changed_threshold)

    raw = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    raw["authorities"]["road_binning_profile_file_sha256"] = "f" * 64
    changed_authority = tmp_path / "changed-authority.json"
    changed_authority.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable hash"):
        load_road_bin_gate(changed_authority)


def test_public_cli_exercises_qualification_and_atomic_output(tmp_path: Path) -> None:
    runner = CliRunner()
    output = tmp_path / "m5.4-report.json"
    result = runner.invoke(app, ["qualify-road-bins", "--output", str(output)])
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["accepted"] is True
    assert report["metrics"]["spatial_affected_bin_f1_lower_95"] == 1.0
    assert output.read_bytes().endswith(b"\n")


def test_public_cli_reports_malformed_gate_as_process_failure(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed-gate.json"
    malformed.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["qualify-road-bins", "--gate", str(malformed)],
    )
    assert result.exit_code == 2
    assert "Road-bin qualification failed" in result.output
