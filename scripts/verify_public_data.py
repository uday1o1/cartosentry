#!/usr/bin/env python3
"""Validate the public-data contract and optionally verify downloaded payloads."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any, Dict

from public_data_manifest import (
    ManifestError,
    checked_relative_path,
    selected_artifacts,
    sha256_file,
    validate_contract,
    verify_pinned_input,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path("benchmarks/data_manifest.yaml")
    )
    parser.add_argument(
        "--source-groups", type=Path, default=Path("benchmarks/source_groups.yaml")
    )
    parser.add_argument("--input-root", type=Path, default=Path("data/public"))
    parser.add_argument(
        "--tier",
        choices=("public-smoke", "public-full", "gpu-perf"),
        default="public-smoke",
    )
    parser.add_argument("--artifact-id", action="append", default=[])
    parser.add_argument("--manifest-only", action="store_true")
    return parser


def _verify_artifact(
    artifact: Dict[str, Any], input_root: Path, repository_root: Path
) -> int:
    aggregate = hashlib.sha256()
    verified_bytes = 0
    for obj in artifact["objects"]:
        if obj.get("method") == "overpass-post":
            verify_pinned_input(repository_root, obj["query_path"], obj["query_sha256"])
            verify_pinned_input(
                repository_root, obj["polygon_path"], obj["polygon_sha256"]
            )
        path = checked_relative_path(input_root, obj["key"])
        if not path.is_file():
            raise ManifestError(f"missing public data object: {path}")
        actual_size = path.stat().st_size
        if actual_size != obj["bytes"]:
            raise ManifestError(
                f"size mismatch for {obj['key']}: {actual_size} != {obj['bytes']}"
            )
        actual_hash = sha256_file(path)
        if actual_hash != obj["sha256"]:
            raise ManifestError(
                f"hash mismatch for {obj['key']}: {actual_hash} != {obj['sha256']}"
            )
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                aggregate.update(chunk)
        verified_bytes += actual_size
    if aggregate.hexdigest() != artifact["content_sha256"]:
        raise ManifestError(f"aggregate content hash mismatch for {artifact['id']}")
    if verified_bytes != artifact["expected_bytes"]:
        raise ManifestError(f"aggregate byte count mismatch for {artifact['id']}")
    print(
        f"verified {artifact['id']}: {len(artifact['objects'])} objects, {verified_bytes} bytes"
    )
    return verified_bytes


def main() -> int:
    args = _parser().parse_args()
    try:
        manifest_path = args.manifest.resolve()
        repository_root = manifest_path.parent.parent
        manifest = validate_contract(manifest_path, args.source_groups.resolve())
        artifacts = selected_artifacts(manifest, args.tier, args.artifact_id)
        tier_bytes = sum(artifact["expected_bytes"] for artifact in artifacts)
        print(
            f"manifest valid: {len(manifest['artifacts'])} artifacts; "
            f"selected {len(artifacts)} artifact(s), {tier_bytes} byte(s)"
        )
        if args.manifest_only:
            return 0
        verified_bytes = sum(
            _verify_artifact(artifact, args.input_root, repository_root)
            for artifact in artifacts
        )
        print(f"verified selection: {verified_bytes} bytes")
        return 0
    except (ManifestError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
