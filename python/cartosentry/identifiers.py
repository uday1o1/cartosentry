"""Deterministic identifiers for persisted CartoSentry artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_PORTABLE_KEYS = frozenset(
    {
        "absolute_path",
        "host_name",
        "hostname",
        "local_context",
        "local_path",
        "machine_id",
        "source_root",
        "source_roots",
    }
)


def _path_leak(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        normalized.startswith(("/", "~/", "//", "file://"))
        or _WINDOWS_ABSOLUTE.match(value) is not None
        or ".." in normalized.split("/")
    )


def assert_portable(value: object, *, location: str = "artifact") -> None:
    """Reject local paths, traversal, and machine identity from portable values."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"portable {location} contains a non-string key")
            key_text = key
            if key_text.lower() in _FORBIDDEN_PORTABLE_KEYS:
                raise ValueError(
                    f"portable {location} contains machine-local field {key_text!r}"
                )
            assert_portable(item, location=f"{location}.{key_text}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            assert_portable(item, location=f"{location}[{index}]")
        return
    if isinstance(value, str) and _path_leak(value):
        raise ValueError(
            f"portable {location} contains a local or traversing path: {value!r}"
        )


def canonical_json_bytes(value: object) -> bytes:
    """Serialize a portable semantic value in one stable JSON representation."""

    assert_portable(value, location="identifier input")
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _identifier(kind: str, value: object) -> str:
    return f"{kind}-sha256-{canonical_sha256(value)}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise ValueError("identifier component has no portable characters")
    return slug[:32]


def make_sequence_id(identity: Mapping[str, object]) -> str:
    """Derive sequence identity from a normalized source-manifest identity."""

    return _identifier("sequence", identity)


def make_stream_id(sequence_id: str, modality: str, sensor_id: str) -> str:
    """Derive an ID that visibly and cryptographically binds sensor identity."""

    digest = canonical_sha256(
        {
            "modality": modality,
            "sensor_id": sensor_id,
            "sequence_id": sequence_id,
        }
    )
    return f"stream-{_slug(modality)}-{_slug(sensor_id)}-{digest}"


def make_frame_id(
    stream_id: str,
    source_frame_key: str,
    capture_interval: Mapping[str, object],
) -> str:
    return _identifier(
        "frame",
        {
            "capture_interval": capture_interval,
            "source_frame_key": source_frame_key,
            "stream_id": stream_id,
        },
    )


def make_finding_id(
    *,
    detector_id: str,
    detector_version: str,
    rule_id: str,
    source_interval: Mapping[str, object],
    stream_ids: Sequence[str],
    evidence_fingerprint: object,
) -> str:
    return _identifier(
        "finding",
        {
            "detector_id": detector_id,
            "detector_version": detector_version,
            "evidence_fingerprint": evidence_fingerprint,
            "rule_id": rule_id,
            "source_interval": source_interval,
            "stream_ids": sorted(stream_ids),
        },
    )


def make_road_bin_id(
    road_graph_id: str, directed_arc_id: str, longitudinal_bin_index: int
) -> str:
    if longitudinal_bin_index < 0:
        raise ValueError("longitudinal bin index must be nonnegative")
    return _identifier(
        "road-bin",
        {
            "directed_arc_id": directed_arc_id,
            "longitudinal_bin_index": longitudinal_bin_index,
            "road_graph_id": road_graph_id,
        },
    )


def make_run_id(
    *,
    sequence_id: str,
    road_graph_id: str,
    profile_id: str,
    engine_version: str,
    configuration_hashes: Mapping[str, str],
) -> str:
    return _identifier(
        "run",
        {
            "configuration_hashes": dict(sorted(configuration_hashes.items())),
            "engine_version": engine_version,
            "profile_id": profile_id,
            "road_graph_id": road_graph_id,
            "sequence_id": sequence_id,
        },
    )


def make_recapture_plan_id(identity: Mapping[str, object]) -> str:
    return _identifier("recapture-plan", identity)


def make_bundle_id(identity: Mapping[str, object]) -> str:
    return _identifier("accepted-bundle", identity)


def make_calibration_id(identity: Mapping[str, object]) -> str:
    return _identifier("calibration", identity)


def make_requirement_id(identity: Mapping[str, object]) -> str:
    return _identifier("requirement", identity)


__all__ = [
    "assert_portable",
    "canonical_json_bytes",
    "canonical_sha256",
    "make_bundle_id",
    "make_calibration_id",
    "make_finding_id",
    "make_frame_id",
    "make_recapture_plan_id",
    "make_requirement_id",
    "make_road_bin_id",
    "make_run_id",
    "make_sequence_id",
    "make_stream_id",
]
