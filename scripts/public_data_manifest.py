#!/usr/bin/env python3
"""Shared manifest validation and hashing for public CartoSentry inputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
ARTIFACT_REQUIRED_FIELDS = {
    "id",
    "source_name",
    "source_url",
    "source_object_keys",
    "retrieved_at_utc",
    "license_url",
    "terms_snapshot_sha256",
    "redistribution",
    "attribution",
    "content_sha256",
    "expected_bytes",
    "partition",
    "source_group_id",
    "weather_tags",
    "route_tags",
    "purpose",
    "tiers",
    "objects",
}
PARTITIONS = {"development", "final_test"}


class ManifestError(ValueError):
    """Raised when a public-data contract is internally inconsistent."""


def load_json_yaml(path: Path) -> dict[str, Any]:
    """Load a JSON-compatible YAML file without adding a bootstrap dependency."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot load {path}: {error}") from error
    if not isinstance(document, dict):
        raise ManifestError(f"{path} must contain a top-level object")
    return document


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_relative_path(root: Path, value: str) -> Path:
    posix_path = PurePosixPath(value)
    if posix_path.is_absolute() or not posix_path.parts or ".." in posix_path.parts:
        raise ManifestError(f"unsafe manifest path: {value!r}")
    if any(part in {"", "."} for part in posix_path.parts):
        raise ManifestError(f"non-canonical manifest path: {value!r}")
    root_resolved = root.resolve()
    candidate = root.joinpath(*posix_path.parts).resolve()
    if os.path.commonpath((str(root_resolved), str(candidate))) != str(root_resolved):
        raise ManifestError(f"manifest path escapes output root: {value!r}")
    return candidate


def object_url(artifact: dict[str, Any], obj: dict[str, Any]) -> str:
    if "url" in obj:
        url = obj["url"]
    elif artifact["source_name"] == "Boreas":
        encoded_key = "/".join(quote(part, safe="") for part in obj["key"].split("/"))
        url = f"https://boreas.s3.amazonaws.com/{encoded_key}"
    else:
        raise ManifestError(f"object {obj['key']!r} has no retrieval URL")
    if not isinstance(url, str):
        raise ManifestError(f"object URL must be a string: {url!r}")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ManifestError(f"object URL must be absolute HTTPS: {url!r}")
    return url


