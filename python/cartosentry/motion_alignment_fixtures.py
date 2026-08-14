"""Analytic moving-LiDAR fixtures with exact static-world alignment truth."""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass

from cartosentry.contracts import (
    RawTime,
    RawTimeEncoding,
    RigidTransform,
    TimeEpoch,
    TimePoint,
    TimeReference,
    TimeRounding,
    UnitQuaternion,
)
from cartosentry.motion_alignment import (
    AlignmentFrameInput,
    AlignmentPointInput,
    PointMotionClass,
)
from cartosentry.trajectory import ReferenceSample

_SAMPLE_PERIOD_NS = 25_000_000
_END_TIME_NS = 3_500_000_000
_FRAME_REFERENCE_TIMES_NS = (
    500_000_000,
    1_000_000_000,
    1_500_000_000,
    2_000_000_000,
    2_500_000_000,
    3_000_000_000,
)
_RELATIVE_TIMES_NS = tuple(range(-200_000_000, 200_000_001, _SAMPLE_PERIOD_NS))
_GENERATOR_VERSION = "analytic-alignment-v2-changing-turn-rate"


@dataclass(frozen=True)
class AnalyticAlignmentFixture:
    family_id: str
    seed: int
    frames: tuple[AlignmentFrameInput, ...]
    reference_samples: tuple[ReferenceSample, ...]
    rig_from_lidar: RigidTransform
    expected_world_points: tuple[tuple[tuple[float, float, float], ...], ...]
    source_sha256: str


