#!/usr/bin/env python3
"""Validate the exact Python, native, container, and toolchain dependency contract."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
EXACT_REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")
PINNED_IMAGE_RE = re.compile(r"^FROM\s+\S+@sha256:[0-9a-f]{64}$", re.MULTILINE)

REQUIRED_NATIVE_DEPENDENCIES = frozenset(
    {
        "apache-arrow",
        "catch2",
        "cli11",
        "curl",
        "eigen",
        "fmt",
        "geographiclib",
        "libosmium",
        "nanoflann",
        "nlohmann-json",
        "opencv",
        "protozero",
        "pybind11",
        "rapidcheck",
        "sophus",
        "spdlog",
        "sqlite",
        "yaml-cpp",
        "zstd",
    }
)
ARCHIVE_DEPENDENCIES = frozenset(
    {
        "catch2",
        "eigen",
        "fmt",
        "geographiclib",
        "nlohmann-json",
        "opencv",
        "sophus",
        "spdlog",
        "sqlite",
        "yaml-cpp",
    }
)
REQUIRED_PYTHON_DEPENDENCIES = frozenset(
    {
        "jinja2",
        "numpy",
        "plotly",
        "pyarrow",
        "pydantic",
        "pyyaml",
        "scipy",
        "shapely",
        "typer",
    }
)
REQUIRED_DEVELOPMENT_DEPENDENCIES = frozenset(
    {
        "cmake",
        "hypothesis",
        "mypy",
        "ninja",
        "pybind11",
        "pytest",
        "ruff",
        "scikit-build-core",
    }
)


class DependencyLockError(ValueError):
    """Raised when the reproducible dependency contract is incomplete."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DependencyLockError(
            f"cannot read dependency lock {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise DependencyLockError("dependency lock root must be an object")
    return value


def _require_https(value: object, context: str) -> None:
    if not isinstance(value, str):
        raise DependencyLockError(f"{context} must be a string")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise DependencyLockError(f"{context} must be an absolute HTTPS URL")


def _exact_requirements(values: object, context: str) -> dict[str, str]:
    if not isinstance(values, list):
        raise DependencyLockError(f"{context} must be a list")
    result: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str):
            raise DependencyLockError(f"{context} entries must be strings")
        match = EXACT_REQUIREMENT_RE.fullmatch(value)
        if match is None:
            raise DependencyLockError(f"{context} dependency is not exact: {value}")
        name = match.group(1).lower()
        if name in result:
            raise DependencyLockError(f"duplicate {context} dependency: {name}")
        result[name] = match.group(2)
    return result


