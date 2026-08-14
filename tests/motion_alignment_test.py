"""Motion-compensated LiDAR alignment and qualification tests."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
from cartosentry.cli import app
from cartosentry.motion_alignment import (
    PROFILE_IMMUTABLE_SHA256,
    AlignmentState,
    AlignmentSupport,
    LidarMotionAlignmentAnalyzer,
    analyze_motion_compensated_alignment,
    load_lidar_alignment_profile,
)
from cartosentry.motion_alignment_fixtures import (
    alignment_input_sha256,
    generate_analytic_alignment_fixture,
)
from cartosentry.motion_alignment_qualification import (
    GATE_IMMUTABLE_SHA256,
    AlignmentFaultOperator,
    _clean_truth_frame_passed,
    _fault_inputs,
    _gap_samples,
    _masked_frames,
    _qualify_partition,
    _sparse_frames,
    _stationary_samples,
    load_motion_alignment_gate,
)
from cartosentry.trajectory import (
    ContinuousReferenceTrajectory,
    ReferenceTrajectoryKind,
    load_trajectory_gate,
)
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPOSITORY_ROOT / "profiles/lidar_alignment_v1.yaml"
GATE_PATH = REPOSITORY_ROOT / "benchmarks/m4_2_alignment_gate.yaml"
TRAJECTORY_GATE_PATH = REPOSITORY_ROOT / "benchmarks/m3_1_trajectory_gate.yaml"
SPLIT_PATH = REPOSITORY_ROOT / "benchmarks/split_manifest.yaml"


def _fixture():
    return generate_analytic_alignment_fixture("sensor-map-dev-001", 10_000)


def _trajectory(samples):
    return ContinuousReferenceTrajectory(
        samples,
        kind=ReferenceTrajectoryKind.ANALYTIC,
        parameters=load_trajectory_gate(TRAJECTORY_GATE_PATH).parameters,
    )


def _analyze(frames, samples, rig_from_lidar, source_sha256):
    profile, profile_file_sha256 = load_lidar_alignment_profile(PROFILE_PATH)
    return analyze_motion_compensated_alignment(
        frames,
        trajectory=_trajectory(samples),
        rig_from_lidar=rig_from_lidar,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_sha256=source_sha256,
    )


def test_profile_and_gate_are_frozen_self_hashed_and_strict(tmp_path: Path) -> None:
    profile, profile_file_sha256 = load_lidar_alignment_profile(PROFILE_PATH)
    gate, gate_file_sha256 = load_motion_alignment_gate(GATE_PATH)

    assert profile.immutable_sha256 == PROFILE_IMMUTABLE_SHA256
    assert gate.immutable_sha256 == GATE_IMMUTABLE_SHA256
    assert gate.authorities.profile_file_sha256 == profile_file_sha256
    assert len(gate_file_sha256) == 64
    assert profile.model_json_schema()["additionalProperties"] is False

    profile_raw = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    profile_raw["alignment"]["minimum_occupancy_jaccard"] = 0.1
    modified_profile = tmp_path / "profile.json"
    modified_profile.write_text(json.dumps(profile_raw), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable hash"):
        load_lidar_alignment_profile(modified_profile)

    gate_raw = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    gate_raw["cases"][0]["value"] = 1.0
    modified_gate = tmp_path / "gate.json"
    modified_gate.write_text(json.dumps(gate_raw), encoding="utf-8")
    with pytest.raises(ValueError, match="gate hash"):
        load_motion_alignment_gate(modified_gate)


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"value":NaN}',
        b"[" * 65 + b"0" + b"]" * 65,
        b" " * (256 * 1024 + 1),
    ],
)
def test_profile_and_gate_reject_hostile_json(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "hostile.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        load_lidar_alignment_profile(path)
    with pytest.raises(ValueError):
        load_motion_alignment_gate(path)


def test_clean_alignment_evaluates_every_point_time_and_matches_truth() -> None:
    fixture = _fixture()
    report = _analyze(
        fixture.frames,
        fixture.reference_samples,
        fixture.rig_from_lidar,
        fixture.source_sha256,
    )

    assert report.state is AlignmentState.PASS
    assert report.statistics.frame_count == 6
    assert report.statistics.pair_count == 5
    assert report.statistics.retained_voxel_frame_upper_bound == 1
    assert all(item.state is AlignmentState.PASS for item in report.pairs)
    assert all(item.occupancy_jaccard == 1.0 for item in report.pairs)
    assert all(item.distinct_measurement_time_count == 17 for item in report.frames)
    assert all(item.per_point_pose_evaluation_count == 68 for item in report.frames)
    assert max(item.analytic_truth_rmse_m or 0.0 for item in report.frames) < 1e-9


def test_fixture_identity_binds_complete_generated_content() -> None:
    fixture = _fixture()
    repeated = _fixture()
    first_frame = fixture.frames[0]
    first_point = next(iter(first_frame.points))
    mutated_point = replace(
        first_point,
        position_lidar_m=(
            first_point.position_lidar_m[0] + 0.001,
            *first_point.position_lidar_m[1:],
        ),
    )
    mutated_frame = replace(
        first_frame,
        points=(mutated_point, *tuple(first_frame.points)[1:]),
    )
    mutated_frames = (mutated_frame, *fixture.frames[1:])
    provenance = {"test": "complete-content-binding"}

    assert fixture.source_sha256 == repeated.source_sha256
    assert alignment_input_sha256(
        fixture.frames,
        fixture.reference_samples,
        fixture.rig_from_lidar,
        provenance=provenance,
    ) != alignment_input_sha256(
        mutated_frames,
        fixture.reference_samples,
        fixture.rig_from_lidar,
        provenance=provenance,
    )


def test_one_pose_per_scan_regression_has_measurable_truth_error() -> None:
    fixture = _fixture()
    trajectory = _trajectory(fixture.reference_samples)
    squared_error = 0.0
    count = 0
    for frame in fixture.frames:
        evaluated = trajectory.evaluate(frame.reference_time_ns)
        assert evaluated.pose is not None
        world_from_lidar = evaluated.pose.compose(fixture.rig_from_lidar)
        for point in frame.points:
            observed = world_from_lidar.apply(point.position_lidar_m)
            assert point.expected_world_m is not None
            squared_error += sum(
                (left - right) ** 2
                for left, right in zip(observed, point.expected_world_m, strict=True)
            )
            count += 1

    uncompensated_rmse_m = math.sqrt(squared_error / count)
    assert uncompensated_rmse_m > 0.1


def test_frozen_fault_controls_and_detectable_cases_are_separated() -> None:
    fixture = _fixture()
    gate, _ = load_motion_alignment_gate(GATE_PATH)

    for case in gate.cases:
        faulted = _fault_inputs(fixture, case)
        report = _analyze(
            faulted.frames,
            faulted.reference_samples,
            faulted.rig_from_lidar,
            faulted.truth.derived_sha256,
        )
        assert report.state is AlignmentState(case.expected_state)
        assert faulted.truth.source_sha256 == fixture.source_sha256
        assert faulted.truth.derived_sha256 != fixture.source_sha256
        if case.operator is AlignmentFaultOperator.POINT_TIME_SHIFT_NS:
            assert faulted.truth.changed_point_count == 6 * 68
            assert faulted.truth.maximum_point_time_delta_ns == int(case.value)
        elif case.operator is AlignmentFaultOperator.TRAJECTORY_SINUSOID_M:
            assert faulted.truth.changed_reference_sample_count > 0
            assert faulted.truth.maximum_trajectory_translation_delta_m == (
                pytest.approx(case.value, rel=0.01)
            )
        else:
            assert faulted.truth.changed_extrinsic_component_count == 1
            assert faulted.truth.extrinsic_rotation_delta_rad == pytest.approx(
                case.value
            )
        if report.state is AlignmentState.FAIL:
            causes = set(report.pairs[0].compatible_causes)
            assert {
                "trajectory error",
                "point-time error",
                "extrinsic calibration error",
            } <= causes


def test_trajectory_gap_is_unknown_and_never_passes_affected_pairs() -> None:
    fixture = _fixture()
    report = _analyze(
        fixture.frames,
        _gap_samples(fixture.reference_samples),
        fixture.rig_from_lidar,
        hashlib.sha256(b"gap-control").hexdigest(),
    )

    affected = [
        item
        for item in report.pairs
        if item.support is AlignmentSupport.UNKNOWN_TRAJECTORY
    ]
    assert report.state is AlignmentState.UNKNOWN
    assert tuple(
        item.frame_index
        for item in report.frames
        if item.support is AlignmentSupport.UNKNOWN_TRAJECTORY
    ) == (2,)
    assert tuple(
        (item.left_frame_index, item.right_frame_index) for item in affected
    ) == ((1, 2), (2, 3))
    assert all(item.state is AlignmentState.UNKNOWN for item in affected)
    assert tuple(
        (item.left_frame_index, item.right_frame_index)
        for item in report.pairs
        if item.state is AlignmentState.PASS
    ) == ((0, 1), (3, 4), (4, 5))
    assert any(frame.representative_unsupported_offsets for frame in report.frames)


def test_missing_analytic_truth_cannot_satisfy_the_clean_gate() -> None:
    fixture = _fixture()
    without_truth = tuple(
        replace(
            frame,
            points=tuple(
                replace(point, expected_world_m=None) for point in frame.points
            ),
        )
        for frame in fixture.frames
    )
    report = _analyze(
        without_truth,
        fixture.reference_samples,
        fixture.rig_from_lidar,
        hashlib.sha256(b"missing-truth-control").hexdigest(),
    )
    gate, _ = load_motion_alignment_gate(GATE_PATH)

    assert all(item.analytic_truth_point_count == 0 for item in report.frames)
    assert all(item.analytic_truth_coverage_fraction == 0.0 for item in report.frames)
    assert all(item.analytic_truth_rmse_m is None for item in report.frames)
    assert not any(_clean_truth_frame_passed(item, gate) for item in report.frames)


def test_structure_motion_and_masks_drive_observability_deterministically() -> None:
    fixture = _fixture()
    sparse = _analyze(
        _sparse_frames(fixture.frames),
        fixture.reference_samples,
        fixture.rig_from_lidar,
        hashlib.sha256(b"sparse-control").hexdigest(),
    )
    stationary = _analyze(
        fixture.frames,
        _stationary_samples(fixture.reference_samples),
        fixture.rig_from_lidar,
        hashlib.sha256(b"stationary-control").hexdigest(),
    )
    masked = _analyze(
        _masked_frames(fixture.frames),
        fixture.reference_samples,
        fixture.rig_from_lidar,
        hashlib.sha256(b"mask-control").hexdigest(),
    )

    assert sparse.state is AlignmentState.UNKNOWN
    assert stationary.state is AlignmentState.UNKNOWN
    assert all(
        item.support is AlignmentSupport.UNKNOWN_OBSERVABILITY
        for item in (*sparse.pairs, *stationary.pairs)
    )
    assert all(item.excluded_dynamic_point_count == 4 for item in masked.frames)
    assert all(item.excluded_near_ego_point_count == 1 for item in masked.frames)
    assert all(item.per_point_pose_evaluation_count == 64 for item in masked.frames)


def test_frame_order_budgets_and_nonfinite_values_fail_closed() -> None:
    fixture = _fixture()
    profile, profile_file_sha256 = load_lidar_alignment_profile(PROFILE_PATH)
    analyzer = LidarMotionAlignmentAnalyzer(
        trajectory=_trajectory(fixture.reference_samples),
        rig_from_lidar=fixture.rig_from_lidar,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_sha256=fixture.source_sha256,
    )
    analyzer.process_frame(fixture.frames[0])
    with pytest.raises(ValueError, match="indices must increase"):
        analyzer.process_frame(fixture.frames[0])

    invalid_point = replace(
        next(iter(fixture.frames[1].points)),
        position_lidar_m=(float("nan"), 0.0, 0.0),
    )
    invalid_frame = replace(
        fixture.frames[1],
        points=(invalid_point, *tuple(fixture.frames[1].points)[1:]),
    )
    with pytest.raises(ValueError, match="structural validation"):
        analyzer.process_frame(invalid_frame)

    tiny_profile = profile.model_copy(
        update={
            "budgets": profile.budgets.model_copy(
                update={"maximum_points_per_frame": 1}
            )
        }
    )
    with pytest.raises(ValueError, match="points per frame"):
        analyze_motion_compensated_alignment(
            fixture.frames,
            trajectory=_trajectory(fixture.reference_samples),
            rig_from_lidar=fixture.rig_from_lidar,
            profile=tiny_profile,
            profile_file_sha256=profile_file_sha256,
            source_sha256=fixture.source_sha256,
        )


def test_development_partition_passes_every_frozen_alignment_gate() -> None:
    gate, _ = load_motion_alignment_gate(GATE_PATH)
    profile, profile_file_sha256 = load_lidar_alignment_profile(PROFILE_PATH)

    report = _qualify_partition(
        "development",
        gate=gate,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        parameters=load_trajectory_gate(TRAJECTORY_GATE_PATH).parameters,
        split_manifest_path=SPLIT_PATH,
    )

    assert report["gate_passed"] is True
    assert report["source_group_count"] == 8
    assert report["case_count"] == 72
    assert report["case_expected_state_fraction"] == 1.0
    assert report["unsupported_gap_unknown_fraction"] == 1.0
    assert report["unobservable_control_unknown_fraction"] == 1.0
    assert report["mask_expected_outcome_fraction"] == 1.0


def test_public_cli_exposes_complete_alignment_workflow() -> None:
    result = CliRunner().invoke(app, ["qualify-lidar-alignment", "--help"])

    assert result.exit_code == 0
    assert "per-point-time motion-compensated" in result.stdout
    assert "--trajectory-gate" in result.stdout
    assert "--lidar-integrity-gate" in result.stdout
