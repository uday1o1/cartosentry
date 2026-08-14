#!/usr/bin/env python3
"""Atheris target for Python binary and persisted manifest boundaries."""

from __future__ import annotations

import sys
from collections.abc import Callable

import atheris  # type: ignore[import-untyped]

with atheris.instrument_imports():
    from cartosentry.adapters.boreas_v1 import parse_boreas_lidar_frame_bytes
    from cartosentry.artifacts import validate_artifact_bytes
    from cartosentry.faults import parse_fault_manifest_bytes
    from cartosentry.ingestion import (
        parse_frame_index_line,
        parse_ingestion_budget_bytes,
    )
    from cartosentry.runstate import (
        ArtifactIntegrityError,
        parse_attempt_manifest_bytes,
        parse_completion_pointer_bytes,
        parse_run_inputs_bytes,
    )
    from cartosentry.synthetic import parse_fixture_set_manifest_bytes

PARSERS: tuple[Callable[[bytes], object], ...] = (
    parse_ingestion_budget_bytes,
    parse_frame_index_line,
    parse_run_inputs_bytes,
    parse_attempt_manifest_bytes,
    parse_completion_pointer_bytes,
    parse_fault_manifest_bytes,
    parse_boreas_lidar_frame_bytes,
    validate_artifact_bytes,
    parse_fixture_set_manifest_bytes,
)


def test_one_input(data: bytes) -> None:
    if not data:
        return
    try:
        PARSERS[data[0] % len(PARSERS)](data[1:])
    except (ArtifactIntegrityError, ValueError):
        return


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
