"""Frozen M3.5 trajectory and timestamp benchmark checkpoint."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry.adapters.boreas_v1 import BoreasAdapter
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.trajectory import (
    M3_1_GATE_SHA256,
    ReferenceSample,
    load_trajectory_gate,
    qualify_reference_trajectory,
    reference_samples_from_postprocessed,
)
from cartosentry.trajectory_integrity import (
    PROFILE_IMMUTABLE_SHA256,
    TrajectoryIntegrityProfile,
    detect_trajectory_integrity,
    load_trajectory_integrity_profile,
)
from cartosentry.trajectory_integrity_qualification import (
    qualify_trajectory_integrity,
)

CHECKPOINT_IMMUTABLE_SHA256 = (
    "962f40053b159db2b7888b484f262d75221e4d0d281b3679c02bbfda7ba376c7"
)
MAXIMUM_CHECKPOINT_BYTES = 128 * 1024
MAXIMUM_AUTHORITY_BYTES = 2 * 1024 * 1024
SOURCE_CHUNK_BYTES = 1024 * 1024


class CheckpointAuthorities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trajectory_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    trajectory_profile_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    trajectory_gate_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    trajectory_gate_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    split_manifest_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    fault_matrix_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    numerical_charter_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    aggregate_charter_revision_file_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    aggregate_charter_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    data_manifest_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_groups_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SyntheticGateContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    required_m3_1_accepted: Literal[True]
    required_m3_2_development_engineering_gate: Literal[True]
    required_m3_2_development_claim_status: Literal["DESCRIPTIVE_ONLY"]
    required_m3_2_calibration_guard_gate: Literal[True]
    threshold_changes_during_checkpoint_forbidden: Literal[True]


class PublicSequenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sequence_id: Annotated[str, Field(pattern=r"^boreas-[0-9-]+$")]
    artifact_id: Annotated[str, Field(min_length=1)]
    trajectory_source_key: Literal["applanix/gps_post_process.csv"]
    trajectory_source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    weather_tags: tuple[str, ...]


class PublicReviewContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_group_id: Literal["boreas-glen-shields-family-v1"]
    partition: Literal["development"]
    clip_length_samples: Annotated[int, Field(ge=128, le=100_000)]
    clip_positions: tuple[Literal["start", "middle", "end"], ...]
    minimum_clip_count: Annotated[int, Field(gt=0)]
    blocking_severities: tuple[Literal["CRITICAL", "BLOCKING_ANALYSIS"], ...]
    maximum_unresolved_false_critical_findings: Literal[0]
    sequences: tuple[PublicSequenceContract, ...]


class ThresholdChangeProcedure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    only_partition: Literal["threshold_calibration"]
    required_document: Literal[
        "docs/trajectory_integrity.md#threshold-change-procedure"
    ]
    requires_new_profile_version: Literal[True]
    requires_new_profile_hash: Literal[True]
    development_or_public_review_tuning_forbidden: Literal[True]
    final_test_tuning_forbidden: Literal[True]


class TemporalCheckpointContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    checkpoint_id: Literal["m3.5-temporal-integrity-v1"]
    checkpoint_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_M3_5_IMPLEMENTATION"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    claim_scope: Literal[
        "Development-only temporal integrity checkpoint with no confirmatory, "
        "final-test, or release claim"
    ]
    authorities: CheckpointAuthorities
    synthetic_gate: SyntheticGateContract
    public_review: PublicReviewContract
    threshold_change_procedure: ThresholdChangeProcedure

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.immutable_sha256 != CHECKPOINT_IMMUTABLE_SHA256:
            raise ValueError("M3.5 checkpoint does not match the pinned identity")
        if self.authorities.trajectory_profile_immutable_sha256 != (
            PROFILE_IMMUTABLE_SHA256
        ):
            raise ValueError("M3.5 checkpoint references an unknown detector profile")
        if self.authorities.trajectory_gate_immutable_sha256 != M3_1_GATE_SHA256:
            raise ValueError("M3.5 checkpoint references an unknown trajectory gate")
        if self.public_review.clip_positions != ("start", "middle", "end"):
            raise ValueError("M3.5 public clip positions must be exact and ordered")
        expected_clip_count = len(self.public_review.sequences) * len(
            self.public_review.clip_positions
        )
        if self.public_review.minimum_clip_count != expected_clip_count:
            raise ValueError("M3.5 public clip count is inconsistent")
        if len(self.public_review.sequences) < 2:
            raise ValueError("M3.5 requires multiple public development sequences")
        return self


def _canonical_sha256(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _load_mapping(path: Path, *, context: str) -> tuple[dict[str, Any], bytes]:
    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_AUTHORITY_BYTES,
            context=context,
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_AUTHORITY_BYTES,
            context=context,
        )
    except ManifestBoundaryError as error:
        raise ValueError(f"{context} is unavailable or malformed") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"{context} must be a JSON object")
    return cast(dict[str, Any], decoded), content


def load_temporal_checkpoint(path: Path) -> tuple[TemporalCheckpointContract, str]:
    """Load and self-authenticate the complete M3.5 checkpoint contract."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_CHECKPOINT_BYTES,
            context="M3.5 temporal checkpoint",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_CHECKPOINT_BYTES,
            context="M3.5 temporal checkpoint",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "M3.5 temporal checkpoint is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("M3.5 temporal checkpoint must be a JSON object")
    raw = cast(dict[str, object], decoded)
    expected = raw.get("immutable_sha256")
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if expected != _canonical_sha256(canonical):
        raise ValueError("M3.5 temporal checkpoint immutable hash is invalid")
    checkpoint = TemporalCheckpointContract.model_validate_json(content)
    return checkpoint, hashlib.sha256(content).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(SOURCE_CHUNK_BYTES), b""):
                digest.update(block)
    except OSError as error:
        raise ValueError(f"cannot hash required input {path.name}") from error
    return digest.hexdigest()


