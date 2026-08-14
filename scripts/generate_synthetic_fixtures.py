#!/usr/bin/env python3
"""Generate or verify the committed deterministic M1.3 fixture set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cartosentry.synthetic import materialize_fixture_set


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if files are stale")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("tests/fixtures/synthetic/v1"),
        help="fixture-set destination",
    )
    parser.add_argument(
        "--split-manifest",
        type=Path,
        default=Path("benchmarks/split_manifest.yaml"),
        help="frozen synthetic-family assignment",
    )
    arguments = parser.parse_args()
    report = materialize_fixture_set(
        arguments.output_root,
        arguments.split_manifest,
        check=arguments.check,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
