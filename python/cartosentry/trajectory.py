"""Continuous local-world reference trajectories and analytic qualification."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry.adapters.base import GeographicObservation, TrajectorySample
from cartosentry.contracts import (
    ContractModel,
    RigidTransform,
    TimePoint,
    UnitQuaternion,
)
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.synthetic import DURATION_NS, generate_fixture
from cartosentry.synthetic_models import (
    SyntheticFixture,
    SyntheticScenario,
    SyntheticTransform,
)

Vector3 = tuple[float, float, float]
M3_1_GATE_SHA256 = "7f8a68faddc0afd2f59f59968b9af7e04cfbfbb012759564c6a61a37416e9370"
MAXIMUM_TRAJECTORY_GATE_BYTES = 64 * 1024
_GATE_KEYS = frozenset(
    {
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
    }
)
_SCENARIOS = (
    "straight",
    "constant_radius_turn",
    "stop_start",
    "stationary",
    "timestamp_gap",
)


class TrajectoryGateParameters(BaseModel):
    """Predeclared M3.1 interpolation and derivative parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    maximum_support_gap_ns: Annotated[int, Field(gt=0)]
    derivative_half_window_samples: Annotated[int, Field(ge=2)]
    derivative_polynomial_degree: Literal[3]
    least_squares_rcond: Annotated[float, Field(gt=0.0)]
    huber_delta: Annotated[float, Field(gt=0.0)]
    robust_maximum_iterations: Annotated[int, Field(gt=0)]
    robust_convergence_tolerance: Annotated[float, Field(gt=0.0)]
    stationary_speed_threshold_mps: Annotated[float, Field(gt=0.0)]
    stationary_minimum_duration_ns: Annotated[int, Field(gt=0)]
    analytic_transition_exclusion_ns: Annotated[int, Field(ge=0)]


