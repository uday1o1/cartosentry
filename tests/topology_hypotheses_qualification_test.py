from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cartosentry.cli import app
from cartosentry.topology_hypotheses_qualification import (
    GATE_IMMUTABLE_SHA256,
    load_topology_hypothesis_gate,
    qualify_topology_hypotheses,
)
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).parents[1]
PROFILE_PATH = REPOSITORY_ROOT / "profiles/topology_hypotheses_v1.yaml"
GATE_PATH = REPOSITORY_ROOT / "benchmarks/m5_5_topology_hypotheses_gate.yaml"
CHARTER_PATH = REPOSITORY_ROOT / "benchmarks/numerical_charter.yaml"


def _qualify() -> dict[str, object]:
    return qualify_topology_hypotheses(
        profile_path=PROFILE_PATH,
        gate_path=GATE_PATH,
        numerical_charter_path=CHARTER_PATH,
    )


def test_frozen_supported_synthetic_topology_gate_passes() -> None:
    report = _qualify()
    assert report["accepted"] is True
    assert report["gates"] == {
        "confirmatory_support": True,
        "synthetic_precision": True,
        "synthetic_recall": True,
        "endpoint_localization": True,
        "false_hypotheses_on_unchanged_distance": True,
        "review_hypothesis_labels": True,
    }
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["synthetic_hypothesis_precision_lower_95"] == 1.0
    assert metrics["synthetic_hypothesis_recall_lower_95"] == 1.0
    assert metrics["endpoint_localization_error_road_bins_upper_95"] == 0.0
    assert metrics["false_hypotheses_per_unchanged_km_upper_95"] < 0.1
    assert metrics["true_positive_hypotheses"] == 36
    assert metrics["false_positive_hypotheses"] == 0
    assert metrics["false_negative_hypotheses"] == 0
    demonstration = report["demonstration"]
    assert isinstance(demonstration, dict)
    assert demonstration["public_result_label"] == "REVIEW_HYPOTHESIS_NOT_GROUND_TRUTH"
    assert demonstration["automatic_map_edit_permitted"] is False


def test_qualification_report_is_deterministic() -> None:
    first = json.dumps(_qualify(), sort_keys=True, separators=(",", ":"))
    second = json.dumps(_qualify(), sort_keys=True, separators=(",", ":"))
    assert (
        hashlib.sha256(first.encode()).hexdigest()
        == hashlib.sha256(second.encode()).hexdigest()
    )


def test_gate_is_self_authenticated(tmp_path: Path) -> None:
    gate, gate_file_sha256 = load_topology_hypothesis_gate(GATE_PATH)
    assert gate.immutable_sha256 == GATE_IMMUTABLE_SHA256
    assert gate_file_sha256 == hashlib.sha256(GATE_PATH.read_bytes()).hexdigest()

    altered = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    altered["thresholds"]["synthetic_recall_minimum"] = 0.79
    altered_path = tmp_path / "altered-gate.json"
    altered_path.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable hash"):
        load_topology_hypothesis_gate(altered_path)


def test_public_cli_writes_the_accepted_review_only_report(tmp_path: Path) -> None:
    output = tmp_path / "topology-qualification.json"
    result = CliRunner().invoke(
        app,
        [
            "qualify-topology-hypotheses",
            "--profile",
            str(PROFILE_PATH),
            "--gate",
            str(GATE_PATH),
            "--numerical-charter",
            str(CHARTER_PATH),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["accepted"] is True
    assert (
        report["result_semantics"]
        == "SUPPORTED_SYNTHETIC_REVIEW_HYPOTHESES_ONLY_NOT_GROUND_TRUTH"
    )
    assert (
        report["demonstration"]["sample_hypothesis"]["result_label"]
        == "REVIEW_HYPOTHESIS_NOT_GROUND_TRUTH"
    )
