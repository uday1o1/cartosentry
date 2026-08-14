#!/usr/bin/env python3
"""Validate the frozen evaluation charter and gate final-test access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PARTITIONS = (
    "development",
    "threshold_calibration",
    "policy_tuning",
    "final_test",
)
ORDINARY_PARTITIONS = PARTITIONS[:-1]
AUTHORIZATION_TEXT = "I_AUTHORIZE_FINAL_TEST_UNBLINDING"
CONFIRMATION_TEXT = "UNBLIND_FINAL_TEST"
FROZEN_PATHS = {
    "split_manifest": Path("benchmarks/split_manifest.yaml"),
    "numerical_charter": Path("benchmarks/numerical_charter.yaml"),
    "fault_matrix": Path("benchmarks/fault_matrix_v1.yaml"),
    "fallback_tree": Path("benchmarks/fallback_tree.yaml"),
    "charter_revisions": Path("benchmarks/charter_revisions.yaml"),
}
CHARTER_COMPONENT_NAMES = (
    "split_manifest",
    "numerical_charter",
    "fault_matrix",
    "fallback_tree",
)


class CharterError(ValueError):
    """Raised when a frozen evaluation contract is invalid."""


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CharterError(f"cannot read evaluation contract {path.name}") from error
    if not isinstance(loaded, dict):
        raise CharterError(f"evaluation contract {path.name} must be a mapping")
    return cast(dict[str, Any], loaded)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise CharterError(f"cannot hash evaluation input {path.name}") from error
    return digest.hexdigest()


def _canonical_sha256(value: dict[str, Any], *, omit: set[str] | None = None) -> str:
    excluded = omit or set()
    canonical = {key: item for key, item in value.items() if key not in excluded}
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _fail(message: str) -> NoReturn:
    raise CharterError(message)


def _require_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{context} must be a nonempty string")
    return value


def _require_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{context} must be a list")
    return value


def _source_group_index(source_groups: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups = _require_list(source_groups.get("source_groups"), "source_groups")
    index: dict[str, dict[str, Any]] = {}
    for raw_group in groups:
        if not isinstance(raw_group, dict):
            _fail("source group entry must be a mapping")
        group = cast(dict[str, Any], raw_group)
        group_id = _require_string(group.get("source_group_id"), "source_group_id")
        if group_id in index:
            _fail(f"duplicate source group {group_id}")
        index[group_id] = group
    return index


def _expanded_synthetic_families(
    split: dict[str, Any], *, include_final: bool
) -> list[dict[str, Any]]:
    raw_expansion = split.get("seed_expansion")
    if not isinstance(raw_expansion, dict):
        _fail("seed_expansion must be a mapping")
    expansion = cast(dict[str, Any], raw_expansion)
    if expansion.get("zero_padding") != 3:
        _fail("synthetic family zero padding must be 3")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_seeds: set[int] = set()
    for raw_family_set in _require_list(
        split.get("synthetic_family_sets"), "synthetic_family_sets"
    ):
        if not isinstance(raw_family_set, dict):
            _fail("synthetic family set must be a mapping")
        family_set = cast(dict[str, Any], raw_family_set)
        partition = _require_string(family_set.get("partition"), "family partition")
        if partition not in PARTITIONS:
            _fail(f"unsupported synthetic partition {partition}")
        if partition == "final_test" and family_set.get("access_state") != "SEALED":
            _fail("final-test synthetic family sets must be SEALED")
        if partition == "final_test" and not include_final:
            continue
        prefix = _require_string(family_set.get("family_prefix"), "family_prefix")
        count = family_set.get("family_count")
        seed_start = family_set.get("seed_start")
        if not isinstance(count, int) or count <= 0:
            _fail("synthetic family_count must be positive")
        if not isinstance(seed_start, int) or seed_start < 0:
            _fail("synthetic seed_start must be a nonnegative integer")
        for offset in range(count):
            family_id = f"{prefix}-{offset + 1:03d}"
            seed = seed_start + offset
            if family_id in seen_ids or seed in seen_seeds:
                _fail("synthetic family identifiers and seeds must be globally unique")
            seen_ids.add(family_id)
            seen_seeds.add(seed)
            result.append(
                {
                    "family_id": family_id,
                    "seed": seed,
                    "partition": partition,
                    "domain": family_set.get("domain"),
                    "family_set_id": family_set.get("family_set_id"),
                }
            )
    return result


def _validate_source_assignments(
    split: dict[str, Any],
    source_groups: dict[str, Any],
    data_manifest: dict[str, Any],
) -> None:
    if split.get("source_group_assignment_version") != source_groups.get(
        "assignment_version"
    ):
        _fail("split manifest source-group version differs from the M0.2 assignment")
    source_index = _source_group_index(source_groups)
    split_index: dict[str, dict[str, Any]] = {}
    for raw_assignment in _require_list(
        split.get("real_source_groups"), "real_source_groups"
    ):
        if not isinstance(raw_assignment, dict):
            _fail("real source-group assignment must be a mapping")
        assignment = cast(dict[str, Any], raw_assignment)
        group_id = _require_string(
            assignment.get("source_group_id"), "split source_group_id"
        )
        if group_id in split_index:
            _fail(f"duplicate split source group {group_id}")
        split_index[group_id] = assignment
    if set(split_index) != set(source_index):
        _fail("split manifest must contain every frozen real source group exactly once")
    for group_id, source_group in source_index.items():
        assignment = split_index[group_id]
        if assignment.get("partition") != source_group.get("partition"):
            _fail(f"source group {group_id} moved across partitions")
        source_sequences = {
            _require_string(item.get("sequence_id"), "source sequence_id")
            for item in _require_list(source_group.get("sequences"), "sequences")
            if isinstance(item, dict)
        }
        split_sequences = set(cast(list[str], assignment.get("sequence_ids", [])))
        if split_sequences != source_sequences:
            _fail(f"source group {group_id} sequence membership changed")
    for raw_artifact in _require_list(data_manifest.get("artifacts"), "artifacts"):
        if not isinstance(raw_artifact, dict):
            _fail("data artifact must be a mapping")
        artifact = cast(dict[str, Any], raw_artifact)
        artifact_id = _require_string(artifact.get("id"), "artifact id")
        group_id = _require_string(
            artifact.get("source_group_id"), f"{artifact_id}.source_group_id"
        )
        if group_id not in source_index:
            _fail(f"artifact {artifact_id} names an unknown source group")
        if artifact.get("partition") != source_index[group_id].get("partition"):
            _fail(f"artifact {artifact_id} does not inherit its source partition")
        known_sequences = {
            cast(str, item.get("sequence_id"))
            for item in cast(list[dict[str, Any]], source_index[group_id]["sequences"])
        }
        if not set(cast(list[str], artifact.get("source_sequence_ids", []))) <= (
            known_sequences
        ):
            _fail(f"artifact {artifact_id} names a sequence outside its source group")


def _validate_split(
    split: dict[str, Any],
    root: Path,
    source_groups: dict[str, Any],
    data: dict[str, Any],
) -> None:
    if split.get("schema_version") != 1 or split.get("split_version") != "v0":
        _fail("split manifest must be schema 1 and version v0")
    if tuple(split.get("partition_order", ())) != PARTITIONS:
        _fail("split partition order is not the frozen four-partition contract")
    if tuple(split.get("ordinary_development_partitions", ())) != ORDINARY_PARTITIONS:
        _fail("ordinary development partition access is invalid")
    if split.get("source_groups_sha256") != _file_sha256(
        root / "benchmarks/source_groups.yaml"
    ):
        _fail("split source_groups_sha256 does not match the frozen file")
    if split.get("data_manifest_sha256") != _file_sha256(
        root / "benchmarks/data_manifest.yaml"
    ):
        _fail("split data_manifest_sha256 does not match the frozen file")
    _validate_source_assignments(split, source_groups, data)
    families = _expanded_synthetic_families(split, include_final=True)
    observed_partitions = {cast(str, item["partition"]) for item in families}
    if observed_partitions != set(PARTITIONS):
        _fail(
            "synthetic family sets must assign at least one family to every partition"
        )


def _validate_numerical_charter(charter: dict[str, Any]) -> None:
    if charter.get("schema_version") != 1 or charter.get("charter_version") != "v0":
        _fail("numerical charter must be schema 1 and version v0")
    expected_hash = _canonical_sha256(charter, omit={"immutable_sha256"})
    if charter.get("immutable_sha256") != expected_hash:
        _fail("numerical charter immutable_sha256 is invalid")
    statistics = cast(dict[str, Any], charter.get("statistics"))
    if statistics.get("bootstrap_replicates") != 10000:
        _fail("clustered bootstrap must use 10000 frozen replicates")
    if not isinstance(statistics.get("bootstrap_seed"), int):
        _fail("clustered bootstrap seed must be an integer")
    event_matching = cast(dict[str, Any], charter.get("event_matching"))
    if event_matching.get("interval_convention") != "half_open":
        _fail("event matching must use half-open intervals")
    gates = charter.get("gates")
    if not isinstance(gates, dict) or not gates:
        _fail("numerical charter must contain named gates")
    required_fields = {
        "operator",
        "value",
        "unit",
        "decision_bound",
        "responsible_metric",
        "rationale",
    }
    for key, raw_gate in gates.items():
        if not isinstance(key, str) or not isinstance(raw_gate, dict):
            _fail("numerical gates must be named mappings")
        gate = cast(dict[str, Any], raw_gate)
        if required_fields.difference(gate):
            _fail(f"numerical gate {key} is missing required metadata")
        for field in (
            "operator",
            "unit",
            "decision_bound",
            "responsible_metric",
            "rationale",
        ):
            _require_string(gate.get(field), f"{key}.{field}")


def _operator_index(matrix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_operator in _require_list(matrix.get("operators"), "operators"):
        if not isinstance(raw_operator, dict):
            _fail("fault operator must be a mapping")
        operator = cast(dict[str, Any], raw_operator)
        operator_id = _require_string(operator.get("operator_id"), "operator_id")
        if operator_id in result:
            _fail(f"duplicate fault operator {operator_id}")
        result[operator_id] = operator
    return result


def ensure_operator_allowed(matrix: dict[str, Any], operator_id: str) -> None:
    allowlist = set(cast(list[str], matrix.get("v1_operator_allowlist", [])))
    operators = set(_operator_index(matrix))
    if allowlist != operators:
        _fail("fault operator allowlist and definitions differ")
    if operator_id not in allowlist:
        _fail(f"fault operator is outside cartosentry-v1-core: {operator_id}")


def derive_fault_id(
    matrix: dict[str, Any],
    *,
    operator_id: str,
    case_id: str,
    source_family_id: str,
    source_identity_sha256: str,
) -> str:
    ensure_operator_allowed(matrix, operator_id)
    operator = _operator_index(matrix)[operator_id]
    cases = {
        cast(str, item.get("case_id"))
        for item in cast(list[dict[str, Any]], operator.get("cases", []))
    }
    if case_id not in cases:
        _fail(f"unknown case {case_id} for operator {operator_id}")
    if len(source_identity_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_identity_sha256
    ):
        _fail("source identity must be a lowercase SHA-256 digest")
    identity = {
        "fault_matrix_id": matrix.get("fault_matrix_id"),
        "operator_id": operator_id,
        "case_id": case_id,
        "source_family_id": source_family_id,
        "source_identity_sha256": source_identity_sha256,
    }
    digest = _canonical_sha256(identity)
    return f"fault-sha256-{digest}"


def _validate_fault_matrix(matrix: dict[str, Any]) -> None:
    if matrix.get("schema_version") != 1:
        _fail("fault matrix schema_version must be 1")
    if matrix.get("fault_matrix_id") != "cartosentry-v1-core":
        _fail("fault matrix identifier must be cartosentry-v1-core")
    index = _operator_index(matrix)
    allowlist = cast(list[str], matrix.get("v1_operator_allowlist", []))
    if len(allowlist) != len(set(allowlist)) or set(allowlist) != set(index):
        _fail("fault matrix exact allowlist is invalid")
    case_ids: set[str] = set()
    for operator_id, operator in index.items():
        cases = _require_list(operator.get("cases"), f"{operator_id}.cases")
        if not cases:
            _fail(f"fault operator {operator_id} has no cases")
        severities: set[str] = set()
        for raw_case in cases:
            if not isinstance(raw_case, dict):
                _fail("fault case must be a mapping")
            case = cast(dict[str, Any], raw_case)
            case_id = _require_string(case.get("case_id"), "case_id")
            if case_id in case_ids:
                _fail(f"duplicate fault case {case_id}")
            case_ids.add(case_id)
            severity = _require_string(case.get("severity"), f"{case_id}.severity")
            if severity not in {"below_threshold", "near_threshold", "detectable"}:
                _fail(f"fault case {case_id} has an unsupported severity")
            severities.add(severity)
            if not isinstance(case.get("parameters"), dict):
                _fail(f"fault case {case_id} parameters must be a mapping")
        if severities != {"below_threshold", "near_threshold", "detectable"}:
            _fail(f"fault operator {operator_id} must span all frozen severity tiers")
    rejected = set(
        cast(list[str], matrix.get("explicitly_rejected_follow_on_operators", []))
    )
    if rejected & set(allowlist):
        _fail("rejected follow-on operators overlap the V1 allowlist")
    for operator_id in rejected:
        try:
            ensure_operator_allowed(matrix, operator_id)
        except CharterError:
            continue
        _fail(f"follow-on fault operator unexpectedly allowed: {operator_id}")


def _validate_fallback_tree(
    tree: dict[str, Any], charter: dict[str, Any], matrix: dict[str, Any]
) -> None:
    if tree.get("schema_version") != 1 or tree.get("fallback_tree_version") != "v1":
        _fail("fallback tree must be schema 1 and version v1")
    gate_keys = set(cast(dict[str, Any], charter.get("gates")))
    branch_ids: set[str] = set()
    required_fields = {
        "branch_id",
        "priority",
        "population",
        "supported_fault_families",
        "severity",
        "metric",
        "statistical_bound",
        "gate_keys",
        "minimum_support",
        "multiplicity",
        "claim_wording",
    }
    tracks = _require_list(tree.get("claim_tracks"), "claim_tracks")
    if not tracks:
        _fail("fallback tree must contain claim tracks")
    for raw_track in tracks:
        if not isinstance(raw_track, dict):
            _fail("fallback claim track must be a mapping")
        track = cast(dict[str, Any], raw_track)
        track_id = _require_string(track.get("track_id"), "track_id")
        branches = _require_list(track.get("branches"), f"{track_id}.branches")
        priorities: list[int] = []
        for raw_branch in branches:
            if not isinstance(raw_branch, dict):
                _fail("fallback branch must be a mapping")
            branch = cast(dict[str, Any], raw_branch)
            if required_fields.difference(branch):
                _fail(f"fallback branch in {track_id} is incomplete")
            branch_id = _require_string(branch.get("branch_id"), "branch_id")
            if branch_id in branch_ids:
                _fail(f"duplicate fallback branch {branch_id}")
            branch_ids.add(branch_id)
            priority = branch.get("priority")
            if not isinstance(priority, int) or priority <= 0:
                _fail(f"fallback branch {branch_id} priority is invalid")
            priorities.append(priority)
            unknown_gates = set(cast(list[str], branch.get("gate_keys"))) - gate_keys
            if unknown_gates:
                _fail(f"fallback branch {branch_id} names unknown gates")
            for field in (
                "population",
                "severity",
                "metric",
                "statistical_bound",
                "minimum_support",
                "multiplicity",
                "claim_wording",
            ):
                _require_string(branch.get(field), f"{branch_id}.{field}")
        if priorities != list(range(1, len(branches) + 1)):
            _fail(f"fallback priorities for {track_id} must be contiguous and ordered")
    fault_tracks = [
        cast(dict[str, Any], track)
        for track in tracks
        if cast(dict[str, Any], track).get("track_id") == "v1-supported-fault-detection"
    ]
    if len(fault_tracks) != 1:
        _fail("fallback tree must contain one V1 fault-detection track")
    fault_branches = {
        cast(str, branch.get("branch_id")): branch
        for branch in cast(list[dict[str, Any]], fault_tracks[0]["branches"])
    }
    matrix_allowlist = set(cast(list[str], matrix["v1_operator_allowlist"]))
    for branch_id in ("fault-primary-all-v1", "fault-narrow-generated-only-v1"):
        fault_branch = fault_branches.get(branch_id)
        if (
            fault_branch is None
            or set(cast(list[str], fault_branch.get("supported_fault_families", [])))
            != matrix_allowlist
        ):
            _fail(f"fallback branch {branch_id} must match the exact fault allowlist")


def _aggregate_component_sha256(component_hashes: dict[str, str]) -> str:
    return _canonical_sha256(component_hashes)


def _validate_charter_revisions(
    revisions: dict[str, Any],
    *,
    root: Path,
    split: dict[str, Any],
    charter: dict[str, Any],
    matrix: dict[str, Any],
    fallback: dict[str, Any],
) -> None:
    if revisions.get("schema_version") != 1:
        _fail("charter revision history schema_version must be 1")
    if revisions.get("charter_id") != "cartosentry-v1-evaluation":
        _fail("charter revision history identifier is invalid")
    if revisions.get("freeze_state") != "FROZEN_PRE_UNBLINDING":
        _fail("charter revision history must remain frozen before unblinding")
    component_versions = {
        "split_manifest": split.get("split_version"),
        "numerical_charter": charter.get("charter_version"),
        "fault_matrix": matrix.get("matrix_version"),
        "fallback_tree": fallback.get("fallback_tree_version"),
    }
    if revisions.get("current_component_versions") != component_versions:
        _fail("charter revision component versions do not match frozen inputs")
    component_hashes = {
        name: _file_sha256(root / FROZEN_PATHS[name])
        for name in CHARTER_COMPONENT_NAMES
    }
    if revisions.get("current_component_file_sha256") != component_hashes:
        _fail("charter revision component hashes do not match frozen inputs")
    aggregate_hash = _aggregate_component_sha256(component_hashes)
    if revisions.get("current_aggregate_sha256") != aggregate_hash:
        _fail("current aggregate charter hash is invalid")
    history = _require_list(revisions.get("revisions"), "charter revisions")
    if not history:
        _fail("charter revision history must record every post-v0 revision")
    previous_new_hashes: dict[str, str] | None = None
    for index, raw_revision in enumerate(history, start=1):
        if not isinstance(raw_revision, dict):
            _fail("charter revision must be a mapping")
        revision = cast(dict[str, Any], raw_revision)
        version = f"v{index}"
        predecessor_version = f"v{index - 1}"
        if revision.get("version") != version:
            _fail("charter revision versions must be contiguous")
        if revision.get("predecessor_version") != predecessor_version:
            _fail(f"charter revision {version} predecessor is invalid")
        for field in (
            "recorded_at_utc",
            "rationale",
            "expected_risk",
            "predecessor_aggregate_sha256",
            "new_aggregate_sha256",
        ):
            _require_string(revision.get(field), f"{version}.{field}")
        if revision.get("unblinding_state") != "PRE_UNBLINDING":
            _fail(f"charter revision {version} was not recorded pre-unblinding")
        affected = _require_list(
            revision.get("affected_detectors"), f"{version}.affected_detectors"
        )
        if not affected or any(
            not isinstance(item, str) or not item for item in affected
        ):
            _fail(f"charter revision {version} affected detectors are invalid")
        partitions = _require_list(
            revision.get("data_partitions"), f"{version}.data_partitions"
        )
        if not partitions or not set(cast(list[str], partitions)).issubset(
            set(ORDINARY_PARTITIONS)
        ):
            _fail(f"charter revision {version} data partitions are invalid")
        predecessor_hashes = revision.get("predecessor_component_file_sha256")
        new_hashes = revision.get("new_component_file_sha256")
        if not isinstance(predecessor_hashes, dict) or set(predecessor_hashes) != set(
            CHARTER_COMPONENT_NAMES
        ):
            _fail(f"charter revision {version} predecessor hashes are invalid")
        if not isinstance(new_hashes, dict) or set(new_hashes) != set(
            CHARTER_COMPONENT_NAMES
        ):
            _fail(f"charter revision {version} new hashes are invalid")
        predecessor_hash_mapping = cast(dict[str, str], predecessor_hashes)
        new_hash_mapping = cast(dict[str, str], new_hashes)
        if revision["predecessor_aggregate_sha256"] != _aggregate_component_sha256(
            predecessor_hash_mapping
        ):
            _fail(f"charter revision {version} predecessor aggregate hash is invalid")
        if revision["new_aggregate_sha256"] != _aggregate_component_sha256(
            new_hash_mapping
        ):
            _fail(f"charter revision {version} new aggregate hash is invalid")
        if (
            previous_new_hashes is not None
            and predecessor_hash_mapping != previous_new_hashes
        ):
            _fail(f"charter revision {version} does not continue the hash chain")
        previous_new_hashes = new_hash_mapping
    latest = cast(dict[str, Any], history[-1])
    if revisions.get("current_version") != latest.get("version"):
        _fail("current charter version does not match the latest revision")
    if latest.get("new_component_file_sha256") != component_hashes:
        _fail("latest charter revision does not bind the frozen inputs")
    if latest.get("new_aggregate_sha256") != aggregate_hash:
        _fail("latest charter revision does not bind the aggregate charter")


def validate_contract(root: Path = REPOSITORY_ROOT) -> dict[str, Any]:
    source_groups = _load_mapping(root / "benchmarks/source_groups.yaml")
    data = _load_mapping(root / "benchmarks/data_manifest.yaml")
    split = _load_mapping(root / FROZEN_PATHS["split_manifest"])
    charter = _load_mapping(root / FROZEN_PATHS["numerical_charter"])
    matrix = _load_mapping(root / FROZEN_PATHS["fault_matrix"])
    fallback = _load_mapping(root / FROZEN_PATHS["fallback_tree"])
    revisions = _load_mapping(root / FROZEN_PATHS["charter_revisions"])
    _validate_split(split, root, source_groups, data)
    _validate_numerical_charter(charter)
    _validate_fault_matrix(matrix)
    _validate_fallback_tree(fallback, charter, matrix)
    _validate_charter_revisions(
        revisions,
        root=root,
        split=split,
        charter=charter,
        matrix=matrix,
        fallback=fallback,
    )
    hashes = {name: _file_sha256(root / path) for name, path in FROZEN_PATHS.items()}
    return {
        "schema_version": 1,
        "state": "VALID",
        "charter_version": revisions["current_version"],
        "charter_immutable_sha256": revisions["current_aggregate_sha256"],
        "component_versions": revisions["current_component_versions"],
        "file_sha256": hashes,
        "ordinary_synthetic_family_count": len(
            _expanded_synthetic_families(split, include_final=False)
        ),
        "sealed_final_synthetic_family_count": len(
            [
                item
                for item in _expanded_synthetic_families(split, include_final=True)
                if item["partition"] == "final_test"
            ]
        ),
    }


def clustered_bootstrap_indices(
    cluster_count: int, *, replicates: int, seed: int
) -> list[list[int]]:
    if cluster_count <= 0 or replicates <= 0:
        _fail("bootstrap cluster and replicate counts must be positive")
    generator = random.Random(seed)
    return [
        [generator.randrange(cluster_count) for _ in range(cluster_count)]
        for _ in range(replicates)
    ]


def select_partition(
    split: dict[str, Any],
    partition: str,
    *,
    unblinding_event: dict[str, Any] | None = None,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, Any]:
    if partition not in PARTITIONS:
        _fail(f"unknown partition {partition}")
    if partition == "final_test":
        if unblinding_event is None or unblinding_event.get("state") != "UNBLINDED":
            _fail("final_test is sealed until an unblinding event is recorded")
        if unblinding_event.get("split_manifest_sha256") != _file_sha256(
            repository_root / FROZEN_PATHS["split_manifest"]
        ):
            _fail("unblinding event does not bind the current split manifest")
    real = [
        item
        for item in cast(list[dict[str, Any]], split["real_source_groups"])
        if item["partition"] == partition
    ]
    synthetic = [
        item
        for item in _expanded_synthetic_families(split, include_final=True)
        if item["partition"] == partition
    ]
    return {"partition": partition, "real_source_groups": real, "synthetic": synthetic}


def _git_output(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _fail("cannot verify the release-candidate Git state")
    return result.stdout.strip()


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _unblind(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    report = validate_contract(root)
    event_path = Path(args.event_log).resolve()
    if event_path.is_relative_to(root):
        _fail("unblinding event log must be outside the repository")
    if event_path.exists():
        _fail("unblinding event log already exists")
    if args.confirmation != CONFIRMATION_TEXT:
        _fail("final-test unblinding confirmation text is invalid")
    try:
        authorization = (
            Path(args.authorization_file).read_text(encoding="utf-8").strip()
        )
    except (OSError, UnicodeError) as error:
        raise CharterError("cannot read unblinding authorization") from error
    if authorization != AUTHORIZATION_TEXT:
        _fail("final-test unblinding authorization is invalid")
    binding = _load_mapping(Path(args.release_binding))
    if binding.get("schema_version") != 1 or binding.get("state") != "BOUND":
        _fail("release binding must be schema 1 in BOUND state")
    current_commit = _git_output(root, "rev-parse", "HEAD")
    if binding.get("git_commit") != current_commit:
        _fail("release binding does not name the current commit")
    if _git_output(root, "status", "--porcelain"):
        _fail("final-test unblinding requires a clean worktree")
    bound_hashes = binding.get("frozen_file_sha256")
    if bound_hashes != report["file_sha256"]:
        _fail("release binding does not match every frozen evaluation file")
    split = _load_mapping(root / FROZEN_PATHS["split_manifest"])
    final_selection = select_partition(
        split,
        "final_test",
        unblinding_event={
            "state": "UNBLINDED",
            "split_manifest_sha256": report["file_sha256"]["split_manifest"],
        },
        repository_root=root,
    )
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    event_basis = {
        "git_commit": current_commit,
        "timestamp_utc": timestamp,
        "split_manifest_sha256": report["file_sha256"]["split_manifest"],
    }
    event = {
        "schema_version": 1,
        "event_id": f"unblind-sha256-{_canonical_sha256(event_basis)}",
        "state": "UNBLINDED",
        "timestamp_utc": timestamp,
        "git_commit": current_commit,
        "release_tag": binding.get("release_tag"),
        "charter_version": report["charter_version"],
        "charter_immutable_sha256": report["charter_immutable_sha256"],
        "split_manifest_sha256": report["file_sha256"]["split_manifest"],
        "fault_matrix_sha256": report["file_sha256"]["fault_matrix"],
        "fallback_tree_sha256": report["file_sha256"]["fallback_tree"],
        "confirmation": CONFIRMATION_TEXT,
        "final_selection": final_selection,
    }
    _atomic_write_json(event_path, event)
    return event


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    select = subparsers.add_parser("select")
    select.add_argument("--partition", choices=PARTITIONS, default="development")
    select.add_argument("--unblinding-event", type=Path)
    fault = subparsers.add_parser("fault-id")
    fault.add_argument("--operator", required=True)
    fault.add_argument("--case", required=True)
    fault.add_argument("--source-family", required=True)
    fault.add_argument("--source-hash", required=True)
    unblind = subparsers.add_parser("unblind")
    unblind.add_argument("--release-binding", type=Path, required=True)
    unblind.add_argument("--authorization-file", type=Path, required=True)
    unblind.add_argument("--event-log", type=Path, required=True)
    unblind.add_argument("--confirmation", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = Path(args.root).resolve()
    try:
        if args.command == "validate":
            result = validate_contract(root)
        elif args.command == "select":
            validate_contract(root)
            split = _load_mapping(root / FROZEN_PATHS["split_manifest"])
            event = (
                _load_mapping(args.unblinding_event)
                if args.unblinding_event is not None
                else None
            )
            result = select_partition(
                split,
                cast(str, args.partition),
                unblinding_event=event,
                repository_root=root,
            )
        elif args.command == "fault-id":
            validate_contract(root)
            matrix = _load_mapping(root / FROZEN_PATHS["fault_matrix"])
            result = {
                "fault_id": derive_fault_id(
                    matrix,
                    operator_id=args.operator,
                    case_id=args.case,
                    source_family_id=args.source_family,
                    source_identity_sha256=args.source_hash,
                )
            }
        elif args.command == "unblind":
            result = _unblind(args)
        else:
            _fail("unknown evaluation charter command")
    except CharterError as error:
        print(f"evaluation charter error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
