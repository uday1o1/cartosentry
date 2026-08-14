"""Public-path and invariant tests for durable run recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from cartosentry.artifacts import StageState
from cartosentry.cli import app
from cartosentry.recovery import (
    demo_run_inputs,
    demo_stage_definitions,
    resume_registered_run,
    run_demo_pipeline,
)
from cartosentry.runstate import (
    ArtifactIntegrityError,
    ArtifactPayload,
    RunEngine,
    build_stage_cache_key,
    parse_run_inputs_bytes,
)
from typer.testing import CliRunner


@pytest.fixture(scope="module")
def qualified_recovery(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, dict[str, object]]:
    output = tmp_path_factory.mktemp("run-recovery") / "evidence"
    result = CliRunner().invoke(app, ["qualify-run-recovery", str(output)])
    assert result.exit_code == 0, result.output
    return output, json.loads(result.stdout)


def test_public_qualification_kills_and_resumes_every_boundary(
    qualified_recovery: tuple[Path, dict[str, object]],
) -> None:
    output, report = qualified_recovery
    assert report["accepted"] is True
    assert report["interruption_case_count"] == 6
    cases = report["crash_cases"]
    assert isinstance(cases, list)
    assert {item["boundary"] for item in cases} == {
        "after-db-running",
        "after-outputs-fsynced",
        "after-manifest-fsynced",
        "after-artifact-published",
        "after-pointer-published",
        "after-db-committed",
    }
    assert all(item["process_exit_code"] == 86 for item in cases)
    assert all(item["matches_uninterrupted"] for item in cases)
    assert report["stale_pointer_repaired"] is True
    assert report["orphaned_publish_adopted"] is True
    assert report["missing_artifact_state"] == "FAILED_FINAL"
    assert report["missing_artifact_reconciliation_stable"] is True
    assert report["forced_scope"] == ["detect", "policy", "report"]
    assert report["force_scope_exact"] is True
    assert report["dry_run_preserved_attempts"] is True
    assert report["dry_run_preserved_control_state"] is True
    assert report["force_attempt_counts_exact"] is True
    assert all(report["cache_contract_checks"].values())
    assert (output / "qualification.json").is_file()
    database = sqlite3.connect(output / "uninterrupted" / "run.sqlite3")
    try:
        assert database.execute("PRAGMA quick_check").fetchone() == ("ok",)
    finally:
        database.close()


def test_public_resume_shows_then_executes_exact_force_closure(
    qualified_recovery: tuple[Path, dict[str, object]],
) -> None:
    output, qualification = qualified_recovery
    run_root = output / "uninterrupted"
    runner = CliRunner()
    run_artifact_before = (run_root / "run.json").read_bytes()
    pointers_before = {
        path.name: path.read_bytes()
        for path in sorted((run_root / "completions").iterdir())
    }
    preview = runner.invoke(
        app,
        ["resume-run", str(run_root), "--force-stage", "detect", "--dry-run"],
    )
    assert preview.exit_code == 0, preview.output
    preview_report = json.loads(preview.stdout)
    assert preview_report["forced_scope"] == ["detect", "policy", "report"]
    assert (run_root / "run.json").read_bytes() == run_artifact_before
    assert {
        path.name: path.read_bytes()
        for path in sorted((run_root / "completions").iterdir())
    } == pointers_before
    resumed = runner.invoke(
        app,
        ["resume-run", str(run_root), "--force-stage", "detect"],
    )
    assert resumed.exit_code == 0, resumed.output
    assert "Force-stage scope: detect, policy, report" in resumed.stderr
    resumed_report = json.loads(resumed.stdout)
    assert resumed_report["complete"] is True
    assert resumed_report["forced_scope"] == ["detect", "policy", "report"]
    assert (
        resumed_report["semantic_sha256"]
        == qualification["uninterrupted_semantic_sha256"]
    )


def test_stage_start_requires_complete_upstream(tmp_path: Path) -> None:
    root = tmp_path / "upstream-gate"
    with RunEngine.create(root, demo_run_inputs(), demo_stage_definitions()) as engine:
        with pytest.raises(ValueError, match="already active"):
            RunEngine.open(root, demo_stage_definitions())
        detect = demo_stage_definitions()[1]
        with pytest.raises(ValueError, match="invalid stage transition"):
            engine.database.mark_state(
                "normalize",
                StageState.COMPLETE,
                error_code=None,
            )
        with pytest.raises(ValueError, match="dependency completes"):
            engine.database.begin_attempt(
                detect.stage_id,
                "a" * 64,
            )


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":"cartosentry.run-inputs.v1",'
        b'"schema_version":"cartosentry.run-inputs.v1"}',
        b"[" * 65 + b"0" + b"]" * 65,
        b'{"schema_version":NaN}',
        b" " * (1024 * 1024 + 1),
    ],
)
def test_run_control_parser_rejects_unsafe_json(content: bytes) -> None:
    with pytest.raises(ArtifactIntegrityError, match="control artifact"):
        parse_run_inputs_bytes(content)


def test_invalid_stage_schema_never_publishes_completion(tmp_path: Path) -> None:
    root = tmp_path / "invalid-stage-output"
    definitions = demo_stage_definitions()
    invalid_normalize = replace(
        definitions[0],
        execute=lambda _: (
            ArtifactPayload(
                artifact_key="normalized",
                relative_path="normalized.json",
                content=b"{}\n",
            ),
        ),
    )
    with RunEngine.create(
        root,
        demo_run_inputs(),
        (invalid_normalize, *definitions[1:]),
    ) as engine:
        with pytest.raises(ValueError, match="schema does not match"):
            engine.run()
        assert (
            engine.database.stage_row("normalize").state is StageState.FAILED_RETRYABLE
        )
        assert not (root / "completions" / "normalize.json").exists()


def test_cache_key_binds_semantic_inputs_only() -> None:
    base = {
        "workflow_id": "cache-contract-v1",
        "stage_id": "detect",
        "source_hashes": {"source": hashlib.sha256(b"source").hexdigest()},
        "upstream_artifact_hashes": {
            "normalize:manifest": hashlib.sha256(b"manifest").hexdigest()
        },
        "relevant_configuration_hashes": {
            "thresholds": hashlib.sha256(b"thresholds").hexdigest()
        },
        "algorithm_version": "detect-v1",
        "numerical_backend": "cpu-reference",
    }
    reference = build_stage_cache_key(**base)
    assert reference == build_stage_cache_key(**base)
    for key, changed in (
        ("source_hashes", {"source": "1" * 64}),
        ("upstream_artifact_hashes", {"normalize:manifest": "2" * 64}),
        ("relevant_configuration_hashes", {"thresholds": "3" * 64}),
        ("algorithm_version", "detect-v2"),
        ("numerical_backend", "cpu-different"),
    ):
        assert build_stage_cache_key(**{**base, key: changed}) != reference


def test_run_input_change_cannot_reuse_existing_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "immutable-inputs"
    run_demo_pipeline(root)
    inputs_path = root / "run-inputs.json"
    inputs = json.loads(inputs_path.read_text())
    inputs["source_hashes"]["fixture-source"] = "f" * 64
    inputs_path.write_text(json.dumps(inputs))
    with pytest.raises(ValueError, match=r"sequence_id|identity"):
        resume_registered_run(root)


@pytest.mark.parametrize("mutation", ["duplicate", "oversized"])
def test_run_control_manifest_boundary_is_bounded_and_duplicate_safe(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = tmp_path / mutation
    run_demo_pipeline(root)
    inputs_path = root / "run-inputs.json"
    if mutation == "duplicate":
        content = inputs_path.read_bytes()
        inputs_path.write_bytes(
            b'{"workflow_id":"resumable-stage-qualification-v1",' + content[1:]
        )
    else:
        inputs_path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ArtifactIntegrityError):
        resume_registered_run(root)
