#!/usr/bin/env python3
"""Exercise and hash every representative frozen V1 fault operator."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from cartosentry.faults import (
    FaultOperatorId,
    FaultRequest,
    inject_fault,
    load_fault_registry,
    serialize_fault_manifest,
    verify_fault_result,
)
from cartosentry.synthetic import generate_fixture, serialize_fixture
from cartosentry.synthetic_models import SyntheticScenario

REPRESENTATIVE_CASES = {
    FaultOperatorId.TIMESTAMP_DISCONTINUITY: "timestamp-gap-50ms-below",
    FaultOperatorId.POSITION_JUMP: "position-jump-0p05m-below",
    FaultOperatorId.POSITION_FREEZE: "position-freeze-0p25s-below",
    FaultOperatorId.POSITION_BIAS: "position-bias-0p25m-below",
    FaultOperatorId.POSITION_DRIFT: "position-drift-0p1m-below",
    FaultOperatorId.POINT_TIME_SHIFT: "point-time-10ms-below",
    FaultOperatorId.RING_LOSS: "ring-loss-1-short",
    FaultOperatorId.AZIMUTH_SECTOR_LOSS: "sector-loss-5deg-short",
    FaultOperatorId.CALIBRATION_PERTURBATION: ("extrinsic-0p01m-0p1deg-below"),
}
QUALIFICATION_SEED = 90210
CLEAN_TRUTH_HASH = hashlib.sha256(
    b"cartosentry-m1.4-qualification-clean-truth\n"
).hexdigest()


def qualify(source_path: Path, matrix_path: Path) -> dict[str, object]:
    source = source_path.read_bytes()
    sector_source = serialize_fixture(
        generate_fixture(
            "fault-sector-family",
            SyntheticScenario.STRAIGHT,
            1701,
            azimuth_columns=32,
        )
    )
    registry = load_fault_registry(matrix_path)
    operators: list[dict[str, object]] = []
    for operator_id in FaultOperatorId:
        selected_source = (
            sector_source
            if operator_id is FaultOperatorId.AZIMUTH_SECTOR_LOSS
            else source
        )
        request = FaultRequest(
            operator_id=operator_id,
            case_id=REPRESENTATIVE_CASES[operator_id],
            seed=QUALIFICATION_SEED,
            clean_source_truth_sha256=CLEAN_TRUTH_HASH,
        )
        result = inject_fault(selected_source, request, registry)
        manifest_bytes = serialize_fault_manifest(result.manifest)
        verification = verify_fault_result(
            selected_source,
            result.derivative_bytes,
            manifest_bytes,
            registry,
        )
        operators.append(
            {
                "accepted": verification["accepted"],
                "attributed_change_count": len(result.manifest.changed_values),
                "case_id": request.case_id,
                "derivative_sha256": hashlib.sha256(
                    result.derivative_bytes
                ).hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "operator_id": operator_id.value,
            }
        )
    return {
        "accepted": all(item["accepted"] is True for item in operators),
        "fault_matrix_sha256": registry.matrix_sha256,
        "operators": operators,
        "qualification_seed": QUALIFICATION_SEED,
        "source_sha256": hashlib.sha256(source).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("tests/fixtures/synthetic/v1/fixtures/sensor-map-dev-001.json"),
    )
    parser.add_argument(
        "--fault-matrix",
        type=Path,
        default=Path("benchmarks/fault_matrix_v1.yaml"),
    )
    arguments = parser.parse_args()
    report = qualify(arguments.source, arguments.fault_matrix)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
