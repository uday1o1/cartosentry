"""Frozen M5.5 repeated-trajectory topology-hypothesis qualification."""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)
from cartosentry.road_bins import (
    PROFILE_FILE_SHA256 as ROAD_BINNING_PROFILE_FILE_SHA256,
)
from cartosentry.road_matching import ALGORITHM_BACKEND
from cartosentry.topology_hypotheses import (
    OffMapTrajectoryInterval,
    TopologyGraphArc,
    TopologyGraphNode,
    TopologyGraphView,
    TopologyHypothesisKind,
    TopologyHypothesisReport,
    load_topology_hypothesis_profile,
    make_off_map_trajectory_interval,
    make_topology_graph_view_from_primitives,
    mine_topology_hypotheses,
)

GATE_IMMUTABLE_SHA256 = (
    "6d2ce0b9ebec0febeda498a2013531db7d41de1fc7f278c994beef3c1e49b7fb"
)
MAXIMUM_GATE_BYTES = 256 * 1024
MAXIMUM_CHARTER_BYTES = 1024 * 1024
ROAD_BIN_LENGTH_M = 20.0


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


class GateAuthorities(StrictModel):
    topology_hypothesis_profile_file_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    topology_hypothesis_profile_immutable_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ]
    road_binning_profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    numerical_charter_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SyntheticPopulation(StrictModel):
    family_count: Literal[12]
    independent_traversals_per_scenario: Literal[5]
    positive_scenarios_per_family: Literal[3]
    expected_positive_hypothesis_count: Literal[36]
    mutation_kinds: tuple[
        Literal["MISSING_CONNECTION"],
        Literal["PERTURBED_GEOMETRY"],
        Literal["ALTERED_CONNECTION"],
    ]
    control_kinds: tuple[Literal["PARALLEL_ROAD"], Literal["UNCHANGED"]]
    scenario_length_m: Annotated[float, Field(ge=100.0, le=100.0)]
    unchanged_exposure_km_per_family: Annotated[float, Field(ge=4.0, le=4.0)]
    noise_offsets_m: tuple[
        Annotated[float, Field(ge=-0.4, le=-0.4)],
        Annotated[float, Field(ge=-0.2, le=-0.2)],
        Annotated[float, Field(ge=0.0, le=0.0)],
        Annotated[float, Field(ge=0.2, le=0.2)],
        Annotated[float, Field(ge=0.4, le=0.4)],
    ]

    @model_validator(mode="after")
    def validate_population(self) -> Self:
        if (
            self.family_count * self.positive_scenarios_per_family
            != self.expected_positive_hypothesis_count
            or len(self.noise_offsets_m) != self.independent_traversals_per_scenario
        ):
            raise ValueError("M5.5 synthetic population support is inconsistent")
        return self


class GateThresholds(StrictModel):
    synthetic_precision_minimum: Annotated[float, Field(ge=0.9, le=0.9)]
    synthetic_recall_minimum: Annotated[float, Field(ge=0.8, le=0.8)]
    endpoint_error_road_bins_maximum: Annotated[float, Field(ge=1.0, le=1.0)]
    false_hypotheses_per_unchanged_km_maximum: Annotated[float, Field(ge=0.1, le=0.1)]


class GateStatistics(StrictModel):
    bootstrap_unit: Literal["synthetic_family_id"]
    bootstrap_seed: Literal[2026081403]
    bootstrap_replicates: Literal[10000]
    confidence_level: Annotated[float, Field(ge=0.95, le=0.95)]
    minimum_independent_clusters: Literal[12]
    minimum_expected_positive_hypotheses: Literal[30]
    zero_count_rate_bound: Literal["EXACT_ONE_SIDED_POISSON_UPPER"]


class TopologyHypothesisGate(StrictModel):
    schema_version: Literal[1]
    gate_id: Literal["m5.5-topology-hypotheses-v1"]
    gate_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_M5_5_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: GateAuthorities
    synthetic_population: SyntheticPopulation
    thresholds: GateThresholds
    statistics: GateStatistics

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"immutable_sha256"})
        if (
            self.immutable_sha256 != GATE_IMMUTABLE_SHA256
            or _canonical_hash(payload) != self.immutable_sha256
        ):
            raise ValueError("M5.5 topology-hypothesis gate identity is not pinned")
        return self