def _authenticate_authorities(
    checkpoint: TemporalCheckpointContract,
    *,
    profile_path: Path,
    trajectory_gate_path: Path,
    split_manifest_path: Path,
    fault_matrix_path: Path,
    numerical_charter_path: Path,
    charter_revisions_path: Path,
    data_manifest_path: Path,
    source_groups_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    authorities = checkpoint.authorities
    expected = {
        "trajectory profile": (
            profile_path,
            authorities.trajectory_profile_file_sha256,
        ),
        "trajectory gate": (
            trajectory_gate_path,
            authorities.trajectory_gate_file_sha256,
        ),
        "split manifest": (
            split_manifest_path,
            authorities.split_manifest_file_sha256,
        ),
        "fault matrix": (fault_matrix_path, authorities.fault_matrix_file_sha256),
        "numerical charter": (
            numerical_charter_path,
            authorities.numerical_charter_file_sha256,
        ),
        "aggregate charter revisions": (
            charter_revisions_path,
            authorities.aggregate_charter_revision_file_sha256,
        ),
        "data manifest": (data_manifest_path, authorities.data_manifest_file_sha256),
        "source groups": (source_groups_path, authorities.source_groups_file_sha256),
    }
    for name, (path, expected_sha256) in expected.items():
        if _file_sha256(path) != expected_sha256:
            raise ValueError(f"{name} does not match the frozen M3.5 checkpoint")
    revisions, _ = _load_mapping(
        charter_revisions_path, context="aggregate charter revisions"
    )
    if revisions.get("current_aggregate_sha256") != (
        authorities.aggregate_charter_immutable_sha256
    ):
        raise ValueError("aggregate charter identity does not match M3.5")
    data_manifest, _ = _load_mapping(data_manifest_path, context="data manifest")
    source_groups, _ = _load_mapping(source_groups_path, context="source groups")
    return data_manifest, source_groups


def _validate_public_sequence_authority(
    sequence: PublicSequenceContract,
    *,
    public_review: PublicReviewContract,
    data_manifest: dict[str, Any],
    source_groups: dict[str, Any],
) -> None:
    artifacts = [
        artifact
        for artifact in cast(list[dict[str, Any]], data_manifest.get("artifacts", []))
        if artifact.get("id") == sequence.artifact_id
    ]
    if len(artifacts) != 1:
        raise ValueError(f"public artifact {sequence.artifact_id} is not exact")
    artifact = artifacts[0]
    if (
        artifact.get("partition") != public_review.partition
        or artifact.get("source_group_id") != public_review.source_group_id
        or artifact.get("source_sequence_ids") != [sequence.sequence_id]
    ):
        raise ValueError(f"public artifact {sequence.artifact_id} moved partition")
    objects = {
        cast(str, item.get("key")): item
        for item in cast(list[dict[str, Any]], artifact.get("objects", []))
    }
    trajectory_object = objects.get(
        f"{sequence.sequence_id}/{sequence.trajectory_source_key}"
    )
    if trajectory_object is None or trajectory_object.get("sha256") != (
        sequence.trajectory_source_sha256
    ):
        raise ValueError(f"public trajectory {sequence.sequence_id} is not pinned")
    groups = [
        group
        for group in cast(list[dict[str, Any]], source_groups.get("source_groups", []))
        if group.get("source_group_id") == public_review.source_group_id
    ]
    if len(groups) != 1 or groups[0].get("partition") != public_review.partition:
        raise ValueError(
            "M3.5 public source group is not an ordinary development group"
        )
    assigned = {
        cast(str, item.get("sequence_id"))
        for item in cast(list[dict[str, Any]], groups[0].get("sequences", []))
    }
    if sequence.sequence_id not in assigned:
        raise ValueError(f"public sequence {sequence.sequence_id} is not assigned")


def _clip_bounds(
    sample_count: int, clip_length: int
) -> tuple[tuple[str, int, int], ...]:
    if sample_count < clip_length:
        raise ValueError("public trajectory is shorter than the frozen clip length")
    starts = (0, (sample_count - clip_length) // 2, sample_count - clip_length)
    if len(set(starts)) != len(starts):
        raise ValueError("public trajectory cannot supply three distinct clips")
    return tuple(
        (position, start, start + clip_length)
        for position, start in zip(("start", "middle", "end"), starts, strict=True)
    )


def review_public_trajectory_clips(
    samples: Iterable[ReferenceSample],
    *,
    sequence: PublicSequenceContract,
    source_file_sha256: str,
    checkpoint: TemporalCheckpointContract,
    profile: TrajectoryIntegrityProfile,
    profile_file_sha256: str,
    trajectory_gate_path: Path,
) -> tuple[dict[str, object], ...]:
    """Run the frozen detector on deterministic public clip positions."""

    source = tuple(samples)
    gate = load_trajectory_gate(trajectory_gate_path)
    reports: list[dict[str, object]] = []
    blocking = set(checkpoint.public_review.blocking_severities)
    for position, start, end in _clip_bounds(
        len(source), checkpoint.public_review.clip_length_samples
    ):
        clip = source[start:end]
        identity = {
            "sequence_id": sequence.sequence_id,
            "trajectory_source_sha256": source_file_sha256,
            "start_sample_index": start,
            "end_sample_index_exclusive": end,
            "first_time_ns": clip[0].time.value_ns,
            "last_time_ns": clip[-1].time.value_ns,
        }
        clip_sha256 = _canonical_sha256(identity)
        detector_report = detect_trajectory_integrity(
            clip,
            source_sha256=clip_sha256,
            partition="development",
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            trajectory_parameters=gate.parameters,
        )
        candidates = [
            event.model_dump(mode="json")
            for event in detector_report.events
            if event.severity.value in blocking
        ]
        reports.append(
            {
                "clip_id": f"{sequence.sequence_id}:{position}",
                "clip_position": position,
                "clip_identity_sha256": clip_sha256,
                **identity,
                "sample_count": len(clip),
                "structural_valid": detector_report.structural_valid,
                "event_count": len(detector_report.events),
                "events": [
                    event.model_dump(mode="json") for event in detector_report.events
                ],
                "false_critical_review": {
                    "candidate_count": len(candidates),
                    "unresolved_count": len(candidates),
                    "candidates": candidates,
                    "disposition": (
                        "EMPTY_NO_CRITICAL_OR_BLOCKING_FINDING"
                        if not candidates
                        else "UNRESOLVED_REQUIRES_ADJUDICATION"
                    ),
                },
            }
        )
    return tuple(reports)


def qualify_temporal_checkpoint(
    *,
    checkpoint_path: Path,
    public_data_root: Path,
    profile_path: Path,
    trajectory_gate_path: Path,
    split_manifest_path: Path,
    fault_matrix_path: Path,
    numerical_charter_path: Path,
    charter_revisions_path: Path,
    data_manifest_path: Path,
    source_groups_path: Path,
) -> dict[str, object]:
    """Run every frozen M3 gate and review real public development clips."""

    checkpoint, checkpoint_file_sha256 = load_temporal_checkpoint(checkpoint_path)
    data_manifest, source_groups = _authenticate_authorities(
        checkpoint,
        profile_path=profile_path,
        trajectory_gate_path=trajectory_gate_path,
        split_manifest_path=split_manifest_path,
        fault_matrix_path=fault_matrix_path,
        numerical_charter_path=numerical_charter_path,
        charter_revisions_path=charter_revisions_path,
        data_manifest_path=data_manifest_path,
        source_groups_path=source_groups_path,
    )
    profile, profile_file_sha256 = load_trajectory_integrity_profile(profile_path)
    m3_1 = qualify_reference_trajectory(trajectory_gate_path)
    m3_2 = qualify_trajectory_integrity(
        profile_path=profile_path,
        trajectory_gate_path=trajectory_gate_path,
        split_manifest_path=split_manifest_path,
        fault_matrix_path=fault_matrix_path,
        charter_path=numerical_charter_path,
    )
    partition_reports = {
        cast(str, item["partition"]): item
        for item in cast(list[dict[str, Any]], m3_2["partitions"])
    }
    development = partition_reports.get("development")
    calibration = partition_reports.get("threshold_calibration")
    if development is None or calibration is None:
        raise ValueError("M3.2 qualification did not return both frozen partitions")

    public_clips: list[dict[str, object]] = []
    for sequence in checkpoint.public_review.sequences:
        _validate_public_sequence_authority(
            sequence,
            public_review=checkpoint.public_review,
            data_manifest=data_manifest,
            source_groups=source_groups,
        )
        sequence_root = public_data_root / sequence.sequence_id
        trajectory_path = sequence_root / sequence.trajectory_source_key
        observed_source_sha256 = _file_sha256(trajectory_path)
        if observed_source_sha256 != sequence.trajectory_source_sha256:
            raise ValueError(
                f"public trajectory {sequence.sequence_id} failed content verification"
            )
        adapter = BoreasAdapter(
            sequence_root,
            source_group_id=checkpoint.public_review.source_group_id,
        )
        samples = reference_samples_from_postprocessed(adapter.pose_samples())
        public_clips.extend(
            review_public_trajectory_clips(
                samples,
                sequence=sequence,
                source_file_sha256=observed_source_sha256,
                checkpoint=checkpoint,
                profile=profile,
                profile_file_sha256=profile_file_sha256,
                trajectory_gate_path=trajectory_gate_path,
            )
        )

    unresolved = sum(
        cast(
            int,
            cast(dict[str, object], clip["false_critical_review"])["unresolved_count"],
        )
        for clip in public_clips
    )
    public_gate = (
        len(public_clips) >= checkpoint.public_review.minimum_clip_count
        and unresolved
        <= checkpoint.public_review.maximum_unresolved_false_critical_findings
    )
    m3_1_gate = m3_1.get("accepted") is True
    m3_2_gate = (
        m3_2.get("accepted") is True
        and development.get("engineering_gate_passed") is True
        and development.get("result_claim_status")
        == checkpoint.synthetic_gate.required_m3_2_development_claim_status
        and calibration.get("engineering_gate_passed") is True
    )
    accepted = m3_1_gate and m3_2_gate and public_gate
    unresolved_blockers = (
        int(not m3_1_gate)
        + int(not m3_2_gate)
        + int(len(public_clips) < checkpoint.public_review.minimum_clip_count)
        + unresolved
    )
    return {
        "schema_version": "cartosentry.temporal-checkpoint-report.v1",
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_version": checkpoint.checkpoint_version,
        "accepted": accepted,
        "claim_scope": checkpoint.claim_scope,
        "threshold_change_status": "UNCHANGED_FROZEN_PROFILE",
        "hashes": {
            **checkpoint.authorities.model_dump(mode="json"),
            "checkpoint_immutable_sha256": checkpoint.immutable_sha256,
            "checkpoint_file_sha256": checkpoint_file_sha256,
        },
        "threshold_change_procedure": checkpoint.threshold_change_procedure.model_dump(
            mode="json"
        ),
        "gates": {
            "m3_1_all_frozen_gates_passed": m3_1_gate,
            "m3_2_development_and_calibration_guard_passed": m3_2_gate,
            "public_false_critical_review_passed": public_gate,
            "unresolved_blocking_detector_defect_count": unresolved_blockers,
        },
        "m3_1_qualification": m3_1,
        "m3_2_qualification": m3_2,
        "public_review": {
            "partition": checkpoint.public_review.partition,
            "source_group_id": checkpoint.public_review.source_group_id,
            "clip_count": len(public_clips),
            "reviewed_duration_ns": sum(
                cast(int, clip["last_time_ns"]) - cast(int, clip["first_time_ns"])
                for clip in public_clips
            ),
            "unresolved_false_critical_finding_count": unresolved,
            "review_conclusion": (
                "No CRITICAL or BLOCKING_ANALYSIS finding was emitted on the "
                "frozen public development clips."
                if unresolved == 0
                else "At least one CRITICAL or BLOCKING_ANALYSIS finding requires "
                "independent adjudication."
            ),
            "warning_findings_are_not_adjudicated_as_false_critical": True,
            "clips": public_clips,
        },
    }


__all__ = [
    "CHECKPOINT_IMMUTABLE_SHA256",
    "PublicSequenceContract",
    "TemporalCheckpointContract",
    "load_temporal_checkpoint",
    "qualify_temporal_checkpoint",
    "review_public_trajectory_clips",
]
