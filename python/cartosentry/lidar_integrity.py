"""Bounded streaming lidar structural and coverage analysis."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry.artifacts import Severity
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.synthetic_models import SyntheticFixture

PROFILE_IMMUTABLE_SHA256 = (
    "ab7b89360fdc136752f7c89c301dd4de42f28181cc020e60d2e759afe1ae5aad"
)
MAXIMUM_PROFILE_BYTES = 256 * 1024
DETECTOR_ID: Literal["lidar-integrity-v1"] = "lidar-integrity-v1"
DETECTOR_VERSION: Literal["1.0.0"] = "1.0.0"


class LidarRule(StrEnum):
    FRAME_COUNT = "frame_count"
    FRAME_CADENCE = "frame_cadence"
    NONFINITE = "nonfinite"
    INVALID_RING = "invalid_ring"
    RANGE = "range"
    INTENSITY = "intensity"
    POINT_TIME = "point_time"
    SCAN_DURATION = "scan_duration"
    RING_LOSS = "ring_loss"
    SECTOR_LOSS = "sector_loss"
    DENSITY = "density"


_SEVERITY_PRIORITY = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.CRITICAL: 2,
    Severity.BLOCKING_ANALYSIS: 3,
}


class LidarSensorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ring_ids: tuple[Annotated[int, Field(ge=0, le=4095)], ...]
    minimum_points_per_ring: Annotated[int, Field(gt=0)]
    azimuth_bins: Annotated[int, Field(ge=4, le=4096)]
    maximum_blank_azimuth_bins: Annotated[int, Field(ge=0)]
    minimum_points_per_frame: Annotated[int, Field(gt=0)]
    minimum_range_m: Annotated[float, Field(gt=0.0)]
    maximum_range_m: Annotated[float, Field(gt=0.0)]
    minimum_intensity: Annotated[float, Field(ge=0.0)]
    maximum_intensity: Annotated[float, Field(ge=0.0)]
    minimum_relative_point_time_ns: int
    maximum_relative_point_time_ns: int
    minimum_observed_point_span_ns: Annotated[int, Field(gt=0)]
    expected_scan_duration_ns: Annotated[int, Field(gt=0)]
    scan_duration_tolerance_ns: Annotated[int, Field(ge=0)]
    expected_frame_cadence_ns: Annotated[int, Field(gt=0)]
    frame_cadence_tolerance_ns: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_model(self) -> Self:
        if not self.ring_ids or tuple(sorted(set(self.ring_ids))) != self.ring_ids:
            raise ValueError("lidar model ring identifiers must be sorted and unique")
        if self.maximum_blank_azimuth_bins >= self.azimuth_bins:
            raise ValueError("lidar model blank-sector limit is invalid")
        if self.maximum_range_m <= self.minimum_range_m:
            raise ValueError("lidar model range interval is invalid")
        if self.maximum_intensity < self.minimum_intensity:
            raise ValueError("lidar model intensity interval is invalid")
        if self.maximum_relative_point_time_ns <= self.minimum_relative_point_time_ns:
            raise ValueError("lidar model point-time interval is invalid")
        return self


class LidarThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    minimum_finite_return_ratio: Annotated[float, Field(ge=0.0, le=1.0)]
    maximum_invalid_records: Literal[0]
    minimum_consecutive_coverage_frames: Annotated[int, Field(gt=0)]
    minimum_density_ratio_to_running_maximum: Annotated[float, Field(gt=0.0, lt=1.0)]
    histogram_bins: Annotated[int, Field(ge=32, le=8192)]
    quantiles: tuple[Annotated[float, Field(gt=0.0, lt=1.0)], ...]

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        if self.minimum_finite_return_ratio != 1.0:
            raise ValueError("lidar finite-return ratio must remain exactly one")
        return self


class LidarBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    maximum_frames: Annotated[int, Field(gt=0)]
    maximum_points_per_frame: Annotated[int, Field(gt=0)]
    maximum_events: Annotated[int, Field(gt=0)]
    maximum_raw_failures: Annotated[int, Field(gt=0)]
    maximum_representative_invalid_offsets: Annotated[int, Field(gt=0)]
    maximum_traced_peak_bytes_public_smoke: Annotated[int, Field(gt=0)]


class LidarEventConsolidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    interval_convention: Literal["half_open"]
    adjacent_frame_tolerance: Literal[1]
    rule_priority: tuple[LidarRule, ...]


class LidarIntegrityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    profile_id: Literal["lidar-integrity-v1"]
    profile_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_AFTER_THRESHOLD_CALIBRATION_BEFORE_M4_1_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    models: dict[str, LidarSensorModel]
    thresholds: LidarThresholds
    budgets: LidarBudgets
    event_consolidation: LidarEventConsolidation

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if self.immutable_sha256 != PROFILE_IMMUTABLE_SHA256:
            raise ValueError("lidar profile does not match the pinned identity")
        if set(self.models) != {"synthetic-spinning-v1", "boreas-128-v1"}:
            raise ValueError("lidar profile model set is not exact")
        if self.event_consolidation.rule_priority != tuple(LidarRule):
            raise ValueError("lidar rule priority is incomplete or reordered")
        if tuple(sorted(set(self.thresholds.quantiles))) != self.thresholds.quantiles:
            raise ValueError("lidar quantiles must be sorted and unique")
        return self


@dataclass(frozen=True)
class LidarPointInput:
    position_m: tuple[float, float, float]
    intensity: float
    ring_id: int
    relative_time_ns: float
    source_offset: int


@dataclass(frozen=True)
class LidarFrameInput:
    frame_index: int
    source_key: str
    reference_time_ns: int
    capture_start_ns: int
    capture_end_ns: int
    points: Iterable[LidarPointInput]


class LidarQuantiles(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    probabilities: tuple[float, ...]
    values: tuple[float | None, ...]
    finite_count: Annotated[int, Field(ge=0)]
    below_histogram_count: Annotated[int, Field(ge=0)]
    above_histogram_count: Annotated[int, Field(ge=0)]


class LidarEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: Annotated[str, Field(pattern=r"^lidar-event-sha256-[0-9a-f]{64}$")]
    detector_id: Literal["lidar-integrity-v1"]
    detector_version: Literal["1.0.0"]
    rule: LidarRule
    severity: Severity
    start_frame_index: Annotated[int, Field(ge=0)]
    end_frame_index_exclusive: Annotated[int, Field(gt=0)]
    start_time_ns: int
    end_time_ns: int
    measurements: dict[str, float]
    representative_source_offsets: tuple[Annotated[int, Field(ge=0)], ...]
    compatible_causes: tuple[str, ...]


class LidarStatistics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    frame_count: Annotated[int, Field(ge=0)]
    point_count: Annotated[int, Field(ge=0)]
    finite_point_count: Annotated[int, Field(ge=0)]
    finite_return_ratio: Annotated[float, Field(ge=0.0, le=1.0)]
    invalid_record_count: Annotated[int, Field(ge=0)]
    range_quantiles_m: LidarQuantiles
    intensity_quantiles: LidarQuantiles
    per_ring_counts: dict[int, Annotated[int, Field(ge=0)]]
    per_azimuth_bin_counts: tuple[Annotated[int, Field(ge=0)], ...]
    maximum_blank_sector_degrees: Annotated[float, Field(ge=0.0, le=360.0)]
    minimum_scan_duration_ns: int | None
    maximum_scan_duration_ns: int | None
    minimum_observed_point_span_ns: float | None
    maximum_observed_point_span_ns: float | None
    minimum_frame_cadence_ns: int | None
    maximum_frame_cadence_ns: int | None
    retained_state_upper_bound_bytes: Annotated[int, Field(gt=0)]


class LidarIntegrityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["cartosentry.lidar-integrity-report.v1"]
    detector_id: Literal["lidar-integrity-v1"]
    detector_version: Literal["1.0.0"]
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_group_id: Annotated[str, Field(min_length=1)]
    partition: Literal["development", "threshold_calibration"]
    sensor_model_id: Literal["synthetic-spinning-v1", "boreas-128-v1"]
    profile_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    statistics: LidarStatistics
    events: tuple[LidarEvent, ...]


@dataclass(frozen=True)
class _RawFailure:
    rule: LidarRule
    frame_index: int
    start_time_ns: int
    end_time_ns: int
    measurements: dict[str, float]
    severity: Severity
    source_offsets: tuple[int, ...] = ()


class _FixedHistogram:
    def __init__(self, minimum: float, maximum: float, bins: int) -> None:
        self.minimum = minimum
        self.maximum = maximum
        self.bins = bins
        self.counts = [0] * bins
        self.finite_count = 0
        self.below = 0
        self.above = 0

    def add(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.finite_count += 1
        if value < self.minimum:
            self.below += 1
            index = 0
        elif value > self.maximum:
            self.above += 1
            index = self.bins - 1
        else:
            fraction = (value - self.minimum) / (self.maximum - self.minimum)
            index = min(self.bins - 1, int(fraction * self.bins))
        self.counts[index] += 1

    def quantiles(self, probabilities: tuple[float, ...]) -> LidarQuantiles:
        if self.finite_count == 0:
            values: tuple[float | None, ...] = tuple(None for _ in probabilities)
        else:
            result: list[float] = []
            for probability in probabilities:
                target = max(1, math.ceil(probability * self.finite_count))
                cumulative = 0
                selected = self.bins - 1
                for index, count in enumerate(self.counts):
                    cumulative += count
                    if cumulative >= target:
                        selected = index
                        break
                width = (self.maximum - self.minimum) / self.bins
                result.append(self.minimum + (selected + 0.5) * width)
            values = tuple(result)
        return LidarQuantiles(
            probabilities=probabilities,
            values=values,
            finite_count=self.finite_count,
            below_histogram_count=self.below,
            above_histogram_count=self.above,
        )


def _canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def load_lidar_integrity_profile(
    path: Path,
) -> tuple[LidarIntegrityProfile, str]:
    """Load and self-authenticate the frozen M4.1 detector profile."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="lidar integrity profile",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="lidar integrity profile",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "lidar integrity profile is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("lidar integrity profile must be a JSON object")
    raw = cast(dict[str, object], decoded)
    expected = raw.get("immutable_sha256")
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if expected != _canonical_hash(canonical):
        raise ValueError("lidar integrity profile immutable hash is invalid")
    return (
        LidarIntegrityProfile.model_validate_json(content),
        hashlib.sha256(content).hexdigest(),
    )


