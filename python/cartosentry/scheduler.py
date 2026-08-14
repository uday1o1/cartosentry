"""Typed public qualification for the native bounded scheduler."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from cartosentry import _core
from cartosentry.contracts import ContractModel
from cartosentry.identifiers import assert_portable

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class SchedulerStressSuite(ContractModel):
    schema_version: Literal[1]
    suite_id: Literal["m2.3-v1"]
    worker_count: Literal[4]
    resident_byte_budget: Literal[32768]
    mixed_unit_count: Literal[2000]
    lidar_stride: Literal[10]
    imu_estimated_bytes: Literal[64]
    lidar_estimated_bytes: Literal[4096]

    @model_validator(mode="after")
    def validate_task_sizes(self) -> Self:
        if max(self.imu_estimated_bytes, self.lidar_estimated_bytes) > (
            self.resident_byte_budget
        ):
            raise ValueError("stress work units must fit the resident byte budget")
        return self


class SchedulerQualificationReport(ContractModel):
    schema_version: Literal["cartosentry.scheduler-qualification.v1"]
    suite_id: Identifier
    suite_sha256: Sha256
    accepted: bool
    resident_byte_budget: int = Field(gt=0)
    peak_resident_bytes: int = Field(ge=0)
    mixed_completed_units: int = Field(ge=0)
    mixed_imu_units: int = Field(ge=0)
    mixed_lidar_units: int = Field(ge=0)
    deterministic_replay_equal: bool
    deterministic_execution_order: tuple[str, ...]
    backpressure_observed: bool
    isolated_failed_units: int = Field(ge=0)
    isolated_completed_units: int = Field(ge=0)
    structured_error_codes: tuple[Literal["TASK_FAILURE", "UNHANDLED_EXCEPTION"], ...]
    cancelled_units: int = Field(ge=0)
    outstanding_units_after_cancel: int = Field(ge=0)
    resident_bytes_after_cancel: int = Field(ge=0)
    completion_pointer_exists: bool

    @model_validator(mode="after")
    def validate_acceptance_claim(self) -> Self:
        expected = (
            self.peak_resident_bytes <= self.resident_byte_budget
            and self.mixed_completed_units == 2000
            and self.mixed_imu_units == 1800
            and self.mixed_lidar_units == 200
            and self.deterministic_replay_equal
            and self.deterministic_execution_order
            == (
                "metadata-1",
                "imu-1",
                "lidar-1",
                "metadata-2",
                "imu-2",
                "lidar-2",
            )
            and self.backpressure_observed
            and self.isolated_failed_units == 2
            and self.isolated_completed_units == 1
            and set(self.structured_error_codes)
            == {"TASK_FAILURE", "UNHANDLED_EXCEPTION"}
            and self.cancelled_units == 4
            and self.outstanding_units_after_cancel == 0
            and self.resident_bytes_after_cancel == 0
            and not self.completion_pointer_exists
        )
        if self.accepted != expected:
            raise ValueError("scheduler acceptance disagrees with measured evidence")
        return self

    def portable_dict(self) -> dict[str, object]:
        value = self.model_dump(mode="json")
        assert_portable(value)
        return value


def load_scheduler_stress_suite(
    path: Path,
) -> tuple[SchedulerStressSuite, str]:
    content = path.read_bytes()
    return (
        SchedulerStressSuite.model_validate(json.loads(content)),
        hashlib.sha256(content).hexdigest(),
    )


def qualify_scheduler(
    output_root: Path, suite_path: Path
) -> SchedulerQualificationReport:
    """Exercise fairness, boundedness, failures, and cancellation natively."""

    suite, suite_sha256 = load_scheduler_stress_suite(suite_path)
    native = _core.qualify_bounded_scheduler(
        str(output_root),
        suite.worker_count,
        suite.resident_byte_budget,
        suite.mixed_unit_count,
        suite.lidar_stride,
        suite.imu_estimated_bytes,
        suite.lidar_estimated_bytes,
    )
    return SchedulerQualificationReport.model_validate(
        {
            "schema_version": "cartosentry.scheduler-qualification.v1",
            "suite_id": suite.suite_id,
            "suite_sha256": suite_sha256,
            **native,
            "deterministic_execution_order": tuple(
                native["deterministic_execution_order"]
            ),
            "structured_error_codes": tuple(native["structured_error_codes"]),
        }
    )


__all__ = [
    "SchedulerQualificationReport",
    "SchedulerStressSuite",
    "load_scheduler_stress_suite",
    "qualify_scheduler",
]
