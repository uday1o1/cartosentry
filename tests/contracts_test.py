"""Analytic and schema tests for canonical time and geometry contracts."""

from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

from cartosentry.cli import app
from cartosentry.contracts import (
    MAX_INT64,
    MIN_INT64,
    CorrectedTime,
    Duration,
    FrameInterval,
    FrameTimes,
    GlobalCoordinate,
    LocalOrigin,
    NamedFrame,
    RawTime,
    RawTimeEncoding,
    RigidTransform,
    TimeEpoch,
    TimePoint,
    TimeReference,
    TimeRounding,
    UnitQuaternion,
    VerticalDatum,
)
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from typer.testing import CliRunner


def integer_raw_time(
    value_ns: int,
    *,
    epoch: TimeEpoch = TimeEpoch.UNIX_UTC,
    reference: TimeReference = TimeReference.SAMPLE,
) -> RawTime:
    return RawTime(
        source_key="synthetic/clock.json",
        field="timestamp",
        unit="ns",
        epoch=epoch,
        reference=reference,
        encoding=RawTimeEncoding.SIGNED_INTEGER,
        integer_value=str(value_ns),
        rounding=TimeRounding.EXACT,
        maximum_conversion_error_ns=0.0,
    )


def time_point(
    value_ns: int,
    *,
    epoch: TimeEpoch = TimeEpoch.UNIX_UTC,
    clock_id: str = "synthetic-clock",
    reference: TimeReference = TimeReference.SAMPLE,
) -> TimePoint:
    return TimePoint(
        value_ns=value_ns,
        epoch=epoch,
        clock_id=clock_id,
        reference=reference,
        raw=integer_raw_time(value_ns, epoch=epoch, reference=reference),
    )


def identity_transform(target_frame: str, source_frame: str) -> RigidTransform:
    return RigidTransform(
        target_frame=target_frame,
        source_frame=source_frame,
        translation_m=(0.0, 0.0, 0.0),
        rotation=UnitQuaternion(w=1.0, x=0.0, y=0.0, z=0.0),
    )


