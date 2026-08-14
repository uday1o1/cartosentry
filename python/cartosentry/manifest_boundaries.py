"""Bounded, duplicate-safe JSON boundaries for persisted ingestion artifacts."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

MAXIMUM_INGESTION_BUDGET_BYTES = 64 * 1024
MAXIMUM_FRAME_INDEX_LINE_BYTES = 1024 * 1024
MAXIMUM_ARTIFACT_JSON_BYTES = 16 * 1024 * 1024
MAXIMUM_JSON_DEPTH = 64


class ManifestBoundaryError(ValueError):
    """Raised when a persisted manifest crosses a frozen safety boundary."""


def _check_json_depth(content: bytes, *, context: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in content:
        if in_string:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                in_string = False
            continue
        if byte == ord('"'):
            in_string = True
        elif byte in (ord("{"), ord("[")):
            depth += 1
            if depth > MAXIMUM_JSON_DEPTH:
                raise ManifestBoundaryError(
                    f"{context} nesting exceeds the supported depth"
                )
        elif byte in (ord("}"), ord("]")) and depth > 0:
            depth -= 1


def decode_bounded_json(content: bytes, *, maximum_bytes: int, context: str) -> object:
    """Decode one bounded JSON value while rejecting duplicate object keys."""

    if not content or len(content) > maximum_bytes:
        raise ManifestBoundaryError(f"{context} size is outside the supported range")
    _check_json_depth(content, context=context)

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ManifestBoundaryError(f"{context} contains a duplicate key")
            value[key] = item
        return value

    def reject_nonfinite_constant(value: str) -> object:
        raise ManifestBoundaryError(
            f"{context} contains non-standard numeric constant {value!r}"
        )

    try:
        return json.loads(
            content,
            object_pairs_hook=unique_object,
            parse_constant=reject_nonfinite_constant,
        )
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as error:
        raise ManifestBoundaryError(f"{context} is not valid JSON") from error


@contextmanager
def _open_regular_binary(path: Path, *, context: str) -> Iterator[BinaryIO]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ManifestBoundaryError(f"{context} is missing or unsafe") from error
    try:
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        with stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ManifestBoundaryError(f"{context} is not a regular file")
            yield stream
    except BaseException:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        raise


def read_bounded_json(
    path: Path, *, maximum_bytes: int, context: str
) -> tuple[object, bytes]:
    """Read and decode one bounded regular JSON file without following symlinks."""

    content = read_bounded_regular_bytes(
        path,
        maximum_bytes=maximum_bytes,
        context=context,
    )
    return (
        decode_bounded_json(content, maximum_bytes=maximum_bytes, context=context),
        content,
    )


def read_bounded_regular_bytes(
    path: Path, *, maximum_bytes: int, context: str
) -> bytes:
    """Read one nonempty bounded regular file without following symbolic links."""

    with _open_regular_binary(path, context=context) as stream:
        metadata = os.fstat(stream.fileno())
        if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
            raise ManifestBoundaryError(
                f"{context} size is outside the supported range"
            )
        content = stream.read(maximum_bytes + 1)
    if not content or len(content) > maximum_bytes:
        raise ManifestBoundaryError(f"{context} size is outside the supported range")
    return content


def iter_bounded_json_lines(
    path: Path, *, maximum_line_bytes: int, context: str
) -> Iterator[tuple[int, bytes, object]]:
    """Incrementally decode bounded JSONL records from one regular file."""

    with _open_regular_binary(path, context=context) as stream:
        line_number = 0
        while line := stream.readline(maximum_line_bytes + 1):
            line_number += 1
            if len(line) > maximum_line_bytes:
                raise ManifestBoundaryError(
                    f"{context} line {line_number} exceeds the supported size"
                )
            yield (
                line_number,
                line,
                decode_bounded_json(
                    line,
                    maximum_bytes=maximum_line_bytes,
                    context=f"{context} line {line_number}",
                ),
            )