class TrajectoryNumericalGate(BaseModel):
    """One complete M0.6-compatible numerical acceptance definition."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operator: Literal["max_le", "fraction_ge"]
    value: Annotated[float, Field(ge=0.0)]
    unit: Annotated[str, Field(min_length=1)]
    decision_bound: Literal["deterministic_exhaustive"]
    responsible_metric: Annotated[str, Field(min_length=1)]
    rationale: Annotated[str, Field(min_length=1)]


class TrajectoryGate(BaseModel):
    """Strict, self-hashed acceptance contract for the M3.1 correction."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    gate_version: Literal["m3.1-trajectory-v1"]
    freeze_state: Literal["FROZEN_BEFORE_M3_1_IMPLEMENTATION"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    parameters: TrajectoryGateParameters
    required_scenarios: list[str]
    gates: dict[str, TrajectoryNumericalGate]

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if tuple(self.required_scenarios) != _SCENARIOS:
            raise ValueError(
                "M3.1 scenarios must retain their frozen order and identity"
            )
        if set(self.gates) != _GATE_KEYS:
            raise ValueError("M3.1 gate keys do not match the frozen contract")
        if any(not math.isfinite(gate.value) for gate in self.gates.values()):
            raise ValueError("M3.1 gates must be finite nonnegative values")
        for key, gate in self.gates.items():
            expected_operator = (
                "fraction_ge"
                if key.startswith("stationary.required_")
                or key == "support.required_unsupported_fraction"
                else "max_le"
            )
            if gate.operator != expected_operator:
                raise ValueError(f"M3.1 gate {key} has the wrong operator")
        return self


@dataclass(frozen=True)
class ReferenceSample:
    """One source pose before the source local-world frame is re-anchored."""

    time: TimePoint
    world_from_rig: RigidTransform
    source_velocity_world_mps: Vector3 | None = None
    geographic: GeographicObservation | None = None


class ReferenceTrajectoryKind(StrEnum):
    POSTPROCESSED = "postprocessed_reference_trajectory"
    ANALYTIC = "analytic_reference_trajectory"


class TrajectorySupport(StrEnum):
    SUPPORTED = "SUPPORTED"
    OUTSIDE_SOURCE_SUPPORT = "OUTSIDE_SOURCE_SUPPORT"
    UNSUPPORTED_GAP = "UNSUPPORTED_GAP"
    INSUFFICIENT_DERIVATIVE_SUPPORT = "INSUFFICIENT_DERIVATIVE_SUPPORT"


class LocalWorldAnchor(ContractModel):
    """Mapping from the source local world into the trajectory-local world."""

    source_world_frame: str
    local_world_frame: Literal["trajectory_local_world"] = "trajectory_local_world"
    source_origin_translation_m: Vector3
    global_observation: GeographicObservation | None


class TrajectoryDerivatives(ContractModel):
    velocity_world_mps: Vector3
    acceleration_world_mps2: Vector3
    jerk_world_mps3: Vector3
    heading_rad: float
    yaw_rate_radps: float
    curvature_per_m: float
    stationary: bool


class TrajectoryEvaluation(ContractModel):
    query_time_ns: int
    support: TrajectorySupport
    pose: RigidTransform | None
    derivatives: TrajectoryDerivatives | None

    @model_validator(mode="after")
    def validate_support_payload(self) -> Self:
        populated = self.pose is not None and self.derivatives is not None
        if (self.support is TrajectorySupport.SUPPORTED) != populated:
            raise ValueError("only supported evaluations may contain trajectory values")
        return self


@dataclass(frozen=True)
class _Segment:
    start: int
    end: int

    @property
    def sample_count(self) -> int:
        return self.end - self.start + 1


def _yaw(rotation: UnitQuaternion) -> float:
    w, x, y, z = rotation.as_wxyz()
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def _angular_error(left: float, right: float) -> float:
    return abs(math.atan2(math.sin(left - right), math.cos(left - right)))


def _vector_error(left: Sequence[float], right: Sequence[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


def _rotation_error(left: UnitQuaternion, right: UnitQuaternion) -> float:
    lw, lx, ly, lz = left.as_wxyz()
    rw, rx, ry, rz = right.as_wxyz()
    scalar = lw * rw + lx * rx + ly * ry + lz * rz
    vector = (
        lw * rx - lx * rw - ly * rz + lz * ry,
        lw * ry + lx * rz - ly * rw - lz * rx,
        lw * rz - lx * ry + ly * rx - lz * rw,
    )
    vector_norm = math.sqrt(sum(value * value for value in vector))
    return 2.0 * math.atan2(vector_norm, abs(scalar))


def _independent_slerp(
    start: UnitQuaternion, end: UnitQuaternion, fraction: float
) -> UnitQuaternion:
    left = np.asarray(start.as_wxyz(), dtype=np.float64)
    right = np.asarray(end.as_wxyz(), dtype=np.float64)
    dot = float(left @ right)
    if dot < 0.0:
        right = -right
        dot = -dot
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.9995:
        interpolated = left + fraction * (right - left)
    else:
        angle = math.acos(dot)
        denominator = math.sin(angle)
        interpolated = (
            math.sin((1.0 - fraction) * angle) / denominator * left
            + math.sin(fraction * angle) / denominator * right
        )
    interpolated /= np.linalg.norm(interpolated)
    return UnitQuaternion(
        w=float(interpolated[0]),
        x=float(interpolated[1]),
        y=float(interpolated[2]),
        z=float(interpolated[3]),
    )


def _rigid_from_synthetic(transform: SyntheticTransform) -> RigidTransform:
    values = transform.row_major_4x4
    return RigidTransform(
        target_frame=transform.target_frame,
        source_frame=transform.source_frame,
        translation_m=transform.translation_m,
        rotation=UnitQuaternion.from_rotation_matrix(
            (
                values[0],
                values[1],
                values[2],
                values[4],
                values[5],
                values[6],
                values[8],
                values[9],
                values[10],
            )
        ),
    )


def reference_samples_from_synthetic(
    fixture: SyntheticFixture,
) -> tuple[ReferenceSample, ...]:
    """Load an analytic fixture into the same contract used by public data."""

    return tuple(
        ReferenceSample(
            time=pose.time,
            world_from_rig=_rigid_from_synthetic(pose.world_from_rig),
        )
        for pose in fixture.trajectory
    )


def reference_samples_from_postprocessed(
    samples: Iterable[TrajectorySample],
) -> tuple[ReferenceSample, ...]:
    """Load Boreas postprocessed poses without implying raw-GNSS reconstruction."""

    return tuple(
        ReferenceSample(
            time=sample.time,
            world_from_rig=sample.world_from_rig,
            source_velocity_world_mps=sample.velocity_enu_mps,
            geographic=sample.geographic,
        )
        for sample in samples
    )


class ContinuousReferenceTrajectory:
    """Gap-aware interpolation and robust local polynomial derivatives."""

    def __init__(
        self,
        samples: Iterable[ReferenceSample],
        *,
        kind: ReferenceTrajectoryKind,
        parameters: TrajectoryGateParameters,
    ) -> None:
        source = tuple(samples)
        minimum_samples = parameters.derivative_polynomial_degree + 1
        if len(source) < minimum_samples:
            raise ValueError(
                f"reference trajectory requires at least {minimum_samples} samples"
            )
        first = source[0]
        epoch = first.time.epoch
        clock_id = first.time.clock_id
        reference = first.time.reference
        target_frame = first.world_from_rig.target_frame
        source_frame = first.world_from_rig.source_frame
        previous_time: int | None = None
        for sample in source:
            if (
                sample.time.epoch is not epoch
                or sample.time.clock_id != clock_id
                or sample.time.reference is not reference
            ):
                raise ValueError(
                    "reference trajectory samples must share one time domain"
                )
            if (
                sample.world_from_rig.target_frame != target_frame
                or sample.world_from_rig.source_frame != source_frame
            ):
                raise ValueError("reference trajectory samples must share named frames")
            if previous_time is not None and sample.time.value_ns <= previous_time:
                raise ValueError(
                    "reference trajectory timestamps must be strictly increasing"
                )
            previous_time = sample.time.value_ns
        origin = first.world_from_rig.translation_m
        self.kind = kind
        self.parameters = parameters
        self.anchor = LocalWorldAnchor(
            source_world_frame=target_frame,
            source_origin_translation_m=origin,
            global_observation=first.geographic,
        )
        self._samples = tuple(
            ReferenceSample(
                time=sample.time,
                world_from_rig=RigidTransform(
                    target_frame="trajectory_local_world",
                    source_frame=source_frame,
                    translation_m=cast(
                        Vector3,
                        tuple(
                            value - offset
                            for value, offset in zip(
                                sample.world_from_rig.translation_m, origin, strict=True
                            )
                        ),
                    ),
                    rotation=sample.world_from_rig.rotation,
                ),
                source_velocity_world_mps=sample.source_velocity_world_mps,
                geographic=sample.geographic,
            )
            for sample in source
        )
        self._times = tuple(sample.time.value_ns for sample in self._samples)
        self._positions = np.asarray(
            [sample.world_from_rig.translation_m for sample in self._samples],
            dtype=np.float64,
        )
        self._segments = self._make_segments()
        raw_headings = np.unwrap(
            np.asarray(
                [_yaw(sample.world_from_rig.rotation) for sample in self._samples]
            )
        )
        self._stationary = self._classify_stationary()
        self._headings = self._hold_stationary_headings(raw_headings)

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    @property
    def support_intervals_ns(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (self._times[item.start], self._times[item.end]) for item in self._segments
        )

    def _make_segments(self) -> tuple[_Segment, ...]:
        starts = [0]
        for index in range(1, len(self._times)):
            if (
                self._times[index] - self._times[index - 1]
                > self.parameters.maximum_support_gap_ns
            ):
                starts.append(index)
        return tuple(
            _Segment(
                start,
                (starts[index + 1] - 1)
                if index + 1 < len(starts)
                else len(self._times) - 1,
            )
            for index, start in enumerate(starts)
        )

    def _classify_stationary(self) -> tuple[bool, ...]:
        speeds = [0.0] * len(self._samples)
        fallback = [False] * len(self._samples)
        for segment in self._segments:
            for index in range(segment.start, segment.end + 1):
                source_velocity = self._samples[index].source_velocity_world_mps
                if source_velocity is not None:
                    speeds[index] = math.sqrt(
                        sum(value * value for value in source_velocity)
                    )
                    continue
                if (
                    segment.sample_count
                    < self.parameters.derivative_polynomial_degree + 1
                ):
                    speeds[index] = math.inf
                    continue
                start, end = self._window(segment, self._times[index])
                coefficients = self._fit(
                    self._times[index], start, end, self._positions
                )
                speeds[index] = float(np.linalg.norm(coefficients[1]))
                fallback[index] = True
        classification_speeds = list(speeds)
        for segment in self._segments:
            for index in range(segment.start, segment.end + 1):
                if not fallback[index]:
                    continue
                start = max(
                    segment.start,
                    index - self.parameters.derivative_half_window_samples,
                )
                end = min(
                    segment.end + 1,
                    index + self.parameters.derivative_half_window_samples + 1,
                )
                classification_speeds[index] = float(np.median(speeds[start:end]))
        candidate = [
            speed <= self.parameters.stationary_speed_threshold_mps
            for speed in classification_speeds
        ]
        result = [False] * len(candidate)
        for segment in self._segments:
            cursor = segment.start
            while cursor <= segment.end:
                if not candidate[cursor]:
                    cursor += 1
                    continue
                run_end = cursor
                while run_end < segment.end and candidate[run_end + 1]:
                    run_end += 1
                if (
                    self._times[run_end] - self._times[cursor]
                    >= self.parameters.stationary_minimum_duration_ns
                ):
                    result[cursor : run_end + 1] = [True] * (run_end - cursor + 1)
                cursor = run_end + 1
        return tuple(result)

    def _hold_stationary_headings(
        self, raw: np.ndarray[Any, np.dtype[np.float64]]
    ) -> np.ndarray[Any, np.dtype[np.float64]]:
        result = raw.astype(np.float64, copy=True)
        for segment in self._segments:
            cursor = segment.start
            while cursor <= segment.end:
                if not self._stationary[cursor]:
                    cursor += 1
                    continue
                run_end = cursor
                while run_end < segment.end and self._stationary[run_end + 1]:
                    run_end += 1
                if cursor > segment.start:
                    held = result[cursor - 1]
                elif run_end < segment.end:
                    held = result[run_end + 1]
                else:
                    held = float(np.median(result[cursor : run_end + 1]))
                result[cursor : run_end + 1] = held
                cursor = run_end + 1
        return result

    def _segment_for(
        self, query_time_ns: int
    ) -> tuple[_Segment | None, TrajectorySupport]:
        if query_time_ns < self._times[0] or query_time_ns > self._times[-1]:
            return None, TrajectorySupport.OUTSIDE_SOURCE_SUPPORT
        for segment in self._segments:
            if self._times[segment.start] <= query_time_ns <= self._times[segment.end]:
                if (
                    segment.sample_count
                    < self.parameters.derivative_polynomial_degree + 1
                ):
                    return None, TrajectorySupport.INSUFFICIENT_DERIVATIVE_SUPPORT
                return segment, TrajectorySupport.SUPPORTED
        return None, TrajectorySupport.UNSUPPORTED_GAP

    def _window(self, segment: _Segment, query_time_ns: int) -> tuple[int, int]:
        width = 2 * self.parameters.derivative_half_window_samples + 1
        insertion = bisect.bisect_left(
            self._times, query_time_ns, segment.start, segment.end + 1
        )
        start = max(
            segment.start, insertion - self.parameters.derivative_half_window_samples
        )
        end = min(segment.end + 1, start + width)
        start = max(segment.start, end - width)
        return start, end

    def _fit(
        self,
        query_time_ns: int,
        start: int,
        end: int,
        values: np.ndarray[Any, np.dtype[np.float64]],
    ) -> np.ndarray[Any, np.dtype[np.float64]]:
        relative_s = np.asarray(
            [
                (self._times[index] - query_time_ns) / 1_000_000_000.0
                for index in range(start, end)
            ],
            dtype=np.float64,
        )
        design = np.column_stack(
            [
                relative_s**degree
                for degree in range(self.parameters.derivative_polynomial_degree + 1)
            ]
        )
        selected = values[start:end]
        if selected.ndim == 1:
            selected = selected[:, np.newaxis]
        weights = np.ones(len(relative_s), dtype=np.float64)
        coefficients: np.ndarray[Any, np.dtype[np.float64]]
        for _ in range(self.parameters.robust_maximum_iterations):
            square_root = np.sqrt(weights)[:, np.newaxis]
            coefficients, _, rank, _ = np.linalg.lstsq(
                design * square_root,
                selected * square_root,
                rcond=self.parameters.least_squares_rcond,
            )
            if rank != design.shape[1]:
                raise ValueError(
                    "trajectory derivative design matrix is rank deficient"
                )
            residual = np.linalg.norm(selected - design @ coefficients, axis=1)
            center = float(np.median(residual))
            scale = 1.4826 * float(np.median(np.abs(residual - center)))
            if scale <= np.finfo(np.float64).eps:
                break
            cutoff = self.parameters.huber_delta * scale
            updated = np.minimum(
                1.0, cutoff / np.maximum(residual, np.finfo(np.float64).eps)
            )
            if (
                float(np.max(np.abs(updated - weights)))
                <= self.parameters.robust_convergence_tolerance
            ):
                weights = updated
                square_root = np.sqrt(weights)[:, np.newaxis]
                coefficients, _, rank, _ = np.linalg.lstsq(
                    design * square_root,
                    selected * square_root,
                    rcond=self.parameters.least_squares_rcond,
                )
                if rank != design.shape[1]:
                    raise ValueError(
                        "trajectory derivative design matrix is rank deficient"
                    )
                break
            weights = updated
        return coefficients

    def evaluate(self, query_time_ns: int) -> TrajectoryEvaluation:
        """Evaluate only inside a sufficiently supported source segment."""

        segment, support = self._segment_for(query_time_ns)
        if segment is None:
            return TrajectoryEvaluation(
                query_time_ns=query_time_ns,
                support=support,
                pose=None,
                derivatives=None,
            )
        insertion = bisect.bisect_left(
            self._times, query_time_ns, segment.start, segment.end + 1
        )
        if insertion <= segment.end and self._times[insertion] == query_time_ns:
            pose = self._samples[insertion].world_from_rig
        else:
            left = insertion - 1
            right = insertion
            fraction = (query_time_ns - self._times[left]) / (
                self._times[right] - self._times[left]
            )
            pose = self._samples[left].world_from_rig.interpolate(
                self._samples[right].world_from_rig, fraction
            )
        start, end = self._window(segment, query_time_ns)
        position_coefficients = self._fit(query_time_ns, start, end, self._positions)
        heading_coefficients = self._fit(query_time_ns, start, end, self._headings)
        velocity = tuple(float(value) for value in position_coefficients[1])
        acceleration = tuple(float(2.0 * value) for value in position_coefficients[2])
        jerk = tuple(float(6.0 * value) for value in position_coefficients[3])
        heading = float(heading_coefficients[0, 0])
        yaw_rate = float(heading_coefficients[1, 0])
        nearest = min(
            range(start, end), key=lambda index: abs(self._times[index] - query_time_ns)
        )
        stationary = self._stationary[nearest]
        speed = math.sqrt(sum(value * value for value in velocity))
        curvature = (
            0.0
            if stationary or speed <= self.parameters.stationary_speed_threshold_mps
            else yaw_rate / speed
        )
        return TrajectoryEvaluation(
            query_time_ns=query_time_ns,
            support=TrajectorySupport.SUPPORTED,
            pose=pose,
            derivatives=TrajectoryDerivatives(
                velocity_world_mps=cast(Vector3, velocity),
                acceleration_world_mps2=cast(Vector3, acceleration),
                jerk_world_mps3=cast(Vector3, jerk),
                heading_rad=heading,
                yaw_rate_radps=yaw_rate,
                curvature_per_m=curvature,
                stationary=stationary,
            ),
        )


def load_trajectory_gate(path: Path) -> TrajectoryGate:
    """Load and authenticate the supplemental deterministic M3.1 gate."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_TRAJECTORY_GATE_BYTES,
            context="M3.1 trajectory gate",
        )
        raw = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_TRAJECTORY_GATE_BYTES,
            context="M3.1 trajectory gate",
        )
    except ManifestBoundaryError as error:
        raise ValueError("M3.1 trajectory gate is unavailable or malformed") from error
    if not isinstance(raw, dict):
        raise ValueError("M3.1 trajectory gate must be a JSON object")
    expected = raw.get("immutable_sha256")
    if expected != M3_1_GATE_SHA256:
        raise ValueError("M3.1 trajectory gate does not match the pinned identity")
    unhashed = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    canonical = json.dumps(
        unhashed, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if expected != hashlib.sha256(canonical).hexdigest():
        raise ValueError("M3.1 trajectory gate immutable hash is invalid")
    return TrajectoryGate.model_validate(raw)


def _truth(scenario: SyntheticScenario, time_s: float) -> dict[str, Any]:
    zero = (0.0, 0.0, 0.0)
    if scenario is SyntheticScenario.CONSTANT_RADIUS_TURN:
        curve_end = 1.0 + math.pi / 2.0
        if time_s < 1.0:
            return {
                "position": (-10.0 + 5.0 * time_s, 0.0, 0.0),
                "velocity": (5.0, 0.0, 0.0),
                "acceleration": zero,
                "jerk": zero,
                "heading": 0.0,
                "yaw_rate": 0.0,
                "curvature": 0.0,
                "stationary": False,
            }
        if time_s < curve_end:
            angle = -math.pi / 2.0 + time_s - 1.0
            return {
                "position": (
                    -5.0 + 5.0 * math.cos(angle),
                    5.0 + 5.0 * math.sin(angle),
                    0.0,
                ),
                "velocity": (-5.0 * math.sin(angle), 5.0 * math.cos(angle), 0.0),
                "acceleration": (-5.0 * math.cos(angle), -5.0 * math.sin(angle), 0.0),
                "jerk": (5.0 * math.sin(angle), -5.0 * math.cos(angle), 0.0),
                "heading": angle + math.pi / 2.0,
                "yaw_rate": 1.0,
                "curvature": 0.2,
                "stationary": False,
            }
        return {
            "position": (0.0, 5.0 + 5.0 * (time_s - curve_end), 0.0),
            "velocity": (0.0, 5.0, 0.0),
            "acceleration": zero,
            "jerk": zero,
            "heading": math.pi / 2.0,
            "yaw_rate": 0.0,
            "curvature": 0.0,
            "stationary": False,
        }
    if scenario is SyntheticScenario.STOP_START:
        if time_s < 1.5:
            return {
                "position": (-10.0 + 4.0 * time_s, 0.0, 0.0),
                "velocity": (4.0, 0.0, 0.0),
                "acceleration": zero,
                "jerk": zero,
                "heading": 0.0,
                "yaw_rate": 0.0,
                "curvature": 0.0,
                "stationary": False,
            }
        if time_s < 2.5:
            return {
                "position": (-4.0, 0.0, 0.0),
                "velocity": zero,
                "acceleration": zero,
                "jerk": zero,
                "heading": 0.0,
                "yaw_rate": 0.0,
                "curvature": 0.0,
                "stationary": True,
            }
        return {
            "position": (-4.0 + 4.0 * (time_s - 2.5), 0.0, 0.0),
            "velocity": (4.0, 0.0, 0.0),
            "acceleration": zero,
            "jerk": zero,
            "heading": 0.0,
            "yaw_rate": 0.0,
            "curvature": 0.0,
            "stationary": False,
        }
    if scenario is SyntheticScenario.STATIONARY:
        return {
            "position": zero,
            "velocity": zero,
            "acceleration": zero,
            "jerk": zero,
            "heading": 0.0,
            "yaw_rate": 0.0,
            "curvature": 0.0,
            "stationary": True,
        }
    return {
        "position": (-10.0 + 5.0 * time_s, 0.0, 0.0),
        "velocity": (5.0, 0.0, 0.0),
        "acceleration": zero,
        "jerk": zero,
        "heading": 0.0,
        "yaw_rate": 0.0,
        "curvature": 0.0,
        "stationary": False,
    }


def _eligible_derivative_time(
    scenario: SyntheticScenario, time_ns: int, exclusion_ns: int
) -> bool:
    if time_ns < exclusion_ns or time_ns > DURATION_NS - exclusion_ns:
        return False
    boundaries: tuple[int, ...] = ()
    if scenario is SyntheticScenario.CONSTANT_RADIUS_TURN:
        boundaries = (1_000_000_000, round((1.0 + math.pi / 2.0) * 1_000_000_000))
    elif scenario is SyntheticScenario.STOP_START:
        boundaries = (1_500_000_000, 2_500_000_000)
    return all(abs(time_ns - boundary) > exclusion_ns for boundary in boundaries)


def _analytic_metrics(
    trajectory: ContinuousReferenceTrajectory,
    scenario: SyntheticScenario,
    fixture: SyntheticFixture,
    exclusion_ns: int,
) -> tuple[dict[str, float], list[bool], list[bool], dict[str, int]]:
    metrics = {
        "interpolation.position_max_error_m": 0.0,
        "interpolation.orientation_max_error_rad": 0.0,
        "derivative.velocity_max_error_mps": 0.0,
        "derivative.acceleration_max_error_mps2": 0.0,
        "derivative.jerk_max_error_mps3": 0.0,
        "derivative.heading_max_error_rad": 0.0,
        "derivative.yaw_rate_max_error_radps": 0.0,
        "derivative.curvature_max_error_per_m": 0.0,
    }
    stationary_checks: list[bool] = []
    moving_checks: list[bool] = []
    support_counts = {
        "interpolation_queries": 0,
        "derivative_queries": 0,
        "interpolation_intervals_covered": 0,
        "derivative_intervals_covered": 0,
    }
    interpolation_intervals: set[int] = set()
    derivative_intervals: set[int] = set()
    support_intervals = trajectory.support_intervals_ns
    source = fixture.trajectory
    origin = cast(Vector3, _truth(scenario, 0.0)["position"])
    for left, right in pairwise(source):
        midpoint_ns = (left.time.value_ns + right.time.value_ns) // 2
        evaluated = trajectory.evaluate(midpoint_ns)
        if evaluated.support is not TrajectorySupport.SUPPORTED:
            continue
        assert evaluated.pose is not None
        support_counts["interpolation_queries"] += 1
        interpolation_intervals.update(
            index
            for index, (start_ns, end_ns) in enumerate(support_intervals)
            if start_ns <= midpoint_ns <= end_ns
        )
        truth = _truth(scenario, midpoint_ns / 1_000_000_000.0)
        elapsed_ns = right.time.value_ns - left.time.value_ns
        fraction = (midpoint_ns - left.time.value_ns) / elapsed_ns
        expected_rotation = _independent_slerp(
            _rigid_from_synthetic(left.world_from_rig).rotation,
            _rigid_from_synthetic(right.world_from_rig).rotation,
            fraction,
        )
        metrics["interpolation.position_max_error_m"] = max(
            metrics["interpolation.position_max_error_m"],
            _vector_error(
                evaluated.pose.translation_m,
                cast(
                    Vector3,
                    tuple(
                        value - offset
                        for value, offset in zip(
                            cast(Vector3, truth["position"]), origin, strict=True
                        )
                    ),
                ),
            ),
        )
        metrics["interpolation.orientation_max_error_rad"] = max(
            metrics["interpolation.orientation_max_error_rad"],
            _rotation_error(evaluated.pose.rotation, expected_rotation),
        )
    for pose in source:
        time_ns = pose.time.value_ns
        if not _eligible_derivative_time(scenario, time_ns, exclusion_ns):
            continue
        evaluated = trajectory.evaluate(time_ns)
        if evaluated.support is not TrajectorySupport.SUPPORTED:
            continue
        assert evaluated.derivatives is not None
        support_counts["derivative_queries"] += 1
        derivative_intervals.update(
            index
            for index, (start_ns, end_ns) in enumerate(support_intervals)
            if start_ns <= time_ns <= end_ns
        )
        derivative = evaluated.derivatives
        truth = _truth(scenario, time_ns / 1_000_000_000.0)
        metrics["derivative.velocity_max_error_mps"] = max(
            metrics["derivative.velocity_max_error_mps"],
            _vector_error(
                derivative.velocity_world_mps, cast(Vector3, truth["velocity"])
            ),
        )
        metrics["derivative.acceleration_max_error_mps2"] = max(
            metrics["derivative.acceleration_max_error_mps2"],
            _vector_error(
                derivative.acceleration_world_mps2, cast(Vector3, truth["acceleration"])
            ),
        )
        metrics["derivative.jerk_max_error_mps3"] = max(
            metrics["derivative.jerk_max_error_mps3"],
            _vector_error(derivative.jerk_world_mps3, cast(Vector3, truth["jerk"])),
        )
        metrics["derivative.heading_max_error_rad"] = max(
            metrics["derivative.heading_max_error_rad"],
            _angular_error(derivative.heading_rad, cast(float, truth["heading"])),
        )
        metrics["derivative.yaw_rate_max_error_radps"] = max(
            metrics["derivative.yaw_rate_max_error_radps"],
            abs(derivative.yaw_rate_radps - cast(float, truth["yaw_rate"])),
        )
        metrics["derivative.curvature_max_error_per_m"] = max(
            metrics["derivative.curvature_max_error_per_m"],
            abs(derivative.curvature_per_m - cast(float, truth["curvature"])),
        )
        if cast(bool, truth["stationary"]):
            stationary_checks.append(derivative.stationary)
        else:
            moving_checks.append(not derivative.stationary)
    support_counts["interpolation_intervals_covered"] = len(interpolation_intervals)
    support_counts["derivative_intervals_covered"] = len(derivative_intervals)
    return metrics, stationary_checks, moving_checks, support_counts


def _with_position_outlier(
    samples: tuple[ReferenceSample, ...], index: int, displacement_m: float
) -> tuple[ReferenceSample, ...]:
    result = list(samples)
    selected = result[index]
    x, y, z = selected.world_from_rig.translation_m
    result[index] = ReferenceSample(
        time=selected.time,
        world_from_rig=RigidTransform(
            target_frame=selected.world_from_rig.target_frame,
            source_frame=selected.world_from_rig.source_frame,
            translation_m=(x, y + displacement_m, z),
            rotation=selected.world_from_rig.rotation,
        ),
    )
    return tuple(result)


def qualify_reference_trajectory(gate_path: Path) -> dict[str, object]:
    """Exercise the public analytic workflow against every frozen M3.1 gate."""

    gate = load_trajectory_gate(gate_path)
    combined = {
        key: 0.0
        for key in _GATE_KEYS
        if key.startswith(("interpolation.", "derivative."))
    }
    stationary_checks: list[bool] = []
    moving_checks: list[bool] = []
    scenario_reports: list[dict[str, object]] = []
    for index, scenario in enumerate(
        (
            SyntheticScenario.STRAIGHT,
            SyntheticScenario.CONSTANT_RADIUS_TURN,
            SyntheticScenario.STOP_START,
            SyntheticScenario.STATIONARY,
        )
    ):
        fixture = generate_fixture(f"trajectory-m3-1-{index}", scenario, 31_000 + index)
        trajectory = ContinuousReferenceTrajectory(
            reference_samples_from_synthetic(fixture),
            kind=ReferenceTrajectoryKind.ANALYTIC,
            parameters=gate.parameters,
        )
        metrics, stationary, moving, support_counts = _analytic_metrics(
            trajectory,
            scenario,
            fixture,
            gate.parameters.analytic_transition_exclusion_ns,
        )
        for key, value in metrics.items():
            combined[key] = max(combined[key], value)
        stationary_checks.extend(stationary)
        moving_checks.extend(moving)
        scenario_reports.append(
            {
                "scenario": scenario.value,
                "sample_count": trajectory.sample_count,
                "support_intervals_ns": trajectory.support_intervals_ns,
                "support_counts": support_counts,
                "metrics": metrics,
            }
        )
    straight = generate_fixture(
        "trajectory-m3-1-gap", SyntheticScenario.STRAIGHT, 31_100
    )
    straight_samples = reference_samples_from_synthetic(straight)
    gapped = tuple(
        sample
        for sample in straight_samples
        if not 1_500_000_000 < sample.time.value_ns < 1_750_000_000
    )
    gap_trajectory = ContinuousReferenceTrajectory(
        gapped,
        kind=ReferenceTrajectoryKind.ANALYTIC,
        parameters=gate.parameters,
    )
    gap_metrics, gap_stationary, gap_moving, gap_support_counts = _analytic_metrics(
        gap_trajectory,
        SyntheticScenario.STRAIGHT,
        straight,
        gate.parameters.analytic_transition_exclusion_ns,
    )
    for key, value in gap_metrics.items():
        combined[key] = max(combined[key], value)
    stationary_checks.extend(gap_stationary)
    moving_checks.extend(gap_moving)
    scenario_reports.append(
        {
            "scenario": "timestamp_gap",
            "sample_count": gap_trajectory.sample_count,
            "support_intervals_ns": gap_trajectory.support_intervals_ns,
            "support_counts": gap_support_counts,
            "metrics": gap_metrics,
        }
    )
    unsupported_probes = (
        (-1, TrajectorySupport.OUTSIDE_SOURCE_SUPPORT),
        (1_515_625_000, TrajectorySupport.UNSUPPORTED_GAP),
        (1_625_000_000, TrajectorySupport.UNSUPPORTED_GAP),
        (1_734_375_000, TrajectorySupport.UNSUPPORTED_GAP),
        (DURATION_NS + 1, TrajectorySupport.OUTSIDE_SOURCE_SUPPORT),
    )
    unsupported = [
        gap_trajectory.evaluate(time_ns).support is expected
        for time_ns, expected in unsupported_probes
    ]
    middle = len(straight_samples) // 2
    outlier_trajectory = ContinuousReferenceTrajectory(
        _with_position_outlier(straight_samples, middle + 1, 5.0),
        kind=ReferenceTrajectoryKind.ANALYTIC,
        parameters=gate.parameters,
    )
    outlier_evaluation = outlier_trajectory.evaluate(
        straight_samples[middle].time.value_ns
    )
    assert outlier_evaluation.derivatives is not None
    combined["derivative.robust_outlier_velocity_max_error_mps"] = _vector_error(
        outlier_evaluation.derivatives.velocity_world_mps, (5.0, 0.0, 0.0)
    )
    stop = generate_fixture(
        "trajectory-m3-1-stationary-outlier", SyntheticScenario.STOP_START, 31_101
    )
    stop_samples = reference_samples_from_synthetic(stop)
    stop_middle = next(
        index
        for index, sample in enumerate(stop_samples)
        if sample.time.value_ns == 2_000_000_000
    )
    stopped_outlier_trajectory = ContinuousReferenceTrajectory(
        _with_position_outlier(stop_samples, stop_middle, 5.0),
        kind=ReferenceTrajectoryKind.ANALYTIC,
        parameters=gate.parameters,
    )
    stationary_outlier_times = (1_750_000_000, 2_000_000_000, 2_250_000_000)
    for time_ns in stationary_outlier_times:
        evaluated = stopped_outlier_trajectory.evaluate(time_ns)
        stationary_checks.append(
            evaluated.derivatives is not None and evaluated.derivatives.stationary
        )
    combined["stationary.required_true_fraction"] = sum(stationary_checks) / len(
        stationary_checks
    )
    combined["stationary.required_moving_fraction"] = sum(moving_checks) / len(
        moving_checks
    )
    combined["support.required_unsupported_fraction"] = sum(unsupported) / len(
        unsupported
    )
    checks = []
    for key in sorted(gate.gates):
        observed = combined[key]
        definition = gate.gates[key]
        passed = (
            observed >= definition.value
            if definition.operator == "fraction_ge"
            else observed <= definition.value
        )
        checks.append(
            {
                "gate": key,
                "observed": round(observed, 12),
                "operator": definition.operator,
                "required": definition.value,
                "unit": definition.unit,
                "decision_bound": definition.decision_bound,
                "responsible_metric": definition.responsible_metric,
                "rationale": definition.rationale,
                "passed": passed,
            }
        )
    expected_scenarios = set(gate.required_scenarios)
    observed_scenarios = {
        cast(str, scenario["scenario"]) for scenario in scenario_reports
    }
    coverage_passed = observed_scenarios == expected_scenarios
    for scenario_report in scenario_reports:
        intervals = cast(
            tuple[tuple[int, int], ...], scenario_report["support_intervals_ns"]
        )
        counts = cast(dict[str, int], scenario_report["support_counts"])
        expected_interpolation_queries = cast(
            int, scenario_report["sample_count"]
        ) - len(intervals)
        scenario_passed = (
            counts["interpolation_queries"] == expected_interpolation_queries
            and counts["derivative_queries"] > 0
            and counts["interpolation_intervals_covered"] == len(intervals)
            and counts["derivative_intervals_covered"] == len(intervals)
        )
        coverage_passed = coverage_passed and scenario_passed
    checks.append(
        {
            "gate": "coverage.required_scenario_support",
            "observed": {
                "scenario_ids": sorted(observed_scenarios),
                "support_counts": {
                    cast(str, item["scenario"]): item["support_counts"]
                    for item in scenario_reports
                },
            },
            "operator": "structural_eq",
            "required": {
                "scenario_ids": list(gate.required_scenarios),
                "every_supported_pair": True,
                "derivative_evidence_in_every_support_interval": True,
            },
            "passed": coverage_passed,
        }
    )
    return {
        "schema_version": "cartosentry.trajectory-qualification.v1",
        "gate_version": gate.gate_version,
        "gate_sha256": gate.immutable_sha256,
        "reference_trajectory_kind": ReferenceTrajectoryKind.ANALYTIC.value,
        "accepted": all(cast(bool, item["passed"]) for item in checks),
        "checks": checks,
        "scenarios": scenario_reports,
        "unsupported_probes": [
            {
                "time_ns": time_ns,
                "expected_support": expected.value,
                "support": gap_trajectory.evaluate(time_ns).support.value,
            }
            for time_ns, expected in unsupported_probes
        ],
        "stationary_outlier_probes": [
            {
                "time_ns": time_ns,
                "stationary": cast(
                    TrajectoryDerivatives,
                    stopped_outlier_trajectory.evaluate(time_ns).derivatives,
                ).stationary,
            }
            for time_ns in stationary_outlier_times
        ],
    }


__all__ = [
    "M3_1_GATE_SHA256",
    "MAXIMUM_TRAJECTORY_GATE_BYTES",
    "ContinuousReferenceTrajectory",
    "LocalWorldAnchor",
    "ReferenceSample",
    "ReferenceTrajectoryKind",
    "TrajectoryDerivatives",
    "TrajectoryEvaluation",
    "TrajectoryGate",
    "TrajectoryGateParameters",
    "TrajectorySupport",
    "load_trajectory_gate",
    "qualify_reference_trajectory",
    "reference_samples_from_postprocessed",
    "reference_samples_from_synthetic",
]