def synthetic_lidar_frames(fixture: SyntheticFixture) -> tuple[LidarFrameInput, ...]:
    """Adapt analytic fixture lidar into the streaming detector contract."""

    return tuple(
        LidarFrameInput(
            frame_index=frame_index,
            source_key=f"synthetic/lidar/{frame.frame_id}",
            reference_time_ns=frame.sensor_time.value_ns,
            capture_start_ns=frame.capture_start.value_ns,
            capture_end_ns=frame.capture_end.value_ns,
            points=tuple(
                LidarPointInput(
                    position_m=point.position_lidar_m,
                    intensity=1.0,
                    ring_id=point.ring_id,
                    relative_time_ns=float(point.relative_time_ns),
                    source_offset=point_index,
                )
                for point_index, point in enumerate(frame.points)
            ),
        )
        for frame_index, frame in enumerate(fixture.lidar_scans)
    )


def _maximum_circular_blank_bins(counts: list[int]) -> int:
    if not counts or all(count == 0 for count in counts):
        return len(counts)
    doubled = counts + counts
    longest = 0
    current = 0
    for count in doubled:
        if count == 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return min(longest, len(counts))


class LidarIntegrityAnalyzer:
    """One-pass fixed-state analyzer that never retains point payloads."""

    def __init__(
        self,
        *,
        profile: LidarIntegrityProfile,
        profile_file_sha256: str,
        sensor_model_id: Literal["synthetic-spinning-v1", "boreas-128-v1"],
        source_sha256: str,
        source_group_id: str,
        partition: Literal["development", "threshold_calibration"],
    ) -> None:
        if len(source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in source_sha256
        ):
            raise ValueError("lidar source hash must be a lowercase SHA-256")
        self.profile = profile
        self.profile_file_sha256 = profile_file_sha256
        self.model_id = sensor_model_id
        self.model = profile.models[sensor_model_id]
        self.source_sha256 = source_sha256
        self.source_group_id = source_group_id
        self.partition = partition
        bins = profile.thresholds.histogram_bins
        self.range_histogram = _FixedHistogram(
            self.model.minimum_range_m, self.model.maximum_range_m, bins
        )
        self.intensity_histogram = _FixedHistogram(
            self.model.minimum_intensity, self.model.maximum_intensity, bins
        )
        self.ring_index = {
            ring_id: index for index, ring_id in enumerate(self.model.ring_ids)
        }
        self.per_ring_counts = [0] * len(self.model.ring_ids)
        self.per_azimuth_counts = [0] * self.model.azimuth_bins
        self.raw_failures: list[_RawFailure] = []
        self.frame_count = 0
        self.point_count = 0
        self.finite_point_count = 0
        self.invalid_record_count = 0
        self.maximum_blank_bins = 0
        self.maximum_frame_point_count = 0
        self.minimum_scan_duration_ns: int | None = None
        self.maximum_scan_duration_ns: int | None = None
        self.minimum_observed_point_span_ns: float | None = None
        self.maximum_observed_point_span_ns: float | None = None
        self.minimum_frame_cadence_ns: int | None = None
        self.maximum_frame_cadence_ns: int | None = None
        self.previous_reference_time_ns: int | None = None
        self.previous_frame_index: int | None = None

    def _append(self, failure: _RawFailure) -> None:
        self.raw_failures.append(failure)
        if len(self.raw_failures) > self.profile.budgets.maximum_raw_failures:
            raise ValueError("lidar raw failure count exceeds the frozen budget")

    def process_frame(self, frame: LidarFrameInput) -> None:
        if self.frame_count >= self.profile.budgets.maximum_frames:
            raise ValueError("lidar frame count exceeds the frozen budget")
        if frame.capture_end_ns <= frame.capture_start_ns:
            raise ValueError("lidar frame capture interval must be nonempty")
        if self.previous_frame_index is not None and frame.frame_index <= (
            self.previous_frame_index
        ):
            raise ValueError("lidar frame indices must increase")
        if self.previous_reference_time_ns is not None:
            cadence = frame.reference_time_ns - self.previous_reference_time_ns
            self.minimum_frame_cadence_ns = (
                cadence
                if self.minimum_frame_cadence_ns is None
                else min(self.minimum_frame_cadence_ns, cadence)
            )
            self.maximum_frame_cadence_ns = (
                cadence
                if self.maximum_frame_cadence_ns is None
                else max(self.maximum_frame_cadence_ns, cadence)
            )
            cadence_error = abs(cadence - self.model.expected_frame_cadence_ns)
            if cadence <= 0 or cadence_error > self.model.frame_cadence_tolerance_ns:
                self._append(
                    _RawFailure(
                        rule=LidarRule.FRAME_CADENCE,
                        frame_index=frame.frame_index,
                        start_time_ns=self.previous_reference_time_ns,
                        end_time_ns=max(
                            self.previous_reference_time_ns + 1,
                            frame.reference_time_ns,
                        ),
                        measurements={
                            "cadence_ns": float(cadence),
                            "cadence_error_ns": float(cadence_error),
                        },
                        severity=Severity.CRITICAL,
                    )
                )
        self.previous_reference_time_ns = frame.reference_time_ns
        self.previous_frame_index = frame.frame_index

        capture_duration = frame.capture_end_ns - frame.capture_start_ns
        self.minimum_scan_duration_ns = (
            capture_duration
            if self.minimum_scan_duration_ns is None
            else min(self.minimum_scan_duration_ns, capture_duration)
        )
        self.maximum_scan_duration_ns = (
            capture_duration
            if self.maximum_scan_duration_ns is None
            else max(self.maximum_scan_duration_ns, capture_duration)
        )
        if (
            abs(capture_duration - self.model.expected_scan_duration_ns)
            > self.model.scan_duration_tolerance_ns
        ):
            self._append(
                _RawFailure(
                    rule=LidarRule.SCAN_DURATION,
                    frame_index=frame.frame_index,
                    start_time_ns=frame.capture_start_ns,
                    end_time_ns=frame.capture_end_ns,
                    measurements={"capture_duration_ns": float(capture_duration)},
                    severity=Severity.CRITICAL,
                )
            )

        frame_ring_counts = [0] * len(self.model.ring_ids)
        frame_azimuth_counts = [0] * self.model.azimuth_bins
        invalid_offsets: dict[LidarRule, list[int]] = {
            rule: []
            for rule in (
                LidarRule.NONFINITE,
                LidarRule.INVALID_RING,
                LidarRule.RANGE,
                LidarRule.INTENSITY,
                LidarRule.POINT_TIME,
            )
        }
        frame_points = 0
        frame_nonfinite = 0
        frame_invalid_range = 0
        frame_invalid_intensity = 0
        frame_invalid_ring = 0
        frame_invalid_point_time = 0
        previous_point_time: float | None = None
        minimum_point_time: float | None = None
        maximum_point_time: float | None = None
        for point in frame.points:
            frame_points += 1
            if frame_points > self.profile.budgets.maximum_points_per_frame:
                raise ValueError("lidar points per frame exceed the frozen budget")
            required = (*point.position_m, point.intensity, point.relative_time_ns)
            if not all(math.isfinite(float(value)) for value in required):
                frame_nonfinite += 1
                self.invalid_record_count += 1
                if (
                    len(invalid_offsets[LidarRule.NONFINITE])
                    < self.profile.budgets.maximum_representative_invalid_offsets
                ):
                    invalid_offsets[LidarRule.NONFINITE].append(point.source_offset)
                continue
            self.finite_point_count += 1
            x, y, z = point.position_m
            point_range = math.sqrt(x * x + y * y + z * z)
            self.range_histogram.add(point_range)
            self.intensity_histogram.add(point.intensity)
            invalid_rules: list[LidarRule] = []
            if (
                not self.model.minimum_range_m
                <= point_range
                <= (self.model.maximum_range_m)
            ):
                frame_invalid_range += 1
                invalid_rules.append(LidarRule.RANGE)
            if (
                not self.model.minimum_intensity
                <= point.intensity
                <= (self.model.maximum_intensity)
            ):
                frame_invalid_intensity += 1
                invalid_rules.append(LidarRule.INTENSITY)
            ring_position = self.ring_index.get(point.ring_id)
            if ring_position is None:
                frame_invalid_ring += 1
                invalid_rules.append(LidarRule.INVALID_RING)
            else:
                frame_ring_counts[ring_position] += 1
                self.per_ring_counts[ring_position] += 1
            azimuth = math.atan2(y, x) % (2.0 * math.pi)
            azimuth_index = min(
                self.model.azimuth_bins - 1,
                int(azimuth / (2.0 * math.pi) * self.model.azimuth_bins),
            )
            frame_azimuth_counts[azimuth_index] += 1
            self.per_azimuth_counts[azimuth_index] += 1
            point_time = point.relative_time_ns
            if (
                not self.model.minimum_relative_point_time_ns
                <= point_time
                <= (self.model.maximum_relative_point_time_ns)
            ):
                frame_invalid_point_time += 1
                invalid_rules.append(LidarRule.POINT_TIME)
            if previous_point_time is not None and point_time < previous_point_time:
                frame_invalid_point_time += 1
                if LidarRule.POINT_TIME not in invalid_rules:
                    invalid_rules.append(LidarRule.POINT_TIME)
            previous_point_time = point_time
            minimum_point_time = (
                point_time
                if minimum_point_time is None
                else min(minimum_point_time, point_time)
            )
            maximum_point_time = (
                point_time
                if maximum_point_time is None
                else max(maximum_point_time, point_time)
            )
            if invalid_rules:
                self.invalid_record_count += 1
                for rule in invalid_rules:
                    if (
                        len(invalid_offsets[rule])
                        < self.profile.budgets.maximum_representative_invalid_offsets
                    ):
                        invalid_offsets[rule].append(point.source_offset)

        self.frame_count += 1
        self.point_count += frame_points
        common = {
            "frame_point_count": float(frame_points),
            "frame_index": float(frame.frame_index),
        }
        if frame_nonfinite:
            self._append(
                _RawFailure(
                    rule=LidarRule.NONFINITE,
                    frame_index=frame.frame_index,
                    start_time_ns=frame.capture_start_ns,
                    end_time_ns=frame.capture_end_ns,
                    measurements={**common, "nonfinite_count": float(frame_nonfinite)},
                    severity=Severity.CRITICAL,
                    source_offsets=tuple(invalid_offsets[LidarRule.NONFINITE]),
                )
            )
        for rule, count in (
            (LidarRule.INVALID_RING, frame_invalid_ring),
            (LidarRule.RANGE, frame_invalid_range),
            (LidarRule.INTENSITY, frame_invalid_intensity),
            (LidarRule.POINT_TIME, frame_invalid_point_time),
        ):
            if count:
                self._append(
                    _RawFailure(
                        rule=rule,
                        frame_index=frame.frame_index,
                        start_time_ns=frame.capture_start_ns,
                        end_time_ns=frame.capture_end_ns,
                        measurements={**common, "invalid_count": float(count)},
                        severity=Severity.CRITICAL,
                        source_offsets=tuple(invalid_offsets[rule]),
                    )
                )
        if minimum_point_time is not None and maximum_point_time is not None:
            observed_span = maximum_point_time - minimum_point_time
            self.minimum_observed_point_span_ns = (
                observed_span
                if self.minimum_observed_point_span_ns is None
                else min(self.minimum_observed_point_span_ns, observed_span)
            )
            self.maximum_observed_point_span_ns = (
                observed_span
                if self.maximum_observed_point_span_ns is None
                else max(self.maximum_observed_point_span_ns, observed_span)
            )
            if observed_span < self.model.minimum_observed_point_span_ns:
                self._append(
                    _RawFailure(
                        rule=LidarRule.SCAN_DURATION,
                        frame_index=frame.frame_index,
                        start_time_ns=frame.capture_start_ns,
                        end_time_ns=frame.capture_end_ns,
                        measurements={"observed_point_span_ns": observed_span},
                        severity=Severity.CRITICAL,
                    )
                )
        missing_ring_count = sum(
            observed < minimum
            for observed, minimum in zip(
                frame_ring_counts,
                [self.model.minimum_points_per_ring] * len(frame_ring_counts),
                strict=True,
            )
        )
        if missing_ring_count:
            self._append(
                _RawFailure(
                    rule=LidarRule.RING_LOSS,
                    frame_index=frame.frame_index,
                    start_time_ns=frame.capture_start_ns,
                    end_time_ns=frame.capture_end_ns,
                    measurements={
                        **common,
                        "missing_supported_ring_count": float(missing_ring_count),
                    },
                    severity=Severity.WARNING,
                )
            )
        blank_bins = _maximum_circular_blank_bins(frame_azimuth_counts)
        self.maximum_blank_bins = max(self.maximum_blank_bins, blank_bins)
        if blank_bins > self.model.maximum_blank_azimuth_bins:
            self._append(
                _RawFailure(
                    rule=LidarRule.SECTOR_LOSS,
                    frame_index=frame.frame_index,
                    start_time_ns=frame.capture_start_ns,
                    end_time_ns=frame.capture_end_ns,
                    measurements={
                        **common,
                        "maximum_blank_azimuth_bins": float(blank_bins),
                    },
                    severity=Severity.WARNING,
                )
            )
        density_ratio = (
            frame_points / self.maximum_frame_point_count
            if self.maximum_frame_point_count
            else 1.0
        )
        if frame_points < self.model.minimum_points_per_frame or (
            density_ratio
            < self.profile.thresholds.minimum_density_ratio_to_running_maximum
        ):
            self._append(
                _RawFailure(
                    rule=LidarRule.DENSITY,
                    frame_index=frame.frame_index,
                    start_time_ns=frame.capture_start_ns,
                    end_time_ns=frame.capture_end_ns,
                    measurements={
                        **common,
                        "minimum_points_per_frame": float(
                            self.model.minimum_points_per_frame
                        ),
                        "density_ratio_to_running_maximum": density_ratio,
                    },
                    severity=Severity.WARNING,
                )
            )
        self.maximum_frame_point_count = max(
            self.maximum_frame_point_count, frame_points
        )

    def _events(self) -> tuple[LidarEvent, ...]:
        priority = {
            rule: index
            for index, rule in enumerate(self.profile.event_consolidation.rule_priority)
        }
        grouped: list[list[_RawFailure]] = []
        for failure in sorted(
            self.raw_failures, key=lambda item: (priority[item.rule], item.frame_index)
        ):
            if (
                not grouped
                or grouped[-1][0].rule is not failure.rule
                or failure.frame_index > grouped[-1][-1].frame_index + 1
            ):
                grouped.append([failure])
            else:
                grouped[-1].append(failure)
        coverage_rules = {
            LidarRule.RING_LOSS,
            LidarRule.SECTOR_LOSS,
            LidarRule.DENSITY,
        }
        events: list[LidarEvent] = []
        for group in grouped:
            rule = group[0].rule
            distinct_frames = len({item.frame_index for item in group})
            if (
                rule in coverage_rules
                and distinct_frames
                < self.profile.thresholds.minimum_consecutive_coverage_frames
            ):
                continue
            measurements: dict[str, float] = {}
            for key in sorted({key for item in group for key in item.measurements}):
                measurements[key] = max(
                    item.measurements[key] for item in group if key in item.measurements
                )
            offsets = tuple(
                sorted({offset for item in group for offset in item.source_offsets})[
                    : self.profile.budgets.maximum_representative_invalid_offsets
                ]
            )
            start_frame = min(item.frame_index for item in group)
            end_frame = max(item.frame_index for item in group) + 1
            start_time = min(item.start_time_ns for item in group)
            end_time = max(item.end_time_ns for item in group)
            identity = {
                "detector_id": DETECTOR_ID,
                "detector_version": DETECTOR_VERSION,
                "source_sha256": self.source_sha256,
                "rule": rule.value,
                "start_frame_index": start_frame,
                "end_frame_index_exclusive": end_frame,
            }
            compatible = {
                LidarRule.FRAME_CADENCE: ("scan loss", "timestamp discontinuity"),
                LidarRule.NONFINITE: ("payload corruption", "decode failure"),
                LidarRule.INVALID_RING: ("sensor-model mismatch", "payload corruption"),
                LidarRule.RANGE: ("range scale", "unit mismatch", "invalid return"),
                LidarRule.INTENSITY: ("intensity encoding mismatch", "invalid return"),
                LidarRule.POINT_TIME: ("point-time corruption", "ordering error"),
                LidarRule.SCAN_DURATION: ("point-time clamp", "scan truncation"),
                LidarRule.RING_LOSS: ("ring loss", "sensor-model mismatch"),
                LidarRule.SECTOR_LOSS: ("sector loss", "scene occlusion"),
                LidarRule.DENSITY: ("density reduction", "sparse scene"),
                LidarRule.FRAME_COUNT: ("scan loss",),
            }[rule]
            events.append(
                LidarEvent(
                    event_id=f"lidar-event-sha256-{_canonical_hash(identity)}",
                    detector_id=DETECTOR_ID,
                    detector_version=DETECTOR_VERSION,
                    rule=rule,
                    severity=max(
                        (item.severity for item in group),
                        key=_SEVERITY_PRIORITY.__getitem__,
                    ),
                    start_frame_index=start_frame,
                    end_frame_index_exclusive=end_frame,
                    start_time_ns=start_time,
                    end_time_ns=end_time,
                    measurements=measurements,
                    representative_source_offsets=offsets,
                    compatible_causes=compatible,
                )
            )
        if len(events) > self.profile.budgets.maximum_events:
            raise ValueError("lidar event count exceeds the frozen budget")
        return tuple(
            sorted(
                events, key=lambda item: (item.start_frame_index, priority[item.rule])
            )
        )

    def finalize(self) -> LidarIntegrityReport:
        if self.frame_count == 0:
            raise ValueError("lidar integrity requires at least one frame")
        finite_ratio = (
            self.finite_point_count / self.point_count if self.point_count else 0.0
        )
        retained_state_upper_bound = (
            self.profile.thresholds.histogram_bins * 2 * 8
            + len(self.per_ring_counts) * 8
            + len(self.per_azimuth_counts) * 8
            + self.profile.budgets.maximum_raw_failures * 256
        )
        return LidarIntegrityReport(
            schema_version="cartosentry.lidar-integrity-report.v1",
            detector_id=DETECTOR_ID,
            detector_version=DETECTOR_VERSION,
            source_sha256=self.source_sha256,
            source_group_id=self.source_group_id,
            partition=self.partition,
            sensor_model_id=self.model_id,
            profile_immutable_sha256=self.profile.immutable_sha256,
            profile_file_sha256=self.profile_file_sha256,
            statistics=LidarStatistics(
                frame_count=self.frame_count,
                point_count=self.point_count,
                finite_point_count=self.finite_point_count,
                finite_return_ratio=finite_ratio,
                invalid_record_count=self.invalid_record_count,
                range_quantiles_m=self.range_histogram.quantiles(
                    self.profile.thresholds.quantiles
                ),
                intensity_quantiles=self.intensity_histogram.quantiles(
                    self.profile.thresholds.quantiles
                ),
                per_ring_counts={
                    ring_id: count
                    for ring_id, count in zip(
                        self.model.ring_ids, self.per_ring_counts, strict=True
                    )
                },
                per_azimuth_bin_counts=tuple(self.per_azimuth_counts),
                maximum_blank_sector_degrees=(
                    self.maximum_blank_bins * 360.0 / self.model.azimuth_bins
                ),
                minimum_scan_duration_ns=self.minimum_scan_duration_ns,
                maximum_scan_duration_ns=self.maximum_scan_duration_ns,
                minimum_observed_point_span_ns=self.minimum_observed_point_span_ns,
                maximum_observed_point_span_ns=self.maximum_observed_point_span_ns,
                minimum_frame_cadence_ns=self.minimum_frame_cadence_ns,
                maximum_frame_cadence_ns=self.maximum_frame_cadence_ns,
                retained_state_upper_bound_bytes=retained_state_upper_bound,
            ),
            events=self._events(),
        )


def analyze_lidar_integrity(
    frames: Iterable[LidarFrameInput],
    *,
    profile: LidarIntegrityProfile,
    profile_file_sha256: str,
    sensor_model_id: Literal["synthetic-spinning-v1", "boreas-128-v1"],
    source_sha256: str,
    source_group_id: str,
    partition: Literal["development", "threshold_calibration"],
) -> LidarIntegrityReport:
    """Analyze a sequence through the fixed-state streaming implementation."""

    analyzer = LidarIntegrityAnalyzer(
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        sensor_model_id=sensor_model_id,
        source_sha256=source_sha256,
        source_group_id=source_group_id,
        partition=partition,
    )
    for frame in frames:
        analyzer.process_frame(frame)
    return analyzer.finalize()


__all__ = [
    "DETECTOR_ID",
    "DETECTOR_VERSION",
    "PROFILE_IMMUTABLE_SHA256",
    "LidarFrameInput",
    "LidarIntegrityAnalyzer",
    "LidarIntegrityProfile",
    "LidarIntegrityReport",
    "LidarPointInput",
    "LidarRule",
    "analyze_lidar_integrity",
    "load_lidar_integrity_profile",
    "synthetic_lidar_frames",
]
