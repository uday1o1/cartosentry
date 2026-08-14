"""Frozen synthetic road-matching qualification tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cartosentry.cli import app
from cartosentry.road_decoder_qualification import (
    GATE_IMMUTABLE_SHA256,
    TRUTH_IMMUTABLE_SHA256,
    load_map_matching_gate,
    load_map_matching_truth,
    qualify_synthetic_road_matching,
)
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GRAPH_PROFILE_PATH = REPOSITORY_ROOT / "profiles/graph_import_v1.yaml"
MATCHING_PROFILE_PATH = REPOSITORY_ROOT / "profiles/map_matching_v1.yaml"
DECODER_PROFILE_PATH = REPOSITORY_ROOT / "profiles/map_decoder_v1.yaml"
GATE_PATH = REPOSITORY_ROOT / "benchmarks/m5_3_map_matching_gate.yaml"
TRUTH_PATH = REPOSITORY_ROOT / "benchmarks/m5_3_map_matching_truth.yaml"
NUMERICAL_CHARTER_PATH = REPOSITORY_ROOT / "benchmarks/numerical_charter.yaml"
FIXTURE_PATH = REPOSITORY_ROOT / "tests/fixtures/road_graphs/topology_v1.osm"
EXPECTED_QUALIFICATION_SHA256 = (
    "76a349e8fa4a7180a2c1939362947a8e6df709963c32c5a6fe0ace2dd28fed03"
)


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


@pytest.fixture(scope="module")
def qualification_report() -> dict[str, object]:
    return qualify_synthetic_road_matching(
        graph_profile_path=GRAPH_PROFILE_PATH,
        matching_profile_path=MATCHING_PROFILE_PATH,
        decoder_profile_path=DECODER_PROFILE_PATH,
        gate_path=GATE_PATH,
        truth_path=TRUTH_PATH,
        numerical_charter_path=NUMERICAL_CHARTER_PATH,
        fixture_path=FIXTURE_PATH,
    )


def test_gate_is_self_hashed_and_has_the_exact_scenario_contract() -> None:
    gate, _ = load_map_matching_gate(GATE_PATH)
    truth, truth_file_sha256 = load_map_matching_truth(TRUTH_PATH)

    assert gate.immutable_sha256 == GATE_IMMUTABLE_SHA256
    assert truth.immutable_sha256 == TRUTH_IMMUTABLE_SHA256
    assert gate.authorities.map_matching_truth_file_sha256 == truth_file_sha256
    assert len(gate.required_scenario_ids) == 28
    assert len(gate.exact_path_scenario_ids) == 25
    assert gate.ambiguity_scenario_ids == (
        "parallel-ambiguous",
        "stopped-vehicle",
    )
    assert len(gate.confident_control_scenario_ids) == 26
    assert gate.stationary_scenario_ids == ("stopped-vehicle",)
    assert tuple(item.scenario_id for item in truth.scenarios) == (
        gate.required_scenario_ids
    )


def test_truth_pins_distinct_missing_edge_families_and_observation_specs() -> None:
    truth, _ = load_map_matching_truth(TRUTH_PATH)
    missing = tuple(
        item for item in truth.scenarios if item.suite == "MISSING_EDGE_TOPOLOGY"
    )

    assert len(missing) == 12
    assert len({item.synthetic_family_id for item in missing}) == 12
    assert len({item.fixture_object_key for item in missing}) == 12
    assert len({item.fixture_sha256 for item in missing}) == 12
    assert len({item.observation_spec_sha256 for item in missing}) == 12
    assert all(item.missing_path is not None for item in missing)
    assert all(
        tuple(run.directed_arc_id for run in item.expected_path_runs) == ("OFF_MAP",)
        for item in missing
    )


def test_frozen_synthetic_gate_passes_with_non_degenerate_intervals(
    qualification_report: dict[str, object],
) -> None:
    assert qualification_report["accepted"] is True
    assert qualification_report["algorithm_backend"] == "C++20_NATIVE_BATCH_V1"
    statistics = qualification_report["statistics"]
    assert isinstance(statistics, dict)
    assert statistics["support_passed"] is True
    assert statistics["eligible_directed_arc_clusters"] >= 12
    assert statistics["eligible_off_map_positive_clusters"] >= 12

    metrics = qualification_report["metrics"]
    assert isinstance(metrics, dict)
    for name in (
        "map.synthetic_directed_arc_accuracy",
        "map.synthetic_off_map_f1",
    ):
        metric = metrics[name]
        assert isinstance(metric, dict)
        assert metric["passed"] is True
        assert metric["interval_degenerate"] is False
        assert metric["degenerate_resample_count"] == 0
        assert metric["one_sided_lower_95"] >= metric["gate_value"]
    mismatch = metrics["map.tiny_path_mismatch_count"]
    assert isinstance(mismatch, dict)
    assert mismatch["value"] == 0
    assert mismatch["passed"] is True
    edit_distance = metrics["map.synthetic_exact_path_edit_distance"]
    assert isinstance(edit_distance, dict)
    assert edit_distance["value"] == 0
    assert edit_distance["passed"] is True
    ambiguity = metrics["map.synthetic_ambiguity_detection"]
    assert isinstance(ambiguity, dict)
    assert ambiguity == {
        "true_positive": 2,
        "true_negative": 26,
        "false_positive": 0,
        "false_negative": 0,
        "accuracy": 1.0,
        "decision_bound": "deterministic_frozen_expectations",
        "passed": True,
    }
    for name in (
        "map.synthetic_off_map_precision",
        "map.synthetic_off_map_recall",
    ):
        metric = metrics[name]
        assert isinstance(metric, dict)
        assert metric["point_estimate"] >= 0.99
        assert metric["degenerate_resample_count"] == 0


def test_exact_ambiguous_off_map_and_stationary_scenarios_are_explicit(
    qualification_report: dict[str, object],
) -> None:
    scenarios = qualification_report["scenarios"]
    assert isinstance(scenarios, list)
    by_id = {item["scenario_id"]: item for item in scenarios}
    assert all(
        item["exact_path_passed"]
        for item in scenarios
        if item["exact_path_gate_eligible"]
    )
    assert by_id["parallel-ambiguous"]["decoded_confidence"] == "AMBIGUOUS"
    assert by_id["parallel-ambiguous"]["confidence_passed"] is True
    assert by_id["stopped-vehicle"]["confidence_passed"] is True
    assert by_id["stopped-vehicle"]["stationary_passed"] is True
    missing = tuple(
        item
        for scenario_id, item in by_id.items()
        if scenario_id.startswith("missing-edge-offmap-")
    )
    assert len({item["graph_id"] for item in missing}) == 12
    assert all(
        all(value is None for value in item["predicted_arc_ids"]) for item in missing
    )


def test_public_cli_reproduces_the_canonical_report(
    tmp_path: Path, qualification_report: dict[str, object]
) -> None:
    output = tmp_path / "qualification.json"
    result = CliRunner().invoke(
        app,
        [
            "qualify-road-matching",
            "--graph-profile",
            str(GRAPH_PROFILE_PATH),
            "--matching-profile",
            str(MATCHING_PROFILE_PATH),
            "--decoder-profile",
            str(DECODER_PROFILE_PATH),
            "--gate",
            str(GATE_PATH),
            "--truth",
            str(TRUTH_PATH),
            "--numerical-charter",
            str(NUMERICAL_CHARTER_PATH),
            "--fixture",
            str(FIXTURE_PATH),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    expected = (
        json.dumps(
            qualification_report,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    assert output.read_text(encoding="utf-8") == expected
    assert hashlib.sha256(expected.encode("utf-8")).hexdigest() == (
        EXPECTED_QUALIFICATION_SHA256
    )
    assert "NaN" not in expected
    assert "Infinity" not in expected


def test_rehashed_gate_tampering_cannot_move_the_frozen_authority(
    tmp_path: Path,
) -> None:
    raw = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    raw["authorities"]["fixture_sha256"] = "f" * 64
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    raw["immutable_sha256"] = _canonical_hash(canonical)
    path = tmp_path / "tampered-gate.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="identity is not pinned"):
        load_map_matching_gate(path)


def test_rehashed_truth_tampering_cannot_move_expected_outcomes(
    tmp_path: Path,
) -> None:
    raw = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    raw["scenarios"][0]["expected_confidence"] = "AMBIGUOUS"
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    raw["immutable_sha256"] = _canonical_hash(canonical)
    path = tmp_path / "tampered-truth.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="identity is not pinned"):
        load_map_matching_truth(path)


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"value":Infinity}',
        b"[" * 65 + b"0" + b"]" * 65,
        b" " * (256 * 1024 + 1),
    ],
)
def test_gate_rejects_hostile_json(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "hostile-gate.json"
    path.write_bytes(content)

    with pytest.raises(ValueError):
        load_map_matching_gate(path)


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"value":NaN}',
        b"[" * 65 + b"0" + b"]" * 65,
        b" " * (1024 * 1024 + 1),
    ],
)
def test_truth_rejects_hostile_json(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "hostile-truth.json"
    path.write_bytes(content)

    with pytest.raises(ValueError):
        load_map_matching_truth(path)
