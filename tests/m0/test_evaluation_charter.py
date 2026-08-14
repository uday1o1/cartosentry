"""Tests for the frozen evaluation charter and unblinding gate."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from evaluation_charter import (  # noqa: E402
    AUTHORIZATION_TEXT,
    CONFIRMATION_TEXT,
    FROZEN_PATHS,
    CharterError,
    _load_mapping,
    _unblind,
    _validate_source_assignments,
    clustered_bootstrap_indices,
    derive_fault_id,
    ensure_operator_allowed,
    select_partition,
    validate_contract,
)


class EvaluationCharterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.split = _load_mapping(REPOSITORY_ROOT / "benchmarks/split_manifest.yaml")
        self.groups = _load_mapping(REPOSITORY_ROOT / "benchmarks/source_groups.yaml")
        self.data = _load_mapping(REPOSITORY_ROOT / "benchmarks/data_manifest.yaml")
        self.matrix = _load_mapping(REPOSITORY_ROOT / "benchmarks/fault_matrix_v1.yaml")

    def test_repository_contract_is_valid_and_deterministic(self) -> None:
        first = validate_contract(REPOSITORY_ROOT)
        second = validate_contract(REPOSITORY_ROOT)
        self.assertEqual(first, second)
        self.assertEqual("VALID", first["state"])
        self.assertEqual(42, first["sealed_final_synthetic_family_count"])

    def test_derivative_cannot_move_source_partition(self) -> None:
        data = copy.deepcopy(self.data)
        data["artifacts"][0]["partition"] = "final_test"
        with self.assertRaisesRegex(CharterError, "inherit its source partition"):
            _validate_source_assignments(self.split, self.groups, data)

    def test_source_group_membership_cannot_change(self) -> None:
        split = copy.deepcopy(self.split)
        split["real_source_groups"][0]["sequence_ids"].pop()
        with self.assertRaisesRegex(CharterError, "sequence membership changed"):
            _validate_source_assignments(split, self.groups, self.data)

    def test_final_partition_is_not_available_to_ordinary_selection(self) -> None:
        with self.assertRaisesRegex(CharterError, "final_test is sealed"):
            select_partition(self.split, "final_test")

    def test_allowed_fault_identifier_is_repeatable(self) -> None:
        arguments = {
            "operator_id": "lidar.point_time_shift",
            "case_id": "point-time-100ms-detectable",
            "source_family_id": "sensor-map-dev-001",
            "source_identity_sha256": "0" * 64,
        }
        first = derive_fault_id(self.matrix, **arguments)
        second = derive_fault_id(self.matrix, **arguments)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^fault-sha256-[0-9a-f]{64}$")

    def test_operator_outside_exact_v1_allowlist_is_rejected(self) -> None:
        for operator in (
            "map.geometry_warp",
            "map.direction_flip",
            "routing.connectivity_break",
            "camera.frame_drop",
            "lidar.intensity_shift",
        ):
            with (
                self.subTest(operator=operator),
                self.assertRaisesRegex(CharterError, "outside"),
            ):
                ensure_operator_allowed(self.matrix, operator)

    def test_clustered_bootstrap_samples_whole_cluster_indices(self) -> None:
        first = clustered_bootstrap_indices(5, replicates=10, seed=17)
        second = clustered_bootstrap_indices(5, replicates=10, seed=17)
        self.assertEqual(first, second)
        self.assertEqual(10, len(first))
        self.assertTrue(all(len(replicate) == 5 for replicate in first))
        self.assertTrue(
            all(0 <= cluster < 5 for replicate in first for cluster in replicate)
        )

    def test_public_cli_validates_and_refuses_final_selection(self) -> None:
        validation = subprocess.run(
            [sys.executable, "scripts/evaluation_charter.py", "validate"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, validation.returncode, validation.stderr)
        self.assertEqual("VALID", json.loads(validation.stdout)["state"])
        selection = subprocess.run(
            [
                sys.executable,
                "scripts/evaluation_charter.py",
                "select",
                "--partition",
                "final_test",
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, selection.returncode)
        self.assertIn("final_test is sealed", selection.stderr)
        self.assertEqual("", selection.stdout)

    def test_unblinding_records_bound_event_for_clean_exact_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            root = workspace / "repository"
            benchmark_directory = root / "benchmarks"
            benchmark_directory.mkdir(parents=True)
            for relative_path in (
                Path("benchmarks/source_groups.yaml"),
                Path("benchmarks/data_manifest.yaml"),
                *FROZEN_PATHS.values(),
            ):
                shutil.copy2(
                    REPOSITORY_ROOT / relative_path,
                    root / relative_path,
                )
            self._git(root, "init", "--quiet")
            self._git(root, "add", "benchmarks")
            self._git(
                root,
                "-c",
                "user.name=CartoSentry Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "test fixture",
            )
            report = validate_contract(root)
            commit = self._git(root, "rev-parse", "HEAD").stdout.strip()
            external = workspace / "external"
            external.mkdir()
            authorization_path = external / "AUTHORIZATION.txt"
            authorization_path.write_text(AUTHORIZATION_TEXT + "\n", encoding="utf-8")
            binding_path = external / "RELEASE_BINDING.json"
            binding_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state": "BOUND",
                        "release_tag": "test-v1",
                        "git_commit": commit,
                        "frozen_file_sha256": report["file_sha256"],
                    }
                ),
                encoding="utf-8",
            )
            event_path = external / "UNBLINDING_EVENT.json"
            event = _unblind(
                argparse.Namespace(
                    root=root,
                    release_binding=binding_path,
                    authorization_file=authorization_path,
                    event_log=event_path,
                    confirmation=CONFIRMATION_TEXT,
                )
            )
            self.assertEqual("UNBLINDED", event["state"])
            self.assertEqual(commit, event["git_commit"])
            self.assertEqual(42, len(event["final_selection"]["synthetic"]))
            self.assertEqual(event, json.loads(event_path.read_text(encoding="utf-8")))
            with self.assertRaisesRegex(CharterError, "already exists"):
                _unblind(
                    argparse.Namespace(
                        root=root,
                        release_binding=binding_path,
                        authorization_file=authorization_path,
                        event_log=event_path,
                        confirmation=CONFIRMATION_TEXT,
                    )
                )

    def _git(self, root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