def load_topology_hypothesis_gate(
    path: Path,
) -> tuple[TopologyHypothesisGate, str]:
    """Load and authenticate the frozen M5.5 acceptance gate."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="M5.5 topology-hypothesis gate",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_GATE_BYTES,
            context="M5.5 topology-hypothesis gate",
        )
    except ManifestBoundaryError as error:
        raise ValueError(
            "M5.5 topology-hypothesis gate is unavailable or malformed"
        ) from error
    if not isinstance(decoded, dict):
        raise ValueError("M5.5 topology-hypothesis gate must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("M5.5 topology-hypothesis gate immutable hash is invalid")
    return (
        TopologyHypothesisGate.model_validate_json(content),
        hashlib.sha256(content).hexdigest(),
    )


def _load_numerical_gates(path: Path, expected_sha256: str) -> dict[str, float]:
    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_CHARTER_BYTES,
            context="numerical charter",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_CHARTER_BYTES,
            context="numerical charter",
        )
    except ManifestBoundaryError as error:
        raise ValueError("numerical charter is unavailable or malformed") from error
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("M5.5 numerical charter authority is foreign")
    if not isinstance(decoded, dict) or not isinstance(decoded.get("gates"), dict):
        raise ValueError("numerical charter does not declare gates")
    gates = cast(dict[str, object], decoded["gates"])
    expected = {
        "topology.synthetic_precision": "fraction_ge",
        "topology.synthetic_recall": "fraction_ge",
        "topology.endpoint_error_bins": "median_le",
        "topology.false_hypotheses_per_unchanged_km": "rate_le",
    }
    result: dict[str, float] = {}
    for key, operator in expected.items():
        raw_gate = gates.get(key)
        if not isinstance(raw_gate, dict) or raw_gate.get("operator") != operator:
            raise ValueError(f"numerical charter gate {key} is malformed")
        value = raw_gate.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"numerical charter gate {key} has an invalid value")
        result[key] = float(value)
    return result


def _node(node_id: str, x_m: float, y_m: float) -> TopologyGraphNode:
    return TopologyGraphNode(node_id=node_id, position_local_m=(x_m, y_m))


def _arc(
    arc_id: str,
    start: TopologyGraphNode,
    end: TopologyGraphNode,
    middle: tuple[float, float] | None = None,
) -> TopologyGraphArc:
    geometry = (
        (start.position_local_m, end.position_local_m)
        if middle is None
        else (start.position_local_m, middle, end.position_local_m)
    )
    return TopologyGraphArc(
        arc_id=arc_id,
        from_node_id=start.node_id,
        to_node_id=end.node_id,
        geometry_local_m=geometry,
    )


def _family_inputs(
    family_index: int,
    gate: TopologyHypothesisGate,
) -> tuple[
    TopologyGraphView,
    tuple[OffMapTrajectoryInterval, ...],
    set[tuple[TopologyHypothesisKind, str, str]],
    dict[str, str],
]:
    family_id = f"m5.5-family-{family_index:02d}"
    nodes: list[TopologyGraphNode] = []
    arcs: list[TopologyGraphArc] = []
    scenario_nodes: dict[str, tuple[TopologyGraphNode, TopologyGraphNode]] = {}

    def add_nodes(
        scenario: str, y_m: float
    ) -> tuple[TopologyGraphNode, TopologyGraphNode]:
        start = _node(f"{family_id}-{scenario}-start", 0.0, y_m)
        end = _node(f"{family_id}-{scenario}-end", 100.0, y_m)
        nodes.extend((start, end))
        scenario_nodes[scenario] = (start, end)
        return start, end

    missing_start, missing_end = add_nodes("missing", 0.0)
    geometry_start, geometry_end = add_nodes("geometry", 100.0)
    arcs.append(
        _arc(
            f"{family_id}-geometry-arc",
            geometry_start,
            geometry_end,
            (50.0, 112.0 + family_index * 0.05),
        )
    )
    altered_start, altered_end = add_nodes("altered", 200.0)
    altered_wrong = _node(f"{family_id}-altered-wrong-end", 100.0, 216.0)
    nodes.append(altered_wrong)
    arcs.append(
        _arc(
            f"{family_id}-altered-arc",
            altered_start,
            altered_wrong,
            (50.0, 208.0),
        )
    )
    parallel_lower_start = _node(f"{family_id}-parallel-lower-start", 0.0, 300.0)
    parallel_lower_end = _node(f"{family_id}-parallel-lower-end", 100.0, 300.0)
    parallel_upper_start = _node(f"{family_id}-parallel-upper-start", 0.0, 308.0)
    parallel_upper_end = _node(f"{family_id}-parallel-upper-end", 100.0, 308.0)
    nodes.extend(
        (
            parallel_lower_start,
            parallel_lower_end,
            parallel_upper_start,
            parallel_upper_end,
        )
    )
    scenario_nodes["parallel"] = (parallel_upper_start, parallel_upper_end)
    arcs.extend(
        (
            _arc(
                f"{family_id}-parallel-lower-arc",
                parallel_lower_start,
                parallel_lower_end,
            ),
            _arc(
                f"{family_id}-parallel-upper-arc",
                parallel_upper_start,
                parallel_upper_end,
            ),
        )
    )
    unchanged_start, unchanged_end = add_nodes("unchanged", 400.0)
    arcs.append(
        _arc(
            f"{family_id}-unchanged-arc",
            unchanged_start,
            unchanged_end,
        )
    )
    graph = make_topology_graph_view_from_primitives(
        source_road_graph_id=(
            f"synthetic-road-graph-sha256-{hashlib.sha256(family_id.encode()).hexdigest()}"
        ),
        coordinate_frame_id="m5.5-synthetic-local-world",
        nodes=tuple(nodes),
        arcs=tuple(arcs),
    )
    expected = {
        (
            TopologyHypothesisKind.POSSIBLE_MISSING_CONNECTION,
            missing_start.node_id,
            missing_end.node_id,
        ),
        (
            TopologyHypothesisKind.POSSIBLE_GEOMETRY_DISAGREEMENT,
            geometry_start.node_id,
            geometry_end.node_id,
        ),
        (
            TopologyHypothesisKind.POSSIBLE_MISSING_CONNECTION,
            altered_start.node_id,
            altered_end.node_id,
        ),
    }
    interval_values: list[OffMapTrajectoryInterval] = []
    scenario_by_interval: dict[str, str] = {}
    scenario_y = {
        "missing": 0.0,
        "geometry": 100.0,
        "altered": 200.0,
        "parallel": 308.0,
        "unchanged": 400.0,
    }
    for scenario, y_m in scenario_y.items():
        for pass_index, noise_m in enumerate(gate.synthetic_population.noise_offsets_m):
            points = tuple(
                (x_m, y_m + noise_m) for x_m in (0.0, 25.0, 50.0, 75.0, 100.0)
            )
            interval = make_off_map_trajectory_interval(
                sequence_id=f"{family_id}-{scenario}-sequence-{pass_index}",
                traversal_id=f"{family_id}-{scenario}-pass-{pass_index}",
                source_group_id=family_id,
                coordinate_frame_id=graph.coordinate_frame_id,
                off_map_state=True,
                positioning_observable=True,
                direction_confident=True,
                stationary=False,
                positioning_quality=0.99,
                points_local_m=points,
            )
            interval_values.append(interval)
            scenario_by_interval[interval.interval_id] = scenario
    return graph, tuple(interval_values), expected, scenario_by_interval


def _fraction(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("M5.5 confidence bound has no resamples")
    ordered = sorted(values)
    index = math.ceil(probability * len(ordered)) - 1
    return ordered[min(max(index, 0), len(ordered) - 1)]


def _bootstrap_metric_lower(
    family_counts: dict[str, tuple[int, int, int]],
    *,
    metric: Literal["precision", "recall"],
    seed: int,
    replicates: int,
    confidence_level: float,
) -> float:
    keys = sorted(family_counts)
    if not keys:
        raise ValueError("M5.5 bootstrap has no independent families")
    random_source = random.Random(seed)
    values: list[float] = []
    for _ in range(replicates):
        true_positive = 0
        false_positive = 0
        false_negative = 0
        for _ in keys:
            selected = family_counts[keys[random_source.randrange(len(keys))]]
            true_positive += selected[0]
            false_positive += selected[1]
            false_negative += selected[2]
        denominator = (
            true_positive + false_positive
            if metric == "precision"
            else true_positive + false_negative
        )
        values.append(_fraction(true_positive, denominator))
    return _quantile(values, 1.0 - confidence_level)


def _bootstrap_median_upper(
    family_values: dict[str, tuple[float, ...]],
    *,
    seed: int,
    replicates: int,
    confidence_level: float,
) -> float:
    keys = sorted(family_values)
    if not keys or any(not family_values[key] for key in keys):
        raise ValueError("M5.5 endpoint bootstrap lacks family support")
    random_source = random.Random(seed)
    values: list[float] = []
    for _ in range(replicates):
        sample: list[float] = []
        for _ in keys:
            sample.extend(family_values[keys[random_source.randrange(len(keys))]])
        values.append(statistics.median(sample))
    return _quantile(values, confidence_level)


def _poisson_cdf(observed_count: int, expected_mean: float) -> float:
    term = math.exp(-expected_mean)
    total = term
    for index in range(1, observed_count + 1):
        term *= expected_mean / index
        total += term
    return total


def _poisson_rate_upper(
    observed_count: int,
    exposure: float,
    confidence_level: float,
) -> float:
    if observed_count < 0 or not exposure > 0.0:
        raise ValueError("M5.5 false-hypothesis rate inputs are invalid")
    target_tail = 1.0 - confidence_level
    lower = 0.0
    upper = max(1.0, float(observed_count + 1))
    while _poisson_cdf(observed_count, upper) > target_tail:
        upper *= 2.0
    for _ in range(100):
        midpoint = (lower + upper) / 2.0
        if _poisson_cdf(observed_count, midpoint) > target_tail:
            lower = midpoint
        else:
            upper = midpoint
    return upper / exposure


def qualify_topology_hypotheses(
    *,
    profile_path: Path,
    gate_path: Path,
    numerical_charter_path: Path,
) -> dict[str, object]:
    """Run the frozen M5.5 supported synthetic mutation benchmark."""

    profile, profile_file_sha256 = load_topology_hypothesis_profile(profile_path)
    gate, gate_file_sha256 = load_topology_hypothesis_gate(gate_path)
    authorities = gate.authorities
    if (
        profile_file_sha256 != authorities.topology_hypothesis_profile_file_sha256
        or profile.immutable_sha256
        != authorities.topology_hypothesis_profile_immutable_sha256
        or authorities.road_binning_profile_file_sha256
        != ROAD_BINNING_PROFILE_FILE_SHA256
    ):
        raise ValueError("M5.5 qualification authority is foreign")
    numerical = _load_numerical_gates(
        numerical_charter_path,
        authorities.numerical_charter_file_sha256,
    )
    thresholds = gate.thresholds
    if numerical != {
        "topology.synthetic_precision": thresholds.synthetic_precision_minimum,
        "topology.synthetic_recall": thresholds.synthetic_recall_minimum,
        "topology.endpoint_error_bins": thresholds.endpoint_error_road_bins_maximum,
        "topology.false_hypotheses_per_unchanged_km": (
            thresholds.false_hypotheses_per_unchanged_km_maximum
        ),
    }:
        raise ValueError("M5.5 gate does not match the numerical charter")

    family_counts: dict[str, tuple[int, int, int]] = {}
    family_endpoint_errors: dict[str, tuple[float, ...]] = {}
    reports: list[TopologyHypothesisReport] = []
    total_true_positive = 0
    total_false_positive = 0
    total_false_negative = 0
    control_false_hypotheses = 0
    geometry_errors: list[float] = []
    all_labels_exact = True
    for family_index in range(gate.synthetic_population.family_count):
        family_id = f"m5.5-family-{family_index:02d}"
        graph, intervals, expected, scenario_by_interval = _family_inputs(
            family_index, gate
        )
        report = mine_topology_hypotheses(
            graph,
            intervals,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
        )
        reports.append(report)
        predicted = {
            (item.kind, item.start_node_id, item.end_node_id)
            for item in report.hypotheses
        }
        true_positive_keys = predicted & expected
        true_positive = len(true_positive_keys)
        false_positive = len(predicted - expected)
        false_negative = len(expected - predicted)
        family_counts[family_id] = (
            true_positive,
            false_positive,
            false_negative,
        )
        total_true_positive += true_positive
        total_false_positive += false_positive
        total_false_negative += false_negative
        cluster_by_id = {item.cluster_id: item for item in report.clusters}
        endpoint_errors: list[float] = []
        for hypothesis in report.hypotheses:
            key = (
                hypothesis.kind,
                hypothesis.start_node_id,
                hypothesis.end_node_id,
            )
            if key in true_positive_keys:
                endpoint_errors.append(
                    hypothesis.endpoint_localization_error_m / ROAD_BIN_LENGTH_M
                )
            if hypothesis.geometry_corridor_error_m is not None:
                geometry_errors.append(hypothesis.geometry_corridor_error_m)
            cluster = cluster_by_id[hypothesis.cluster_id]
            scenarios = {
                scenario_by_interval[interval_id]
                for interval_id in cluster.supporting_interval_ids
            }
            if len(scenarios) != 1:
                raise ValueError("M5.5 cluster crossed synthetic scenario boundaries")
            if scenarios.pop() in {"parallel", "unchanged"}:
                control_false_hypotheses += 1
            all_labels_exact = all_labels_exact and (
                hypothesis.result_label == "REVIEW_HYPOTHESIS_NOT_GROUND_TRUTH"
                and hypothesis.review_required
                and not hypothesis.automatic_map_edit_permitted
                and hypothesis.ground_truth_status == "NOT_GROUND_TRUTH"
            )
        family_endpoint_errors[family_id] = tuple(endpoint_errors)

    statistics_gate = gate.statistics
    precision = _fraction(
        total_true_positive, total_true_positive + total_false_positive
    )
    recall = _fraction(total_true_positive, total_true_positive + total_false_negative)
    precision_lower = _bootstrap_metric_lower(
        family_counts,
        metric="precision",
        seed=statistics_gate.bootstrap_seed,
        replicates=statistics_gate.bootstrap_replicates,
        confidence_level=statistics_gate.confidence_level,
    )
    recall_lower = _bootstrap_metric_lower(
        family_counts,
        metric="recall",
        seed=statistics_gate.bootstrap_seed + 1,
        replicates=statistics_gate.bootstrap_replicates,
        confidence_level=statistics_gate.confidence_level,
    )
    all_endpoint_errors = [
        value for values in family_endpoint_errors.values() for value in values
    ]
    endpoint_median = statistics.median(all_endpoint_errors)
    endpoint_upper = _bootstrap_median_upper(
        family_endpoint_errors,
        seed=statistics_gate.bootstrap_seed + 2,
        replicates=statistics_gate.bootstrap_replicates,
        confidence_level=statistics_gate.confidence_level,
    )
    unchanged_exposure_km = (
        gate.synthetic_population.family_count
        * gate.synthetic_population.unchanged_exposure_km_per_family
    )
    false_rate = control_false_hypotheses / unchanged_exposure_km
    false_rate_upper = _poisson_rate_upper(
        control_false_hypotheses,
        unchanged_exposure_km,
        statistics_gate.confidence_level,
    )
    support_gate = (
        len(family_counts) >= statistics_gate.minimum_independent_clusters
        and gate.synthetic_population.expected_positive_hypothesis_count
        >= statistics_gate.minimum_expected_positive_hypotheses
        and len(all_endpoint_errors)
        == gate.synthetic_population.expected_positive_hypothesis_count
    )
    precision_gate = (
        support_gate and precision_lower >= thresholds.synthetic_precision_minimum
    )
    recall_gate = support_gate and recall_lower >= thresholds.synthetic_recall_minimum
    endpoint_gate = (
        support_gate and endpoint_upper <= thresholds.endpoint_error_road_bins_maximum
    )
    false_rate_gate = (
        false_rate_upper <= thresholds.false_hypotheses_per_unchanged_km_maximum
    )
    hypothesis_label_gate = all_labels_exact and all(
        item.result_semantics
        == "REVIEW_HYPOTHESES_ONLY_NO_GROUND_TRUTH_OR_AUTOMATIC_MAP_EDIT"
        for item in reports
    )
    accepted = (
        precision_gate
        and recall_gate
        and endpoint_gate
        and false_rate_gate
        and hypothesis_label_gate
    )
    sample_hypothesis = reports[0].hypotheses[0]
    return {
        "schema_version": "cartosentry.m5.5-topology-hypothesis-qualification.v1",
        "accepted": accepted,
        "algorithm_backend": ALGORITHM_BACKEND,
        "result_semantics": (
            "SUPPORTED_SYNTHETIC_REVIEW_HYPOTHESES_ONLY_NOT_GROUND_TRUTH"
        ),
        "authorities": {
            "gate_file_sha256": gate_file_sha256,
            "gate_immutable_sha256": gate.immutable_sha256,
            "topology_hypothesis_profile_file_sha256": profile_file_sha256,
            "topology_hypothesis_profile_immutable_sha256": (profile.immutable_sha256),
            "road_binning_profile_file_sha256": (
                authorities.road_binning_profile_file_sha256
            ),
            "numerical_charter_file_sha256": (
                authorities.numerical_charter_file_sha256
            ),
        },
        "support": {
            "independent_synthetic_family_count": len(family_counts),
            "expected_positive_hypothesis_count": (
                gate.synthetic_population.expected_positive_hypothesis_count
            ),
            "independent_traversals_per_scenario": (
                gate.synthetic_population.independent_traversals_per_scenario
            ),
            "mutation_kinds": list(gate.synthetic_population.mutation_kinds),
            "control_kinds": list(gate.synthetic_population.control_kinds),
            "unchanged_exposure_km": unchanged_exposure_km,
        },
        "metrics": {
            "synthetic_hypothesis_precision": precision,
            "synthetic_hypothesis_precision_lower_95": precision_lower,
            "synthetic_hypothesis_recall": recall,
            "synthetic_hypothesis_recall_lower_95": recall_lower,
            "endpoint_localization_error_road_bins_median": endpoint_median,
            "endpoint_localization_error_road_bins_upper_95": endpoint_upper,
            "false_hypotheses_per_unchanged_km": false_rate,
            "false_hypotheses_per_unchanged_km_upper_95": false_rate_upper,
            "true_positive_hypotheses": total_true_positive,
            "false_positive_hypotheses": total_false_positive,
            "false_negative_hypotheses": total_false_negative,
            "control_false_hypotheses": control_false_hypotheses,
            "geometry_corridor_error_m_median": statistics.median(geometry_errors),
        },
        "gates": {
            "confirmatory_support": support_gate,
            "synthetic_precision": precision_gate,
            "synthetic_recall": recall_gate,
            "endpoint_localization": endpoint_gate,
            "false_hypotheses_on_unchanged_distance": false_rate_gate,
            "review_hypothesis_labels": hypothesis_label_gate,
        },
        "demonstration": {
            "sample_hypothesis": sample_hypothesis.model_dump(mode="json"),
            "sample_report_id": reports[0].report_id,
            "public_result_label": "REVIEW_HYPOTHESIS_NOT_GROUND_TRUTH",
            "automatic_map_edit_permitted": False,
        },
    }


__all__ = [
    "GATE_IMMUTABLE_SHA256",
    "TopologyHypothesisGate",
    "load_topology_hypothesis_gate",
    "qualify_topology_hypotheses",
]