def _validate_sha256(value: Any, context: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ManifestError(f"{context} must be a lowercase SHA-256 digest")


def _validate_utc_timestamp(value: Any, context: str) -> None:
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        raise ManifestError(f"{context} must be a whole-second UTC timestamp")


def _validate_https_url(value: Any, context: str) -> None:
    if not isinstance(value, str):
        raise ManifestError(f"{context} must be an HTTPS URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ManifestError(f"{context} must be an HTTPS URL")


def _group_contract(
    groups: dict[str, Any],
) -> tuple[dict[str, str], dict[str, set[str]]]:
    if groups.get("schema_version") != 1:
        raise ManifestError("source_groups.yaml schema_version must be 1")
    _validate_utc_timestamp(groups.get("assigned_at_utc"), "source-group assignment")
    policy = groups.get("partition_policy")
    required_policy = {
        "immutable_after_inspection": True,
        "derivatives_inherit_partition": True,
        "clips_inherit_partition": True,
        "adapters_do_not_change_partition": True,
        "cross_partition_source_groups_forbidden": True,
    }
    if policy != required_policy:
        raise ManifestError("source-group partition policy is incomplete or mutable")

    partitions: dict[str, str] = {}
    sequences: dict[str, set[str]] = {}
    for group in groups.get("source_groups", []):
        group_id = group.get("source_group_id")
        partition = group.get("partition")
        if not isinstance(group_id, str) or not group_id:
            raise ManifestError("source group is missing source_group_id")
        if group_id in partitions:
            raise ManifestError(f"duplicate source group: {group_id}")
        if partition not in PARTITIONS:
            raise ManifestError(
                f"invalid partition for source group {group_id}: {partition}"
            )
        if group.get("partition_locked") is not True:
            raise ManifestError(f"source group is not locked: {group_id}")
        partitions[group_id] = partition
        sequences[group_id] = set()
        for sequence in group.get("sequences", []):
            sequence_id = sequence.get("sequence_id")
            if not isinstance(sequence_id, str) or not sequence_id:
                raise ManifestError(f"source group {group_id} has an invalid sequence")
            if any(sequence_id in known for known in sequences.values()):
                raise ManifestError(
                    f"sequence appears in multiple source groups: {sequence_id}"
                )
            sequences[group_id].add(sequence_id)
    if not partitions:
        raise ManifestError("source_groups.yaml has no source groups")
    return partitions, sequences


def validate_contract(manifest_path: Path, source_groups_path: Path) -> dict[str, Any]:
    manifest = load_json_yaml(manifest_path)
    groups = load_json_yaml(source_groups_path)
    group_partitions, group_sequences = _group_contract(groups)

    if manifest.get("schema_version") != 1:
        raise ManifestError("data_manifest.yaml schema_version must be 1")
    if manifest.get("raw_data_tracked_in_git") is not False:
        raise ManifestError("the manifest must forbid tracking raw data in Git")

    tiers = manifest.get("tiers")
    required_tiers = {"public-smoke", "public-full", "gpu-perf"}
    if not isinstance(tiers, dict) or set(tiers) != required_tiers:
        raise ManifestError(
            "manifest tiers must be exactly public-smoke, public-full, and gpu-perf"
        )
    for tier_id, tier in tiers.items():
        if not isinstance(tier, dict) or not isinstance(tier.get("maximum_bytes"), int):
            raise ManifestError(f"tier {tier_id} needs an integer maximum_bytes")
        if tier["maximum_bytes"] <= 0 or not tier.get("purpose"):
            raise ManifestError(f"tier {tier_id} has an invalid budget or purpose")

    provenance = manifest.get("provenance_sources")
    if not isinstance(provenance, list) or not provenance:
        raise ManifestError("manifest has no provenance snapshots")
    snapshot_hashes: set[str] = set()
    for record in provenance:
        for field in ("name", "url", "retrieved_at_utc", "expected_bytes", "sha256"):
            if field not in record:
                raise ManifestError(f"provenance record is missing {field}")
        _validate_sha256(record["sha256"], f"provenance {record['name']} sha256")
        _validate_https_url(record["url"], f"provenance {record['name']} URL")
        _validate_utc_timestamp(
            record["retrieved_at_utc"], f"provenance {record['name']} retrieval"
        )
        if (
            not isinstance(record["expected_bytes"], int)
            or record["expected_bytes"] <= 0
        ):
            raise ManifestError(
                f"provenance {record['name']} has invalid expected_bytes"
            )
        snapshot_hashes.add(record["sha256"])
    assignment_snapshot = groups.get("assignment_source_sha256")
    _validate_sha256(assignment_snapshot, "source-group assignment snapshot")
    _validate_https_url(
        groups.get("assignment_source_url"), "source-group assignment source"
    )
    if assignment_snapshot not in snapshot_hashes:
        raise ManifestError(
            "source-group assignment snapshot is absent from provenance"
        )

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ManifestError("manifest has no artifacts")
    artifact_ids: set[str] = set()
    tier_bytes = {tier: 0 for tier in tiers}
    for artifact in artifacts:
        missing = ARTIFACT_REQUIRED_FIELDS.difference(artifact)
        if missing:
            raise ManifestError(f"artifact is missing fields: {sorted(missing)}")
        artifact_id = artifact["id"]
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ManifestError("artifact id must be a non-empty string")
        if artifact_id in artifact_ids:
            raise ManifestError(f"duplicate artifact id: {artifact_id}")
        artifact_ids.add(artifact_id)
        _validate_https_url(artifact["source_url"], f"{artifact_id} source URL")
        _validate_https_url(artifact["license_url"], f"{artifact_id} license URL")
        _validate_utc_timestamp(
            artifact["retrieved_at_utc"], f"{artifact_id} retrieval"
        )
        _validate_sha256(
            artifact["terms_snapshot_sha256"], f"{artifact_id} terms snapshot"
        )
        _validate_sha256(artifact["content_sha256"], f"{artifact_id} content hash")
        if artifact["terms_snapshot_sha256"] not in snapshot_hashes:
            raise ManifestError(
                f"{artifact_id} terms snapshot is not pinned in provenance_sources"
            )
        group_id = artifact["source_group_id"]
        if group_id not in group_partitions:
            raise ManifestError(f"{artifact_id} references an unknown source group")
        if artifact["partition"] != group_partitions[group_id]:
            raise ManifestError(
                f"{artifact_id} partition disagrees with its locked source group"
            )
        if artifact["partition"] not in PARTITIONS:
            raise ManifestError(f"{artifact_id} has an invalid partition")
        for text_field in ("source_name", "redistribution", "attribution", "purpose"):
            if (
                not isinstance(artifact[text_field], str)
                or not artifact[text_field].strip()
            ):
                raise ManifestError(f"{artifact_id} has an invalid {text_field}")
        for tag_field in ("weather_tags", "route_tags"):
            tags = artifact[tag_field]
            if not isinstance(tags, list) or not all(
                isinstance(tag, str) and tag for tag in tags
            ):
                raise ManifestError(f"{artifact_id} has invalid {tag_field}")
        declared_sequences = artifact.get("source_sequence_ids", [])
        if artifact["source_name"] == "Boreas" and not declared_sequences:
            raise ManifestError(
                f"{artifact_id} must declare its Boreas source sequence"
            )
        if not set(declared_sequences).issubset(group_sequences[group_id]):
            raise ManifestError(
                f"{artifact_id} contains a sequence outside its source group"
            )
        artifact_tiers = artifact["tiers"]
        if not isinstance(artifact_tiers, list) or not artifact_tiers:
            raise ManifestError(f"{artifact_id} has no data tier")
        if not set(artifact_tiers).issubset(tiers):
            raise ManifestError(f"{artifact_id} references an unknown data tier")

        objects = artifact["objects"]
        if not isinstance(objects, list) or not objects:
            raise ManifestError(f"{artifact_id} has no source objects")
        keys: list[str] = []
        source_keys: list[str] = []
        object_bytes = 0
        for obj in objects:
            key = obj.get("key")
            if not isinstance(key, str) or not key:
                raise ManifestError(f"{artifact_id} has an object with no key")
            checked_relative_path(Path("/manifest-output-root"), key)
            if key in keys:
                raise ManifestError(f"{artifact_id} has a duplicate object key: {key}")
            keys.append(key)
            source_key = obj.get("source_key", key)
            if not isinstance(source_key, str) or not source_key:
                raise ManifestError(f"{artifact_id}:{key} has an invalid source key")
            source_keys.append(source_key)
            if not isinstance(obj.get("bytes"), int) or obj["bytes"] <= 0:
                raise ManifestError(f"{artifact_id}:{key} has invalid bytes")
            object_bytes += obj["bytes"]
            _validate_sha256(obj.get("sha256"), f"{artifact_id}:{key} hash")
            object_url(artifact, obj)
            if obj.get("method", "https-get") not in {"https-get", "overpass-post"}:
                raise ManifestError(
                    f"{artifact_id}:{key} has an unsupported retrieval method"
                )
            if obj.get("method") == "overpass-post":
                if obj.get("normalization") != "overpass-osm-base-v1":
                    raise ManifestError(
                        f"{artifact_id}:{key} lacks frozen Overpass normalization"
                    )
                _validate_utc_timestamp(
                    obj.get("snapshot_utc"), f"{artifact_id}:{key} snapshot"
                )
                for path_field, hash_field in (
                    ("query_path", "query_sha256"),
                    ("polygon_path", "polygon_sha256"),
                ):
                    if path_field not in obj or hash_field not in obj:
                        raise ManifestError(
                            f"{artifact_id}:{key} lacks pinned Overpass inputs"
                        )
                    _validate_sha256(
                        obj[hash_field], f"{artifact_id}:{key} {hash_field}"
                    )
        if source_keys != artifact["source_object_keys"]:
            raise ManifestError(
                f"{artifact_id} source_object_keys do not match ordered objects"
            )
        if object_bytes != artifact["expected_bytes"]:
            raise ManifestError(
                f"{artifact_id} expected_bytes disagree with its objects"
            )
        for tier_id in artifact_tiers:
            tier_bytes[tier_id] += object_bytes

    for tier_id, total in tier_bytes.items():
        if total > tiers[tier_id]["maximum_bytes"]:
            raise ManifestError(
                f"tier {tier_id} requires {total} bytes, above its "
                f"{tiers[tier_id]['maximum_bytes']} byte cap"
            )
    return manifest


def selected_artifacts(
    manifest: dict[str, Any],
    tier: str | None,
    artifact_ids: Sequence[str],
) -> list[dict[str, Any]]:
    known_ids = {artifact["id"] for artifact in manifest["artifacts"]}
    unknown = set(artifact_ids).difference(known_ids)
    if unknown:
        raise ManifestError(f"unknown artifact ids: {sorted(unknown)}")
    selected = []
    for artifact in manifest["artifacts"]:
        if tier is not None and tier not in artifact["tiers"]:
            continue
        if artifact_ids and artifact["id"] not in artifact_ids:
            continue
        selected.append(artifact)
    if not selected:
        raise ManifestError("selection contains no artifacts")
    return selected


def selected_objects(
    artifacts: Iterable[dict[str, Any]], object_keys: Sequence[str]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    requested = set(object_keys)
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[str] = set()
    for artifact in artifacts:
        for obj in artifact["objects"]:
            if requested and obj["key"] not in requested:
                continue
            result.append((artifact, obj))
            seen.add(obj["key"])
    unknown = requested.difference(seen)
    if unknown:
        raise ManifestError(f"selected object keys were not found: {sorted(unknown)}")
    if not result:
        raise ManifestError("selection contains no objects")
    return result


def verify_pinned_input(
    repository_root: Path, relative_path: str, expected_hash: str
) -> Path:
    path = checked_relative_path(repository_root, relative_path)
    if not path.is_file():
        raise ManifestError(f"pinned retrieval input does not exist: {relative_path}")
    actual_hash = sha256_file(path)
    if actual_hash != expected_hash:
        raise ManifestError(
            f"pinned retrieval input hash mismatch for {relative_path}: {actual_hash}"
        )
    return path
