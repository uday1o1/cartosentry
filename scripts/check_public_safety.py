#!/usr/bin/env python3
"""Reject private-domain references and machine-specific personal paths."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

MAX_SCAN_BYTES = 8 * 1024 * 1024
URL_PATTERN = re.compile(r"https?://[^\s<>()\]}'\"]+")
POSIX_PERSONAL_PATH = re.compile(r"/(?:Users|home)/[^/\s]+(?:/|\b)")
MACOS_TEMP_PATH = re.compile(r"/(?:private/)?var/folders/[^\s]+")
WINDOWS_PERSONAL_PATH = re.compile(r"(?i)[a-z]:\\Users\\[^\\\s]+(?:\\|\b)")
DEFAULT_PRIVATE_DOMAIN_LABELS = frozenset(
    {
        "corp",
        "corporate",
        "internal",
        "intranet",
        "jira",
        "confluence",
        "artifactory",
    }
)
ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _configured_private_labels() -> frozenset[str]:
    configured = os.environ.get("CARTOSENTRY_PRIVATE_DOMAIN_LABELS", "")
    additions = {
        value.strip().lower() for value in configured.split(",") if value.strip()
    }
    return DEFAULT_PRIVATE_DOMAIN_LABELS | additions


def _is_private_host(host: str, private_labels: frozenset[str]) -> bool:
    normalized = host.rstrip(".").lower()
    if not normalized or normalized in ALLOWED_LOCAL_HOSTS:
        return False

    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        labels = set(normalized.split("."))
        return bool(labels & private_labels) or normalized.endswith((".lan", ".local"))

    return address.is_private or address.is_link_local


def violations(text: str) -> list[tuple[str, int]]:
    """Return rule identifiers and one-based line numbers for unsafe text."""

    findings: list[tuple[str, int]] = []
    private_labels = _configured_private_labels()

    for line_number, line in enumerate(text.splitlines(), start=1):
        if POSIX_PERSONAL_PATH.search(line):
            findings.append(("personal-posix-path", line_number))
        if MACOS_TEMP_PATH.search(line):
            findings.append(("machine-temp-path", line_number))
        if WINDOWS_PERSONAL_PATH.search(line):
            findings.append(("personal-windows-path", line_number))
        for match in URL_PATTERN.finditer(line):
            host = urlsplit(match.group(0)).hostname or ""
            if _is_private_host(host, private_labels):
                findings.append(("private-domain", line_number))

    return findings


def _repository_files(root: Path) -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    names = [name for name in result.stdout.split(b"\0") if name]
    return [root / os.fsdecode(name) for name in names]


def _read_candidate(path: Path, root: Path) -> str | None:
    if path.is_symlink():
        return os.readlink(path)

    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError):
        raise ValueError(
            f"refusing to scan path outside the repository: {path}"
        ) from None

    if not resolved.is_file():
        return None

    size = resolved.stat().st_size
    if size > MAX_SCAN_BYTES:
        raise ValueError(f"tracked file exceeds the public-safety scan limit: {path}")

    content = resolved.read_bytes()
    if b"\0" in content:
        return None
    return content.decode("utf-8", errors="replace")


def scan(paths: list[Path], root: Path) -> int:
    unsafe = False
    for path in paths:
        try:
            text = _read_candidate(path, root)
        except (OSError, ValueError) as error:
            print(f"public-safety: {error}", file=sys.stderr)
            unsafe = True
            continue

        if text is None:
            continue
        relative = path.relative_to(root)
        for rule, line_number in violations(text):
            print(f"{relative}:{line_number}: {rule}", file=sys.stderr)
            unsafe = True
    return 1 if unsafe else 0


def self_test() -> int:
    unsafe_cases = {
        "private-domain": "See https://docs." + "internal.example/spec",
        "personal-posix-path": "/" + "Users/example/project/output.json",
        "personal-windows-path": "C:\\" + "Users\\example\\project\\output.json",
        "machine-temp-path": "/private/" + "var/folders/example/output.json",
    }
    for expected_rule, sample in unsafe_cases.items():
        rules = {rule for rule, _ in violations(sample)}
        if expected_rule not in rules:
            print(f"self-test failed to detect {expected_rule}", file=sys.stderr)
            return 1

    safe_sample = (
        "Public docs: https://nvidia.github.io/ncore/data/formats and /data/demo"
    )
    if violations(safe_sample):
        print(
            "self-test rejected a public source or generic data path", file=sys.stderr
        )
        return 1
    if not _is_private_host("docs.restricted.example", frozenset({"restricted"})):
        print("self-test ignored a configured private-domain label", file=sys.stderr)
        return 1
    print("public-safety self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="repository-relative files")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="exercise positive and negative controls",
    )
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    root = Path(__file__).resolve().parents[1]
    paths = (
        [root / path for path in args.paths] if args.paths else _repository_files(root)
    )
    return scan(paths, root)


if __name__ == "__main__":
    raise SystemExit(main())
