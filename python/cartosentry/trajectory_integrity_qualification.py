"""Split-bound M3.2 trajectory detector qualification."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Callable, Sequence
from pathlib import Path
from statistics import median
from typing import Any, cast

from cartosentry.artifacts import Observability, Severity
from cartosentry.contracts import RigidTransform, TimePoint, UnitQuaternion
from cartosentry.faults import (
    ExpectedGateOutcome,
    FaultOperatorId,
    FaultRequest,
    FaultSeverity,
    inject_fault,
    load_fault_registry,
)
from cartosentry.manifest_boundaries import (
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.synthetic import (
    generate_fixture,
    sensor_map_family_assignments,
    serialize_fixture,
)
from cartosentry.synthetic_models import SyntheticPartition, SyntheticScenario
from cartosentry.trajectory import (
    ReferenceSample,
    TrajectoryGateParameters,
    load_trajectory_gate,
)
from cartosentry.trajectory_integrity import (
    MAXIMUM_CHARTER_BYTES,
    ReferenceEvidenceKind,
    ReferenceIndependenceBasis,
    ReferenceProvenanceKind,
    RuleSupport,
    TrajectoryIntegrityEvent,
    TrajectoryIntegrityProfile,
    TrajectoryIntegrityReport,
    TrajectoryRule,
    detect_trajectory_integrity,
    load_trajectory_integrity_profile,
    make_reference_position_evidence,
    parse_synthetic_trajectory_bytes,
)

_STRUCTURAL_RULES = frozenset(
    {
        TrajectoryRule.TIMESTAMP_REGRESSION,
        TrajectoryRule.DUPLICATE_TIMESTAMP,
        TrajectoryRule.TIMESTAMP_GAP,
        TrajectoryRule.COORDINATE_CONTINUITY,
    }
)
_CONTENT_RULE_BY_OPERATOR = {
    FaultOperatorId.POSITION_JUMP: TrajectoryRule.POSITION_JUMP,
    FaultOperatorId.POSITION_FREEZE: TrajectoryRule.POSITION_FREEZE,
    FaultOperatorId.POSITION_DRIFT: TrajectoryRule.VELOCITY_RESIDUAL,
}
_QUALIFICATION_OPERATORS = (
    FaultOperatorId.TIMESTAMP_DISCONTINUITY,
    FaultOperatorId.POSITION_JUMP,
    FaultOperatorId.POSITION_FREEZE,
    FaultOperatorId.POSITION_BIAS,
    FaultOperatorId.POSITION_DRIFT,
)


def _authenticated_json(
    path: Path, *, maximum_bytes: int, context: str
) -> tuple[dict[str, Any], bytes]:
    content = read_bounded_regular_bytes(
        path,
        maximum_bytes=maximum_bytes,
        context=context,
    )
    decoded = decode_bounded_json(
        content,
        maximum_bytes=maximum_bytes,
        context=context,
    )
    if not isinstance(decoded, dict):
        raise ValueError(f"{context} must be an object")
    expected = decoded.get("immutable_sha256")
    unhashed = {
        key: value for key, value in decoded.items() if key != "immutable_sha256"
    }
    canonical = json.dumps(
        unhashed, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if expected != hashlib.sha256(canonical).hexdigest():
        raise ValueError(f"{context} immutable hash is invalid")
    return cast(dict[str, Any], decoded), content


def _load_charter(path: Path) -> tuple[dict[str, Any], bytes]:
    charter, content = _authenticated_json(
        path,
        maximum_bytes=MAXIMUM_CHARTER_BYTES,
        context="numerical charter",
    )
    gates = charter.get("gates")
    statistics = charter.get("statistics")
    support = charter.get("confirmatory_support")
    matching = charter.get("event_matching")
    if not all(
        isinstance(item, dict) for item in (gates, statistics, support, matching)
    ):
        raise ValueError("numerical charter is missing M3.2 methodology sections")
    required = set(cast(dict[str, Any], gates))
    if not {
        "structural.event_recall",
        "structural.event_precision",
        "content.supported_fault_recall",
        "content.false_critical_per_clean_sensor_hour",
        "content.event_boundary_median_stride",
    }.issubset(required):
        raise ValueError("numerical charter is missing M3.2 gates")
    return charter, content


def _iou(event: TrajectoryIntegrityEvent, start_ns: int, end_ns: int) -> float:
    intersection = max(
        0,
        min(event.end_time_ns, end_ns) - max(event.start_time_ns, start_ns),
    )
    union = max(event.end_time_ns, end_ns) - min(event.start_time_ns, start_ns)
    return intersection / union


def _matched_event(
    report: TrajectoryIntegrityReport,
    rule: TrajectoryRule,
    start_ns: int,
    end_ns: int,
    minimum_iou: float,
) -> tuple[TrajectoryIntegrityEvent | None, float]:
    candidates = [
        (event, _iou(event, start_ns, end_ns))
        for event in report.events
        if rule in event.triggered_rules
    ]
    if not candidates:
        return None, 0.0
    event, overlap = max(
        candidates,
        key=lambda item: (item[1], -item[0].start_time_ns, item[0].event_id),
    )
    return (event if overlap >= minimum_iou else None), overlap


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("cannot compute a quantile without values")
    ordered = sorted(values)
    location = probability * (len(ordered) - 1)
    lower = int(location)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = location - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _bootstrap(
    clusters: Sequence[dict[str, Any]],
    estimator: Callable[[Sequence[dict[str, Any]]], float],
    *,
    seed: int,
    replicates: int,
    confidence_level: float,
) -> dict[str, float | int | bool]:
    if not clusters:
        raise ValueError("cluster bootstrap requires eligible source groups")
    point = estimator(clusters)
    generator = random.Random(seed)
    estimates: list[float] = []
    degenerate_resamples = 0
    for _ in range(replicates):
        resample = tuple(clusters[generator.randrange(len(clusters))] for _ in clusters)
        try:
            estimates.append(estimator(resample))
        except ValueError:
            degenerate_resamples += 1
    if not estimates:
        raise ValueError("every clustered bootstrap resample was degenerate")
    alpha = 1.0 - confidence_level
    result: dict[str, float | int | bool] = {
        "point_estimate": point,
        "two_sided_lower_95": _quantile(estimates, alpha / 2.0),
        "two_sided_upper_95": _quantile(estimates, 1.0 - alpha / 2.0),
        "one_sided_lower_95": _quantile(estimates, alpha),
        "one_sided_upper_95": _quantile(estimates, confidence_level),
        "degenerate_resample_count": degenerate_resamples,
    }
    result["interval_degenerate"] = (
        result["two_sided_lower_95"] == result["two_sided_upper_95"]
    )
    return result


def _ratio(
    numerator: str, denominator: str
) -> Callable[[Sequence[dict[str, Any]]], float]:
    def estimate(clusters: Sequence[dict[str, Any]]) -> float:
        total_denominator = sum(float(item[denominator]) for item in clusters)
        if total_denominator <= 0.0:
            raise ValueError(f"qualification denominator {denominator} is empty")
        return sum(float(item[numerator]) for item in clusters) / total_denominator

    return estimate


def _boundary_median(clusters: Sequence[dict[str, Any]]) -> float:
    values = [
        float(value)
        for cluster in clusters
        for value in cast(list[float], cluster["boundary_errors_stride"])
    ]
    if not values:
        raise ValueError("boundary qualification has no matched events")
    return float(median(values))


def _stratum_ratio(
    operator_id: str, severity: str
) -> Callable[[Sequence[dict[str, Any]]], float]:
    def estimate(clusters: Sequence[dict[str, Any]]) -> float:
        passed = 0
        total = 0
        for cluster in clusters:
            strata = cast(dict[str, dict[str, dict[str, int]]], cluster["strata"])
            result = strata[operator_id][severity]
            passed += result["passed"]
            total += result["total"]
        if total == 0:
            raise ValueError("severity stratum is empty")
        return passed / total

    return estimate


def _duration_samples(
    template: ReferenceSample,
    *,
    duration_ns: int,
    period_ns: int,
) -> tuple[ReferenceSample, ...]:
    if duration_ns % period_ns != 0:
        raise ValueError("clean qualification duration must divide into exact samples")
    rotation = template.world_from_rig.rotation
    samples: list[ReferenceSample] = []
    for time_ns in range(0, duration_ns + 1, period_ns):
        raw = template.time.raw.model_copy(
            update={
                "field": "trajectory_clean_duration_sample",
                "integer_value": str(time_ns),
            }
        )
        time = TimePoint(
            value_ns=time_ns,
            epoch=template.time.epoch,
            clock_id=template.time.clock_id,
            reference=template.time.reference,
            raw=raw,
        )
        samples.append(
            ReferenceSample(
                time=time,
                world_from_rig=RigidTransform(
                    target_frame=template.world_from_rig.target_frame,
                    source_frame=template.world_from_rig.source_frame,
                    translation_m=(5.0 * time_ns / 1_000_000_000.0, 0.0, 0.0),
                    rotation=rotation,
                ),
                source_velocity_world_mps=(5.0, 0.0, 0.0),
            )
        )
    return tuple(samples)


def _analytic_motion_samples(
    template: ReferenceSample,
    *,
    position: Callable[[float], tuple[float, float, float]],
    velocity: Callable[[float], tuple[float, float, float]],
    yaw_rate_radps: float = 0.0,
    duration_ns: int = 4_000_000_000,
    period_ns: int = 100_000_000,
) -> tuple[ReferenceSample, ...]:
    samples: list[ReferenceSample] = []
    for time_ns in range(0, duration_ns + 1, period_ns):
        seconds = time_ns / 1_000_000_000.0
        raw = template.time.raw.model_copy(
            update={
                "field": "trajectory_detector_control_sample",
                "integer_value": str(time_ns),
            }
        )
        time = TimePoint(
            value_ns=time_ns,
            epoch=template.time.epoch,
            clock_id=template.time.clock_id,
            reference=template.time.reference,
            raw=raw,
        )
        yaw = yaw_rate_radps * seconds
        cosine = math.cos(yaw)
        sine = math.sin(yaw)
        rotation = UnitQuaternion.from_rotation_matrix(
            (cosine, -sine, 0.0, sine, cosine, 0.0, 0.0, 0.0, 1.0)
        )
        samples.append(
            ReferenceSample(
                time=time,
                world_from_rig=RigidTransform(
                    target_frame=template.world_from_rig.target_frame,
                    source_frame=template.world_from_rig.source_frame,
                    translation_m=position(seconds),
                    rotation=rotation,
                ),
                source_velocity_world_mps=velocity(seconds),
            )
        )
    return tuple(samples)


def qualify_trajectory_detector_controls(
    *,
    template: ReferenceSample,
    partition: SyntheticPartition,
    profile: TrajectoryIntegrityProfile,
    profile_file_sha256: str,
    trajectory_parameters: TrajectoryGateParameters,
) -> dict[str, bool]:
    """Exercise every M3.2 rule family at a frozen boundary and failure case."""

    def detect(
        samples: Sequence[ReferenceSample], label: str
    ) -> TrajectoryIntegrityReport:
        return detect_trajectory_integrity(
            samples,
            source_sha256=hashlib.sha256(label.encode("utf-8")).hexdigest(),
            partition=partition,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            trajectory_parameters=trajectory_parameters,
        )

    def has_rule(report: TrajectoryIntegrityReport, rule: TrajectoryRule) -> bool:
        return any(rule in event.triggered_rules for event in report.events)

    controls: dict[str, bool] = {}
    kinematic_cases = (
        (
            TrajectoryRule.MAXIMUM_SPEED,
            profile.thresholds["trajectory.maximum_speed_mps"].value,
            lambda value: (
                lambda seconds: (value * seconds, 0.0, 0.0),
                lambda _seconds: (value, 0.0, 0.0),
                0.0,
            ),
        ),
        (
            TrajectoryRule.MAXIMUM_ACCELERATION,
            profile.thresholds["trajectory.maximum_acceleration_mps2"].value,
            lambda value: (
                lambda seconds: (0.5 * value * seconds**2, 0.0, 0.0),
                lambda seconds: (value * seconds, 0.0, 0.0),
                0.0,
            ),
        ),
        (
            TrajectoryRule.MAXIMUM_JERK,
            profile.thresholds["trajectory.maximum_jerk_mps3"].value,
            lambda value: (
                lambda seconds: (value * seconds**3 / 6.0, 0.0, 0.0),
                lambda seconds: (0.5 * value * seconds**2, 0.0, 0.0),
                0.0,
            ),
        ),
        (
            TrajectoryRule.MAXIMUM_YAW_RATE,
            profile.thresholds["trajectory.maximum_yaw_rate_radps"].value,
            lambda value: (
                lambda seconds: (5.0 * seconds, 0.0, 0.0),
                lambda _seconds: (5.0, 0.0, 0.0),
                value,
            ),
        ),
    )
    for rule, threshold, factory in kinematic_cases:
        boundary_position, boundary_velocity, boundary_yaw = factory(threshold)
        failure_position, failure_velocity, failure_yaw = factory(threshold * 1.2)
        boundary = detect(
            _analytic_motion_samples(
                template,
                position=boundary_position,
                velocity=boundary_velocity,
                yaw_rate_radps=boundary_yaw,
            ),
            f"detector-control:{partition}:{rule.value}:boundary",
        )
        failure = detect(
            _analytic_motion_samples(
                template,
                position=failure_position,
                velocity=failure_velocity,
                yaw_rate_radps=failure_yaw,
            ),
            f"detector-control:{partition}:{rule.value}:failure",
        )
        controls[f"{rule.value}.boundary_passes"] = not has_rule(boundary, rule)
        controls[f"{rule.value}.detectable_fails"] = has_rule(failure, rule)

    clean = _analytic_motion_samples(
        template,
        position=lambda seconds: (5.0 * seconds, 0.0, 0.0),
        velocity=lambda _seconds: (5.0, 0.0, 0.0),
    )
    clean_report = detect(clean, f"detector-control:{partition}:structural-clean")
    coordinate_samples = list(clean)
    coordinate_source = coordinate_samples[10]
    coordinate_samples[10] = ReferenceSample(
        time=coordinate_source.time,
        world_from_rig=RigidTransform(
            target_frame="incompatible_world_frame",
            source_frame=coordinate_source.world_from_rig.source_frame,
            translation_m=coordinate_source.world_from_rig.translation_m,
            rotation=coordinate_source.world_from_rig.rotation,
        ),
        source_velocity_world_mps=coordinate_source.source_velocity_world_mps,
    )
    coordinate_report = detect(
        coordinate_samples, f"detector-control:{partition}:coordinate-failure"
    )
    controls["coordinate_continuity.boundary_passes"] = not has_rule(
        clean_report, TrajectoryRule.COORDINATE_CONTINUITY
    )
    controls["coordinate_continuity.detectable_fails"] = has_rule(
        coordinate_report, TrajectoryRule.COORDINATE_CONTINUITY
    )

    regression_samples = list(clean)
    regression_source = regression_samples[10]
    regressed_ns = regression_samples[9].time.value_ns - 1
    regression_samples[10] = ReferenceSample(
        time=TimePoint(
            value_ns=regressed_ns,
            epoch=regression_source.time.epoch,
            clock_id=regression_source.time.clock_id,
            reference=regression_source.time.reference,
            raw=regression_source.time.raw.model_copy(
                update={"integer_value": str(regressed_ns)}
            ),
        ),
        world_from_rig=regression_source.world_from_rig,
        source_velocity_world_mps=regression_source.source_velocity_world_mps,
    )
    regression_report = detect(
        regression_samples, f"detector-control:{partition}:regression-failure"
    )
    controls["timestamp_regression.boundary_passes"] = not has_rule(
        clean_report, TrajectoryRule.TIMESTAMP_REGRESSION
    )
    controls["timestamp_regression.detectable_fails"] = has_rule(
        regression_report, TrajectoryRule.TIMESTAMP_REGRESSION
    )

    def pulse_samples(second_index: int) -> tuple[ReferenceSample, ...]:
        pulsed = list(clean)
        for index in (5, second_index):
            sample = pulsed[index]
            x, y, z = sample.world_from_rig.translation_m
            pulsed[index] = ReferenceSample(
                time=sample.time,
                world_from_rig=RigidTransform(
                    target_frame=sample.world_from_rig.target_frame,
                    source_frame=sample.world_from_rig.source_frame,
                    translation_m=(x + 0.1, y, z),
                    rotation=sample.world_from_rig.rotation,
                ),
                source_velocity_world_mps=sample.source_velocity_world_mps,
            )
        return tuple(pulsed)

    joined = detect(
        pulse_samples(11), f"detector-control:{partition}:hysteresis-joined"
    )
    split = detect(pulse_samples(16), f"detector-control:{partition}:hysteresis-split")
    joined_count = sum(
        TrajectoryRule.VELOCITY_RESIDUAL in event.triggered_rules
        for event in joined.events
    )
    split_count = sum(
        TrajectoryRule.VELOCITY_RESIDUAL in event.triggered_rules
        for event in split.events
    )
    controls["two_clear_window_hysteresis.joins_near_failures"] = joined_count == 1
    controls["two_clear_window_hysteresis.splits_distant_failures"] = split_count == 2
    return controls


def _rule_support(
    report: TrajectoryIntegrityReport, rule: TrajectoryRule
) -> RuleSupport:
    return next(item for item in report.support if item.rule is rule)


def _partition_qualification(
    partition: SyntheticPartition,
    *,
    split_manifest_path: Path,
    profile: TrajectoryIntegrityProfile,
    profile_file_sha256: str,
    trajectory_gate_path: Path,
    fault_matrix_path: Path,
    charter: dict[str, Any],
) -> dict[str, Any]:
    assignments = sensor_map_family_assignments(split_manifest_path, partition)
    expected_partition = profile.qualification.partitions[partition]
    expected_group_ids = profile.qualification.authorities.expected_source_group_ids[
        partition
    ]
    observed_group_ids = [item[0] for item in assignments]
    if (
        len(assignments) != expected_partition.minimum_source_groups
        or observed_group_ids != expected_group_ids
    ):
        raise ValueError(f"{partition} source groups do not match the frozen profile")
    registry = load_fault_registry(fault_matrix_path)
    parameters = load_trajectory_gate(trajectory_gate_path).parameters
    control_family_id, _control_scenario, control_seed = assignments[0]
    control_template = parse_synthetic_trajectory_bytes(
        serialize_fixture(
            generate_fixture(
                control_family_id,
                SyntheticScenario.STRAIGHT,
                control_seed,
                partition=partition,
            )
        )
    ).samples[0]
    detector_controls = qualify_trajectory_detector_controls(
        template=control_template,
        partition=partition,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        trajectory_parameters=parameters,
    )
    matching = cast(dict[str, Any], charter["event_matching"])
    minimum_iou = float(matching["minimum_iou"])
    clusters: list[dict[str, Any]] = []
    case_coverage: dict[str, dict[str, int]] = {
        operator.value: {severity.value: 0 for severity in FaultSeverity}
        for operator in _QUALIFICATION_OPERATORS
    }
    bias_controls = {
        "no_reference_not_observable": 0,
        "no_reference_total": 0,
        "reference_supported_detected": 0,
        "reference_supported_total": 0,
    }
    stationary_false_freezes = 0
    distinct_events_by_stratum: dict[str, dict[str, set[str]]] = {
        operator.value: {severity.value: set() for severity in FaultSeverity}
        for operator in _QUALIFICATION_OPERATORS
    }
    expected_outcomes = {
        operator.value: {
            severity.value: {"passed": 0, "total": 0} for severity in FaultSeverity
        }
        for operator in _QUALIFICATION_OPERATORS
    }
    cases = [
        registered
        for key, registered in sorted(
            registry.cases.items(), key=lambda item: (item[0][0].value, item[0][1])
        )
        if registered.operator_id in _QUALIFICATION_OPERATORS
    ]
    declared_outcome_by_stratum = {
        (case.operator_id.value, case.severity.value): case.expected_gate_outcome.value
        for case in cases
    }
    if len(declared_outcome_by_stratum) != len(_QUALIFICATION_OPERATORS) * len(
        FaultSeverity
    ):
        raise ValueError(
            "qualification needs exactly one case per operator and severity"
        )
    for family_id, scenario, family_seed in assignments:
        control_fixture = generate_fixture(
            family_id,
            scenario,
            family_seed,
            partition=partition,
        )
        control_bytes = serialize_fixture(control_fixture)
        control_parsed = parse_synthetic_trajectory_bytes(control_bytes)
        control_report = detect_trajectory_integrity(
            control_parsed.samples,
            source_sha256=control_parsed.source_sha256,
            partition=partition,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            trajectory_parameters=parameters,
        )
        if scenario.value == "stationary":
            stationary_false_freezes += sum(
                TrajectoryRule.POSITION_FREEZE in event.triggered_rules
                for event in control_report.events
            )
        clean_fixture = generate_fixture(
            family_id,
            SyntheticScenario.STRAIGHT,
            family_seed,
            partition=partition,
        )
        clean_bytes = serialize_fixture(clean_fixture)
        clean_parsed = parse_synthetic_trajectory_bytes(clean_bytes)
        clean_report = detect_trajectory_integrity(
            clean_parsed.samples,
            source_sha256=clean_parsed.source_sha256,
            partition=partition,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            trajectory_parameters=parameters,
        )
        clean_reference = make_reference_position_evidence(
            clean_parsed.samples,
            evidence_kind=ReferenceEvidenceKind.DECLARED_INDEPENDENT_REFERENCE,
            reference_source_sha256=clean_parsed.source_sha256,
            provenance_kind=ReferenceProvenanceKind.IMMUTABLE_PREINJECTION_SOURCE,
            provenance="frozen synthetic clean-source trajectory",
            independence_basis=ReferenceIndependenceBasis.PREINJECTION_HASH_DISTINCT,
        )
        initial_structural_predictions = sum(
            bool(_STRUCTURAL_RULES.intersection(event.triggered_rules))
            for report in (control_report, clean_report)
            for event in report.events
        )
        cluster: dict[str, Any] = {
            "source_group_id": family_id,
            "structural_truth": 0,
            "structural_detected": 0,
            "structural_predictions": initial_structural_predictions,
            "structural_matches": 0,
            "content_truth": 0,
            "content_detected": 0,
            "clean_critical": 0,
            "clean_hours": 0.0,
            "boundary_errors_stride": [],
            "strata": {
                operator.value: {
                    severity.value: {"passed": 0, "total": 0}
                    for severity in FaultSeverity
                }
                for operator in _QUALIFICATION_OPERATORS
            },
        }
        for case in cases:
            repeats = 3
            seen_case_events: set[str] = set()
            for repetition in range(repeats):
                base_fault_seed = (
                    family_seed * 101
                    + list(FaultOperatorId).index(case.operator_id) * 17
                    + repetition
                )
                for seed_offset in range(128):
                    fault_seed = base_fault_seed + seed_offset
                    injected = inject_fault(
                        clean_bytes,
                        FaultRequest(
                            operator_id=case.operator_id,
                            case_id=case.case_id,
                            seed=fault_seed,
                            clean_source_truth_sha256=clean_parsed.source_sha256,
                            injected_observable=(
                                case.operator_id is not FaultOperatorId.POSITION_BIAS
                            ),
                        ),
                        registry,
                    )
                    injected_truth = injected.manifest.source_interval
                    event_identity = hashlib.sha256(
                        json.dumps(
                            {
                                "derivative_sha256": hashlib.sha256(
                                    injected.derivative_bytes
                                ).hexdigest(),
                                "start_ns": injected_truth.start.value_ns,
                                "end_ns": injected_truth.end.value_ns,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    if event_identity not in seen_case_events:
                        seen_case_events.add(event_identity)
                        break
                else:
                    raise ValueError(
                        "qualification could not generate a distinct injected event"
                    )
                if injected.manifest.inherited_partition != partition:
                    raise ValueError("fault derivative crossed its source partition")
                derivative = parse_synthetic_trajectory_bytes(injected.derivative_bytes)
                if derivative.partition != partition:
                    raise ValueError("fault derivative bytes changed source partition")
                report = detect_trajectory_integrity(
                    derivative.samples,
                    source_sha256=derivative.source_sha256,
                    partition=partition,
                    profile=profile,
                    profile_file_sha256=profile_file_sha256,
                    trajectory_parameters=parameters,
                )
                case_coverage[case.operator_id.value][case.severity.value] += 1
                truth = injected.manifest.source_interval
                stratum_events = distinct_events_by_stratum[case.operator_id.value][
                    case.severity.value
                ]
                if event_identity in stratum_events:
                    raise ValueError(
                        "qualification generated a duplicate injected event"
                    )
                stratum_events.add(event_identity)
                structural_events = [
                    event
                    for event in report.events
                    if _STRUCTURAL_RULES.intersection(event.triggered_rules)
                ]
                cluster["structural_predictions"] = int(
                    cluster["structural_predictions"]
                ) + len(structural_events)
                expected_finding = (
                    case.expected_gate_outcome
                    is ExpectedGateOutcome.EXPECT_FINDING_WHEN_OBSERVABLE
                )
                if case.operator_id is FaultOperatorId.POSITION_BIAS:
                    bias_controls["no_reference_total"] += 1
                    support = _rule_support(
                        report, TrajectoryRule.REFERENCE_POSITION_RESIDUAL
                    )
                    if support.observability is Observability.NOT_OBSERVABLE and all(
                        TrajectoryRule.REFERENCE_POSITION_RESIDUAL
                        not in event.triggered_rules
                        and TrajectoryRule.POSITION_JUMP not in event.triggered_rules
                        for event in report.events
                    ):
                        bias_controls["no_reference_not_observable"] += 1
                    if expected_finding:
                        bias_controls["reference_supported_total"] += 1
                    supported = detect_trajectory_integrity(
                        derivative.samples,
                        source_sha256=derivative.source_sha256,
                        partition=partition,
                        profile=profile,
                        profile_file_sha256=profile_file_sha256,
                        trajectory_parameters=parameters,
                        reference_evidence=clean_reference,
                    )
                    matched, _ = _matched_event(
                        supported,
                        TrajectoryRule.REFERENCE_POSITION_RESIDUAL,
                        truth.start.value_ns,
                        truth.end.value_ns,
                        minimum_iou,
                    )
                    reference_predictions = sum(
                        TrajectoryRule.REFERENCE_POSITION_RESIDUAL
                        in event.triggered_rules
                        for event in supported.events
                    )
                    outcome_passed = (
                        matched is not None
                        if expected_finding
                        else reference_predictions == 0
                    )
                    if expected_finding:
                        bias_controls["reference_supported_detected"] += int(
                            matched is not None
                        )
                else:
                    expected_rule = (
                        TrajectoryRule.TIMESTAMP_GAP
                        if case.operator_id is FaultOperatorId.TIMESTAMP_DISCONTINUITY
                        else _CONTENT_RULE_BY_OPERATOR[case.operator_id]
                    )
                    matched, _overlap = _matched_event(
                        report,
                        expected_rule,
                        truth.start.value_ns,
                        truth.end.value_ns,
                        minimum_iou,
                    )
                    expected_rule_predictions = sum(
                        expected_rule in event.triggered_rules
                        for event in report.events
                    )
                    outcome_passed = (
                        matched is not None
                        if expected_finding
                        else expected_rule_predictions == 0
                    )
                    if expected_finding:
                        if case.operator_id is FaultOperatorId.TIMESTAMP_DISCONTINUITY:
                            cluster["structural_truth"] = (
                                int(cluster["structural_truth"]) + 1
                            )
                            cluster["structural_detected"] = int(
                                cluster["structural_detected"]
                            ) + int(matched is not None)
                            cluster["structural_matches"] = int(
                                cluster["structural_matches"]
                            ) + int(matched is not None)
                        else:
                            cluster["content_truth"] = int(cluster["content_truth"]) + 1
                            cluster["content_detected"] = int(
                                cluster["content_detected"]
                            ) + int(matched is not None)
                    if (
                        matched is not None
                        and case.operator_id
                        is not FaultOperatorId.TIMESTAMP_DISCONTINUITY
                    ):
                        stride = clean_fixture.sample_period_ns
                        errors = cast(list[float], cluster["boundary_errors_stride"])
                        errors.extend(
                            (
                                abs(matched.start_time_ns - truth.start.value_ns)
                                / stride,
                                abs(matched.end_time_ns - truth.end.value_ns) / stride,
                            )
                        )
                stratum = cast(dict[str, dict[str, dict[str, int]]], cluster["strata"])[
                    case.operator_id.value
                ][case.severity.value]
                stratum["total"] += 1
                stratum["passed"] += int(outcome_passed)
                aggregate = expected_outcomes[case.operator_id.value][
                    case.severity.value
                ]
                aggregate["total"] += 1
                aggregate["passed"] += int(outcome_passed)

        duration_samples = _duration_samples(
            clean_parsed.samples[0],
            duration_ns=profile.qualification.clean_duration_per_source_group_ns,
            period_ns=profile.qualification.clean_duration_sample_period_ns,
        )
        duration_identity = json.dumps(
            {
                "family_id": family_id,
                "partition": partition,
                "scenario": profile.qualification.clean_duration_scenario,
                "duration_ns": profile.qualification.clean_duration_per_source_group_ns,
                "period_ns": profile.qualification.clean_duration_sample_period_ns,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        duration_report = detect_trajectory_integrity(
            duration_samples,
            source_sha256=hashlib.sha256(duration_identity).hexdigest(),
            partition=partition,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            trajectory_parameters=parameters,
        )
        cluster["structural_predictions"] = int(
            cluster["structural_predictions"]
        ) + sum(
            bool(_STRUCTURAL_RULES.intersection(event.triggered_rules))
            for event in duration_report.events
        )
        cluster["clean_critical"] = sum(
            event.severity in {Severity.CRITICAL, Severity.BLOCKING_ANALYSIS}
            for event in duration_report.events
        )
        cluster["clean_hours"] = (
            profile.qualification.clean_duration_per_source_group_ns
            / 3_600_000_000_000.0
        )
        clusters.append(cluster)

    statistics = cast(dict[str, Any], charter["statistics"])
    replicates = int(statistics["bootstrap_replicates"])
    bootstrap_seed = int(statistics["bootstrap_seed"])
    confidence_level = float(statistics["confidence_level"])
    metrics: dict[str, dict[str, Any]] = {
        "structural.event_recall": _bootstrap(
            clusters,
            _ratio("structural_detected", "structural_truth"),
            seed=bootstrap_seed,
            replicates=replicates,
            confidence_level=confidence_level,
        ),
        "structural.event_precision": _bootstrap(
            clusters,
            _ratio("structural_matches", "structural_predictions"),
            seed=bootstrap_seed + 1,
            replicates=replicates,
            confidence_level=confidence_level,
        ),
        "content.supported_fault_recall": _bootstrap(
            clusters,
            _ratio("content_detected", "content_truth"),
            seed=bootstrap_seed + 2,
            replicates=replicates,
            confidence_level=confidence_level,
        ),
        "content.false_critical_per_clean_sensor_hour": _bootstrap(
            clusters,
            _ratio("clean_critical", "clean_hours"),
            seed=bootstrap_seed + 3,
            replicates=replicates,
            confidence_level=confidence_level,
        ),
        "content.event_boundary_median_stride": _bootstrap(
            clusters,
            _boundary_median,
            seed=bootstrap_seed + 4,
            replicates=replicates,
            confidence_level=confidence_level,
        ),
    }
    clean_critical_count = sum(int(item["clean_critical"]) for item in clusters)
    clean_exposure_hours = sum(float(item["clean_hours"]) for item in clusters)
    if clean_critical_count == 0:
        false_critical = metrics["content.false_critical_per_clean_sensor_hour"]
        false_critical["clustered_bootstrap_one_sided_upper_95"] = false_critical[
            "one_sided_upper_95"
        ]
        alpha = 1.0 - confidence_level
        false_critical["one_sided_upper_95"] = -math.log(alpha) / clean_exposure_hours
        false_critical["two_sided_upper_95"] = (
            -math.log(alpha / 2.0) / clean_exposure_hours
        )
        false_critical["zero_event_bound_method"] = (
            profile.qualification.zero_event_rate_upper_bound
        )
        false_critical["observed_event_count"] = 0
    severity_metrics: dict[str, dict[str, dict[str, Any]]] = {}
    for operator in _QUALIFICATION_OPERATORS:
        severity_metrics[operator.value] = {}
        for severity in FaultSeverity:
            outcome = expected_outcomes[operator.value][severity.value]
            result: dict[str, Any] = {
                "distinct_injected_events": len(
                    distinct_events_by_stratum[operator.value][severity.value]
                ),
                "expected_gate_outcome": declared_outcome_by_stratum[
                    (operator.value, severity.value)
                ],
                "expected_outcome_passed": outcome["passed"],
                "expected_outcome_total": outcome["total"],
                "expected_outcome_adherence": outcome["passed"] / outcome["total"],
            }
            if (
                declared_outcome_by_stratum[(operator.value, severity.value)]
                == ExpectedGateOutcome.EXPECT_FINDING_WHEN_OBSERVABLE.value
            ):
                result["supported_recall"] = _bootstrap(
                    clusters,
                    _stratum_ratio(operator.value, severity.value),
                    seed=(
                        bootstrap_seed
                        + 100
                        + list(_QUALIFICATION_OPERATORS).index(operator)
                    ),
                    replicates=replicates,
                    confidence_level=confidence_level,
                )
            else:
                result["supported_recall"] = None
            severity_metrics[operator.value][severity.value] = result
    gates = cast(dict[str, dict[str, Any]], charter["gates"])
    support_contract = cast(dict[str, Any], charter["confirmatory_support"])
    group_count = len(clusters)
    clean_hours = clean_exposure_hours
    event_support = all(
        len(events)
        >= int(support_contract["minimum_injected_events_per_fault_severity_stratum"])
        for operator in distinct_events_by_stratum.values()
        for events in operator.values()
    )
    confirmatory_support = (
        group_count >= int(support_contract["minimum_independent_clusters"])
        and clean_hours >= float(support_contract["minimum_clean_sensor_hours"])
        and event_support
    )
    for key, metric_result in metrics.items():
        gate = gates[key]
        value = float(gate["value"])
        gate_operator = str(gate["operator"])
        point = float(metric_result["point_estimate"])
        if gate_operator == "fraction_ge":
            point_passed = point >= value
            bound_passed = float(metric_result["one_sided_lower_95"]) >= value
        elif gate_operator in {"rate_le", "median_le"}:
            point_passed = point <= value
            bound_passed = float(metric_result["one_sided_upper_95"]) <= value
        else:
            raise ValueError(f"unsupported M3.2 gate operator {gate_operator}")
        metric_result.update(
            {
                "gate_operator": gate_operator,
                "gate_value": value,
                "unit": gate["unit"],
                "decision_bound": gate["decision_bound"],
                "eligible_source_groups": group_count,
                "engineering_point_gate_passed": point_passed,
                "bound_gate_passed": bound_passed,
                "confirmatory_gate_passed": None,
                "claim_status": "NONCONFIRMATORY_FROZEN_SYNTHETIC_GATE_EVIDENCE",
            }
        )
    all_cases_exercised = all(
        count > 0 for operator in case_coverage.values() for count in operator.values()
    )
    engineering_passed = (
        all(bool(item["engineering_point_gate_passed"]) for item in metrics.values())
        and all_cases_exercised
        and all(
            outcome["passed"] == outcome["total"]
            for operator in expected_outcomes.values()
            for outcome in operator.values()
        )
        and all(detector_controls.values())
        and stationary_false_freezes
        <= profile.qualification.stationary_false_freeze_count_maximum
        and bias_controls["no_reference_not_observable"]
        == bias_controls["no_reference_total"]
        and bias_controls["reference_supported_detected"]
        == bias_controls["reference_supported_total"]
    )
    return {
        "partition": partition,
        "family_set_id": expected_partition.family_set_id,
        "predeclared_partition_claim_status": expected_partition.claim_status,
        "result_claim_status": expected_partition.claim_status,
        "source_group_count": group_count,
        "source_group_ids": [item[0] for item in assignments],
        "case_coverage": case_coverage,
        "severity_strata": severity_metrics,
        "clean_sensor_hours": clean_hours,
        "stationary_false_freeze_count": stationary_false_freezes,
        "bias_control": bias_controls,
        "detector_controls": detector_controls,
        "metrics": metrics,
        "support": {
            "confirmatory_support_sufficient_but_claim_forbidden": (
                confirmatory_support
            ),
            "event_support_sufficient": event_support,
            "minimum_independent_clusters": support_contract[
                "minimum_independent_clusters"
            ],
            "minimum_events_per_severity_stratum": support_contract[
                "minimum_injected_events_per_fault_severity_stratum"
            ],
            "minimum_clean_sensor_hours": support_contract[
                "minimum_clean_sensor_hours"
            ],
        },
        "confirmatory_inference": {
            "performed": False,
            "reason": (
                "The frozen M3.2 synthetic corpus is an exhaustive engineering and "
                "calibration gate; the profile forbids a confirmatory claim."
            ),
            "charter_family": "trajectory-v1",
            "multiplicity": "NOT_APPLIED_NO_CONFIRMATORY_CLAIM",
        },
        "engineering_gate_passed": engineering_passed,
        "confirmatory_gate_passed": None,
    }


def qualify_trajectory_integrity(
    *,
    profile_path: Path,
    trajectory_gate_path: Path,
    split_manifest_path: Path,
    fault_matrix_path: Path,
    charter_path: Path,
) -> dict[str, object]:
    """Run the exact development and calibration M3.2 qualification workflow."""

    profile, profile_file_sha256 = load_trajectory_integrity_profile(profile_path)
    charter, charter_content = _load_charter(charter_path)
    split_content = read_bounded_regular_bytes(
        split_manifest_path,
        maximum_bytes=MAXIMUM_CHARTER_BYTES,
        context="split manifest",
    )
    fault_matrix_content = read_bounded_regular_bytes(
        fault_matrix_path,
        maximum_bytes=MAXIMUM_CHARTER_BYTES,
        context="fault matrix",
    )
    trajectory_gate_content = read_bounded_regular_bytes(
        trajectory_gate_path,
        maximum_bytes=MAXIMUM_CHARTER_BYTES,
        context="trajectory gate",
    )
    authorities = profile.qualification.authorities
    observed_authorities = {
        "split manifest": (
            hashlib.sha256(split_content).hexdigest(),
            authorities.split_manifest_file_sha256,
        ),
        "fault matrix": (
            hashlib.sha256(fault_matrix_content).hexdigest(),
            authorities.fault_matrix_file_sha256,
        ),
        "numerical charter": (
            hashlib.sha256(charter_content).hexdigest(),
            authorities.numerical_charter_file_sha256,
        ),
        "trajectory gate": (
            hashlib.sha256(trajectory_gate_content).hexdigest(),
            authorities.trajectory_gate_file_sha256,
        ),
    }
    for authority_name, (observed, expected) in observed_authorities.items():
        if observed != expected:
            raise ValueError(f"{authority_name} does not match the frozen M3.2 profile")
    if charter["immutable_sha256"] != authorities.numerical_charter_immutable_sha256:
        raise ValueError("numerical charter identity does not match the M3.2 profile")
    qualification_partitions: tuple[SyntheticPartition, ...] = (
        "development",
        "threshold_calibration",
    )
    partitions = [
        _partition_qualification(
            partition,
            split_manifest_path=split_manifest_path,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            trajectory_gate_path=trajectory_gate_path,
            fault_matrix_path=fault_matrix_path,
            charter=charter,
        )
        for partition in qualification_partitions
    ]
    development, calibration = partitions
    accepted = (
        development["engineering_gate_passed"] is True
        and development["result_claim_status"] == "DESCRIPTIVE_ONLY"
        and calibration["engineering_gate_passed"] is True
        and calibration["result_claim_status"] == "CALIBRATION_ONLY"
        and calibration["support"]["event_support_sufficient"] is True
    )
    statistics = cast(dict[str, Any], charter["statistics"])
    bootstrap_units = cast(dict[str, Any], statistics["bootstrap_unit_by_domain"])
    return {
        "schema_version": "cartosentry.trajectory-integrity-qualification.v1",
        "qualification_version": "m3.2-trajectory-integrity-v1",
        "accepted": accepted,
        "claim_scope": (
            "Frozen synthetic M3.2 engineering and calibration gates only; no "
            "confirmatory, final-test, public-data, or release claim is made."
        ),
        "hashes": {
            "profile_immutable_sha256": profile.immutable_sha256,
            "profile_file_sha256": profile_file_sha256,
            "trajectory_gate_file_sha256": hashlib.sha256(
                trajectory_gate_content
            ).hexdigest(),
            "split_manifest_sha256": hashlib.sha256(split_content).hexdigest(),
            "fault_matrix_sha256": hashlib.sha256(fault_matrix_content).hexdigest(),
            "charter_file_sha256": hashlib.sha256(charter_content).hexdigest(),
            "charter_immutable_sha256": charter["immutable_sha256"],
        },
        "bootstrap": {
            "unit": bootstrap_units["synthetic_sensor_and_map"],
            "seed": statistics["bootstrap_seed"],
            "replicates": statistics["bootstrap_replicates"],
            "cluster_splitting_forbidden": True,
        },
        "partitions": partitions,
    }


__all__ = [
    "qualify_trajectory_detector_controls",
    "qualify_trajectory_integrity",
]
