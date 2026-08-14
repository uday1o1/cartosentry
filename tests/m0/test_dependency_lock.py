"""Mutation tests for the reproducible dependency contract."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from check_dependency_lock import (  # noqa: E402
    DependencyLockError,
    validate_dependency_contract,
)


class DependencyLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lock_path = REPOSITORY_ROOT / "cmake" / "dependencies.lock.json"
        self.pyproject_path = REPOSITORY_ROOT / "pyproject.toml"
        self.dockerfile_path = REPOSITORY_ROOT / "docker" / "linux-x86_64.Dockerfile"
        self.lock = json.loads(self.lock_path.read_text(encoding="utf-8"))

    def _validate_mutation(self, lock: object) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / "dependencies.lock.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            validate_dependency_contract(
                lock_path, self.pyproject_path, self.dockerfile_path
            )

    def test_repository_dependency_contract_is_valid(self) -> None:
        result = validate_dependency_contract(
            self.lock_path, self.pyproject_path, self.dockerfile_path
        )
        self.assertEqual(19, len(result["dependencies"]))

    def test_truncated_git_identity_is_rejected(self) -> None:
        lock = copy.deepcopy(self.lock)
        lock["dependencies"]["eigen"]["source_commit"] = "3147391"
        with self.assertRaisesRegex(DependencyLockError, "full Git commit"):
            self._validate_mutation(lock)

    def test_archive_hash_mutation_is_rejected(self) -> None:
        lock = copy.deepcopy(self.lock)
        lock["dependencies"]["opencv"]["archive_sha256"] = "not-a-hash"
        with self.assertRaisesRegex(DependencyLockError, "must be SHA-256"):
            self._validate_mutation(lock)

    def test_partial_archive_identity_is_rejected(self) -> None:
        lock = copy.deepcopy(self.lock)
        del lock["dependencies"]["fmt"]["archive_bytes"]
        with self.assertRaisesRegex(DependencyLockError, "complete archive identity"):
            self._validate_mutation(lock)


if __name__ == "__main__":
    unittest.main()