def validate_dependency_contract(
    lock_path: Path, pyproject_path: Path, dockerfile_path: Path
) -> dict[str, Any]:
    lock = _load_json(lock_path)
    if lock.get("schema_version") != 1:
        raise DependencyLockError("dependency lock schema_version must be 1")

    dependencies = lock.get("dependencies")
    if not isinstance(dependencies, dict):
        raise DependencyLockError("dependency lock must contain dependencies")
    missing_native = REQUIRED_NATIVE_DEPENDENCIES.difference(dependencies)
    if missing_native:
        raise DependencyLockError(
            f"dependency lock is missing native entries: {sorted(missing_native)}"
        )

    for name, raw_entry in dependencies.items():
        if not isinstance(name, str) or not isinstance(raw_entry, dict):
            raise DependencyLockError("native dependency entries must be objects")
        entry: dict[str, Any] = raw_entry
        for field in (
            "version",
            "source_url",
            "source_ref",
            "source_commit",
            "license",
            "role",
        ):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise DependencyLockError(f"{name}.{field} must be a nonempty string")
        _require_https(entry["source_url"], f"{name}.source_url")
        if name != "sqlite" and not GIT_COMMIT_RE.fullmatch(entry["source_commit"]):
            raise DependencyLockError(f"{name}.source_commit must be a full Git commit")
        if name == "sqlite" and not SHA256_RE.fullmatch(entry["source_commit"]):
            raise DependencyLockError("sqlite.source_commit must be its full source ID")

        archive_fields = {"archive_url", "archive_sha256", "archive_bytes"}
        present_archive_fields = archive_fields.intersection(entry)
        if name in ARCHIVE_DEPENDENCIES and present_archive_fields != archive_fields:
            raise DependencyLockError(f"{name} must have a complete archive identity")
        if present_archive_fields:
            if present_archive_fields != archive_fields:
                raise DependencyLockError(f"{name} has a partial archive identity")
            _require_https(entry["archive_url"], f"{name}.archive_url")
            if not isinstance(entry["archive_sha256"], str) or not SHA256_RE.fullmatch(
                entry["archive_sha256"]
            ):
                raise DependencyLockError(f"{name}.archive_sha256 must be SHA-256")
            if (
                not isinstance(entry["archive_bytes"], int)
                or entry["archive_bytes"] <= 0
            ):
                raise DependencyLockError(f"{name}.archive_bytes must be positive")

    try:
        with pyproject_path.open("rb") as handle:
            pyproject = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DependencyLockError(f"cannot read pyproject.toml: {error}") from error

    project = pyproject.get("project")
    build_system = pyproject.get("build-system")
    groups = pyproject.get("dependency-groups")
    if (
        not isinstance(project, dict)
        or not isinstance(build_system, dict)
        or not isinstance(groups, dict)
    ):
        raise DependencyLockError("pyproject dependency sections are incomplete")
    if project.get("requires-python") != ">=3.12,<3.13":
        raise DependencyLockError("requires-python must stay within Python 3.12")

    runtime = _exact_requirements(project.get("dependencies"), "runtime")
    development = _exact_requirements(groups.get("dev"), "development")
    build = _exact_requirements(build_system.get("requires"), "build-system")
    if REQUIRED_PYTHON_DEPENDENCIES.difference(runtime):
        raise DependencyLockError("runtime dependency ownership set is incomplete")
    if REQUIRED_DEVELOPMENT_DEPENDENCIES.difference(development):
        raise DependencyLockError("development dependency ownership set is incomplete")
    if set(build) != {"pybind11", "scikit-build-core"}:
        raise DependencyLockError(
            "build-system must contain only its exact direct requirements"
        )
    if build["pybind11"] != dependencies["pybind11"]["version"]:
        raise DependencyLockError("Python and native pybind11 versions disagree")
    if runtime["pyarrow"] != dependencies["apache-arrow"]["version"]:
        raise DependencyLockError("PyArrow and native Arrow versions disagree")

    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    if PINNED_IMAGE_RE.search(dockerfile) is None:
        raise DependencyLockError("Linux qualification image must use a SHA-256 digest")
    if "snapshot.debian.org/archive/debian/20260813T000000Z" not in dockerfile:
        raise DependencyLockError(
            "Linux qualification image must use the frozen snapshot"
        )
    if "git=1:2.39.5-0+deb12u3" not in dockerfile:
        raise DependencyLockError(
            "Linux qualification image must pin Git for release-state validation"
        )
    if "uv-0.11.23" not in dockerfile or "sha256=7a85330d" not in dockerfile:
        raise DependencyLockError(
            "Linux qualification uv wheel must be versioned and hashed"
        )
    return lock


def self_test() -> int:
    if SHA256_RE.fullmatch("0" * 64) is None or SHA256_RE.fullmatch("z" * 64):
        print("dependency-lock self-test failed", file=sys.stderr)
        return 1
    if EXACT_REQUIREMENT_RE.fullmatch("pytest>=9") is not None:
        print("dependency-lock self-test accepted a range", file=sys.stderr)
        return 1
    print("dependency-lock self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    root = Path(__file__).resolve().parents[1]
    try:
        lock = validate_dependency_contract(
            root / "cmake" / "dependencies.lock.json",
            root / "pyproject.toml",
            root / "docker" / "linux-x86_64.Dockerfile",
        )
    except (DependencyLockError, OSError) as error:
        print(f"dependency-lock: {error}", file=sys.stderr)
        return 1
    print(f"validated {len(lock['dependencies'])} locked native dependencies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
