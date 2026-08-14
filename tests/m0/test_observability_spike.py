"""Native and public contracts for the M0.5 observability spike."""

from __future__ import annotations

from pathlib import Path

import pytest
from cartosentry import _core
from cartosentry.cli import app
from cartosentry.spikes.observability import ObservabilityGate
from pydantic import ValidationError
from typer.testing import CliRunner


def _gate_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "spike_version": "m0.5-observability-v1",
        "sequence_id": "boreas-2021-09-02-11-42",
        "graph_import_profile": "m0.5-directed-candidate-v1",
        "distance_coverage_method": "endpoint-half-distance-v1",
        "parameters": {
            "injected_point_time_shift_ns": 100_000_000,
            "injected_trajectory_shift_m": 1.0,
            "lidar_point_stride": 128,
            "map_trajectory_stride_rows": 200,
            "candidate_search_radius_m": 30.0,
            "confident_lateral_distance_m": 8.0,
            "confident_heading_error_rad": 0.7853981633974483,
            "confident_score_separation": 0.15,
            "minimum_moving_speed_mps": 1.0,
            "minimum_alignment_separation_m": 0.05,
        },
        "required_observable_scenarios": ["turning", "moving"],
        "required_nonobservable_controls": ["static", "sparse_structure"],
        "expected_scenario_ids": [
            "straight",
            "turning",
            "static",
            "sparse_structure",
            "moving",
        ],
        "expected_synthetic_scenarios": 5,
        "minimum_lidar_frames": 10,
        "minimum_sampled_points": 5000,
        "minimum_imported_directed_arcs": 100,
        "minimum_confident_public_moving_distance_fraction": 0.85,
        "require_public_observable_motion": True,
        "require_public_observable_structure": True,
        "require_public_point_time_separation": True,
        "require_public_trajectory_separation": True,
        "require_exact_route_validation": True,
        "require_exact_route_brute_force_match": True,
    }


def test_native_synthetic_suite_separates_only_observable_controls() -> None:
    scenarios = _core.run_synthetic_observability_suite(100_000_000, 1.0, 0.05)
    by_id = {scenario["scenario_id"]: scenario for scenario in scenarios}

    assert len(scenarios) == 5
    for scenario_id in ("turning", "moving"):
        assert by_id[scenario_id]["observability"] == "OBSERVABLE"
        assert by_id[scenario_id]["point_time_shift_separated"] is True
        assert by_id[scenario_id]["trajectory_shift_separated"] is True
    for scenario_id in ("static", "sparse_structure"):
        assert by_id[scenario_id]["observability"] == "NOT_OBSERVABLE"
        assert by_id[scenario_id]["point_time_shift_separated"] is False
        assert by_id[scenario_id]["trajectory_shift_separated"] is False


def test_native_synthetic_suite_rejects_invalid_perturbations() -> None:
    with pytest.raises(ValueError, match="observability parameters"):
        _core.run_synthetic_observability_suite(0, 1.0, 0.05)


def test_native_tiny_route_matches_brute_force_and_independent_validator() -> None:
    route = _core.solve_tiny_required_route()

    assert route["exact_cost"] == 8.0
    assert route["brute_force_cost"] == 8.0
    assert route["exact_matches_brute_force"] is True
    assert route["exact_route_valid"] is True


def test_gate_cannot_weaken_public_coverage_below_plan_floor() -> None:
    weakened = _gate_payload()
    weakened["minimum_confident_public_moving_distance_fraction"] = 0.849

    with pytest.raises(ValidationError):
        ObservabilityGate.model_validate(weakened)


def test_public_cli_exposes_the_complete_observability_workflow() -> None:
    result = CliRunner().invoke(app, ["qualify-observability", "--help"])

    assert result.exit_code == 0
    assert "--road-graph" in result.output
    assert "--gate" in result.output
    assert "motion compensation" in result.output
    assert str(Path.cwd()) not in result.output
