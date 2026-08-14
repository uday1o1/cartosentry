#!/usr/bin/env python3
"""Generate the committed M1.2 schemas and conformance examples."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from cartosentry.artifacts import (
    ARTIFACT_MODEL_BY_SCHEMA,
    AcceptedDataBundle,
    AdapterIdentity,
    AggregationRule,
    ArtifactModel,
    ArtifactReference,
    BundleInterval,
    CalibrationIdentity,
    CharterReference,
    CoordinateMetadata,
    EvidenceReference,
    Finding,
    MandatoryRequirement,
    Measurement,
    MeasurementUnit,
    Observability,
    Reachability,
    ReadinessProfile,
    ReadinessState,
    RecapturePlan,
    RecaptureRequirement,
    RootCauseHypothesis,
    RouteBudget,
    Run,
    SensorDescriptor,
    SensorModality,
    SequenceManifest,
    Severity,
    SourceFile,
    SourceInterval,
    SourcePartition,
    StageRecord,
    StageState,
    Threshold,
    ThresholdOperator,
    TimestampMetadata,
    WindowPolicy,
)
from cartosentry.contracts import (
    RawTime,
    RawTimeEncoding,
    TimeEpoch,
    TimePoint,
    TimeReference,
    TimeRounding,
    VerticalDatum,
)
from cartosentry.identifiers import (
    make_bundle_id,
    make_calibration_id,
    make_finding_id,
    make_frame_id,
    make_recapture_plan_id,
    make_requirement_id,
    make_road_bin_id,
    make_run_id,
    make_sequence_id,
    make_stream_id,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = REPOSITORY_ROOT / "schemas"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def time_point(value_ns: int) -> TimePoint:
    return TimePoint(
        value_ns=value_ns,
        epoch=TimeEpoch.UNIX_UTC,
        clock_id="synthetic-common",
        reference=TimeReference.SAMPLE,
        raw=RawTime(
            source_key="synthetic/trajectory.json",
            field="timestamp",
            unit="ns",
            epoch=TimeEpoch.UNIX_UTC,
            reference=TimeReference.SAMPLE,
            encoding=RawTimeEncoding.SIGNED_INTEGER,
            integer_value=str(value_ns),
            rounding=TimeRounding.EXACT,
            maximum_conversion_error_ns=0.0,
        ),
    )


def valid_artifacts() -> dict[str, ArtifactModel]:
    source_identity = "1" * 64
    source_file = SourceFile(
        source_key="synthetic/trajectory.json",
        sha256="2" * 64,
        byte_count=4096,
    )
    calibration_payload = {
        "sha256": "3" * 64,
        "source_frame": "lidar",
        "target_frame": "rig",
    }
    calibration = CalibrationIdentity(
        calibration_id=make_calibration_id(calibration_payload),
        sha256="3" * 64,
        target_frame="rig",
        source_frame="lidar",
    )
    adapter = AdapterIdentity(
        adapter_id="synthetic",
        adapter_version="1.0.0",
        capabilities=("trajectory", "lidar-points"),
    )
    coordinate = CoordinateMetadata(
        global_frame="WGS84",
        local_frame="local_world",
        rig_frame="rig",
        vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
    )
    sensor_identity = {
        "modality": SensorModality.LIDAR.value,
        "sensor_id": "roof-lidar",
        "coordinate_frame": "lidar",
        "calibration_ids": [calibration.calibration_id],
    }
    timestamp_identity = {
        "epoch": TimeEpoch.UNIX_UTC.value,
        "clock_id": "synthetic-common",
        "reference": TimeReference.SAMPLE.value,
        "raw_unit": "ns",
        "modality": SensorModality.LIDAR.value,
        "sensor_id": "roof-lidar",
    }
    sequence_identity: dict[str, object] = {
        "adapter": adapter.model_dump(mode="json"),
        "calibrations": [calibration.model_dump(mode="json")],
        "coordinate_metadata": coordinate.model_dump(mode="json"),
        "sensors": [sensor_identity],
        "source_files": [source_file.model_dump(mode="json")],
        "source_identity_sha256": source_identity,
        "timestamp_metadata": [timestamp_identity],
    }
    sequence_id = make_sequence_id(sequence_identity)
    stream_id = make_stream_id(sequence_id, "lidar", "roof-lidar")
    sensor = SensorDescriptor(
        stream_id=stream_id,
        modality=SensorModality.LIDAR,
        sensor_id="roof-lidar",
        coordinate_frame="lidar",
        calibration_ids=(calibration.calibration_id,),
    )
    timestamp = TimestampMetadata(
        stream_id=stream_id,
        epoch=TimeEpoch.UNIX_UTC,
        clock_id="synthetic-common",
        reference=TimeReference.SAMPLE,
        raw_unit="ns",
    )
    sequence = SequenceManifest(
        schema_version="cartosentry.sequence-manifest.v1",
        sequence_id=sequence_id,
        source_identity_sha256=source_identity,
        source_group_id="synthetic-v1",
        partition=SourcePartition.DEVELOPMENT,
        adapter=adapter,
        sensors=(sensor,),
        source_files=(source_file,),
        calibrations=(calibration,),
        timestamp_metadata=(timestamp,),
        coordinate_metadata=coordinate,
        declared_gaps=(),
    )

    road_graph_id = "road-graph-sha256-" + "4" * 64
    configuration_hashes = {"engine": "5" * 64, "profile": "6" * 64}
    run_id = make_run_id(
        sequence_id=sequence_id,
        road_graph_id=road_graph_id,
        profile_id="structural-preflight-v1",
        engine_version="0.1.0",
        configuration_hashes=configuration_hashes,
    )
    artifact_reference = ArtifactReference(
        source_key="artifacts/evidence.parquet",
        sha256="7" * 64,
        byte_count=1024,
        media_type="application/vnd.apache.parquet",
    )
    run = Run(
        schema_version="cartosentry.run.v1",
        run_id=run_id,
        sequence_id=sequence_id,
        road_graph_id=road_graph_id,
        profile_id="structural-preflight-v1",
        engine_version="0.1.0",
        configuration_hashes=configuration_hashes,
        state=StageState.COMPLETE,
        stages={
            "structural-preflight": StageRecord(
                state=StageState.COMPLETE,
                attempt_id="attempt-001",
                output_hashes={"evidence": "7" * 64},
            )
        },
        artifacts=(artifact_reference,),
        local_context=None,
    )

    interval = SourceInterval(start=time_point(0), end=time_point(1_000_000_000))
    frame_id = make_frame_id(
        stream_id,
        "frames/000001.bin",
        interval.model_dump(mode="json"),
    )
    evidence = EvidenceReference(
        source_artifact_sha256="2" * 64,
        source_interval=interval,
        frame_ids=(frame_id,),
        derived_artifact_sha256="7" * 64,
        detector_version="1.0.0",
        transformation_lineage=("decode-v1", "time-normalization-v1"),
    )
    finding_id = make_finding_id(
        detector_id="lidar-time-integrity",
        detector_version="1.0.0",
        rule_id="maximum-observable-offset",
        source_interval=interval.model_dump(mode="json"),
        stream_ids=(stream_id,),
        evidence_fingerprint=[evidence.model_dump(mode="json")],
    )
    finding = Finding(
        schema_version="cartosentry.finding.v1",
        finding_id=finding_id,
        detector_id="lidar-time-integrity",
        detector_version="1.0.0",
        rule_id="maximum-observable-offset",
        severity=Severity.CRITICAL,
        observability=Observability.OBSERVABLE,
        readiness_effect=ReadinessState.FAIL,
        streams=(stream_id,),
        interval=interval,
        measurement=Measurement(
            name="estimated-offset",
            value=42_000_000.0,
            unit=MeasurementUnit.NANOSECOND,
        ),
        threshold=Threshold(
            operator=ThresholdOperator.ABSOLUTE_LESS_THAN_OR_EQUAL,
            value=15_000_000.0,
            unit=MeasurementUnit.NANOSECOND,
            charter_key="time-alignment.maximum-offset-ns",
        ),
        road_bin_ids=(),
        evidence=(evidence,),
        hypotheses=(
            RootCauseHypothesis(
                possible_cause="The source clocks may not be synchronized.",
                supporting_evidence=(evidence,),
                contradicting_evidence=(),
            ),
        ),
        remediation="Verify clock synchronization before recollection.",
    )

    profile = ReadinessProfile(
        schema_version="cartosentry.readiness-profile.v1",
        profile_id="structural-preflight-v1",
        profile_version="1.0.0",
        supported_adapter_capabilities=("trajectory", "lidar-points"),
        required_modalities=(SensorModality.TRAJECTORY, SensorModality.LIDAR),
        required_detectors=("source-integrity", "lidar-time-integrity"),
        aggregation_rules=(
            AggregationRule(
                rule_id="full-sequence-integrity",
                window_policy=WindowPolicy.FULL_SEQUENCE,
                window_value=None,
                window_unit=None,
                stride_value=None,
                stride_unit=None,
            ),
        ),
        mandatory_requirements=(
            MandatoryRequirement(
                requirement_id="lidar-time-monotonic",
                evidence_key="lidar-time-integrity",
                operator=ThresholdOperator.EQUAL,
                threshold=1.0,
                unit=MeasurementUnit.BOOLEAN,
                minimum_observability="OBSERVABLE",
                charter_key="lidar.time-monotonic",
            ),
        ),
        optional_review_features=("evidence-thumbnails",),
        charter_references=(
            CharterReference(
                charter_key="lidar.time-monotonic",
                document_sha256="8" * 64,
            ),
        ),
    )

    road_bin_id = make_road_bin_id(road_graph_id, "arc-001", 0)
    requirement_payload = {
        "road_bin_id": road_bin_id,
        "run_id": run_id,
        "required_modality": "lidar",
    }
    requirement = RecaptureRequirement(
        requirement_id=make_requirement_id(requirement_payload),
        road_bin_id=road_bin_id,
        directed_arc_id="arc-001",
        start_offset_m=10.0,
        end_offset_m=30.0,
        required_modality=SensorModality.LIDAR,
        traversal_direction="FORWARD",
        minimum_continuous_observation_m=20.0,
        sensor_warmup_m=5.0,
        priority_weight=10,
        reason="The selected road bin lacks observable lidar support.",
        reachability=Reachability.REACHABLE,
    )
    budget = RouteBudget(maximum_distance_m=1000.0, maximum_duration_ns=None)
    plan_identity: dict[str, object] = {
        "budget": budget.model_dump(mode="json"),
        "depot_node_id": "depot",
        "requirements": [requirement.model_dump(mode="json")],
        "road_graph_id": road_graph_id,
        "route_arc_ids": ["connector-001", "arc-001", "connector-002"],
        "run_id": run_id,
    }
    plan = RecapturePlan(
        schema_version="cartosentry.recapture-plan.v1",
        recapture_plan_id=make_recapture_plan_id(plan_identity),
        run_id=run_id,
        road_graph_id=road_graph_id,
        depot_node_id="depot",
        requirements=(requirement,),
        route_arc_ids=("connector-001", "arc-001", "connector-002"),
        covered_requirement_ids=(requirement.requirement_id,),
        deferred_requirement_ids=(),
        unreachable_requirement_ids=(),
        estimated_distance_m=120.0,
        estimated_duration_ns=20_000_000_000,
        budget=budget,
        validation_state=ReadinessState.PASS,
    )

    accepted_intervals = (
        BundleInterval(
            source_key="synthetic/trajectory.json",
            interval=interval,
            reason="All mandatory requirements passed.",
        ),
    )
    bundle_identity: dict[str, object] = {
        "schema_version": "cartosentry.accepted-data-bundle.v1",
        "immutable": True,
        "source_sequence_sha256": source_identity,
        "sequence_id": sequence_id,
        "profile_id": "structural-preflight-v1",
        "accepted_intervals": [
            item.model_dump(mode="json") for item in accepted_intervals
        ],
        "excluded_intervals": [],
        "required_calibration_ids": [calibration.calibration_id],
        "derived_artifacts": [artifact_reference.model_dump(mode="json")],
        "raw_data_shards": [],
    }
    bundle = AcceptedDataBundle(
        schema_version="cartosentry.accepted-data-bundle.v1",
        bundle_id=make_bundle_id(bundle_identity),
        immutable=True,
        source_sequence_sha256=source_identity,
        sequence_id=sequence_id,
        profile_id="structural-preflight-v1",
        accepted_intervals=accepted_intervals,
        excluded_intervals=(),
        required_calibration_ids=(calibration.calibration_id,),
        derived_artifacts=(artifact_reference,),
        raw_data_shards=(),
    )

    return {
        SequenceManifest.schema_name: sequence,
        Run.schema_name: run,
        Finding.schema_name: finding,
        ReadinessProfile.schema_name: profile,
        RecapturePlan.schema_name: plan,
        AcceptedDataBundle.schema_name: bundle,
    }


def _schema_file_name(schema_name: str) -> str:
    return schema_name.removeprefix("cartosentry.") + ".schema.json"


def generated_files() -> dict[Path, str]:
    artifacts = valid_artifacts()
    generated: dict[Path, str] = {}
    index_entries: list[dict[str, str]] = []
    for schema_name, model_type in ARTIFACT_MODEL_BY_SCHEMA.items():
        schema = model_type.model_json_schema()
        schema["$schema"] = SCHEMA_DIALECT
        schema["$id"] = f"https://schemas.cartosentry.dev/{schema_name}.json"
        text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        relative = Path("v1") / _schema_file_name(schema_name)
        generated[relative] = text
        index_entries.append(
            {
                "path": relative.as_posix(),
                "schema": schema_name,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
        example_name = schema_name.removeprefix("cartosentry.").removesuffix(".v1")
        generated[Path("examples/valid") / f"{example_name}.json"] = (
            json.dumps(artifacts[schema_name].portable_dict(), indent=2, sort_keys=True)
            + "\n"
        )

    valid: dict[str, dict[str, Any]] = {
        schema: artifact.portable_dict() for schema, artifact in artifacts.items()
    }
    unknown = copy.deepcopy(valid[SequenceManifest.schema_name])
    unknown["unexpected"] = True
    missing = copy.deepcopy(valid[Run.schema_name])
    del missing["run_id"]
    wrong_unit = copy.deepcopy(valid[Finding.schema_name])
    wrong_unit["threshold"]["unit"] = "m"
    invalid_enum = copy.deepcopy(valid[ReadinessProfile.schema_name])
    invalid_enum["required_modalities"][0] = "sonar"
    path_leak = copy.deepcopy(valid[AcceptedDataBundle.schema_name])
    path_leak["derived_artifacts"][0]["source_key"] = (
        "/recordings/private/evidence.parquet"
    )
    downgrade = copy.deepcopy(valid[RecapturePlan.schema_name])
    downgrade["schema_version"] = "cartosentry.recapture-plan.v0"
    invalid = {
        "sequence-manifest.unknown-field.json": unknown,
        "run.missing-required-field.json": missing,
        "finding.wrong-unit.json": wrong_unit,
        "readiness-profile.invalid-enum.json": invalid_enum,
        "accepted-data-bundle.path-leak.json": path_leak,
        "recapture-plan.schema-downgrade.json": downgrade,
    }
    for name, value in invalid.items():
        generated[Path("examples/invalid") / name] = (
            json.dumps(value, indent=2, sort_keys=True) + "\n"
        )
    generated[Path("index.json")] = (
        json.dumps(
            {"schema_version": 1, "schemas": index_entries},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return generated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    differences: list[str] = []
    for relative, content in generated_files().items():
        destination = SCHEMA_ROOT / relative
        if args.check:
            if not destination.is_file() or destination.read_text() != content:
                differences.append(relative.as_posix())
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    if differences:
        for difference in differences:
            print(f"generated artifact is stale: schemas/{difference}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
