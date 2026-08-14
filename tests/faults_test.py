"""Registry, provenance, determinism, and isolation tests for the V1 faults."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from cartosentry.cli import app
from cartosentry.faults import (
    FAULT_MATRIX_ID,
    ChangeKind,
    FaultManifest,
    FaultOperatorId,
    FaultRequest,
    inject_fault,
    load_fault_registry,
    materialize_fault_result,
    parse_fault_manifest_bytes,
    serialize_fault_manifest,
    verify_fault_result,
)
from cartosentry.identifiers import canonical_sha256, make_road_bin_id
from cartosentry.synthetic import generate_fixture, serialize_fixture
from cartosentry.synthetic_models import SyntheticScenario, SyntheticTransform
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = REPOSITORY_ROOT / "benchmarks/fault_matrix_v1.yaml"
SOURCE_PATH = (
    REPOSITORY_ROOT / "tests/fixtures/synthetic/v1/fixtures/sensor-map-dev-001.json"
)
CLEAN_TRUTH_HASH = hashlib.sha256(b"immutable synthetic clean truth\n").hexdigest()
REPRESENTATIVE_CASES = {
    FaultOperatorId.TIMESTAMP_DISCONTINUITY: "timestamp-gap-20ms-below",
    FaultOperatorId.POSITION_JUMP: "position-jump-0p05m-below",
    FaultOperatorId.POINT_TIME_SHIFT: "point-time-10ms-below",
    FaultOperatorId.RING_LOSS: "ring-loss-1-short",
    FaultOperatorId.AZIMUTH_SECTOR_LOSS: "sector-loss-5deg-short",
    FaultOperatorId.CALIBRATION_PERTURBATION: ("extrinsic-0p01m-0p1deg-below"),
}
ALLOWED_CHANGE_PATTERNS = {
    FaultOperatorId.TIMESTAMP_DISCONTINUITY: re.compile(
        r"^/trajectory/[0-9]+/time/(value_ns|raw/integer_value)$"
    ),
    FaultOperatorId.POSITION_JUMP: re.compile(
        r"^/trajectory/[0-9]+/world_from_rig/row_major_4x4/(3|7|11)$"
    ),
    FaultOperatorId.POINT_TIME_SHIFT: re.compile(
        r"^/lidar_scans/[0-9]+/points/[0-9]+/relative_time_ns$"
    ),
    FaultOperatorId.RING_LOSS: re.compile(r"^/lidar_scans/[0-9]+/points/[0-9]+$"),
    FaultOperatorId.AZIMUTH_SECTOR_LOSS: re.compile(
        r"^/lidar_scans/[0-9]+/points/[0-9]+$"
    ),
    FaultOperatorId.CALIBRATION_PERTURBATION: re.compile(
        r"^/rig/rig_from_lidar/row_major_4x4/(0|1|3|4|5|7|11)$"
    ),
}


def _source(operator_id: FaultOperatorId) -> bytes:
    if operator_id is FaultOperatorId.AZIMUTH_SECTOR_LOSS:
        return serialize_fixture(
            generate_fixture(
                "fault-sector-family",
                SyntheticScenario.STRAIGHT,
                1701,
                azimuth_columns=32,
            )
        )
    return SOURCE_PATH.read_bytes()


def _request(operator_id: FaultOperatorId, seed: int) -> FaultRequest:
    return FaultRequest(
        operator_id=operator_id,
        case_id=REPRESENTATIVE_CASES[operator_id],
        seed=seed,
        clean_source_truth_sha256=CLEAN_TRUTH_HASH,
    )


def _independent_changes(
    source: Any, derived: Any, pointer: str = ""
) -> list[tuple[str, ChangeKind, str, str | None]]:
    if isinstance(source, dict) and isinstance(derived, dict):
        assert source.keys() == derived.keys()
        result: list[tuple[str, ChangeKind, str, str | None]] = []
        for key in source:
            result.extend(
                _independent_changes(source[key], derived[key], f"{pointer}/{key}")
            )
        return result
    if isinstance(source, list) and isinstance(derived, list):
        if len(source) == len(derived):
            result = []
            for index, (left, right) in enumerate(zip(source, derived, strict=True)):
                result.extend(_independent_changes(left, right, f"{pointer}/{index}"))
            return result
        result = []
        derived_index = 0
        for source_index, item in enumerate(source):
            if derived_index < len(derived) and item == derived[derived_index]:
                derived_index += 1
            else:
                result.append(
                    (
                        f"{pointer}/{source_index}",
                        ChangeKind.REMOVED,
                        canonical_sha256(item),
                        None,
                    )
                )
        assert derived_index == len(derived)
        return result
    if source != derived:
        return [
            (
                pointer,
                ChangeKind.MODIFIED,
                canonical_sha256(source),
                canonical_sha256(derived),
            )
        ]
    return []


def test_registry_exactly_covers_frozen_v1_allowlist_and_typed_cases() -> None:
    matrix = json.loads(MATRIX_PATH.read_text())
    registry = load_fault_registry(MATRIX_PATH)
    assert matrix["fault_matrix_id"] == FAULT_MATRIX_ID
    assert set(matrix["v1_operator_allowlist"]) == {
        operator.value for operator in FaultOperatorId
    }
    assert {operator for operator, _ in registry.cases} == set(FaultOperatorId)
    assert len(registry.cases) == sum(
        len(operator["cases"]) for operator in matrix["operators"]
    )
    assert (
        registry.matrix_sha256 == hashlib.sha256(MATRIX_PATH.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("operator_id", list(FaultOperatorId))
def test_representative_operator_is_deterministic_and_independently_verifiable(
    operator_id: FaultOperatorId,
) -> None:
    registry = load_fault_registry(MATRIX_PATH)
    source = _source(operator_id)
    request = _request(operator_id, 90210)
    first = inject_fault(source, request, registry)
    second = inject_fault(source, request, registry)
    assert first == second
    assert hashlib.sha256(first.derivative_bytes).hexdigest() == (
        first.manifest.resulting_artifacts[0].sha256
    )
    report = verify_fault_result(
        source,
        first.derivative_bytes,
        serialize_fault_manifest(first.manifest),
        registry,
    )
    assert report["accepted"] is True
    assert first.manifest.operator_id is operator_id
    assert first.manifest.inherited_partition == "development"
    assert first.manifest.clean_source_truth_sha256 == CLEAN_TRUTH_HASH
    if operator_id is FaultOperatorId.CALIBRATION_PERTURBATION:
        derivative = json.loads(first.derivative_bytes)
        SyntheticTransform.model_validate_json(
            json.dumps(derivative["rig"]["rig_from_lidar"])
        )


@given(st.integers(min_value=0, max_value=(2**64) - 1))
@settings(max_examples=5, deadline=None)
def test_every_semantic_change_is_attributed_and_unrelated_fields_survive(
    seed: int,
) -> None:
    registry = load_fault_registry(MATRIX_PATH)
    for operator_id in FaultOperatorId:
        source = _source(operator_id)
        result = inject_fault(source, _request(operator_id, seed), registry)
        independent = _independent_changes(
            json.loads(source), json.loads(result.derivative_bytes)
        )
        recorded = [
            (
                item.json_pointer,
                item.change_kind,
                item.source_value_sha256,
                item.derived_value_sha256,
            )
            for item in result.manifest.changed_values
        ]
        assert recorded == independent
        allowed = ALLOWED_CHANGE_PATTERNS[operator_id]
        assert all(allowed.fullmatch(pointer) for pointer, *_ in independent)


def test_invalid_operator_range_fails_before_creating_output(tmp_path: Path) -> None:
    registry = load_fault_registry(MATRIX_PATH)
    source = SOURCE_PATH.read_bytes()
    request = FaultRequest(
        operator_id=FaultOperatorId.RING_LOSS,
        case_id="ring-loss-16-long",
        seed=1,
        clean_source_truth_sha256=CLEAN_TRUTH_HASH,
    )
    output_root = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="duration exceeds"):
        result = inject_fault(source, request, registry)
        materialize_fault_result(output_root, result)
    assert not output_root.exists()
    assert source == SOURCE_PATH.read_bytes()


def test_fault_matrix_rejects_duplicate_keys(tmp_path: Path) -> None:
    matrix = tmp_path / "fault-matrix.json"
    matrix.write_text(
        '{"fault_matrix_id":"cartosentry-v1-core",'
        '"fault_matrix_id":"cartosentry-v1-core"}'
    )
    with pytest.raises(ValueError, match="duplicate key"):
        load_fault_registry(matrix)


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":"cartosentry.fault-manifest.v1",'
        b'"schema_version":"cartosentry.fault-manifest.v1"}',
        b"[" * 65 + b"0" + b"]" * 65,
        b'{"seed":Infinity}',
        b" " * (16 * 1024 * 1024 + 1),
    ],
)
def test_fault_manifest_parser_rejects_unsafe_json(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_fault_manifest_bytes(content)


def test_every_committed_v1_family_can_only_generate_allowlisted_derivatives() -> None:
    registry = load_fault_registry(MATRIX_PATH)
    manifest = json.loads(
        (REPOSITORY_ROOT / "tests/fixtures/synthetic/v1/manifest.json").read_text()
    )
    allowed = {item.value for item in FaultOperatorId}
    for fixture_record in manifest["fixtures"]:
        source = (
            REPOSITORY_ROOT
            / "tests/fixtures/synthetic/v1"
            / fixture_record["relative_path"]
        ).read_bytes()
        result = inject_fault(
            source,
            _request(FaultOperatorId.TIMESTAMP_DISCONTINUITY, 11),
            registry,
        )
        assert result.manifest.operator_id.value in allowed
        assert result.manifest.source_group_id == fixture_record["synthetic_family_id"]


def test_expected_road_bin_contract_is_preserved_in_manifest() -> None:
    source = SOURCE_PATH.read_bytes()
    fixture = json.loads(source)
    road_bin_id = make_road_bin_id(fixture["road_graph"]["road_graph_id"], "main", 0)
    request = FaultRequest(
        operator_id=FaultOperatorId.POSITION_JUMP,
        case_id="position-jump-0p05m-below",
        seed=3,
        clean_source_truth_sha256=CLEAN_TRUTH_HASH,
        expected_affected_road_bins=(road_bin_id,),
    )
    result = inject_fault(source, request, load_fault_registry(MATRIX_PATH))
    assert result.manifest.expected_affected_road_bins == (road_bin_id,)


def test_unknown_or_follow_on_operator_fails_registry_validation(
    tmp_path: Path,
) -> None:
    matrix = json.loads(MATRIX_PATH.read_text())
    matrix["v1_operator_allowlist"].append("imu.time_shift")
    invalid = tmp_path / "invalid-matrix.json"
    invalid.write_text(json.dumps(matrix))
    with pytest.raises(ValueError, match="allowlist"):
        load_fault_registry(invalid)
    with pytest.raises(ValueError):
        FaultOperatorId("imu.time_shift")


def test_fault_manifest_is_strict_frozen_and_rejects_path_leakage() -> None:
    result = inject_fault(
        SOURCE_PATH.read_bytes(),
        _request(FaultOperatorId.POSITION_JUMP, 8),
        load_fault_registry(MATRIX_PATH),
    )
    assert FaultManifest.model_json_schema()["additionalProperties"] is False
    with pytest.raises(ValidationError):
        result.manifest.seed = 9  # type: ignore[misc]
    invalid = result.manifest.model_dump(mode="json")
    invalid["resulting_artifacts"][0]["artifact_key"] = "../derivative.json"
    with pytest.raises(ValidationError, match="traversing path"):
        FaultManifest.model_validate_json(json.dumps(invalid))


def test_public_cli_injects_and_verifies_real_files(tmp_path: Path) -> None:
    truth = tmp_path / "clean-truth.json"
    truth.write_text('{"state":"frozen"}\n')
    output_root = tmp_path / "fault-output"
    runner = CliRunner()
    injected = runner.invoke(
        app,
        [
            "inject-synthetic-fault",
            str(SOURCE_PATH),
            str(output_root),
            "--operator",
            FaultOperatorId.RING_LOSS.value,
            "--case",
            "ring-loss-1-short",
            "--seed",
            "42",
            "--clean-source-truth",
            str(truth),
            "--fault-matrix",
            str(MATRIX_PATH),
        ],
    )
    assert injected.exit_code == 0, injected.output
    assert (output_root / "derivative.json").is_file()
    assert (output_root / "manifest.json").is_file()
    verified = runner.invoke(
        app,
        [
            "verify-synthetic-fault",
            str(SOURCE_PATH),
            str(output_root),
            "--fault-matrix",
            str(MATRIX_PATH),
        ],
    )
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.stdout)["accepted"] is True


def test_public_cli_invalid_range_leaves_no_output(tmp_path: Path) -> None:
    truth = tmp_path / "clean-truth.json"
    truth.write_text('{"state":"frozen"}\n')
    output_root = tmp_path / "invalid-output"
    result = CliRunner().invoke(
        app,
        [
            "inject-synthetic-fault",
            str(SOURCE_PATH),
            str(output_root),
            "--operator",
            FaultOperatorId.RING_LOSS.value,
            "--case",
            "ring-loss-16-long",
            "--seed",
            "42",
            "--clean-source-truth",
            str(truth),
            "--fault-matrix",
            str(MATRIX_PATH),
        ],
    )
    assert result.exit_code == 2
    assert "duration exceeds" in result.output
    assert not output_root.exists()


def test_verifier_rejects_tampered_derivative() -> None:
    registry = load_fault_registry(MATRIX_PATH)
    source = SOURCE_PATH.read_bytes()
    result = inject_fault(
        source, _request(FaultOperatorId.TIMESTAMP_DISCONTINUITY, 5), registry
    )
    tampered = result.derivative_bytes.replace(b'"seed":10000', b'"seed":10001', 1)
    report = verify_fault_result(
        source, tampered, serialize_fault_manifest(result.manifest), registry
    )
    assert report["accepted"] is False
    assert report["derivative_matches"] is False
