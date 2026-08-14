"""Conformance tests for versioned persisted artifacts and identifiers."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cartosentry import _core
from cartosentry.artifacts import (
    ARTIFACT_MODEL_BY_SCHEMA,
    Finding,
    LocalRunContext,
    Run,
    canonicalize_portable_artifact,
    validate_artifact,
    validate_artifact_json,
)
from cartosentry.cli import app
from cartosentry.identifiers import (
    canonical_sha256,
    make_finding_id,
    make_run_id,
    make_sequence_id,
)
from pydantic import ValidationError
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VALID_EXAMPLES = REPOSITORY_ROOT / "schemas/examples/valid"
INVALID_EXAMPLES = REPOSITORY_ROOT / "schemas/examples/invalid"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected object in {path}")
    return value


class ArtifactSchemaTest(unittest.TestCase):
    def test_every_artifact_round_trips_between_python_and_cpp(self) -> None:
        examples = sorted(VALID_EXAMPLES.glob("*.json"))
        self.assertEqual(6, len(examples))
        for path in examples:
            with self.subTest(path=path.name):
                artifact = validate_artifact_json(path.read_text(encoding="utf-8"))
                before = artifact.portable_dict()
                canonical = canonicalize_portable_artifact(artifact)
                after = validate_artifact_json(canonical)
                self.assertEqual(before, after.portable_dict())
                self.assertEqual(
                    canonical,
                    _core.canonicalize_artifact_json(canonical, artifact.schema_name),
                )

    def test_committed_schemas_are_strict_and_reproducible(self) -> None:
        for schema_name, model in ARTIFACT_MODEL_BY_SCHEMA.items():
            with self.subTest(schema=schema_name):
                schema = model.model_json_schema()
                self.assertFalse(schema["additionalProperties"])
                self.assertIn("schema_version", schema["required"])
        completed = subprocess.run(
            [sys.executable, "scripts/generate_artifact_schemas.py", "--check"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_invalid_examples_fail_for_the_intended_reason(self) -> None:
        cases = {
            "sequence-manifest.unknown-field.json": "extra_forbidden",
            "run.missing-required-field.json": "Field required",
            "finding.wrong-unit.json": "units differ",
            "readiness-profile.invalid-enum.json": "camera",
            "accepted-data-bundle.path-leak.json": "local or traversing path",
            "recapture-plan.schema-downgrade.json": "unsupported artifact schema",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                text = (INVALID_EXAMPLES / name).read_text(encoding="utf-8")
                with self.assertRaisesRegex((ValueError, ValidationError), expected):
                    validate_artifact_json(text)

    def test_nested_unknown_fields_and_duplicate_json_keys_are_rejected(self) -> None:
        finding = load_json(VALID_EXAMPLES / "finding.json")
        finding["measurement"]["undocumented"] = True
        with self.assertRaisesRegex(ValidationError, "extra_forbidden"):
            validate_artifact(finding)
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            validate_artifact_json(
                '{"schema_version":"cartosentry.run.v1",'
                '"schema_version":"cartosentry.run.v1"}'
            )

    def test_native_validator_rejects_paths_and_unknown_required_shape(self) -> None:
        run = load_json(VALID_EXAMPLES / "run.json")
        run["profile_id"] = "C:\\recordings\\profile.json"
        with self.assertRaisesRegex(ValueError, "local path"):
            _core.canonicalize_artifact_json(json.dumps(run), "cartosentry.run.v1")
        del run["run_id"]
        with self.assertRaisesRegex(ValueError, "missing required"):
            _core.canonicalize_artifact_json(json.dumps(run), "cartosentry.run.v1")


class IdentifierAndRedactionTest(unittest.TestCase):
    def test_run_identifier_and_export_are_stable_across_local_roots(self) -> None:
        payload = load_json(VALID_EXAMPLES / "run.json")
        left_payload = payload | {
            "local_context": LocalRunContext(
                source_roots=("/Volumes/source-a",),
                host_name="workstation-a",
                machine_id="machine-a",
            ).model_dump(mode="json")
        }
        right_payload = payload | {
            "local_context": LocalRunContext(
                source_roots=("D:\\recordings",),
                host_name="workstation-b",
                machine_id="machine-b",
            ).model_dump(mode="json")
        }
        left = validate_artifact_json(json.dumps(left_payload))
        right = validate_artifact_json(json.dumps(right_payload))
        if not isinstance(left, Run) or not isinstance(right, Run):
            self.fail("run example resolved to the wrong artifact model")
        self.assertEqual(left.run_id, right.run_id)
        self.assertEqual(left.portable_dict(), right.portable_dict())
        portable_text = json.dumps(left.portable_dict())
        self.assertNotIn("Volumes", portable_text)
        self.assertNotIn("workstation", portable_text)
        self.assertNotIn("machine-a", portable_text)

    def test_identifier_hashes_are_canonical_and_reject_local_inputs(self) -> None:
        left = {"source_files": ["b", "a"], "metadata": {"b": 2, "a": 1}}
        right = {"metadata": {"a": 1, "b": 2}, "source_files": ["b", "a"]}
        self.assertEqual(make_sequence_id(left), make_sequence_id(right))
        self.assertEqual(canonical_sha256(left), canonical_sha256(right))
        with self.assertRaisesRegex(ValueError, "local or traversing path"):
            make_sequence_id({"source_key": "/private/source.bin"})

    def test_run_and_finding_identity_sort_unordered_inputs(self) -> None:
        run_arguments = {
            "sequence_id": "sequence-sha256-" + "1" * 64,
            "road_graph_id": "road-graph-sha256-" + "2" * 64,
            "profile_id": "profile-v1",
            "engine_version": "1.0.0",
        }
        self.assertEqual(
            make_run_id(
                **run_arguments,
                configuration_hashes={"z": "3" * 64, "a": "4" * 64},
            ),
            make_run_id(
                **run_arguments,
                configuration_hashes={"a": "4" * 64, "z": "3" * 64},
            ),
        )
        finding = Finding.model_validate_json(
            (VALID_EXAMPLES / "finding.json").read_text(encoding="utf-8")
        )
        interval = finding.interval.model_dump(mode="json")
        evidence = [item.model_dump(mode="json") for item in finding.evidence]
        self.assertEqual(
            finding.finding_id,
            make_finding_id(
                detector_id=finding.detector_id,
                detector_version=finding.detector_version,
                rule_id=finding.rule_id,
                source_interval=interval,
                stream_ids=tuple(reversed(finding.streams)),
                evidence_fingerprint=evidence,
            ),
        )


class ArtifactCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_public_validation_path_accepts_every_valid_artifact(self) -> None:
        for path in sorted(VALID_EXAMPLES.glob("*.json")):
            with self.subTest(path=path.name):
                result = self.runner.invoke(app, ["validate-artifact", str(path)])
                self.assertEqual(0, result.exit_code, result.output)
                report = json.loads(result.stdout)
                self.assertTrue(report["accepted"])
                self.assertEqual(
                    load_json(path)["schema_version"], report["schema_version"]
                )

    def test_public_validation_path_rejects_invalid_artifact(self) -> None:
        path = INVALID_EXAMPLES / "accepted-data-bundle.path-leak.json"
        result = self.runner.invoke(app, ["validate-artifact", str(path)])
        self.assertEqual(2, result.exit_code)
        self.assertIn("path", result.output)

    def test_portable_export_atomically_strips_local_run_context(self) -> None:
        payload = load_json(VALID_EXAMPLES / "run.json")
        payload["local_context"] = {
            "source_roots": ["/private/recordings"],
            "host_name": "private-host",
            "machine_id": "private-machine",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "local-run.json"
            destination = root / "portable-run.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            result = self.runner.invoke(
                app,
                ["export-portable-artifact", str(source), str(destination)],
            )
            self.assertEqual(0, result.exit_code, result.output)
            exported = load_json(destination)
            self.assertNotIn("local_context", exported)
            self.assertEqual(payload["run_id"], exported["run_id"])
            self.assertEqual(
                Run.model_validate_json(json.dumps(exported)).portable_dict(),
                Run.model_validate_json(json.dumps(payload)).portable_dict(),
            )


if __name__ == "__main__":
    unittest.main()
