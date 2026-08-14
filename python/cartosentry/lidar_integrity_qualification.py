"""Frozen split-bound M4.1 lidar integrity qualification."""

from __future__ import annotations

import hashlib
import json
import tracemalloc
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, cast

from cartosentry.adapters import BoreasAdapter
from cartosentry.artifacts import Severity
from cartosentry.lidar_faults import (
    LidarFaultOperator,
    LidarQualificationGate,
    RegisteredLidarFaultCase,
    inject_lidar_fault,
    load_lidar_qualification_gate,
    registered_lidar_fault_cases,
)
from cartosentry.lidar_integrity import (
    LidarEvent,
    LidarFrameInput,
    LidarIntegrityProfile,
    LidarPointInput,
    LidarRule,
    analyze_lidar_integrity,
    load_lidar_integrity_profile,
    synthetic_lidar_frames,
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

_STRUCTURAL_OPERATORS = frozenset(
    {
        LidarFaultOperator.SCAN_LOSS,
        LidarFaultOperator.NONFINITE,
        LidarFaultOperator.RANGE_SCALE,
        LidarFaultOperator.POINT_TIME_CORRUPTION,
    }
)
_COVERAGE_OPERATORS = frozenset(
    {
        LidarFaultOperator.RING_LOSS,
        LidarFaultOperator.SECTOR_LOSS,
        LidarFaultOperator.DENSITY_REDUCTION,
    }
)
_PRIMARY_RULE = {
    LidarFaultOperator.SCAN_LOSS: LidarRule.FRAME_CADENCE,
    LidarFaultOperator.RING_LOSS: LidarRule.RING_LOSS,
    LidarFaultOperator.SECTOR_LOSS: LidarRule.SECTOR_LOSS,
    LidarFaultOperator.DENSITY_REDUCTION: LidarRule.DENSITY,
    LidarFaultOperator.NONFINITE: LidarRule.NONFINITE,
    LidarFaultOperator.RANGE_SCALE: LidarRule.RANGE,
    LidarFaultOperator.POINT_TIME_CORRUPTION: LidarRule.POINT_TIME,
}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _load_mapping(path: Path, *, context: str) -> dict[str, Any]:
    content = read_bounded_regular_bytes(
        path, maximum_bytes=4 * 1024 * 1024, context=context
    )
    decoded = decode_bounded_json(
        content, maximum_bytes=4 * 1024 * 1024, context=context
    )
    if not isinstance(decoded, dict):
        raise ValueError(f"{context} must be an object")
    return cast(dict[str, Any], decoded)


def _authenticate_authorities(
    gate: LidarQualificationGate,
    *,
    profile_path: Path,
    split_manifest_path: Path,
    numerical_charter_path: Path,
    representative_fault_matrix_path: Path,
) -> None:
    expected = {
        profile_path: gate.authorities.profile_file_sha256,
        split_manifest_path: gate.authorities.split_manifest_file_sha256,
        numerical_charter_path: gate.authorities.numerical_charter_file_sha256,
        representative_fault_matrix_path: (
            gate.authorities.representative_fault_matrix_file_sha256
        ),
    }
    for path, expected_sha256 in expected.items():
        if _file_sha256(path) != expected_sha256:
            raise ValueError(f"{path.name} does not match the frozen M4.1 authority")


def _rules(report_events: tuple[Any, ...]) -> set[LidarRule]:
    return {cast(LidarRule, event.rule) for event in report_events}


def _case_passed(
    case: RegisteredLidarFaultCase, observed_rules: set[LidarRule]
) -> bool:
    if case.expected_rule is not None:
        return case.expected_rule in observed_rules
    return _PRIMARY_RULE[case.operator_id] not in observed_rules


def _boundary_error_frames(event: LidarEvent, start: int, end: int) -> int:
    boundaries = (start, end)
    start_error = min(abs(event.start_frame_index - value) for value in boundaries)
    end_error = min(
        abs(event.end_frame_index_exclusive - value) for value in boundaries
    )
    return max(start_error, end_error)


def _qualify_partition(
    partition: Literal["development", "threshold_calibration"],
    *,
    gate: LidarQualificationGate,
    profile: LidarIntegrityProfile,
    profile_file_sha256: str,
    split_manifest_path: Path,
) -> dict[str, object]:
    partition_gate = gate.partitions[partition]
    assignments = sensor_map_family_assignments(split_manifest_path, partition)
    expected_ids = tuple(
        f"{partition_gate.family_prefix}{index:03d}"
        for index in range(1, partition_gate.family_count + 1)
    )
    if tuple(item[0] for item in assignments) != expected_ids:
        raise ValueError(f"{partition} source-group membership is not exact")
    cases = registered_lidar_fault_cases(gate)
    outcomes: list[dict[str, object]] = []
    clean_false_critical = 0
    clean_event_count = 0
    boundary_errors: list[int] = []
    for family_id, scenario, seed in assignments:
        fixture = generate_fixture(
            family_id,
            scenario,
            seed,
            partition=partition,
        )
        source_content = serialize_fixture(fixture)
        source_sha256 = hashlib.sha256(source_content).hexdigest()
        clean_frames = synthetic_lidar_frames(fixture)
        clean = analyze_lidar_integrity(
            clean_frames,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            sensor_model_id="synthetic-spinning-v1",
            source_sha256=source_sha256,
            source_group_id=family_id,
            partition=partition,
        )
        clean_event_count += len(clean.events)
        clean_false_critical += sum(
            event.severity in {Severity.CRITICAL, Severity.BLOCKING_ANALYSIS}
            for event in clean.events
        )
        for case in cases:
            faulted = inject_lidar_fault(
                clean_frames,
                case,
                source_sha256=source_sha256,
                seed=seed,
            )
            report = analyze_lidar_integrity(
                faulted.frames,
                profile=profile,
                profile_file_sha256=profile_file_sha256,
                sensor_model_id="synthetic-spinning-v1",
                source_sha256=faulted.truth.derived_sha256,
                source_group_id=family_id,
                partition=partition,
            )
            observed_rules = _rules(report.events)
            passed = _case_passed(case, observed_rules)
            boundary_error: int | None = None
            if case.expected_rule is not None:
                matches = [
                    event for event in report.events if event.rule is case.expected_rule
                ]
                if matches:
                    boundary_error = min(
                        _boundary_error_frames(
                            event,
                            faulted.truth.start_frame_index,
                            faulted.truth.end_frame_index_exclusive,
                        )
                        for event in matches
                    )
                    boundary_errors.append(boundary_error)
            outcomes.append(
                {
                    "source_group_id": family_id,
                    "operator_id": case.operator_id,
                    "case_id": case.case_id,
                    "severity": case.severity,
                    "expected_rule": case.expected_rule,
                    "observed_rules": sorted(rule.value for rule in observed_rules),
                    "outcome_passed": passed,
                    "boundary_error_frames": boundary_error,
                    "truth": faulted.truth.model_dump(mode="json"),
                }
            )
    structural = [
        outcome
        for outcome in outcomes
        if LidarFaultOperator(cast(str, outcome["operator_id"]))
        in _STRUCTURAL_OPERATORS
    ]
    coverage = [
        outcome
        for outcome in outcomes
        if LidarFaultOperator(cast(str, outcome["operator_id"])) in _COVERAGE_OPERATORS
    ]
    structural_fraction = sum(
        bool(item["outcome_passed"]) for item in structural
    ) / len(structural)
    coverage_fraction = sum(bool(item["outcome_passed"]) for item in coverage) / len(
        coverage
    )
    maximum_boundary_error = max(boundary_errors, default=0)
    gate_passed = (
        structural_fraction == gate.gates["structural_expected_outcome_fraction"].value
        and coverage_fraction
        == gate.gates["supported_coverage_expected_outcome_fraction"].value
        and clean_false_critical <= gate.gates["clean_false_critical_count"].value
        and maximum_boundary_error <= gate.gates["event_boundary_error_frames"].value
    )
    return {
        "partition": partition,
        "claim_status": partition_gate.claim_status,
        "source_group_count": len(assignments),
        "case_count": len(outcomes),
        "clean_event_count": clean_event_count,
        "clean_false_critical_count": clean_false_critical,
        "structural_expected_outcome_fraction": structural_fraction,
        "supported_coverage_expected_outcome_fraction": coverage_fraction,
        "maximum_event_boundary_error_frames": maximum_boundary_error,
        "gate_passed": gate_passed,
        "outcomes": outcomes,
    }


def _manifest_public_frames(
    *,
    data_manifest: dict[str, Any],
    sequence_id: str,
    maximum_frames: int,
) -> tuple[tuple[str, str, int], ...]:
    candidates = [
        artifact
        for artifact in cast(list[dict[str, Any]], data_manifest.get("artifacts", []))
        if artifact.get("id") == "boreas-public-smoke-clear-v1"
    ]
    if len(candidates) != 1:
        raise ValueError("clear public smoke artifact is not exact")
    artifact = candidates[0]
    if (
        artifact.get("partition") != "development"
        or artifact.get("source_group_id") != "boreas-glen-shields-family-v1"
        or artifact.get("source_sequence_ids") != [sequence_id]
    ):
        raise ValueError("public lidar smoke artifact moved partition")
    result = sorted(
        (
            cast(str, item["key"]),
            cast(str, item["sha256"]),
            cast(int, item["bytes"]),
        )
        for item in cast(list[dict[str, Any]], artifact.get("objects", []))
        if cast(str, item.get("key", "")).startswith(f"{sequence_id}/lidar/")
    )
    if len(result) < maximum_frames:
        raise ValueError("public lidar smoke has too few pinned frames")
    return tuple(result[:maximum_frames])


def _boreas_frames(
    adapter: BoreasAdapter,
    *,
    expected_keys: tuple[str, ...],
) -> Iterator[LidarFrameInput]:
    selected = tuple(
        frame for _, frame in zip(expected_keys, adapter.frames(), strict=False)
    )
    if tuple(frame.payload.source_key for frame in selected) != expected_keys:
        raise ValueError("public lidar frame ordering differs from the manifest")
    for frame_index, frame in enumerate(selected):
        midpoint = frame.times.sensor_time
        if midpoint is None:
            raise ValueError("public lidar frame lacks a scan midpoint")

        def points() -> Iterator[LidarPointInput]:
            for point in adapter.lidar_points(frame):
                yield LidarPointInput(
                    position_m=point.position_lidar_m,
                    intensity=point.intensity,
                    ring_id=point.laser_id,
                    relative_time_ns=float(point.relative_time.offset_ns),
                    source_offset=point.byte_offset,
                )

        yield LidarFrameInput(
            frame_index=frame_index,
            source_key=frame.payload.source_key,
            reference_time_ns=midpoint.value_ns,
            capture_start_ns=midpoint.value_ns - 50_000_000,
            capture_end_ns=midpoint.value_ns + 50_000_000,
            points=points(),
        )


def _qualify_public_smoke(
    *,
    public_data_root: Path,
    data_manifest_path: Path,
    split_manifest_path: Path,
    profile: LidarIntegrityProfile,
    profile_file_sha256: str,
    gate: LidarQualificationGate,
) -> dict[str, object]:
    split = _load_mapping(split_manifest_path, context="split manifest")
    if _file_sha256(data_manifest_path) != split.get("data_manifest_sha256"):
        raise ValueError("data manifest does not match the frozen split")
    data_manifest = _load_mapping(data_manifest_path, context="data manifest")
    sequence_id = "boreas-2021-09-02-11-42"
    pinned = _manifest_public_frames(
        data_manifest=data_manifest,
        sequence_id=sequence_id,
        maximum_frames=10,
    )
    for key, expected_sha256, expected_bytes in pinned:
        path = public_data_root / key
        if (
            not path.is_file()
            or path.stat().st_size != expected_bytes
            or _file_sha256(path) != expected_sha256
        ):
            raise ValueError(f"public lidar frame {key} failed content verification")
    aggregate_source_sha256 = hashlib.sha256(
        json.dumps(
            pinned, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()
    adapter = BoreasAdapter(
        public_data_root / sequence_id,
        source_group_id="boreas-glen-shields-family-v1",
    )
    expected_keys = tuple(key.removeprefix(f"{sequence_id}/") for key, _, _ in pinned)
    tracemalloc.start()
    try:
        report = analyze_lidar_integrity(
            _boreas_frames(adapter, expected_keys=expected_keys),
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            sensor_model_id="boreas-128-v1",
            source_sha256=aggregate_source_sha256,
            source_group_id="boreas-glen-shields-family-v1",
            partition="development",
        )
        _current, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    critical_count = sum(
        event.severity in {Severity.CRITICAL, Severity.BLOCKING_ANALYSIS}
        for event in report.events
    )
    accepted = (
        peak_bytes <= gate.gates["public_smoke_peak_traced_bytes"].value
        and report.statistics.retained_state_upper_bound_bytes
        <= profile.budgets.maximum_traced_peak_bytes_public_smoke
    )
    return {
        "accepted": accepted,
        "claim_status": "DEVELOPMENT_PUBLIC_SMOKE_ONLY",
        "sequence_id": sequence_id,
        "source_group_id": "boreas-glen-shields-family-v1",
        "partition": "development",
        "manifest_verified_frame_count": len(pinned),
        "aggregate_source_sha256": aggregate_source_sha256,
        "peak_traced_bytes": peak_bytes,
        "peak_budget_bytes": gate.gates["public_smoke_peak_traced_bytes"].value,
        "retained_state_upper_bound_bytes": (
            report.statistics.retained_state_upper_bound_bytes
        ),
        "frame_count": report.statistics.frame_count,
        "point_count": report.statistics.point_count,
        "critical_or_blocking_event_count": critical_count,
        "events": [event.model_dump(mode="json") for event in report.events],
        "statistics": report.statistics.model_dump(mode="json"),
    }


def qualify_lidar_integrity(
    *,
    gate_path: Path,
    profile_path: Path,
    split_manifest_path: Path,
    numerical_charter_path: Path,
    representative_fault_matrix_path: Path,
    data_manifest_path: Path,
    public_data_root: Path,
) -> dict[str, object]:
    """Run all synthetic outcomes and the bounded real public smoke path."""

    gate, gate_file_sha256 = load_lidar_qualification_gate(gate_path)
    _authenticate_authorities(
        gate,
        profile_path=profile_path,
        split_manifest_path=split_manifest_path,
        numerical_charter_path=numerical_charter_path,
        representative_fault_matrix_path=representative_fault_matrix_path,
    )
    profile, profile_file_sha256 = load_lidar_integrity_profile(profile_path)
    if profile.immutable_sha256 != gate.authorities.profile_immutable_sha256:
        raise ValueError("lidar profile identity differs from the M4.1 gate")
    partitions = [
        _qualify_partition(
            partition,
            gate=gate,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            split_manifest_path=split_manifest_path,
        )
        for partition in ("development", "threshold_calibration")
    ]
    public_smoke = _qualify_public_smoke(
        public_data_root=public_data_root,
        data_manifest_path=data_manifest_path,
        split_manifest_path=split_manifest_path,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        gate=gate,
    )
    accepted = all(cast(bool, item["gate_passed"]) for item in partitions) and cast(
        bool, public_smoke["accepted"]
    )
    return {
        "schema_version": "cartosentry.lidar-integrity-qualification-report.v1",
        "gate_id": gate.gate_id,
        "gate_version": gate.gate_version,
        "accepted": accepted,
        "claim_scope": gate.claim_scope,
        "matrix_status": gate.matrix_status,
        "hashes": {
            **gate.authorities.model_dump(mode="json"),
            "gate_immutable_sha256": gate.immutable_sha256,
            "gate_file_sha256": gate_file_sha256,
        },
        "partitions": partitions,
        "public_smoke": public_smoke,
    }


__all__ = ["qualify_lidar_integrity"]
