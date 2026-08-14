"""Trajectory detector, observability, interval, and profile contract tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cartosentry.artifacts import Observability
from cartosentry.cli import app
from cartosentry.contracts import RigidTransform
from cartosentry.faults import (
    FaultOperatorId,
    FaultRequest,
    inject_fault,
    load_fault_registry,
)
from cartosentry.synthetic import generate_fixture, serialize_fixture
from cartosentry.synthetic_models import SyntheticScenario
from cartosentry.trajectory import ReferenceSample, load_trajectory_gate
from cartosentry.trajectory_integrity import (
    PROFILE_IMMUTABLE_SHA256,
    ReferenceEvidenceKind,
    ReferenceIndependenceBasis,
    ReferencePositionEvidence,
    ReferenceProvenanceKind,
    TrajectoryRule,
    detect_trajectory_integrity,
    load_trajectory_integrity_profile,
    make_reference_position_evidence,
    parse_synthetic_trajectory_bytes,
)
from cartosentry.trajectory_integrity_qualification import (
    qualify_trajectory_detector_controls,
    qualify_trajectory_integrity,
)
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPOSITORY_ROOT / "profiles/trajectory_integrity_v1.yaml"
TRAJECTORY_GATE_PATH = REPOSITORY_ROOT / "benchmarks/m3_1_trajectory_gate.yaml"
FAULT_MATRIX_PATH = REPOSITORY_ROOT / "benchmarks/fault_matrix_v1.yaml"
CLEAN_TRUTH_SHA256 = hashlib.sha256(b"frozen clean source truth\n").hexdigest()


def _detector_inputs() -> tuple[object, str, object]:
    profile, profile_file_sha256 = load_trajectory_integrity_profile(PROFILE_PATH)
    parameters = load_trajectory_gate(TRAJECTORY_GATE_PATH).parameters
    return profile, profile_file_sha256, parameters


def _detect(content: bytes, *, reference: bytes | None = None):
    parsed = parse_synthetic_trajectory_bytes(content)
    profile, profile_file_sha256, parameters = _detector_inputs()
    reference_evidence = None
    if reference is not None:
        clean = parse_synthetic_trajectory_bytes(reference)
        reference_evidence = make_reference_position_evidence(
            clean.samples,
            evidence_kind=ReferenceEvidenceKind.DECLARED_INDEPENDENT_REFERENCE,
            reference_source_sha256=clean.source_sha256,
            provenance_kind=ReferenceProvenanceKind.IMMUTABLE_PREINJECTION_SOURCE,
            provenance="synthetic pre-injection clean trajectory",
            independence_basis=ReferenceIndependenceBasis.PREINJECTION_HASH_DISTINCT,
        )
    return detect_trajectory_integrity(
        parsed.samples,
        source_sha256=parsed.source_sha256,
        partition=parsed.partition,
        profile=profile,  # type: ignore[arg-type]
        profile_file_sha256=profile_file_sha256,
        trajectory_parameters=parameters,  # type: ignore[arg-type]
        reference_evidence=reference_evidence,
    )


def _fault(
    operator_id: FaultOperatorId,
    case_id: str,
    *,
    seed: int = 71,
    scenario: SyntheticScenario = SyntheticScenario.STRAIGHT,
) -> tuple[bytes, object]:
    clean = serialize_fixture(generate_fixture("detector-family", scenario, 44001))
    result = inject_fault(
        clean,
        FaultRequest(
            operator_id=operator_id,
            case_id=case_id,
            seed=seed,
            clean_source_truth_sha256=CLEAN_TRUTH_SHA256,
        ),
        load_fault_registry(FAULT_MATRIX_PATH),
    )
    return clean, result


def _support(report: object, rule: TrajectoryRule):
    return next(item for item in report.support if item.rule is rule)  # type: ignore[attr-defined]


def _iou(left_start: int, left_end: int, right_start: int, right_end: int) -> float:
    intersection = max(0, min(left_end, right_end) - max(left_start, right_start))
    union = max(left_end, right_end) - min(left_start, right_start)
    return intersection / union


def test_profile_is_self_hashed_strict_and_pinned(tmp_path: Path) -> None:
    profile, _file_hash = load_trajectory_integrity_profile(PROFILE_PATH)
    assert profile.immutable_sha256 == PROFILE_IMMUTABLE_SHA256
    assert profile.model_json_schema()["additionalProperties"] is False
    tampered = json.loads(PROFILE_PATH.read_text())
    tampered["thresholds"]["trajectory.maximum_speed_mps"]["value"] = 71.0
    invalid = tmp_path / "profile.json"
    invalid.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="immutable hash"):
        load_trajectory_integrity_profile(invalid)


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"value":NaN}',
        b"[" * 65 + b"0" + b"]" * 65,
        b" " * (64 * 1024 + 1),
    ],
)
def test_profile_boundary_rejects_hostile_json(tmp_path: Path, content: bytes) -> None:
    invalid = tmp_path / "invalid-profile.json"
    invalid.write_bytes(content)
    with pytest.raises(ValueError):
        load_trajectory_integrity_profile(invalid)


def test_stationary_clean_trajectory_is_not_classified_as_frozen() -> None:
    content = serialize_fixture(
        generate_fixture(
            "stationary-detector-control",
            SyntheticScenario.STATIONARY,
            44002,
        )
    )
    report = _detect(content)
    assert report.structural_valid is True
    assert all(
        TrajectoryRule.POSITION_FREEZE not in event.triggered_rules
        for event in report.events
    )
    assert (
        _support(report, TrajectoryRule.POSITION_FREEZE).observability
        is Observability.OBSERVABLE
    )


@pytest.mark.parametrize(
    ("operator_id", "case_id", "expected_rule"),
    [
        (
            FaultOperatorId.TIMESTAMP_DISCONTINUITY,
            "timestamp-gap-250ms-detectable",
            TrajectoryRule.TIMESTAMP_GAP,
        ),
        (
            FaultOperatorId.POSITION_JUMP,
            "position-jump-2m-detectable",
            TrajectoryRule.POSITION_JUMP,
        ),
        (
            FaultOperatorId.POSITION_FREEZE,
            "position-freeze-1s-detectable",
            TrajectoryRule.POSITION_FREEZE,
        ),
        (
            FaultOperatorId.POSITION_DRIFT,
            "position-drift-2m-detectable",
            TrajectoryRule.VELOCITY_RESIDUAL,
        ),
    ],
)
def test_detectable_faults_are_found_from_derivative_bytes_without_manifest(
    operator_id: FaultOperatorId,
    case_id: str,
    expected_rule: TrajectoryRule,
) -> None:
    _clean, result = _fault(operator_id, case_id)
    report = _detect(result.derivative_bytes)
    assert any(expected_rule in event.triggered_rules for event in report.events)
    if operator_id is FaultOperatorId.TIMESTAMP_DISCONTINUITY:
        assert all(
            TrajectoryRule.TIMESTAMP_REGRESSION not in event.triggered_rules
            for event in report.events
        )


def test_paired_position_steps_cover_the_injected_interval() -> None:
    _clean, result = _fault(
        FaultOperatorId.POSITION_JUMP, "position-jump-2m-detectable", seed=77
    )
    report = _detect(result.derivative_bytes)
    event = next(
        item
        for item in report.events
        if TrajectoryRule.POSITION_JUMP in item.triggered_rules
    )
    truth = result.manifest.source_interval
    assert (
        _iou(
            event.start_time_ns,
            event.end_time_ns,
            truth.start.value_ns,
            truth.end.value_ns,
        )
        >= 0.5
    )


def test_short_below_gate_freeze_is_not_emitted() -> None:
    _clean, result = _fault(
        FaultOperatorId.POSITION_FREEZE, "position-freeze-0p25s-below"
    )
    report = _detect(result.derivative_bytes)
    assert all(
        TrajectoryRule.POSITION_FREEZE not in event.triggered_rules
        for event in report.events
    )


def test_freeze_event_records_duration_gate_measurement() -> None:
    _clean, result = _fault(
        FaultOperatorId.POSITION_FREEZE, "position-freeze-1s-detectable"
    )
    report = _detect(result.derivative_bytes)
    event = next(
        event
        for event in report.events
        if TrajectoryRule.POSITION_FREEZE in event.triggered_rules
    )
    duration = next(
        measurement
        for measurement in event.measurements
        if measurement.threshold_key == "trajectory.freeze_minimum_duration_ns"
    )
    assert duration.value >= duration.threshold_value
    assert duration.unit.value == "ns"


def test_whole_support_bias_is_unobservable_without_reference() -> None:
    clean, result = _fault(FaultOperatorId.POSITION_BIAS, "position-bias-3m-detectable")
    unsupported = _detect(result.derivative_bytes)
    bias_support = _support(unsupported, TrajectoryRule.REFERENCE_POSITION_RESIDUAL)
    assert bias_support.observability is Observability.NOT_OBSERVABLE
    assert all(
        TrajectoryRule.REFERENCE_POSITION_RESIDUAL not in event.triggered_rules
        for event in unsupported.events
    )
    supported = _detect(result.derivative_bytes, reference=clean)
    assert any(
        TrajectoryRule.REFERENCE_POSITION_RESIDUAL in event.triggered_rules
        for event in supported.events
    )
    assert all(
        cause.confirmed is False
        for event in supported.events
        for cause in event.compatible_causes
    )
    clean_parsed = parse_synthetic_trajectory_bytes(clean)
    faulted_parsed = parse_synthetic_trajectory_bytes(result.derivative_bytes)
    assert [item.source_velocity_world_mps for item in faulted_parsed.samples] == [
        item.source_velocity_world_mps for item in clean_parsed.samples
    ]


def test_velocity_dependent_rules_fail_closed_without_source_velocity() -> None:
    clean = serialize_fixture(
        generate_fixture("missing-velocity-control", SyntheticScenario.STRAIGHT, 44009)
    )
    parsed = parse_synthetic_trajectory_bytes(clean)
    samples = tuple(
        ReferenceSample(
            time=sample.time,
            world_from_rig=sample.world_from_rig,
            source_velocity_world_mps=None,
        )
        for sample in parsed.samples
    )
    profile, profile_file_sha256, parameters = _detector_inputs()
    report = detect_trajectory_integrity(
        samples,
        source_sha256=parsed.source_sha256,
        partition=parsed.partition,
        profile=profile,  # type: ignore[arg-type]
        profile_file_sha256=profile_file_sha256,
        trajectory_parameters=parameters,  # type: ignore[arg-type]
    )
    for rule in (
        TrajectoryRule.POSITION_JUMP,
        TrajectoryRule.POSITION_FREEZE,
        TrajectoryRule.VELOCITY_RESIDUAL,
    ):
        assert _support(report, rule).observability is Observability.NOT_OBSERVABLE
    assert (
        _support(report, TrajectoryRule.MAXIMUM_SPEED).observability
        is Observability.OBSERVABLE
    )


def test_reference_evidence_validates_hash_time_frames_and_independence() -> None:
    clean, result = _fault(FaultOperatorId.POSITION_BIAS, "position-bias-3m-detectable")
    clean_parsed = parse_synthetic_trajectory_bytes(clean)
    faulted = parse_synthetic_trajectory_bytes(result.derivative_bytes)
    evidence = make_reference_position_evidence(
        clean_parsed.samples,
        evidence_kind=ReferenceEvidenceKind.DECLARED_INDEPENDENT_REFERENCE,
        reference_source_sha256=clean_parsed.source_sha256,
        provenance_kind=ReferenceProvenanceKind.IMMUTABLE_PREINJECTION_SOURCE,
        provenance="synthetic pre-injection clean trajectory",
        independence_basis=ReferenceIndependenceBasis.PREINJECTION_HASH_DISTINCT,
    )
    tampered = evidence.model_dump(mode="json")
    tampered["samples"][0]["position_m"][0] += 1.0
    with pytest.raises(ValueError, match="hash"):
        ReferencePositionEvidence.model_validate_json(json.dumps(tampered))
    inconsistent = evidence.model_dump(mode="json")
    inconsistent["provenance_kind"] = "PAIRED_COORDINATE_SAME_SOURCE"
    inconsistent["evidence_sha256"] = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in inconsistent.items()
                if key != "evidence_sha256"
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="independence contract"):
        ReferencePositionEvidence.model_validate_json(json.dumps(inconsistent))

    shifted = list(clean_parsed.samples)
    shifted[0] = ReferenceSample(
        time=shifted[0].time.model_copy(
            update={"value_ns": shifted[0].time.value_ns + 1}
        ),
        world_from_rig=shifted[0].world_from_rig,
        source_velocity_world_mps=shifted[0].source_velocity_world_mps,
    )
    misaligned = make_reference_position_evidence(
        shifted,
        evidence_kind=ReferenceEvidenceKind.DECLARED_INDEPENDENT_REFERENCE,
        reference_source_sha256=clean_parsed.source_sha256,
        provenance_kind=ReferenceProvenanceKind.IMMUTABLE_PREINJECTION_SOURCE,
        provenance="synthetic pre-injection clean trajectory",
        independence_basis=ReferenceIndependenceBasis.PREINJECTION_HASH_DISTINCT,
    )
    profile, profile_file_sha256, parameters = _detector_inputs()
    with pytest.raises(ValueError, match="timestamps do not align"):
        detect_trajectory_integrity(
            faulted.samples,
            source_sha256=faulted.source_sha256,
            partition=faulted.partition,
            profile=profile,  # type: ignore[arg-type]
            profile_file_sha256=profile_file_sha256,
            trajectory_parameters=parameters,  # type: ignore[arg-type]
            reference_evidence=misaligned,
        )
    wrong_clock_samples = tuple(
        ReferenceSample(
            time=sample.time.model_copy(update={"clock_id": "unrelated-clock"}),
            world_from_rig=sample.world_from_rig,
            source_velocity_world_mps=sample.source_velocity_world_mps,
        )
        for sample in clean_parsed.samples
    )
    wrong_clock = make_reference_position_evidence(
        wrong_clock_samples,
        evidence_kind=ReferenceEvidenceKind.DECLARED_INDEPENDENT_REFERENCE,
        reference_source_sha256=clean_parsed.source_sha256,
        provenance_kind=ReferenceProvenanceKind.IMMUTABLE_PREINJECTION_SOURCE,
        provenance="synthetic pre-injection clean trajectory",
        independence_basis=ReferenceIndependenceBasis.PREINJECTION_HASH_DISTINCT,
    )
    with pytest.raises(ValueError, match="time domains do not align"):
        detect_trajectory_integrity(
            faulted.samples,
            source_sha256=faulted.source_sha256,
            partition=faulted.partition,
            profile=profile,  # type: ignore[arg-type]
            profile_file_sha256=profile_file_sha256,
            trajectory_parameters=parameters,  # type: ignore[arg-type]
            reference_evidence=wrong_clock,
        )
    wrong_frame_samples = tuple(
        ReferenceSample(
            time=sample.time,
            world_from_rig=RigidTransform(
                target_frame="unrelated_world",
                source_frame=sample.world_from_rig.source_frame,
                translation_m=sample.world_from_rig.translation_m,
                rotation=sample.world_from_rig.rotation,
            ),
            source_velocity_world_mps=sample.source_velocity_world_mps,
        )
        for sample in clean_parsed.samples
    )
    wrong_frames = make_reference_position_evidence(
        wrong_frame_samples,
        evidence_kind=ReferenceEvidenceKind.DECLARED_INDEPENDENT_REFERENCE,
        reference_source_sha256=clean_parsed.source_sha256,
        provenance_kind=ReferenceProvenanceKind.IMMUTABLE_PREINJECTION_SOURCE,
        provenance="synthetic pre-injection clean trajectory",
        independence_basis=ReferenceIndependenceBasis.PREINJECTION_HASH_DISTINCT,
    )
    with pytest.raises(ValueError, match="named frames do not align"):
        detect_trajectory_integrity(
            faulted.samples,
            source_sha256=faulted.source_sha256,
            partition=faulted.partition,
            profile=profile,  # type: ignore[arg-type]
            profile_file_sha256=profile_file_sha256,
            trajectory_parameters=parameters,  # type: ignore[arg-type]
            reference_evidence=wrong_frames,
        )
    self_reference = make_reference_position_evidence(
        faulted.samples,
        evidence_kind=ReferenceEvidenceKind.DECLARED_INDEPENDENT_REFERENCE,
        reference_source_sha256=faulted.source_sha256,
        provenance_kind=ReferenceProvenanceKind.IMMUTABLE_PREINJECTION_SOURCE,
        provenance="invalid self reference",
        independence_basis=ReferenceIndependenceBasis.PREINJECTION_HASH_DISTINCT,
    )
    with pytest.raises(ValueError, match="cannot be self-derived"):
        detect_trajectory_integrity(
            faulted.samples,
            source_sha256=faulted.source_sha256,
            partition=faulted.partition,
            profile=profile,  # type: ignore[arg-type]
            profile_file_sha256=profile_file_sha256,
            trajectory_parameters=parameters,  # type: ignore[arg-type]
            reference_evidence=self_reference,
        )
    paired_reference = make_reference_position_evidence(
        faulted.samples,
        evidence_kind=ReferenceEvidenceKind.PAIRED_COORDINATE_SELF_CONSISTENCY,
        reference_source_sha256=faulted.source_sha256,
        provenance_kind=ReferenceProvenanceKind.PAIRED_COORDINATE_SAME_SOURCE,
        provenance="paired position field from the evaluated source",
        independence_basis=(
            ReferenceIndependenceBasis.NOT_INDEPENDENT_PAIRED_COORDINATE
        ),
    )
    paired_report = detect_trajectory_integrity(
        faulted.samples,
        source_sha256=faulted.source_sha256,
        partition=faulted.partition,
        profile=profile,  # type: ignore[arg-type]
        profile_file_sha256=profile_file_sha256,
        trajectory_parameters=parameters,  # type: ignore[arg-type]
        reference_evidence=paired_reference,
    )
    paired_support = _support(paired_report, TrajectoryRule.REFERENCE_POSITION_RESIDUAL)
    assert "paired" in paired_support.detail.lower()
    assert (
        paired_support.evidence_kind
        is ReferenceEvidenceKind.PAIRED_COORDINATE_SELF_CONSISTENCY
    )


def test_duplicate_timestamp_blocks_content_but_remains_reported() -> None:
    clean = serialize_fixture(
        generate_fixture("duplicate-time-control", SyntheticScenario.STRAIGHT, 44003)
    )
    parsed = parse_synthetic_trajectory_bytes(clean)
    samples = list(parsed.samples)
    selected = samples[9]
    samples[9] = ReferenceSample(
        time=samples[8].time,
        world_from_rig=selected.world_from_rig,
        source_velocity_world_mps=selected.source_velocity_world_mps,
    )
    profile, profile_file_sha256, parameters = _detector_inputs()
    report = detect_trajectory_integrity(
        samples,
        source_sha256=parsed.source_sha256,
        partition=parsed.partition,
        profile=profile,  # type: ignore[arg-type]
        profile_file_sha256=profile_file_sha256,
        trajectory_parameters=parameters,  # type: ignore[arg-type]
    )
    assert report.structural_valid is False
    assert any(
        TrajectoryRule.DUPLICATE_TIMESTAMP in event.triggered_rules
        for event in report.events
    )
    assert (
        _support(report, TrajectoryRule.MAXIMUM_SPEED).observability
        is Observability.NOT_APPLICABLE
    )


def test_detection_is_byte_stable_for_identical_input() -> None:
    _clean, result = _fault(
        FaultOperatorId.POSITION_DRIFT, "position-drift-2m-detectable"
    )
    first = _detect(result.derivative_bytes)
    second = _detect(result.derivative_bytes)
    assert first.model_dump_json() == second.model_dump_json()


def test_distant_opposite_steps_are_not_paired_into_one_event() -> None:
    clean = serialize_fixture(
        generate_fixture("distant-step-control", SyntheticScenario.STRAIGHT, 44004)
    )
    parsed = parse_synthetic_trajectory_bytes(clean)
    samples = list(parsed.samples)
    for index in range(1, len(samples) - 2):
        sample = samples[index]
        x, y, z = sample.world_from_rig.translation_m
        samples[index] = ReferenceSample(
            time=sample.time,
            world_from_rig=RigidTransform(
                target_frame=sample.world_from_rig.target_frame,
                source_frame=sample.world_from_rig.source_frame,
                translation_m=(x + 2.0, y, z),
                rotation=sample.world_from_rig.rotation,
            ),
            source_velocity_world_mps=sample.source_velocity_world_mps,
        )
    profile, profile_file_sha256, parameters = _detector_inputs()
    report = detect_trajectory_integrity(
        samples,
        source_sha256=parsed.source_sha256,
        partition=parsed.partition,
        profile=profile,  # type: ignore[arg-type]
        profile_file_sha256=profile_file_sha256,
        trajectory_parameters=parameters,  # type: ignore[arg-type]
    )
    jump_events = [
        event
        for event in report.events
        if TrajectoryRule.POSITION_JUMP in event.triggered_rules
    ]
    assert len(jump_events) == 2
    assert all(
        event.end_time_ns - event.start_time_ns <= 500_000_000 for event in jump_events
    )


def test_all_detector_boundary_and_failure_controls_pass_directly() -> None:
    parsed = parse_synthetic_trajectory_bytes(
        serialize_fixture(
            generate_fixture(
                "detector-control-family", SyntheticScenario.STRAIGHT, 44010
            )
        )
    )
    profile, profile_file_sha256, parameters = _detector_inputs()
    controls = qualify_trajectory_detector_controls(
        template=parsed.samples[0],
        partition=parsed.partition,
        profile=profile,  # type: ignore[arg-type]
        profile_file_sha256=profile_file_sha256,
        trajectory_parameters=parameters,  # type: ignore[arg-type]
    )
    assert controls
    assert all(controls.values()), controls


@pytest.mark.parametrize(
    ("source_name", "expected_message"),
    [
        ("split_manifest.yaml", "split manifest"),
        ("fault_matrix_v1.yaml", "fault matrix"),
        ("numerical_charter.yaml", "numerical charter"),
        ("m3_1_trajectory_gate.yaml", "trajectory gate"),
    ],
)
def test_qualification_rejects_substituted_authority(
    tmp_path: Path, source_name: str, expected_message: str
) -> None:
    source = REPOSITORY_ROOT / "benchmarks" / source_name
    substituted = tmp_path / source_name
    if source_name == "numerical_charter.yaml":
        payload = json.loads(source.read_text())
        payload["statistics"]["unlisted_slices"] = "SUBSTITUTED"
        unhashed = {
            key: value for key, value in payload.items() if key != "immutable_sha256"
        }
        payload["immutable_sha256"] = hashlib.sha256(
            json.dumps(
                unhashed,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        substituted.write_text(json.dumps(payload))
    else:
        substituted.write_bytes(source.read_bytes() + b" ")
    paths = {
        "split_manifest_path": REPOSITORY_ROOT / "benchmarks/split_manifest.yaml",
        "fault_matrix_path": FAULT_MATRIX_PATH,
        "charter_path": REPOSITORY_ROOT / "benchmarks/numerical_charter.yaml",
        "trajectory_gate_path": TRAJECTORY_GATE_PATH,
    }
    key_by_name = {
        "split_manifest.yaml": "split_manifest_path",
        "fault_matrix_v1.yaml": "fault_matrix_path",
        "numerical_charter.yaml": "charter_path",
        "m3_1_trajectory_gate.yaml": "trajectory_gate_path",
    }
    paths[key_by_name[source_name]] = substituted
    with pytest.raises(ValueError, match=expected_message):
        qualify_trajectory_integrity(profile_path=PROFILE_PATH, **paths)


def test_public_cli_qualifies_exact_development_and_calibration_groups(
    tmp_path: Path,
) -> None:
    output = tmp_path / "trajectory-integrity-qualification.json"
    result = CliRunner().invoke(
        app,
        [
            "qualify-trajectory-integrity",
            "--profile",
            str(PROFILE_PATH),
            "--trajectory-gate",
            str(TRAJECTORY_GATE_PATH),
            "--split-manifest",
            str(REPOSITORY_ROOT / "benchmarks/split_manifest.yaml"),
            "--fault-matrix",
            str(FAULT_MATRIX_PATH),
            "--charter",
            str(REPOSITORY_ROOT / "benchmarks/numerical_charter.yaml"),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text())
    assert report["accepted"] is True
    development, calibration = report["partitions"]
    assert development["source_group_count"] == 8
    assert development["result_claim_status"] == "DESCRIPTIVE_ONLY"
    assert development["confirmatory_gate_passed"] is None
    assert calibration["source_group_count"] == 12
    assert calibration["clean_sensor_hours"] == 3.0
    assert calibration["result_claim_status"] == "CALIBRATION_ONLY"
    assert calibration["confirmatory_gate_passed"] is None
    assert calibration["confirmatory_inference"]["performed"] is False
    assert all(
        stratum["distinct_injected_events"] >= 30
        for operator in calibration["severity_strata"].values()
        for stratum in operator.values()
    )
    assert all(
        stratum["expected_outcome_passed"] == stratum["expected_outcome_total"]
        for operator in calibration["severity_strata"].values()
        for stratum in operator.values()
    )
    assert calibration["stationary_false_freeze_count"] == 0
    assert calibration["detector_controls"]
    assert all(calibration["detector_controls"].values())
    false_critical = calibration["metrics"][
        "content.false_critical_per_clean_sensor_hour"
    ]
    assert 0.0 < false_critical["one_sided_upper_95"] <= 1.0
    assert false_critical["zero_event_bound_method"] == "exact_poisson_exposure_95"
    assert set(report["hashes"]) == {
        "charter_file_sha256",
        "charter_immutable_sha256",
        "fault_matrix_sha256",
        "profile_file_sha256",
        "profile_immutable_sha256",
        "split_manifest_sha256",
        "trajectory_gate_file_sha256",
    }
