"""Analytic, property, integration, and CLI tests for reference trajectories."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from itertools import islice
from pathlib import Path

import cartosentry.trajectory as trajectory_module
import pytest
from cartosentry.adapters import BoreasAdapter
from cartosentry.cli import app
from cartosentry.contracts import RigidTransform, UnitQuaternion
from cartosentry.synthetic import generate_fixture
from cartosentry.synthetic_models import SyntheticScenario
from cartosentry.trajectory import (
    MAXIMUM_TRAJECTORY_GATE_BYTES,
    ContinuousReferenceTrajectory,
    ReferenceSample,
    ReferenceTrajectoryKind,
    TrajectorySupport,
    load_trajectory_gate,
    qualify_reference_trajectory,
    reference_samples_from_postprocessed,
    reference_samples_from_synthetic,
)
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = REPOSITORY_ROOT / "benchmarks/m3_1_trajectory_gate.yaml"
PUBLIC_SEQUENCE = REPOSITORY_ROOT / "data/public/boreas-2021-09-02-11-42"


def _straight_samples() -> tuple[ReferenceSample, ...]:
    fixture = generate_fixture("trajectory-test", SyntheticScenario.STRAIGHT, 44)
    return reference_samples_from_synthetic(fixture)


def _trajectory(
    samples: tuple[ReferenceSample, ...],
    *,
    kind: ReferenceTrajectoryKind = ReferenceTrajectoryKind.ANALYTIC,
) -> ContinuousReferenceTrajectory:
    return ContinuousReferenceTrajectory(
        samples,
        kind=kind,
        parameters=load_trajectory_gate(GATE_PATH).parameters,
    )


def test_public_qualification_passes_every_predeclared_gate() -> None:
    report = qualify_reference_trajectory(GATE_PATH)
    assert report["accepted"] is True
    assert {item["gate"] for item in report["checks"]} == {
        "interpolation.position_max_error_m",
        "interpolation.orientation_max_error_rad",
        "derivative.velocity_max_error_mps",
        "derivative.acceleration_max_error_mps2",
        "derivative.jerk_max_error_mps3",
        "derivative.heading_max_error_rad",
        "derivative.yaw_rate_max_error_radps",
        "derivative.curvature_max_error_per_m",
        "derivative.robust_outlier_velocity_max_error_mps",
        "stationary.required_true_fraction",
        "stationary.required_moving_fraction",
        "support.required_unsupported_fraction",
        "coverage.required_scenario_support",
    }
    assert all(item["passed"] for item in report["checks"])
    assert all(
        {"operator", "unit", "decision_bound", "responsible_metric", "rationale"}
        <= set(item)
        for item in report["checks"]
        if item["gate"] != "coverage.required_scenario_support"
    )
    assert all(item["stationary"] for item in report["stationary_outlier_probes"])


def test_public_cli_exercises_complete_analytic_workflow(tmp_path: Path) -> None:
    output = tmp_path / "trajectory-report.json"
    result = CliRunner().invoke(
        app,
        [
            "qualify-reference-trajectory",
            "--gate",
            str(GATE_PATH),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["accepted"] is True
    assert report["reference_trajectory_kind"] == "analytic_reference_trajectory"
    assert {item["scenario"] for item in report["scenarios"]} == {
        "straight",
        "constant_radius_turn",
        "stop_start",
        "stationary",
        "timestamp_gap",
    }
    gap = next(
        item for item in report["scenarios"] if item["scenario"] == "timestamp_gap"
    )
    assert gap["support_counts"]["derivative_queries"] > 0
    assert len(gap["support_intervals_ns"]) == 2


def test_gate_hash_detects_threshold_rewrites(tmp_path: Path) -> None:
    modified = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    modified["gates"]["derivative.velocity_max_error_mps"]["value"] = 100.0
    unhashed = {
        key: value for key, value in modified.items() if key != "immutable_sha256"
    }
    modified["immutable_sha256"] = hashlib.sha256(
        json.dumps(
            unhashed, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()
    path = tmp_path / "modified.json"
    path.write_text(json.dumps(modified), encoding="utf-8")
    with pytest.raises(ValueError, match="pinned identity"):
        load_trajectory_gate(path)


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":1,"schema_version":1}',
        b" " * (MAXIMUM_TRAJECTORY_GATE_BYTES + 1),
    ],
)
def test_gate_boundary_rejects_duplicate_or_oversized_json(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "unsafe.json"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="unavailable or malformed"):
        load_trajectory_gate(path)


def test_local_world_anchor_is_explicit_and_interpolation_is_continuous() -> None:
    trajectory = _trajectory(_straight_samples())
    assert trajectory.anchor.source_world_frame == "synthetic_world"
    assert trajectory.anchor.source_origin_translation_m == (-10.0, 0.0, 0.0)
    assert trajectory.anchor.global_observation is None
    midpoint = trajectory.evaluate(15_625_000)
    assert midpoint.support is TrajectorySupport.SUPPORTED
    assert midpoint.pose is not None
    assert midpoint.pose.target_frame == "trajectory_local_world"
    assert midpoint.pose.translation_m == pytest.approx((0.078125, 0.0, 0.0))


def test_no_extrapolation_or_interpolation_across_long_gap() -> None:
    samples = tuple(
        sample
        for sample in _straight_samples()
        if not 1_500_000_000 < sample.time.value_ns < 1_750_000_000
    )
    trajectory = _trajectory(samples)
    assert trajectory.support_intervals_ns == (
        (0, 1_500_000_000),
        (1_750_000_000, 4_000_000_000),
    )
    assert trajectory.evaluate(-1).support is TrajectorySupport.OUTSIDE_SOURCE_SUPPORT
    assert (
        trajectory.evaluate(1_625_000_000).support is TrajectorySupport.UNSUPPORTED_GAP
    )
    assert (
        trajectory.evaluate(4_000_000_001).support
        is TrajectorySupport.OUTSIDE_SOURCE_SUPPORT
    )
    assert trajectory.evaluate(1_500_000_000).support is TrajectorySupport.SUPPORTED
    assert trajectory.evaluate(1_750_000_000).support is TrajectorySupport.SUPPORTED


def test_stationary_heading_policy_suppresses_orientation_noise() -> None:
    fixture = generate_fixture("stationary-heading", SyntheticScenario.STATIONARY, 45)
    noisy = []
    for index, sample in enumerate(reference_samples_from_synthetic(fixture)):
        yaw = (-1.0 if index % 2 else 1.0) * 0.4
        noisy.append(
            replace(
                sample,
                world_from_rig=RigidTransform(
                    target_frame=sample.world_from_rig.target_frame,
                    source_frame=sample.world_from_rig.source_frame,
                    translation_m=sample.world_from_rig.translation_m,
                    rotation=UnitQuaternion(
                        w=math.cos(yaw / 2.0),
                        x=0.0,
                        y=0.0,
                        z=math.sin(yaw / 2.0),
                    ),
                ),
            )
        )
    evaluated = _trajectory(tuple(noisy)).evaluate(2_000_000_000)
    assert evaluated.derivatives is not None
    assert evaluated.derivatives.stationary is True
    assert evaluated.derivatives.heading_rad == pytest.approx(0.4, abs=1e-12)
    assert evaluated.derivatives.yaw_rate_radps == pytest.approx(0.0, abs=1e-12)


def test_fully_stationary_segment_preserves_observed_heading() -> None:
    fixture = generate_fixture(
        "stationary-heading-one", SyntheticScenario.STATIONARY, 46
    )
    yaw = 1.0
    observed = tuple(
        replace(
            sample,
            world_from_rig=RigidTransform(
                target_frame=sample.world_from_rig.target_frame,
                source_frame=sample.world_from_rig.source_frame,
                translation_m=sample.world_from_rig.translation_m,
                rotation=UnitQuaternion(
                    w=math.cos(yaw / 2.0),
                    x=0.0,
                    y=0.0,
                    z=math.sin(yaw / 2.0),
                ),
            ),
        )
        for sample in reference_samples_from_synthetic(fixture)
    )
    evaluated = _trajectory(observed).evaluate(2_000_000_000)
    assert evaluated.derivatives is not None
    assert evaluated.derivatives.heading_rad == pytest.approx(1.0, abs=1e-12)
    assert evaluated.derivatives.yaw_rate_radps == pytest.approx(0.0, abs=1e-12)


def test_relative_quaternion_error_resolves_nanoradian_differences() -> None:
    identity = UnitQuaternion(w=1.0, x=0.0, y=0.0, z=0.0)
    angle = 5e-9
    perturbed = UnitQuaternion(
        w=math.cos(angle / 2.0),
        x=0.0,
        y=0.0,
        z=math.sin(angle / 2.0),
    )
    assert trajectory_module._rotation_error(identity, perturbed) == pytest.approx(
        angle, rel=1e-12
    )


def test_heading_is_unwrapped_before_yaw_rate_differentiation() -> None:
    rotating = []
    for sample in _straight_samples():
        yaw = 2.8 + sample.time.value_ns / 1_000_000_000.0
        rotating.append(
            replace(
                sample,
                world_from_rig=RigidTransform(
                    target_frame=sample.world_from_rig.target_frame,
                    source_frame=sample.world_from_rig.source_frame,
                    translation_m=sample.world_from_rig.translation_m,
                    rotation=UnitQuaternion(
                        w=math.cos(yaw / 2.0),
                        x=0.0,
                        y=0.0,
                        z=math.sin(yaw / 2.0),
                    ),
                ),
            )
        )
    evaluated = _trajectory(tuple(rotating)).evaluate(2_000_000_000)
    assert evaluated.derivatives is not None
    assert evaluated.derivatives.heading_rad == pytest.approx(4.8, abs=1e-9)
    assert evaluated.derivatives.yaw_rate_radps == pytest.approx(1.0, abs=1e-9)


def test_source_velocity_controls_stationarity_when_positions_are_frozen() -> None:
    frozen_positions = []
    samples = _straight_samples()
    for sample in samples:
        frozen_positions.append(
            replace(
                sample,
                world_from_rig=RigidTransform(
                    target_frame=sample.world_from_rig.target_frame,
                    source_frame=sample.world_from_rig.source_frame,
                    translation_m=samples[0].world_from_rig.translation_m,
                    rotation=sample.world_from_rig.rotation,
                ),
                source_velocity_world_mps=(5.0, 0.0, 0.0),
            )
        )
    evaluated = _trajectory(tuple(frozen_positions)).evaluate(2_000_000_000)
    assert evaluated.derivatives is not None
    assert evaluated.derivatives.stationary is False


def test_loader_rejects_ambiguous_time_or_frame_domains() -> None:
    samples = _straight_samples()
    duplicated = list(samples)
    duplicated[5] = replace(duplicated[5], time=duplicated[4].time)
    with pytest.raises(ValueError, match="strictly increasing"):
        _trajectory(tuple(duplicated))
    mismatched = list(samples)
    selected = mismatched[5]
    mismatched[5] = replace(
        selected,
        world_from_rig=RigidTransform(
            target_frame="another_world",
            source_frame=selected.world_from_rig.source_frame,
            translation_m=selected.world_from_rig.translation_m,
            rotation=selected.world_from_rig.rotation,
        ),
    )
    with pytest.raises(ValueError, match="named frames"):
        _trajectory(tuple(mismatched))


@pytest.mark.skipif(
    not PUBLIC_SEQUENCE.is_dir(),
    reason="verified Boreas public-smoke data unavailable",
)
def test_actual_postprocessed_boreas_loader_and_anchor() -> None:
    adapter = BoreasAdapter(
        PUBLIC_SEQUENCE,
        source_group_id="boreas-glen-shields-family-v1",
    )
    source = reference_samples_from_postprocessed(islice(adapter.pose_samples(), 64))
    trajectory = _trajectory(source, kind=ReferenceTrajectoryKind.POSTPROCESSED)
    assert trajectory.kind is ReferenceTrajectoryKind.POSTPROCESSED
    assert trajectory.anchor.source_world_frame == "enu_ref"
    assert trajectory.anchor.global_observation is not None
    query = (source[31].time.value_ns + source[32].time.value_ns) // 2
    evaluated = trajectory.evaluate(query)
    assert evaluated.support is TrajectorySupport.SUPPORTED
    assert evaluated.pose is not None
    assert evaluated.derivatives is not None
