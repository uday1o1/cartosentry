"""Contract tests for public-data provenance and retrieval safety."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from download_public_data import normalize_overpass_osm_base  # noqa: E402
from public_data_manifest import (  # noqa: E402
    ManifestError,
    checked_relative_path,
    load_json_yaml,
    validate_contract,
)


class PublicDataContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest_path = REPOSITORY_ROOT / "benchmarks/data_manifest.yaml"
        self.groups_path = REPOSITORY_ROOT / "benchmarks/source_groups.yaml"
        self.manifest = load_json_yaml(self.manifest_path)
        self.groups = load_json_yaml(self.groups_path)

    def _validate_mutation(self, manifest: object, groups: object) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path = root / "manifest.json"
            groups_path = root / "groups.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            groups_path.write_text(json.dumps(groups), encoding="utf-8")
            validate_contract(manifest_path, groups_path)

    def test_repository_contract_is_valid(self) -> None:
        manifest = validate_contract(self.manifest_path, self.groups_path)
        self.assertEqual(4, len(manifest["artifacts"]))

    def test_missing_attribution_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        del manifest["artifacts"][0]["attribution"]
        with self.assertRaisesRegex(ManifestError, "missing fields"):
            self._validate_mutation(manifest, self.groups)

    def test_partition_disagreement_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["artifacts"][0]["partition"] = "final_test"
        with self.assertRaisesRegex(ManifestError, "partition disagrees"):
            self._validate_mutation(manifest, self.groups)

    def test_sequence_in_multiple_groups_is_rejected(self) -> None:
        groups = copy.deepcopy(self.groups)
        duplicate = copy.deepcopy(groups["source_groups"][0]["sequences"][0])
        groups["source_groups"][1]["sequences"].append(duplicate)
        with self.assertRaisesRegex(ManifestError, "multiple source groups"):
            self._validate_mutation(self.manifest, groups)

    def test_tier_over_budget_is_rejected(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["tiers"]["public-smoke"]["maximum_bytes"] = 1
        with self.assertRaisesRegex(ManifestError, "above its"):
            self._validate_mutation(manifest, self.groups)

    def test_unsafe_destination_is_rejected(self) -> None:
        with self.assertRaisesRegex(ManifestError, "unsafe manifest path"):
            checked_relative_path(Path("/tmp/data"), "../credential")

    def test_overpass_normalization_changes_only_replication_base(self) -> None:
        payload = (
            b'<osm version="0.6"><meta osm_base="2026-08-14T01:05:05Z"/>'
            b'<node id="1"/></osm>'
        )
        normalized = normalize_overpass_osm_base(payload, "2026-08-14T00:47:44Z")
        self.assertEqual(
            b'<osm version="0.6"><meta osm_base="2026-08-14T00:47:44Z"/>'
            b'<node id="1"/></osm>',
            normalized,
        )

    def test_overpass_response_older_than_snapshot_is_rejected(self) -> None:
        payload = b'<osm><meta osm_base="2026-08-13T23:00:00Z"/></osm>'
        with self.assertRaisesRegex(ManifestError, "predates requested snapshot"):
            normalize_overpass_osm_base(payload, "2026-08-14T00:47:44Z")


if __name__ == "__main__":
    unittest.main()
