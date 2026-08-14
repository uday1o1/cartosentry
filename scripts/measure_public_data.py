#!/usr/bin/env python3
"""Measure input storage rates for pinned Boreas artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from public_data_manifest import ManifestError, selected_artifacts, validate_contract


def _timestamp_extent(
    path: Path, expected_field: str, timestamp_divisor: float = 1.0
) -> Tuple[float, float]:
    first: Optional[float] = None
    last: Optional[float] = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or expected_field not in reader.fieldnames:
            raise ManifestError(f"{path} is missing timestamp field {expected_field}")
        for row in reader:
            try:
                value = float(row[expected_field]) / timestamp_divisor
            except (TypeError, ValueError) as error:
                raise ManifestError(f"{path} contains an invalid timestamp") from error
            if first is None:
                first = value
            if last is not None and value <= last:
                raise ManifestError(f"{path} timestamps are not strictly increasing")
            last = value
    if first is None or last is None or last <= first:
        raise ManifestError(f"{path} has insufficient timestamp support")
    return first, last


def _stream_measurement(
    data_root: Path,
    artifact: Dict[str, Any],
    suffix: str,
    timestamp_field: str,
    timestamp_divisor: float = 1.0,
) -> Dict[str, Any]:
    matches = [obj for obj in artifact["objects"] if obj["key"].endswith(suffix)]
    if len(matches) != 1:
        raise ManifestError(f"{artifact['id']} does not contain exactly one {suffix}")
    obj = matches[0]
    first, last = _timestamp_extent(
        data_root / obj["key"], timestamp_field, timestamp_divisor
    )
    support_seconds = last - first
    return {
        "bytes": obj["bytes"],
        "support_seconds": support_seconds,
        "bytes_per_second": obj["bytes"] / support_seconds,
    }


def _lidar_measurement(data_root: Path, artifact: Dict[str, Any]) -> Dict[str, Any]:
    objects = [obj for obj in artifact["objects"] if "/lidar/" in obj["key"]]
    if len(objects) < 2:
        raise ManifestError(f"{artifact['id']} has insufficient lidar frames")
    timestamps = [int(Path(obj["key"]).stem) / 1_000_000.0 for obj in objects]
    periods = [later - earlier for earlier, later in zip(timestamps, timestamps[1:])]
    if any(period <= 0.0 for period in periods):
        raise ManifestError(
            f"{artifact['id']} lidar timestamps are not strictly increasing"
        )
    support_seconds = timestamps[-1] - timestamps[0] + statistics.median(periods)
    total_bytes = sum(obj["bytes"] for obj in objects)
    return {
        "frames": len(objects),
        "bytes": total_bytes,
        "support_seconds": support_seconds,
        "bytes_per_second": total_bytes / support_seconds,
        "mebibytes_per_second": total_bytes / support_seconds / (1024 * 1024),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path("benchmarks/data_manifest.yaml")
    )
    parser.add_argument(
        "--source-groups", type=Path, default=Path("benchmarks/source_groups.yaml")
    )
    parser.add_argument("--data-root", type=Path, default=Path("data/public"))
    args = parser.parse_args()
    try:
        manifest = validate_contract(
            args.manifest.resolve(), args.source_groups.resolve()
        )
        artifacts = selected_artifacts(
            manifest,
            "public-smoke",
            ["boreas-public-smoke-clear-v1", "boreas-public-smoke-snow-v1"],
        )
        result: Dict[str, Any] = {"schema_version": 1, "artifacts": {}}
        for artifact in artifacts:
            result["artifacts"][artifact["id"]] = {
                "gps_post_process": _stream_measurement(
                    args.data_root,
                    artifact,
                    "/applanix/gps_post_process.csv",
                    "GPSTime",
                ),
                "lidar_poses": _stream_measurement(
                    args.data_root,
                    artifact,
                    "/applanix/lidar_poses.csv",
                    "GPSTime",
                    1_000_000.0,
                ),
                "lidar": _lidar_measurement(args.data_root, artifact),
            }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ManifestError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
