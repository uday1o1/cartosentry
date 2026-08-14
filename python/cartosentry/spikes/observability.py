"""M0.5 motion, alignment, map-candidate, and routing qualification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Self, cast

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry import _core


class ObservabilityParameters(BaseModel):
    """Frozen parameters used by native synthetic and public-data checks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    injected_point_time_shift_ns: int = Field(gt=0)
    injected_trajectory_shift_m: float = Field(gt=0.0)
    lidar_point_stride: int = Field(gt=0)
    map_trajectory_stride_rows: int = Field(gt=0)
    candidate_search_radius_m: float = Field(gt=0.0)
    confident_lateral_distance_m: float = Field(gt=0.0)
    confident_heading_error_rad: float = Field(gt=0.0)
    confident_score_separation: float = Field(ge=0.0)
    minimum_moving_speed_mps: float = Field(ge=0.0)
    minimum_alignment_separation_m: float = Field(gt=0.0)


class ObservabilityGate(BaseModel):
    """Frozen acceptance gate for the M0.5 risk-retirement spike."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1)
    spike_version: str = Field(min_length=1)
    sequence_id: str = Field(min_length=1)
    graph_import_profile: str = Field(min_length=1)
    distance_coverage_method: str = Field(min_length=1)
    parameters: ObservabilityParameters
    required_observable_scenarios: list[str] = Field(min_length=1)
    required_nonobservable_controls: list[str] = Field(min_length=1)
    expected_scenario_ids: list[str] = Field(min_length=1)
    expected_synthetic_scenarios: int = Field(gt=0)
    minimum_lidar_frames: int = Field(ge=2)
    minimum_sampled_points: int = Field(gt=0)
    minimum_imported_directed_arcs: int = Field(gt=0)
    minimum_confident_public_moving_distance_fraction: float = Field(ge=0.85, le=1.0)
    require_public_observable_motion: bool
    require_public_observable_structure: bool
    require_public_point_time_separation: bool
    require_public_trajectory_separation: bool
    require_exact_route_validation: bool
    require_exact_route_brute_force_match: bool

    @model_validator(mode="after")
    def validate_scenario_contract(self) -> Self:
        expected = set(self.expected_scenario_ids)
        observable = set(self.required_observable_scenarios)
        nonobservable = set(self.required_nonobservable_controls)
        if len(expected) != len(self.expected_scenario_ids):
            raise ValueError("expected synthetic scenario identifiers must be unique")
        if observable & nonobservable:
            raise ValueError("observable and nonobservable scenarios must be disjoint")
        if not (observable | nonobservable) <= expected:
            raise ValueError("required scenarios must be members of the expected set")
        if self.expected_synthetic_scenarios != len(expected):
            raise ValueError("synthetic scenario count must match expected identifiers")
        return self


def _load_gate(path: Path) -> ObservabilityGate:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError("observability gate is unavailable or malformed") from error
    return ObservabilityGate.model_validate(loaded)


def _normalize_floats(value: Any) -> Any:
    if isinstance(value, float):
        rounded = round(value, 9)
        return 0.0 if rounded == 0.0 else rounded
    if isinstance(value, dict):
        return {key: _normalize_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_floats(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_floats(item) for item in value]
    return value


def _check(name: str, observed: Any, required: str, passed: bool) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "observed": observed,
        "required": required,
    }


def _evaluate_gate(
    normalized: dict[str, Any], gate: ObservabilityGate
) -> list[dict[str, Any]]:
    scenarios = cast(list[dict[str, Any]], normalized["synthetic_scenarios"])
    scenario_by_id = {cast(str, item["scenario_id"]): item for item in scenarios}
    scenario_ids = [cast(str, item["scenario_id"]) for item in scenarios]
    alignment = cast(dict[str, Any], normalized["public_alignment"])
    map_match = cast(dict[str, Any], normalized["public_map_match"])
    tiny_route = cast(dict[str, Any], normalized["tiny_route"])
    checks = [
        _check(
            "spike-version",
            normalized["spike_version"],
            f"equal to {gate.spike_version}",
            normalized["spike_version"] == gate.spike_version,
        ),
        _check(
            "synthetic-scenario-count",
            len(scenarios),
            f"equal to {gate.expected_synthetic_scenarios}",
            len(scenarios) == gate.expected_synthetic_scenarios,
        ),
        _check(
            "synthetic-scenario-identities",
            scenario_ids,
            f"exactly {gate.expected_scenario_ids}",
            len(scenario_ids) == len(set(scenario_ids))
            and set(scenario_ids) == set(gate.expected_scenario_ids),
        ),
        _check(
            "public-sequence",
            alignment["sequence_id"],
            f"equal to {gate.sequence_id}",
            alignment["sequence_id"] == gate.sequence_id,
        ),
        _check(
            "public-lidar-frame-support",
            alignment["lidar_frames"],
            f"at least {gate.minimum_lidar_frames}",
            alignment["lidar_frames"] >= gate.minimum_lidar_frames,
        ),
        _check(
            "public-point-support",
            alignment["sampled_points"],
            f"at least {gate.minimum_sampled_points}",
            alignment["sampled_points"] >= gate.minimum_sampled_points,
        ),
        _check(
            "directed-graph-profile",
            map_match["graph_import_profile"],
            f"equal to {gate.graph_import_profile}",
            map_match["graph_import_profile"] == gate.graph_import_profile,
        ),
        _check(
            "distance-coverage-method",
            map_match["distance_coverage_method"],
            f"equal to {gate.distance_coverage_method}",
            map_match["distance_coverage_method"] == gate.distance_coverage_method,
        ),
        _check(
            "directed-graph-arc-support",
            map_match["imported_directed_arcs"],
            f"at least {gate.minimum_imported_directed_arcs}",
            map_match["imported_directed_arcs"] >= gate.minimum_imported_directed_arcs,
        ),
        _check(
            "confident-public-moving-distance",
            map_match["confident_distance_fraction"],
            (f"at least {gate.minimum_confident_public_moving_distance_fraction}"),
            map_match["confident_distance_fraction"]
            >= gate.minimum_confident_public_moving_distance_fraction,
        ),
    ]
    for scenario_id in gate.required_observable_scenarios:
        scenario = scenario_by_id.get(scenario_id)
        passed = (
            scenario is not None
            and scenario["observability"] == "OBSERVABLE"
            and scenario["point_time_shift_separated"] is True
            and scenario["trajectory_shift_separated"] is True
        )
        checks.append(
            _check(
                f"observable-synthetic-{scenario_id}",
                scenario,
                "OBSERVABLE with both injected defects separated",
                passed,
            )
        )
    for scenario_id in gate.required_nonobservable_controls:
        scenario = scenario_by_id.get(scenario_id)
        passed = (
            scenario is not None
            and scenario["observability"] == "NOT_OBSERVABLE"
            and scenario["point_time_shift_separated"] is False
            and scenario["trajectory_shift_separated"] is False
        )
        checks.append(
            _check(
                f"nonobservable-synthetic-{scenario_id}",
                scenario,
                "NOT_OBSERVABLE without passing defect separation",
                passed,
            )
        )
    required_public = (
        ("public-observable-motion", "observable_motion", "motion observability")
        if gate.require_public_observable_motion
        else None,
        (
            "public-observable-structure",
            "observable_structure",
            "scene-structure observability",
        )
        if gate.require_public_observable_structure
        else None,
        (
            "public-point-time-separation",
            "point_time_shift_separated",
            "point-time perturbation separation",
        )
        if gate.require_public_point_time_separation
        else None,
        (
            "public-trajectory-separation",
            "trajectory_shift_separated",
            "trajectory perturbation separation",
        )
        if gate.require_public_trajectory_separation
        else None,
    )
    for requirement in required_public:
        if requirement is not None:
            name, field, description = requirement
            checks.append(
                _check(
                    name,
                    alignment[field],
                    f"true for {description}",
                    alignment[field] is True,
                )
            )
    if gate.require_exact_route_validation:
        checks.append(
            _check(
                "tiny-route-independent-validation",
                tiny_route["exact_route_valid"],
                "true",
                tiny_route["exact_route_valid"] is True,
            )
        )
    if gate.require_exact_route_brute_force_match:
        checks.append(
            _check(
                "tiny-route-brute-force-equality",
                {
                    "exact": tiny_route["exact_cost"],
                    "brute_force": tiny_route["brute_force_cost"],
                    "match": tiny_route["exact_matches_brute_force"],
                },
                "exact cost equal to independent brute-force cost",
                tiny_route["exact_matches_brute_force"] is True,
            )
        )
    return checks


def qualify_observability(
    sequence_root: Path,
    *,
    road_graph_path: Path,
    gate_path: Path,
) -> dict[str, Any]:
    """Run the complete M0.5 spike and evaluate its frozen gate."""

    gate = _load_gate(gate_path)
    parameters = gate.parameters
    raw = _core.run_observability_spike(
        str(sequence_root),
        str(road_graph_path),
        parameters.injected_point_time_shift_ns,
        parameters.injected_trajectory_shift_m,
        parameters.lidar_point_stride,
        parameters.map_trajectory_stride_rows,
        parameters.candidate_search_radius_m,
        parameters.confident_lateral_distance_m,
        parameters.confident_heading_error_rad,
        parameters.confident_score_separation,
        parameters.minimum_moving_speed_mps,
        parameters.minimum_alignment_separation_m,
    )
    runtime = {"elapsed_seconds": raw.pop("elapsed_seconds")}
    raw["parameters"] = parameters.model_dump(mode="json")
    normalized = cast(dict[str, Any], _normalize_floats(raw))
    runtime = cast(dict[str, Any], _normalize_floats(runtime))
    serialized = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    checks = _evaluate_gate(normalized, gate)
    accepted = all(cast(bool, check["passed"]) for check in checks)
    return {
        "schema_version": "cartosentry.observability-report.v1",
        "state": "ACCEPTED" if accepted else "FAILED",
        "accepted": accepted,
        "normalized_sha256": hashlib.sha256(serialized).hexdigest(),
        "normalized": normalized,
        "runtime": runtime,
        "gate_checks": checks,
    }
