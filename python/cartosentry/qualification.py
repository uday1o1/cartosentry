"""Public numerical qualification for canonical M1.1 contracts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from cartosentry.contracts import (
    MAX_INT64,
    MIN_INT64,
    GlobalCoordinate,
    LocalOrigin,
    NamedFrame,
    RigidTransform,
    TimeEpoch,
    TimePoint,
    TimeReference,
    UnitQuaternion,
    VerticalDatum,
)

QUALIFICATION_VERSION = "m1.1-contracts-v1"
GATE_KEYS = (
    "geometry.wgs84_local_roundtrip_m",
    "geometry.se3_point_roundtrip_m",
    "geometry.rotation_orthonormality_frobenius",
    "geometry.quaternion_norm_deviation",
    "time.persisted_integer_mismatch_count",
)


def _load_gates(charter_path: Path) -> dict[str, dict[str, Any]]:
    try:
        charter = json.loads(charter_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("numerical charter is unavailable or malformed") from error
    if not isinstance(charter, dict) or not isinstance(charter.get("gates"), dict):
        raise ValueError("numerical charter does not contain named gates")
    expected_hash = charter.get("immutable_sha256")
    unhashed = {
        key: value for key, value in charter.items() if key != "immutable_sha256"
    }
    canonical = json.dumps(
        unhashed, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if expected_hash != hashlib.sha256(canonical).hexdigest():
        raise ValueError("numerical charter immutable hash is invalid")
    raw_gates = cast(dict[str, Any], charter["gates"])
    gates: dict[str, dict[str, Any]] = {}
    for key in GATE_KEYS:
        raw_gate = raw_gates.get(key)
        if not isinstance(raw_gate, dict):
            raise ValueError(f"numerical charter is missing M1.1 gate {key}")
        gate = cast(dict[str, Any], raw_gate)
        value = gate.get("value")
        if gate.get("operator") != "max_le":
            raise ValueError(f"M1.1 gate {key} must use max_le")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"M1.1 gate {key} must have a numeric maximum")
        if not isinstance(gate.get("unit"), str):
            raise ValueError(f"M1.1 gate {key} must have a unit")
        gates[key] = gate
    return gates


def _time_mismatches() -> int:
    mismatches = 0
    lexemes = {
        MIN_INT64: "-9223372036.854775808",
        -1: "-0.000000001",
        0: "0.000000000",
        1: "0.000000001",
        MAX_INT64: "9223372036.854775807",
    }
    for value_ns, lexeme in lexemes.items():
        point = TimePoint.from_decimal_seconds(
            lexeme,
            source_key="synthetic/qualification.json",
            field="timestamp",
            epoch=TimeEpoch.UNIX_UTC,
            clock_id="qualification-clock",
            reference=TimeReference.SAMPLE,
        )
        recovered = TimePoint.model_validate_json(point.model_dump_json())
        mismatches += int(recovered.value_ns != value_ns)
    return mismatches


def _rotation_orthonormality_error(quaternion: UnitQuaternion) -> float:
    w, x, y, z = quaternion.as_wxyz()
    rotation = (
        (1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)),
        (2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)),
        (2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)),
    )
    squared_error = 0.0
    for row in range(3):
        for column in range(3):
            inner = sum(
                rotation[index][row] * rotation[index][column] for index in range(3)
            )
            expected = 1.0 if row == column else 0.0
            squared_error += (inner - expected) ** 2
    return math.sqrt(squared_error)


def _se3_roundtrip_error() -> float:
    half_sqrt = math.sqrt(0.5)
    transform = RigidTransform(
        target_frame="world",
        source_frame="rig",
        translation_m=(6_378_137.0, -4_000_000.0, 2_000_000.0),
        rotation=UnitQuaternion(w=half_sqrt, x=0.0, y=0.0, z=half_sqrt),
    )
    source = (1000.0, -2000.0, 3000.0)
    recovered = transform.inverse().apply(transform.apply(source))
    return max(abs(left - right) for left, right in zip(source, recovered))


def _wgs84_roundtrip_error() -> float:
    origin = LocalOrigin(
        frame=NamedFrame(
            frame_id="local_world",
            x_axis="east",
            y_axis="north",
            z_axis="up",
        ),
        global_coordinate=GlobalCoordinate(
            latitude_deg=43.784,
            longitude_deg=-79.472,
            altitude_m=183.0,
            vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
        ),
    )
    maximum = 0.0
    for point in (
        GlobalCoordinate(
            latitude_deg=43.794,
            longitude_deg=-79.462,
            altitude_m=210.0,
            vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
        ),
        GlobalCoordinate(
            latitude_deg=43.9,
            longitude_deg=-79.3,
            altitude_m=250.0,
            vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
        ),
    ):
        local = origin.to_local(point)
        recovered_local = origin.to_local(origin.to_global(local))
        maximum = max(
            maximum,
            math.hypot(
                recovered_local.position_m[0] - local.position_m[0],
                recovered_local.position_m[1] - local.position_m[1],
            ),
        )
    return maximum


def qualify_contracts(charter_path: Path) -> dict[str, Any]:
    """Exercise the persisted and native contract path against frozen gates."""

    gates = _load_gates(charter_path)
    recoverable_input = (1.0 + 5e-7, 0.0, 0.0, 0.0)
    quaternion = UnitQuaternion(
        w=recoverable_input[0],
        x=recoverable_input[1],
        y=recoverable_input[2],
        z=recoverable_input[3],
    )
    measurements = {
        "geometry.wgs84_local_roundtrip_m": _wgs84_roundtrip_error(),
        "geometry.se3_point_roundtrip_m": _se3_roundtrip_error(),
        "geometry.rotation_orthonormality_frobenius": (
            _rotation_orthonormality_error(quaternion)
        ),
        "geometry.quaternion_norm_deviation": abs(
            math.sqrt(sum(value * value for value in recoverable_input)) - 1.0
        ),
        "time.persisted_integer_mismatch_count": _time_mismatches(),
    }
    controls: dict[str, bool] = {}
    control_operations: dict[str, Callable[[], object]] = {
        "invalid_quaternion_rejected": lambda: UnitQuaternion(
            w=1.0 + 2e-6, x=0.0, y=0.0, z=0.0
        ),
        "reflection_matrix_rejected": lambda: UnitQuaternion.from_rotation_matrix(
            (-1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        ),
        "extrapolation_rejected": lambda: RigidTransform(
            target_frame="world",
            source_frame="rig",
            translation_m=(0.0, 0.0, 0.0),
            rotation=UnitQuaternion(w=1.0, x=0.0, y=0.0, z=0.0),
        ).interpolate(
            RigidTransform(
                target_frame="world",
                source_frame="rig",
                translation_m=(1.0, 0.0, 0.0),
                rotation=UnitQuaternion(w=1.0, x=0.0, y=0.0, z=0.0),
            ),
            1.01,
        ),
    }
    for name, operation in control_operations.items():
        try:
            operation()
        except ValueError:
            controls[name] = True
        else:
            controls[name] = False
    checks = [
        {
            "charter_key": key,
            "observed": measurements[key],
            "required_maximum": gates[key]["value"],
            "unit": gates[key]["unit"],
            "passed": measurements[key] <= gates[key]["value"],
        }
        for key in GATE_KEYS
    ]
    return {
        "schema_version": 1,
        "qualification_version": QUALIFICATION_VERSION,
        "measurements": measurements,
        "evidence_scope": {
            "geometry.wgs84_local_roundtrip_m": "two Toronto-area global points",
            "geometry.se3_point_roundtrip_m": "one large-translation rotated point",
            "geometry.rotation_orthonormality_frobenius": (
                "one normalized recoverable quaternion"
            ),
            "geometry.quaternion_norm_deviation": (
                "one near-tolerance recoverable quaternion plus rejection control"
            ),
            "time.persisted_integer_mismatch_count": (
                "signed-int64 endpoints, negative one, zero, and positive one"
            ),
        },
        "controls": controls,
        "checks": checks,
        "accepted": all(item["passed"] for item in checks) and all(controls.values()),
    }


__all__ = ["qualify_contracts"]
