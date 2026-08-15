"""Blind-review and public route qualification contracts for Milestone 5.6."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cartosentry.cli import app
from cartosentry.public_road_matching_qualification import (
    GATE_IMMUTABLE_SHA256,
    ReviewDecision,
    load_public_road_matching_gate,
    prepare_public_route_review,
)
from pydantic import ValidationError
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).parents[1]
PUBLIC_DATA_ROOT = REPOSITORY_ROOT / "data/public"
GATE_PATH = REPOSITORY_ROOT / "benchmarks/m5_6_public_road_matching_gate.yaml"
PROTOCOL_PATH = REPOSITORY_ROOT / "docs/public_route_adjudication.md"
DATA_MANIFEST_PATH = REPOSITORY_ROOT / "benchmarks/data_manifest.yaml"
SOURCE_GROUPS_PATH = REPOSITORY_ROOT / "benchmarks/source_groups.yaml"
SPLIT_MANIFEST_PATH = REPOSITORY_ROOT / "benchmarks/split_manifest.yaml"
CHARTER_PATH = REPOSITORY_ROOT / "benchmarks/numerical_charter.yaml"
GRAPH_PROFILE_PATH = REPOSITORY_ROOT / "profiles/graph_import_v1.yaml"
MATCHING_PROFILE_PATH = REPOSITORY_ROOT / "profiles/map_matching_v1.yaml"
DECODER_PROFILE_PATH = REPOSITORY_ROOT / "profiles/map_decoder_v1.yaml"
PUBLIC_TRAJECTORY_PATH = (
    PUBLIC_DATA_ROOT / "boreas-2021-09-02-11-42/applanix/gps_post_process.csv"
)
PUBLIC_GRAPH_PATH = PUBLIC_DATA_ROOT / "road_graphs/toronto-glen-shields-v1.osm"
EXPECTED_PACKET_SHA256 = (
    "ae84815296972a5cda2cfe9368206c037d1cf7a5f95605982376a5d802c1f44a"
)


def _prepare() -> dict[str, object]:
    return prepare_public_route_review(
        public_data_root=PUBLIC_DATA_ROOT,
        gate_path=GATE_PATH,
        protocol_path=PROTOCOL_PATH,
        data_manifest_path=DATA_MANIFEST_PATH,
        source_groups_path=SOURCE_GROUPS_PATH,
        split_manifest_path=SPLIT_MANIFEST_PATH,
        numerical_charter_path=CHARTER_PATH,
        graph_profile_path=GRAPH_PROFILE_PATH,
        matching_profile_path=MATCHING_PROFILE_PATH,
        decoder_profile_path=DECODER_PROFILE_PATH,
    )


def _contains_key(value: object, keys: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            key in keys or _contains_key(item, keys) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_key(item, keys) for item in value)
    return False


def test_pre_review_gate_is_self_authenticated_and_exact() -> None:
    gate, gate_file_sha256 = load_public_road_matching_gate(GATE_PATH)

    assert gate.immutable_sha256 == GATE_IMMUTABLE_SHA256
    assert gate_file_sha256 == hashlib.sha256(GATE_PATH.read_bytes()).hexdigest()
    assert gate.public_source.partition == "development"
    assert gate.thresholds.confident_moving_distance_fraction_minimum == 0.85


def test_pre_review_gate_rejects_tampering(tmp_path: Path) -> None:
    altered = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    altered["review_sample"]["source_record_stride"] = 201
    path = tmp_path / "altered-gate.json"
    path.write_text(json.dumps(altered), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable hash"):
        load_public_road_matching_gate(path)


def test_unresolved_decision_cannot_carry_a_forced_arc() -> None:
    with pytest.raises(ValidationError, match="only a directed-arc"):
        ReviewDecision.model_validate(
            {
                "source_record_index": 200,
                "observation_id": f"observation-sha256-{'1' * 64}",
                "label": "UNRESOLVED",
                "expected_directed_arc_id": f"osm-arc-sha256-{'2' * 64}",
                "evidence_codes": ("INSUFFICIENT_EVIDENCE",),
            }
        )


@pytest.mark.skipif(
    not PUBLIC_TRAJECTORY_PATH.is_file() or not PUBLIC_GRAPH_PATH.is_file(),
    reason="manifest-pinned public inputs are not materialized",
)
def test_real_blind_review_packet_is_deterministic_and_redacted() -> None:
    packet = _prepare()
    gate, _ = load_public_road_matching_gate(GATE_PATH)

    assert packet["packet_immutable_sha256"] == EXPECTED_PACKET_SHA256
    assert packet["selected_source_record_count"] == 1075
    assert packet["moving_review_observation_count"] == 869
    assert packet["moving_distance_m"] == 7912.507802064
    assert packet["partition"] == "development"
    assert packet["production_decoder_output_included"] is False
    assert packet["final_test_material_included"] is False
    assert not _contains_key(packet, set(gate.review_sample.forbidden_packet_fields))


def test_public_commands_expose_blind_preparation_and_checked_qualification() -> None:
    prepare_help = CliRunner().invoke(app, ["prepare-public-road-review", "--help"])
    qualify_help = CliRunner().invoke(app, ["qualify-public-road-matching", "--help"])

    assert prepare_help.exit_code == 0, prepare_help.output
    assert "without running the decoder" in prepare_help.output
    assert "--public-data-root" in prepare_help.output
    assert "--output" in prepare_help.output
    assert qualify_help.exit_code == 0, qualify_help.output
    assert "frozen blind route decisions" in qualify_help.output
    assert "--adjudication" in qualify_help.output
    assert str(REPOSITORY_ROOT) not in prepare_help.output
    assert str(REPOSITORY_ROOT) not in qualify_help.output
