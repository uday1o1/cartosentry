#!/usr/bin/env python3
"""Download public CartoSentry inputs with exact size and hash verification."""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from public_data_manifest import (
    ManifestError,
    checked_relative_path,
    object_url,
    selected_artifacts,
    selected_objects,
    sha256_file,
    validate_contract,
    verify_pinned_input,
)

USER_AGENT = "CartoSentry-public-data/0.2"
OSM_BASE_RE = re.compile(rb'<meta osm_base="([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z)"/>')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path("benchmarks/data_manifest.yaml")
    )
    parser.add_argument(
        "--source-groups", type=Path, default=Path("benchmarks/source_groups.yaml")
    )
    parser.add_argument("--output-root", type=Path, default=Path("data/public"))
    parser.add_argument(
        "--tier",
        choices=("public-smoke", "public-full", "gpu-perf"),
        default="public-smoke",
    )
    parser.add_argument("--artifact-id", action="append", default=[])
    parser.add_argument("--object-key", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-final-test", action="store_true")
    parser.add_argument("--unblinding-record", type=Path)
    return parser


def _open_request(
    artifact: Dict[str, Any], obj: Dict[str, Any], repository_root: Path
) -> Any:
    url = object_url(artifact, obj)
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    method = obj.get("method", "https-get")
    if method == "overpass-post":
        query_path = verify_pinned_input(
            repository_root, obj["query_path"], obj["query_sha256"]
        )
        verify_pinned_input(repository_root, obj["polygon_path"], obj["polygon_sha256"])
        query = query_path.read_text(encoding="utf-8")
        body = urllib.parse.urlencode({"data": query}).encode("utf-8")
        return urllib.request.urlopen(
            urllib.request.Request(url, data=body, headers=headers, method="POST"),
            timeout=300,
        )
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=headers, method="GET"), timeout=300
    )


def normalize_overpass_osm_base(payload: bytes, snapshot: str) -> bytes:
    """Replace only Overpass's live replication-base metadata with the query snapshot."""
    matches = list(OSM_BASE_RE.finditer(payload))
    if len(matches) != 1:
        raise ManifestError("Overpass response has no unique osm_base metadata")
    response_base = matches[0].group(1).decode("ascii")
    if response_base < snapshot:
        raise ManifestError(
            f"Overpass response base {response_base} predates requested snapshot {snapshot}"
        )
    replacement = f'<meta osm_base="{snapshot}"/>'.encode("ascii")
    return OSM_BASE_RE.sub(replacement, payload, count=1)


def _check_final_test_authorization(
    artifacts: Iterable[Dict[str, Any]],
    allow_final_test: bool,
    unblinding_record: Optional[Path],
) -> None:
    contains_final = any(
        artifact["partition"] == "final_test" for artifact in artifacts
    )
    if not contains_final:
        return
    if not allow_final_test:
        raise ManifestError("final-test inputs require --allow-final-test")
    if unblinding_record is None or not unblinding_record.is_file():
        raise ManifestError("final-test inputs require an existing --unblinding-record")


def _download(
    artifact: Dict[str, Any],
    obj: Dict[str, Any],
    destination: Path,
    repository_root: Path,
) -> str:
    if destination.exists():
        if not destination.is_file():
            raise ManifestError(f"destination is not a regular file: {destination}")
        actual_size = destination.stat().st_size
        actual_hash = sha256_file(destination)
        if actual_size == obj["bytes"] and actual_hash == obj["sha256"]:
            return "verified-existing"
        raise ManifestError(
            f"existing destination is corrupt: {destination} "
            f"(bytes={actual_size}, sha256={actual_hash})"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.partial.{os.getpid()}")
    if partial.exists():
        partial.unlink()
    try:
        with (
            _open_request(artifact, obj, repository_root) as response,
            partial.open("xb") as handle,
        ):
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if obj.get("normalization") == "overpass-osm-base-v1":
            payload = partial.read_bytes()
            payload = normalize_overpass_osm_base(payload, obj["snapshot_utc"])
            with partial.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        actual_size = partial.stat().st_size
        actual_hash = sha256_file(partial)
        if actual_size != obj["bytes"] or actual_hash != obj["sha256"]:
            rejected = destination.with_name(
                f".{destination.name}.rejected-{actual_hash[:12]}"
            )
            os.replace(partial, rejected)
            raise ManifestError(
                f"download verification failed for {obj['key']}: "
                f"bytes={actual_size}, sha256={actual_hash}; "
                f"unverified response quarantined at {rejected}"
            )
        os.replace(partial, destination)
        return "downloaded"
    finally:
        if partial.exists():
            partial.unlink()


def main() -> int:
    args = _parser().parse_args()
    try:
        manifest_path = args.manifest.resolve()
        repository_root = manifest_path.parent.parent
        manifest = validate_contract(manifest_path, args.source_groups.resolve())
        artifacts = selected_artifacts(manifest, args.tier, args.artifact_id)
        _check_final_test_authorization(
            artifacts, args.allow_final_test, args.unblinding_record
        )
        objects = selected_objects(artifacts, args.object_key)
        total_bytes = sum(obj["bytes"] for _, obj in objects)
        print(
            f"selection: {len(artifacts)} artifact(s), {len(objects)} object(s), "
            f"{total_bytes} byte(s)"
        )
        for artifact, obj in objects:
            destination = checked_relative_path(args.output_root, obj["key"])
            if args.dry_run:
                print(f"would-fetch {obj['key']} -> {destination}")
                continue
            status = _download(artifact, obj, destination, repository_root)
            print(f"{status} {obj['key']}")
        return 0
    except (ManifestError, OSError, urllib.error.URLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