class TimeContractTest(unittest.TestCase):
    def test_decimal_time_preserves_lexeme_and_integer_nanoseconds(self) -> None:
        point = TimePoint.from_decimal_seconds(
            "1630597311.041161",
            source_key="applanix/gps_post_process.csv",
            field="GPSTime",
            epoch=TimeEpoch.UNIX_UTC,
            clock_id="boreas-applanix",
            reference=TimeReference.SAMPLE,
        )
        self.assertEqual(1_630_597_311_041_161_000, point.value_ns)
        self.assertEqual("1630597311.041161", point.raw.decimal_lexeme)
        self.assertEqual(
            point,
            TimePoint.model_validate_json(point.model_dump_json()),
        )

    @given(st.integers(min_value=MIN_INT64, max_value=MAX_INT64))
    def test_signed_int64_time_json_round_trip_has_zero_mismatches(
        self, value_ns: int
    ) -> None:
        point = time_point(value_ns)
        recovered = TimePoint.model_validate_json(point.model_dump_json())
        self.assertEqual(value_ns, recovered.value_ns)

    def test_persisted_time_rejects_float_nanoseconds(self) -> None:
        payload = time_point(1).model_dump(mode="json")
        payload["value_ns"] = 1.0
        with self.assertRaises(ValidationError):
            TimePoint.model_validate(payload)

    def test_time_addition_rejects_signed_int64_overflow(self) -> None:
        self.assertEqual(11, time_point(10).shifted_value_ns(Duration(value_ns=1)))
        with self.assertRaisesRegex(OverflowError, "out of range"):
            time_point(MAX_INT64).shifted_value_ns(Duration(value_ns=1))

    def test_raw_time_domain_must_match_canonical_time_domain(self) -> None:
        with self.assertRaisesRegex(ValidationError, "epochs differ"):
            TimePoint(
                value_ns=1,
                epoch=TimeEpoch.GPS,
                clock_id="sensor",
                reference=TimeReference.SAMPLE,
                raw=integer_raw_time(1, epoch=TimeEpoch.UNIX_UTC),
            )

    def test_raw_time_requires_exactly_its_declared_representation(self) -> None:
        payload = integer_raw_time(1).model_dump()
        payload["decimal_lexeme"] = "0.000000001"
        with self.assertRaisesRegex(ValidationError, "exactly"):
            RawTime.model_validate(payload)
        payload = integer_raw_time(1).model_dump()
        payload.update(
            {
                "encoding": RawTimeEncoding.DECIMAL_LEXEME,
                "integer_value": None,
                "decimal_lexeme": "1e-9",
                "rounding": TimeRounding.NEAREST_NANOSECOND_HALF_AWAY_FROM_ZERO,
                "maximum_conversion_error_ns": 0.5,
            }
        )
        with self.assertRaisesRegex(ValidationError, "plain decimal"):
            RawTime.model_validate(payload)

    def test_missing_time_epoch_or_reference_is_rejected(self) -> None:
        payload = time_point(1).model_dump(mode="json")
        del payload["epoch"]
        with self.assertRaises(ValidationError):
            TimePoint.model_validate(payload)
        payload = time_point(1).model_dump(mode="json")
        del payload["reference"]
        with self.assertRaises(ValidationError):
            TimePoint.model_validate(payload)

    def test_incomparable_clocks_and_epochs_are_rejected(self) -> None:
        end = time_point(10)
        with self.assertRaisesRegex(ValueError, "incomparable"):
            end.difference(time_point(0, clock_id="another-clock"))
        with self.assertRaisesRegex(ValueError, "incomparable"):
            end.difference(time_point(0, epoch=TimeEpoch.GPS))

    def test_frame_interval_is_nonempty_and_half_open(self) -> None:
        interval = FrameInterval(
            capture_start=time_point(10), capture_end=time_point(20)
        )
        self.assertEqual(
            10, interval.capture_end.difference(interval.capture_start).value_ns
        )
        with self.assertRaises(ValidationError):
            FrameInterval(capture_start=time_point(10), capture_end=time_point(10))
        with self.assertRaises(ValidationError):
            FrameInterval(capture_start=time_point(20), capture_end=time_point(10))

    def test_instantaneous_frame_does_not_invent_capture_interval(self) -> None:
        frame = FrameTimes(sensor_time=time_point(100))
        self.assertIsNone(frame.capture_start)
        self.assertIsNone(frame.capture_end)
        with self.assertRaises(ValidationError):
            FrameTimes(capture_start=time_point(100))

    def test_clock_correction_retains_original_domain_and_applicability(self) -> None:
        correction = CorrectedTime(
            original=time_point(
                15, epoch=TimeEpoch.SENSOR_BOOT, clock_id="sensor-boot"
            ),
            corrected_value_ns=1_700_000_000_000_000_015,
            target_epoch=TimeEpoch.UNIX_UTC,
            target_clock_id="utc",
            correction_model_id="clock-linear-v1",
            correction_model_sha256="1" * 64,
            pivot_ns=0,
            offset_ns=1_700_000_000_000_000_000,
            rate_ppb=0.0,
            uncertainty_ns=100,
            applicability=FrameInterval(
                capture_start=time_point(
                    10, epoch=TimeEpoch.SENSOR_BOOT, clock_id="sensor-boot"
                ),
                capture_end=time_point(
                    20, epoch=TimeEpoch.SENSOR_BOOT, clock_id="sensor-boot"
                ),
            ),
        )
        self.assertEqual(TimeEpoch.UNIX_UTC, correction.target_epoch)
        self.assertEqual(TimeEpoch.SENSOR_BOOT, correction.original.epoch)
        self.assertNotIn("CORRECTED_COMMON", [item.value for item in TimeEpoch])
        with self.assertRaisesRegex(ValidationError, "retain sensor_time"):
            FrameTimes(
                sensor_time=time_point(
                    16, epoch=TimeEpoch.SENSOR_BOOT, clock_id="sensor-boot"
                ),
                corrected_sensor_time=correction,
            )
        with self.assertRaises(ValidationError):
            CorrectedTime(
                **(
                    correction.model_dump()
                    | {
                        "original": time_point(
                            20,
                            epoch=TimeEpoch.SENSOR_BOOT,
                            clock_id="sensor-boot",
                        )
                    }
                )
            )

    def test_time_schema_has_required_tags_and_no_float_second_timestamp(self) -> None:
        schema = TimePoint.model_json_schema()
        self.assertEqual(
            {"value_ns", "epoch", "clock_id", "reference", "raw"},
            set(schema["required"]),
        )
        serialized = json.dumps(schema, sort_keys=True)
        self.assertNotIn("timestamp_us", serialized)
        self.assertNotIn('"value_s"', serialized)

    def test_no_contract_schema_persists_untyped_floating_point_seconds(self) -> None:
        schema_models = (
            CorrectedTime,
            Duration,
            FrameInterval,
            FrameTimes,
            GlobalCoordinate,
            LocalOrigin,
            NamedFrame,
            RawTime,
            RigidTransform,
            TimePoint,
            UnitQuaternion,
        )
        for model in schema_models:
            with self.subTest(model=model.__name__):
                schema = model.model_json_schema()
                stack: list[object] = [schema]
                while stack:
                    value = stack.pop()
                    if isinstance(value, dict):
                        properties = value.get("properties", {})
                        assert isinstance(properties, dict)
                        for name, field_schema in properties.items():
                            if name.endswith(("_s", "_seconds", "seconds")):
                                self.assertNotEqual("number", field_schema.get("type"))
                        stack.extend(value.values())
                    elif isinstance(value, list):
                        stack.extend(value)