def alignment_input_sha256(
    frames: tuple[AlignmentFrameInput, ...],
    reference_samples: tuple[ReferenceSample, ...],
    rig_from_lidar: RigidTransform,
    *,
    provenance: object,
) -> str:
    """Hash the complete generated or faulted alignment input contract."""

    payload = {
        "schema_version": "cartosentry.alignment-input-content.v1",
        "provenance": provenance,
        "frames": [
            {
                "frame_index": frame.frame_index,
                "source_key": frame.source_key,
                "reference_time_ns": frame.reference_time_ns,
                "points": [
                    {
                        "position_lidar_m": point.position_lidar_m,
                        "relative_time_ns": point.relative_time_ns,
                        "source_offset": point.source_offset,
                        "motion_class": point.motion_class,
                        "expected_world_m": point.expected_world_m,
                    }
                    for point in frame.points
                ],
            }
            for frame in frames
        ],
        "reference_samples": [
            {
                "time": sample.time.model_dump(mode="json"),
                "world_from_rig": sample.world_from_rig.model_dump(mode="json"),
                "source_velocity_world_mps": sample.source_velocity_world_mps,
                "geographic": (
                    sample.geographic.model_dump(mode="json")
                    if sample.geographic is not None
                    else None
                ),
            }
            for sample in reference_samples
        ],
        "rig_from_lidar": rig_from_lidar.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _time(value_ns: int, family_id: str) -> TimePoint:
    return TimePoint(
        value_ns=value_ns,
        epoch=TimeEpoch.SENSOR_BOOT,
        clock_id="analytic-alignment-clock",
        reference=TimeReference.SAMPLE,
        raw=RawTime(
            source_key=f"synthetic/{family_id}/trajectory",
            field="time_ns",
            unit="ns",
            epoch=TimeEpoch.SENSOR_BOOT,
            reference=TimeReference.SAMPLE,
            encoding=RawTimeEncoding.SIGNED_INTEGER,
            integer_value=str(value_ns),
            rounding=TimeRounding.EXACT,
            maximum_conversion_error_ns=0.0,
        ),
    )


def _yaw_rotation(yaw: float) -> UnitQuaternion:
    cosine = math.cos(yaw)
    sine = math.sin(yaw)
    return UnitQuaternion.from_rotation_matrix(
        (cosine, -sine, 0.0, sine, cosine, 0.0, 0.0, 0.0, 1.0)
    )


def _world_from_rig(time_ns: int) -> RigidTransform:
    seconds = time_ns / 1_000_000_000.0
    angle = 0.15 * seconds + 0.12 * seconds * seconds
    radius = 5.0
    return RigidTransform(
        target_frame="analytic_world",
        source_frame="rig",
        translation_m=(
            radius * math.cos(angle),
            radius * math.sin(angle),
            0.0,
        ),
        rotation=_yaw_rotation(angle + math.pi / 2.0),
    )


def _velocity(time_ns: int) -> tuple[float, float, float]:
    seconds = time_ns / 1_000_000_000.0
    angle = 0.15 * seconds + 0.12 * seconds * seconds
    angular_rate = 0.15 + 0.24 * seconds
    radius = 5.0
    return (
        -radius * angular_rate * math.sin(angle),
        radius * angular_rate * math.cos(angle),
        0.0,
    )


def _landmarks(seed: int) -> tuple[tuple[float, float, float], ...]:
    generator = random.Random(seed)
    points: list[tuple[float, float, float]] = []
    for time_index, _relative_time in enumerate(_RELATIVE_TIMES_NS):
        base_angle = 2.0 * math.pi * time_index / len(_RELATIVE_TIMES_NS)
        for layer in range(4):
            radius = 11.0 + 1.5 * layer + generator.uniform(-0.2, 0.2)
            angle = base_angle + 0.09 * layer + generator.uniform(-0.01, 0.01)
            points.append(
                (
                    radius * math.cos(angle),
                    radius * math.sin(angle),
                    0.5 + 0.8 * layer + generator.uniform(-0.03, 0.03),
                )
            )
    return tuple(points)


def generate_analytic_alignment_fixture(
    family_id: str, seed: int
) -> AnalyticAlignmentFixture:
    """Generate a turning rig and fixed static targets with exact point times."""

    if not family_id:
        raise ValueError("analytic alignment family identifier is required")
    if seed < 0:
        raise ValueError("analytic alignment seed must be nonnegative")
    rig_from_lidar = RigidTransform(
        target_frame="rig",
        source_frame="lidar",
        translation_m=(0.0, 0.0, 1.8),
        rotation=_yaw_rotation(0.0),
    )
    samples = tuple(
        ReferenceSample(
            time=_time(time_ns, family_id),
            world_from_rig=_world_from_rig(time_ns),
            source_velocity_world_mps=_velocity(time_ns),
        )
        for time_ns in range(0, _END_TIME_NS + 1, _SAMPLE_PERIOD_NS)
    )
    landmarks = _landmarks(seed)
    origin = _world_from_rig(0).translation_m
    frames: list[AlignmentFrameInput] = []
    expected: list[tuple[tuple[float, float, float], ...]] = []
    for frame_index, reference_time_ns in enumerate(_FRAME_REFERENCE_TIMES_NS):
        points: list[AlignmentPointInput] = []
        truth: list[tuple[float, float, float]] = []
        for point_index, landmark in enumerate(landmarks):
            time_index = point_index // 4
            relative_time_ns = _RELATIVE_TIMES_NS[time_index]
            measurement_time_ns = reference_time_ns + relative_time_ns
            lidar_from_world = (
                _world_from_rig(measurement_time_ns).compose(rig_from_lidar).inverse()
            )
            local_landmark = (
                landmark[0] - origin[0],
                landmark[1] - origin[1],
                landmark[2] - origin[2],
            )
            points.append(
                AlignmentPointInput(
                    position_lidar_m=lidar_from_world.apply(landmark),
                    relative_time_ns=relative_time_ns,
                    source_offset=point_index * 24,
                    motion_class=PointMotionClass.STATIC,
                    expected_world_m=local_landmark,
                )
            )
            truth.append(local_landmark)
        frames.append(
            AlignmentFrameInput(
                frame_index=frame_index,
                source_key=f"synthetic/{family_id}/lidar/{frame_index:03d}",
                reference_time_ns=reference_time_ns,
                points=tuple(points),
            )
        )
        expected.append(tuple(truth))
    source_sha256 = alignment_input_sha256(
        tuple(frames),
        samples,
        rig_from_lidar,
        provenance={
            "schema_version": "cartosentry.analytic-alignment-fixture.v2",
            "generator_version": _GENERATOR_VERSION,
            "family_id": family_id,
            "seed": seed,
        },
    )
    return AnalyticAlignmentFixture(
        family_id=family_id,
        seed=seed,
        frames=tuple(frames),
        reference_samples=samples,
        rig_from_lidar=rig_from_lidar,
        expected_world_points=tuple(expected),
        source_sha256=source_sha256,
    )


__all__ = [
    "AnalyticAlignmentFixture",
    "alignment_input_sha256",
    "generate_analytic_alignment_fixture",
]
