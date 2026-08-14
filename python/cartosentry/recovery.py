"""Repository-owned qualification workflow for crash-safe stage recovery."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field

from cartosentry.artifacts import StageState
from cartosentry.contracts import ContractModel, Sha256
from cartosentry.identifiers import (
    assert_portable,
    canonical_json_bytes,
    make_road_graph_id,
    make_sequence_id,
)
from cartosentry.runstate import (
    ArtifactPayload,
    CommitBoundary,
    CompletionPointer,
    RunEngine,
    RunExecutionReport,
    RunInputs,
    StageAction,
    StageDefinition,
    StageExecutionContext,
    build_stage_cache_key,
    load_run_inputs,
)

DEMO_WORKFLOW_ID = "resumable-stage-qualification-v1"
EXPECTED_CRASH_EXIT_CODE = 86


class CrashRecoveryCase(ContractModel):
    boundary: CommitBoundary
    process_exit_code: int
    resumed_complete: bool
    semantic_sha256: Sha256
    matches_uninterrupted: bool
    recovery_actions: tuple[StageAction, ...]


class RunRecoveryQualification(ContractModel):
    schema_version: Literal["cartosentry.run-recovery-qualification.v1"]
    qualification_id: Literal["m2.4-v1"]
    accepted: bool
    uninterrupted_semantic_sha256: Sha256
    crash_cases: tuple[CrashRecoveryCase, ...]
    stale_pointer_repaired: bool
    orphaned_publish_adopted: bool
    missing_artifact_state: StageState
    missing_artifact_reconciliation_stable: bool
    forced_stage: Literal["detect"]
    forced_scope: tuple[str, ...]
    force_scope_exact: bool
    forced_semantic_sha256: Sha256
    force_matches_uninterrupted: bool
    dry_run_preserved_attempts: bool
    dry_run_preserved_control_state: bool
    force_attempt_counts_exact: bool
    cache_contract_checks: dict[str, bool]
    interruption_case_count: int = Field(gt=0)

    def portable_dict(self) -> dict[str, object]:
        value = self.model_dump(mode="json")
        assert_portable(value)
        return value


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def demo_run_inputs() -> RunInputs:
    source_hashes = {
        "fixture-source": _digest("resumable-stage-source-v1"),
        "fixture-calibration": _digest("resumable-stage-calibration-v1"),
    }
    return RunInputs(
        schema_version="cartosentry.run-inputs.v1",
        workflow_id=DEMO_WORKFLOW_ID,
        sequence_id=make_sequence_id({"source_hashes": source_hashes}),
        road_graph_id=make_road_graph_id({"graph": "resumable-stage-graph-v1"}),
        profile_id="structural-preflight-v1",
        engine_version="0.1.0",
        source_hashes=source_hashes,
        configuration_hashes={
            "adapter": _digest("adapter-config-v1"),
            "detector-thresholds": _digest("detector-thresholds-v1"),
            "policy": _digest("policy-config-v1"),
            "report-theme": _digest("report-theme-v1"),
        },
        numerical_backend="cpu-reference",
    )


def _json_payload(
    artifact_key: str,
    relative_path: str,
    value: object,
) -> ArtifactPayload:
    return ArtifactPayload(
        artifact_key=artifact_key,
        relative_path=relative_path,
        content=canonical_json_bytes(value) + b"\n",
    )


def _normalize(context: StageExecutionContext) -> tuple[ArtifactPayload, ...]:
    return (
        _json_payload(
            "normalized",
            "normalized.json",
            {
                "adapter_configuration_sha256": context.inputs.configuration_hashes[
                    "adapter"
                ],
                "source_hashes": dict(sorted(context.inputs.source_hashes.items())),
                "stage": context.stage_id,
            },
        ),
    )


def _detect(context: StageExecutionContext) -> tuple[ArtifactPayload, ...]:
    return (
        _json_payload(
            "findings",
            "findings.json",
            {
                "finding_count": 1,
                "input_hashes": dict(sorted(context.upstream_artifact_hashes.items())),
                "rule": "fixture.timestamp-continuity",
                "stage": context.stage_id,
                "threshold_configuration_sha256": (
                    context.inputs.configuration_hashes["detector-thresholds"]
                ),
            },
        ),
    )


def _policy(context: StageExecutionContext) -> tuple[ArtifactPayload, ...]:
    return (
        _json_payload(
            "verdict",
            "verdict.json",
            {
                "input_hashes": dict(sorted(context.upstream_artifact_hashes.items())),
                "policy_configuration_sha256": context.inputs.configuration_hashes[
                    "policy"
                ],
                "stage": context.stage_id,
                "verdict": "FAIL",
            },
        ),
    )


def _report(context: StageExecutionContext) -> tuple[ArtifactPayload, ...]:
    return (
        _json_payload(
            "report",
            "report.json",
            {
                "input_hashes": dict(sorted(context.upstream_artifact_hashes.items())),
                "stage": context.stage_id,
                "theme_configuration_sha256": context.inputs.configuration_hashes[
                    "report-theme"
                ],
            },
        ),
    )


def _validate_demo_outputs(
    context: StageExecutionContext,
    payloads: Sequence[ArtifactPayload],
) -> None:
    expected_fields = {
        "normalize": {"adapter_configuration_sha256", "source_hashes", "stage"},
        "detect": {
            "finding_count",
            "input_hashes",
            "rule",
            "stage",
            "threshold_configuration_sha256",
        },
        "policy": {
            "input_hashes",
            "policy_configuration_sha256",
            "stage",
            "verdict",
        },
        "report": {"input_hashes", "stage", "theme_configuration_sha256"},
    }
    for payload in payloads:
        if payload.media_type != "application/json":
            raise ValueError("qualification stage outputs must be JSON")
        try:
            value = json.loads(payload.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("qualification stage output is invalid JSON") from error
        if (
            not isinstance(value, dict)
            or set(value) != expected_fields[context.stage_id]
        ):
            raise ValueError("qualification stage output schema does not match")
        if value.get("stage") != context.stage_id:
            raise ValueError("qualification stage output identity does not match")
        assert_portable(value)
        if payload.content != canonical_json_bytes(value) + b"\n":
            raise ValueError("qualification stage output is not canonical JSON")


def demo_stage_definitions() -> tuple[StageDefinition, ...]:
    return (
        StageDefinition(
            stage_id="normalize",
            dependencies=(),
            algorithm_version="normalize-v1",
            relevant_configuration_keys=("adapter",),
            output_keys=("normalized",),
            execute=_normalize,
            validate_outputs=_validate_demo_outputs,
        ),
        StageDefinition(
            stage_id="detect",
            dependencies=("normalize",),
            algorithm_version="detect-v1",
            relevant_configuration_keys=("detector-thresholds",),
            output_keys=("findings",),
            execute=_detect,
            validate_outputs=_validate_demo_outputs,
        ),
        StageDefinition(
            stage_id="policy",
            dependencies=("detect",),
            algorithm_version="policy-v1",
            relevant_configuration_keys=("policy",),
            output_keys=("verdict",),
            execute=_policy,
            validate_outputs=_validate_demo_outputs,
        ),
        StageDefinition(
            stage_id="report",
            dependencies=("policy",),
            algorithm_version="report-v1",
            relevant_configuration_keys=("report-theme",),
            output_keys=("report",),
            execute=_report,
            validate_outputs=_validate_demo_outputs,
        ),
    )


def run_demo_pipeline(
    root: Path,
    *,
    force_stage: str | None = None,
    dry_run: bool = False,
    crash_boundary: CommitBoundary | None = None,
    crash_stage: str = "detect",
) -> RunExecutionReport:
    definitions = demo_stage_definitions()
    if root.exists():
        engine = RunEngine.open(root, definitions)
    else:
        engine = RunEngine.create(root, demo_run_inputs(), definitions)

    def crash(boundary: CommitBoundary, stage_id: str) -> None:
        if boundary is crash_boundary and stage_id == crash_stage:
            os._exit(EXPECTED_CRASH_EXIT_CODE)

    with engine:
        return engine.run(
            force_stage=force_stage,
            dry_run=dry_run,
            crash_callback=crash if crash_boundary is not None else None,
        )


def resume_registered_run(
    root: Path,
    *,
    force_stage: str | None = None,
    dry_run: bool = False,
) -> RunExecutionReport:
    inputs = load_run_inputs(root)
    if inputs.workflow_id != DEMO_WORKFLOW_ID:
        raise ValueError(f"unsupported persisted workflow: {inputs.workflow_id}")
    return run_demo_pipeline(
        root,
        force_stage=force_stage,
        dry_run=dry_run,
    )


def _attempt_counts(root: Path) -> dict[str, int]:
    with RunEngine.open(root, demo_stage_definitions()) as engine:
        return {
            definition.stage_id: engine.database.stage_row(
                definition.stage_id
            ).attempt_counter
            for definition in engine.definitions
        }


def _control_file_hashes(root: Path) -> dict[str, str]:
    paths = [root / "run-inputs.json", root / "run.json"]
    paths.extend(sorted((root / "completions").glob("*.json")))
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def _completion_pointer(root: Path, stage_id: str) -> CompletionPointer:
    return CompletionPointer.model_validate(
        json.loads((root / "completions" / f"{stage_id}.json").read_bytes())
    )


def _crash_worker(root: Path, boundary: CommitBoundary) -> int:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "cartosentry.runstate_worker",
            "--root",
            str(root),
            "--stage",
            "detect",
            "--boundary",
            boundary.value,
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != EXPECTED_CRASH_EXIT_CODE:
        raise RuntimeError(
            "recovery worker did not terminate at the requested commit boundary"
        )
    return completed.returncode


def _cache_contract_checks() -> dict[str, bool]:
    inputs = demo_run_inputs()
    detect = demo_stage_definitions()[1]
    upstream = {"normalize:normalized": _digest("normalized-output")}
    relevant = {
        "detector-thresholds": inputs.configuration_hashes["detector-thresholds"]
    }

    def key(
        *,
        source_hashes: dict[str, str] | None = None,
        configuration_hashes: dict[str, str] | None = None,
        numerical_backend: str | None = None,
    ) -> str:
        return build_stage_cache_key(
            workflow_id=inputs.workflow_id,
            stage_id=detect.stage_id,
            source_hashes=source_hashes or inputs.source_hashes,
            upstream_artifact_hashes=upstream,
            relevant_configuration_hashes=configuration_hashes or relevant,
            algorithm_version=detect.algorithm_version,
            numerical_backend=numerical_backend or inputs.numerical_backend,
        )

    detect_key = key()
    theme_key = key()
    threshold_key = key(
        configuration_hashes={"detector-thresholds": _digest("different-thresholds")}
    )
    backend_key = key(numerical_backend="different-backend")
    source_key = key(
        source_hashes={
            **inputs.source_hashes,
            "fixture-source": _digest("different-source"),
        }
    )
    upstream_key = build_stage_cache_key(
        workflow_id=inputs.workflow_id,
        stage_id=detect.stage_id,
        source_hashes=inputs.source_hashes,
        upstream_artifact_hashes={
            "normalize:normalized": _digest("different-normalized-output")
        },
        relevant_configuration_hashes=relevant,
        algorithm_version=detect.algorithm_version,
        numerical_backend=inputs.numerical_backend,
    )
    normalize = demo_stage_definitions()[0]
    normalize_reference = build_stage_cache_key(
        workflow_id=inputs.workflow_id,
        stage_id=normalize.stage_id,
        source_hashes=inputs.source_hashes,
        upstream_artifact_hashes={},
        relevant_configuration_hashes={
            "adapter": inputs.configuration_hashes["adapter"]
        },
        algorithm_version=normalize.algorithm_version,
        numerical_backend=inputs.numerical_backend,
    )
    normalize_changed = build_stage_cache_key(
        workflow_id=inputs.workflow_id,
        stage_id=normalize.stage_id,
        source_hashes=inputs.source_hashes,
        upstream_artifact_hashes={},
        relevant_configuration_hashes={"adapter": _digest("different-adapter")},
        algorithm_version=normalize.algorithm_version,
        numerical_backend=inputs.numerical_backend,
    )
    return {
        "cache_key_is_sha256": len(detect_key) == 64,
        "report_theme_does_not_invalidate_detection": theme_key == detect_key,
        "thresholds_invalidate_detection": threshold_key != detect_key,
        "backend_invalidates_detection": backend_key != detect_key,
        "source_invalidates_detection": source_key != detect_key,
        "upstream_invalidates_detection": upstream_key != detect_key,
        "adapter_invalidates_normalization": normalize_changed != normalize_reference,
    }


def qualify_run_recovery(output_root: Path) -> RunRecoveryQualification:
    """Kill real worker processes at every commit boundary and resume them."""

    if output_root.exists():
        raise ValueError("run recovery qualification output must not already exist")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    output_root.mkdir()

    clean_root = output_root / "uninterrupted"
    clean = run_demo_pipeline(clean_root)
    if not clean.complete or clean.semantic_sha256 is None:
        raise RuntimeError("uninterrupted qualification run did not complete")
    uninterrupted = clean.semantic_sha256

    crash_cases: list[CrashRecoveryCase] = []
    orphaned_publish_adopted = False
    for boundary in CommitBoundary:
        case_root = output_root / f"crash-{boundary.value}"
        return_code = _crash_worker(case_root, boundary)
        resumed = resume_registered_run(case_root)
        if resumed.semantic_sha256 is None:
            raise RuntimeError("resumed qualification run did not complete")
        recovery_actions = tuple(item.action for item in resumed.actions)
        if boundary in {
            CommitBoundary.AFTER_ARTIFACT_PUBLISHED,
            CommitBoundary.AFTER_POINTER_PUBLISHED,
        }:
            orphaned_publish_adopted = orphaned_publish_adopted or any(
                action in {StageAction.ADOPTED_PUBLISHED, StageAction.REPAIRED_POINTER}
                for action in recovery_actions
            )
        crash_cases.append(
            CrashRecoveryCase(
                boundary=boundary,
                process_exit_code=return_code,
                resumed_complete=resumed.complete,
                semantic_sha256=resumed.semantic_sha256,
                matches_uninterrupted=resumed.semantic_sha256 == uninterrupted,
                recovery_actions=recovery_actions,
            )
        )

    pointer_root = output_root / "stale-pointer"
    pointer_clean = run_demo_pipeline(pointer_root)
    pointer_path = pointer_root / "completions" / "policy.json"
    pointer_path.unlink()
    directory_descriptor = os.open(pointer_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    pointer_resumed = resume_registered_run(pointer_root)
    stale_pointer_repaired = (
        pointer_resumed.semantic_sha256 == pointer_clean.semantic_sha256
        and pointer_path.is_file()
        and any(
            item.stage_id == "policy" and item.action is StageAction.REPAIRED_POINTER
            for item in pointer_resumed.actions
        )
    )

    missing_root = output_root / "missing-artifact"
    run_demo_pipeline(missing_root)
    pointer = _completion_pointer(missing_root, "policy")
    artifact_directory = missing_root / pointer.artifact_directory
    verdict = artifact_directory / "verdict.json"
    verdict.unlink()
    first_missing = resume_registered_run(missing_root)
    second_missing = resume_registered_run(missing_root)
    missing_state = first_missing.stage_states["policy"]
    missing_stable = (
        missing_state is StageState.FAILED_FINAL
        and second_missing.stage_states["policy"] is StageState.FAILED_FINAL
        and first_missing.semantic_sha256 is None
        and second_missing.semantic_sha256 is None
    )

    force_root = output_root / "forced"
    force_clean = run_demo_pipeline(force_root)
    counts_before = _attempt_counts(force_root)
    controls_before = _control_file_hashes(force_root)
    dry_run = resume_registered_run(
        force_root,
        force_stage="detect",
        dry_run=True,
    )
    counts_after_dry_run = _attempt_counts(force_root)
    controls_after_dry_run = _control_file_hashes(force_root)
    forced = resume_registered_run(force_root, force_stage="detect")
    expected_scope = ("detect", "policy", "report")
    force_scope_exact = forced.forced_scope == expected_scope
    if forced.semantic_sha256 is None:
        raise RuntimeError("forced qualification run did not complete")
    counts_after_force = _attempt_counts(force_root)
    dry_run_preserved_attempts = counts_before == counts_after_dry_run
    dry_run_preserved_control_state = controls_before == controls_after_dry_run
    force_attempt_counts_exact = counts_after_force["normalize"] == counts_before[
        "normalize"
    ] and all(
        counts_after_force[stage_id] == counts_before[stage_id] + 1
        for stage_id in expected_scope
    )
    dry_run_and_force_exact = (
        dry_run.forced_scope == expected_scope
        and dry_run_preserved_attempts
        and dry_run_preserved_control_state
        and force_attempt_counts_exact
    )

    cache_checks = _cache_contract_checks()
    accepted = (
        all(
            item.process_exit_code == EXPECTED_CRASH_EXIT_CODE
            and item.resumed_complete
            and item.matches_uninterrupted
            for item in crash_cases
        )
        and stale_pointer_repaired
        and orphaned_publish_adopted
        and missing_stable
        and force_scope_exact
        and forced.semantic_sha256 == uninterrupted
        and force_clean.semantic_sha256 == uninterrupted
        and dry_run_and_force_exact
        and all(cache_checks.values())
    )
    qualification = RunRecoveryQualification(
        schema_version="cartosentry.run-recovery-qualification.v1",
        qualification_id="m2.4-v1",
        accepted=accepted,
        uninterrupted_semantic_sha256=uninterrupted,
        crash_cases=tuple(crash_cases),
        stale_pointer_repaired=stale_pointer_repaired,
        orphaned_publish_adopted=orphaned_publish_adopted,
        missing_artifact_state=missing_state,
        missing_artifact_reconciliation_stable=missing_stable,
        forced_stage="detect",
        forced_scope=forced.forced_scope,
        force_scope_exact=force_scope_exact,
        forced_semantic_sha256=forced.semantic_sha256,
        force_matches_uninterrupted=forced.semantic_sha256 == uninterrupted,
        dry_run_preserved_attempts=dry_run_preserved_attempts,
        dry_run_preserved_control_state=dry_run_preserved_control_state,
        force_attempt_counts_exact=force_attempt_counts_exact,
        cache_contract_checks=cache_checks,
        interruption_case_count=len(crash_cases),
    )
    report_path = output_root / "qualification.json"
    serialized = canonical_json_bytes(qualification.portable_dict()) + b"\n"
    with report_path.open("xb") as stream:
        stream.write(serialized)
        stream.flush()
        os.fsync(stream.fileno())
    return qualification


__all__ = [
    "DEMO_WORKFLOW_ID",
    "EXPECTED_CRASH_EXIT_CODE",
    "CrashRecoveryCase",
    "RunRecoveryQualification",
    "demo_run_inputs",
    "demo_stage_definitions",
    "qualify_run_recovery",
    "resume_registered_run",
    "run_demo_pipeline",
]