class GeometryContractTest(unittest.TestCase):
    def test_named_rig_frame_is_right_handed_x_forward_y_left_z_up(self) -> None:
        rig = NamedFrame.canonical_rig()
        self.assertEqual("RIGHT_HANDED", rig.handedness)
        self.assertEqual(
            ("forward", "left", "up"), (rig.x_axis, rig.y_axis, rig.z_axis)
        )

    def test_T_world_rig_normalizes_rig_source_quaternion(self) -> None:
        quaternion = UnitQuaternion(w=1.0000005, x=0.0, y=0.0, z=0.0)
        self.assertAlmostEqual(1.0, quaternion.w)
        self.assertAlmostEqual(
            1.0, math.sqrt(sum(value * value for value in quaternion.as_wxyz()))
        )
        with self.assertRaises(ValueError):
            UnitQuaternion(w=1.000002, x=0.0, y=0.0, z=0.0)
        with self.assertRaises(ValueError):
            UnitQuaternion(w=0.0, x=0.0, y=0.0, z=0.0)
        positive = UnitQuaternion(w=0.0, x=0.0, y=0.0, z=1.0)
        negative = UnitQuaternion(w=0.0, x=0.0, y=0.0, z=-1.0)
        self.assertEqual(positive, negative)

    def test_T_world_rig_rejects_reflection_from_rig_source(self) -> None:
        reflection = (-1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        with self.assertRaisesRegex(ValueError, "proper rigid rotation"):
            UnitQuaternion.from_rotation_matrix(reflection)

    def test_T_world_rig_and_T_rig_lidar_compose_named_source_target(self) -> None:
        world_from_rig = identity_transform("world", "rig").model_copy(
            update={"translation_m": (10.0, 0.0, 0.0)}
        )
        rig_from_lidar = identity_transform("rig", "lidar").model_copy(
            update={"translation_m": (0.0, 2.0, 0.0)}
        )
        world_from_lidar = world_from_rig.compose(rig_from_lidar)
        self.assertEqual("world", world_from_lidar.target_frame)
        self.assertEqual("lidar", world_from_lidar.source_frame)
        self.assertEqual((11.0, 3.0, 1.0), world_from_lidar.apply((1.0, 1.0, 1.0)))
        with self.assertRaisesRegex(ValueError, "outer source frame"):
            rig_from_lidar.compose(world_from_rig)

    def test_T_rig_world_inverse_round_trip_names_world_source_rig_target(self) -> None:
        world_from_rig = RigidTransform(
            target_frame="world",
            source_frame="rig",
            translation_m=(6_378_137.0, -4_000_000.0, 2_000_000.0),
            rotation=UnitQuaternion(w=1.0, x=0.0, y=0.0, z=0.0),
        )
        point_rig = (1000.0, -2000.0, 3000.0)
        point_world = world_from_rig.apply(point_rig)
        rig_from_world = world_from_rig.inverse()
        recovered = rig_from_world.apply(point_world)
        self.assertEqual("rig", rig_from_world.target_frame)
        self.assertEqual("world", rig_from_world.source_frame)
        self.assertLessEqual(
            max(abs(left - right) for left, right in zip(point_rig, recovered)),
            1e-9,
        )

    def test_T_world_rig_source_target_json_round_trip_preserves_wxyz(
        self,
    ) -> None:
        world_from_rig = RigidTransform(
            target_frame="world",
            source_frame="rig",
            translation_m=(1.0, 2.0, 3.0),
            rotation=UnitQuaternion(w=0.5, x=0.5, y=0.5, z=0.5),
        )
        recovered = RigidTransform.model_validate_json(world_from_rig.model_dump_json())
        self.assertEqual(world_from_rig, recovered)
        self.assertEqual("wxyz", recovered.rotation.serialization_order)

    def test_T_world_rig_interpolation_rejects_source_target_extrapolation(
        self,
    ) -> None:
        begin = identity_transform("world", "rig")
        end = RigidTransform(
            target_frame="world",
            source_frame="rig",
            translation_m=(10.0, 0.0, 0.0),
            rotation=UnitQuaternion(w=0.0, x=0.0, y=0.0, z=1.0),
        )
        midpoint = begin.interpolate(end, 0.5)
        transformed = midpoint.apply((1.0, 0.0, 0.0))
        self.assertAlmostEqual(5.0, transformed[0], places=12)
        self.assertAlmostEqual(1.0, transformed[1], places=12)
        with self.assertRaisesRegex(ValueError, "extrapolation"):
            begin.interpolate(end, -0.01)
        with self.assertRaisesRegex(ValueError, "extrapolation"):
            begin.interpolate(end, 1.01)

    def test_WGS84_global_local_world_round_trip_is_below_one_millimeter(self) -> None:
        origin_global = GlobalCoordinate(
            latitude_deg=43.784,
            longitude_deg=-79.472,
            altitude_m=183.0,
            vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
        )
        origin = LocalOrigin(
            frame=NamedFrame(
                frame_id="local_world",
                x_axis="east",
                y_axis="north",
                z_axis="up",
            ),
            global_coordinate=origin_global,
        )
        point = GlobalCoordinate(
            latitude_deg=43.9,
            longitude_deg=-79.3,
            altitude_m=250.0,
            vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
        )
        local = origin.to_local(point)
        recovered = origin.to_global(local)
        residual = origin.to_local(recovered)
        self.assertLessEqual(
            math.hypot(
                residual.position_m[0] - local.position_m[0],
                residual.position_m[1] - local.position_m[1],
            ),
            0.001,
        )

    def test_WGS84_local_world_rejects_unknown_vertical_datum(self) -> None:
        origin = GlobalCoordinate(
            latitude_deg=43.784,
            longitude_deg=-79.472,
            altitude_m=183.0,
            vertical_datum=VerticalDatum.UNKNOWN_VERTICAL_DATUM,
        )
        with self.assertRaises(ValidationError):
            LocalOrigin(
                frame=NamedFrame(
                    frame_id="local_world",
                    x_axis="east",
                    y_axis="north",
                    z_axis="up",
                ),
                global_coordinate=origin,
            )


def test_public_cli_qualifies_all_frozen_m1_1_numerical_contracts() -> None:
    result = CliRunner().invoke(app, ["qualify-contracts"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["accepted"] is True
    assert len(report["checks"]) == 5
    assert all(item["passed"] for item in report["checks"])
    assert all(report["controls"].values())
    assert len(report["evidence_scope"]) == 5


def test_public_cli_rejects_post_freeze_threshold_change(tmp_path: Path) -> None:
    charter_path = Path("benchmarks/numerical_charter.yaml")
    charter = json.loads(charter_path.read_text(encoding="utf-8"))
    charter["gates"]["geometry.se3_point_roundtrip_m"]["value"] = 1.0
    modified_path = tmp_path / "modified-charter.json"
    modified_path.write_text(json.dumps(charter), encoding="utf-8")

    result = CliRunner().invoke(
        app, ["qualify-contracts", "--charter", str(modified_path)]
    )

    assert result.exit_code == 2
    assert "immutable hash is invalid" in result.output


if __name__ == "__main__":
    unittest.main()
