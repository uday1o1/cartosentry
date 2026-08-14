"""HMM road-candidate emission and directed-transition scoring tests."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest
from cartosentry.cli import app
from cartosentry.contracts import TimeEpoch, TimePoint, TimeReference
from cartosentry.road_graph import (
    ArcDirection,
    DirectedRoadArc,
    DirectedRoadGraph,
    GraphSourceKind,
    RoadGraphSpatialIndex,
    import_osm_road_graph,
    load_graph_import_profile,
    validate_graph_identity,
)
from cartosentry.road_matching import (
    ALGORITHM_BACKEND,
    NEGATIVE_INFINITY,
    PROFILE_IMMUTABLE_SHA256,
    CandidateState,
    RoadCandidate,
    TransitionRejection,
    best_emission_candidate,
    generate_road_candidates,
    load_map_matching_profile,
    make_road_match_observation,
    score_road_transition,
    validate_matching_graph_authority,
)
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GRAPH_PROFILE_PATH = REPOSITORY_ROOT / "profiles/graph_import_v1.yaml"
MATCH_PROFILE_PATH = REPOSITORY_ROOT / "profiles/map_matching_v1.yaml"
NUMERICAL_CHARTER_PATH = REPOSITORY_ROOT / "benchmarks/numerical_charter.yaml"
FIXTURE_PATH = REPOSITORY_ROOT / "tests/fixtures/road_graphs/topology_v1.osm"
FIXTURE_SHA256 = "eda30cc433fae67cd95584d89e7d6de0f124aed4b396990b0b3da6c3489a4616"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rehash_graph(graph: DirectedRoadGraph) -> DirectedRoadGraph:
    unidentified = graph.model_copy(
        update={"graph_id": f"road-graph-sha256-{'0' * 64}"}
    )
    payload = unidentified.model_dump(mode="json", exclude={"graph_id"})
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    rehashed = unidentified.model_copy(
        update={"graph_id": f"road-graph-sha256-{digest}"}
    )
    validated = DirectedRoadGraph.model_validate_json(rehashed.model_dump_json())
    validate_graph_identity(validated)
    return validated


@pytest.fixture(scope="module")
def matching_profile():
    profile, _ = load_map_matching_profile(MATCH_PROFILE_PATH)
    return profile


@pytest.fixture(scope="module")
def graph() -> DirectedRoadGraph:
    profile, profile_file_sha256 = load_graph_import_profile(GRAPH_PROFILE_PATH)
    return import_osm_road_graph(
        FIXTURE_PATH,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_object_key="tests/fixtures/road_graphs/topology_v1.osm",
        expected_source_sha256=FIXTURE_SHA256,
        source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
    )


def _time(seconds: str) -> TimePoint:
    return TimePoint.from_decimal_seconds(
        seconds,
        source_key="tests/road-matching",
        field="time",
        epoch=TimeEpoch.UNIX_UTC,
        clock_id="synthetic-road-clock",
        reference=TimeReference.SAMPLE,
    )


def _position(arc: DirectedRoadArc, fraction: float, lateral_m: float = 0.0):
    left, right = arc.geometry_local_m[0], arc.geometry_local_m[-1]
    delta_x = right[0] - left[0]
    delta_y = right[1] - left[1]
    length = math.hypot(delta_x, delta_y)
    normal = (-delta_y / length, delta_x / length)
    return (
        left[0] + fraction * delta_x + lateral_m * normal[0],
        left[1] + fraction * delta_y + lateral_m * normal[1],
    )


def _observation(
    graph: DirectedRoadGraph,
    position: tuple[float, float],
    *,
    time: str,
    heading: float | None = 0.0,
    speed: float = 5.0,
    uncertainty: float | None = None,
):
    return make_road_match_observation(
        time=_time(time),
        local_frame_id=graph.local_frame.frame.frame_id,
        position_local_m=position,
        heading_rad=heading,
        speed_mps=speed,
        horizontal_uncertainty_m=uncertainty,
        horizontal_uncertainty_basis=(
            "DECLARED_TRUSTWORTHY" if uncertainty is not None else None
        ),
    )


def _arc(
    graph: DirectedRoadGraph, way_id: int, direction: ArcDirection
) -> DirectedRoadArc:
    return next(
        item
        for item in graph.arcs
        if item.source_way_id == way_id and item.direction is direction
    )


def _candidate(
    graph: DirectedRoadGraph,
    profile,
    observation,
    way_id: int,
    direction: ArcDirection,
) -> RoadCandidate:
    arc_id = _arc(graph, way_id, direction).arc_id
    return next(
        item
        for item in generate_road_candidates(graph, observation, profile=profile)
        if item.directed_arc_id == arc_id
    )


def test_parameter_charter_is_frozen_complete_and_authoritative(tmp_path: Path) -> None:
    profile, profile_file_sha256 = load_map_matching_profile(MATCH_PROFILE_PATH)
    graph_profile, graph_profile_file_sha256 = load_graph_import_profile(
        GRAPH_PROFILE_PATH
    )
    assert profile.immutable_sha256 == PROFILE_IMMUTABLE_SHA256
    assert len(profile_file_sha256) == 64
    assert (
        profile.authorities.graph_import_profile_file_sha256
        == graph_profile_file_sha256
    )
    assert (
        profile.authorities.graph_import_profile_immutable_sha256
        == graph_profile.immutable_sha256
    )
    assert profile.authorities.numerical_charter_file_sha256 == _sha256(
        NUMERICAL_CHARTER_PATH
    )
    assert set(profile.parameter_charter.model_dump()) == {
        "candidate",
        "emission",
        "transition",
    }
    assert set(profile.parameter_charter.candidate.model_dump()) == {
        "minimum_search_radius_m",
        "default_search_radius_m",
        "maximum_search_radius_m",
        "uncertainty_radius_multiplier",
        "maximum_on_road_candidates",
        "distance_rounding_decimal_places",
    }
    assert set(profile.parameter_charter.emission.model_dump()) == {
        "base_lateral_sigma_m",
        "maximum_lateral_sigma_m",
        "heading_sigma_rad",
        "heading_disabled_below_speed_mps",
        "heading_full_weight_speed_mps",
        "off_map_log_likelihood",
        "score_rounding_decimal_places",
    }
    assert set(profile.parameter_charter.transition.model_dump()) == {
        "path_discrepancy_scale_m",
        "maximum_absolute_speed_mps",
        "observed_speed_excess_allowance_mps",
        "speed_excess_penalty_per_mps",
        "turn_penalty",
        "u_turn_penalty",
        "off_map_enter_log_likelihood",
        "off_map_exit_log_likelihood",
        "off_map_stay_log_likelihood",
        "maximum_graph_search_distance_m",
        "maximum_graph_search_states",
        "score_rounding_decimal_places",
    }

    raw = json.loads(MATCH_PROFILE_PATH.read_text(encoding="utf-8"))
    raw["parameter_charter"]["emission"]["off_map_log_likelihood"] = -500.0
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable hash"):
        load_map_matching_profile(changed)


def test_self_consistent_foreign_graph_profile_authority_fails_closed(
    tmp_path: Path,
    graph: DirectedRoadGraph,
    matching_profile,
) -> None:
    foreign = _rehash_graph(
        graph.model_copy(
            update={
                "profile_immutable_sha256": "f" * 64,
                "profile_file_sha256": "e" * 64,
            }
        )
    )
    observation = _observation(
        graph,
        _position(_arc(graph, 140, ArcDirection.FORWARD), 0.5),
        time="1",
    )
    validate_graph_identity(foreign)
    with pytest.raises(ValueError, match="foreign import-profile authority"):
        validate_matching_graph_authority(foreign, matching_profile)
    with pytest.raises(ValueError, match="foreign import-profile authority"):
        generate_road_candidates(foreign, observation, profile=matching_profile)

    original_candidates = generate_road_candidates(
        graph, observation, profile=matching_profile
    )
    selected = best_emission_candidate(original_candidates)
    with pytest.raises(ValueError, match="foreign import-profile authority"):
        score_road_transition(
            foreign,
            observation,
            selected,
            observation,
            selected,
            profile=matching_profile,
        )

    foreign_path = tmp_path / "foreign-graph.json"
    foreign_path.write_text(foreign.model_dump_json(), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "score-road-candidates",
            str(foreign_path),
            "--x-m",
            str(observation.position_local_m[0]),
            "--y-m",
            str(observation.position_local_m[1]),
            "--time-seconds",
            "1",
            "--speed-mps",
            "5",
        ],
    )
    assert result.exit_code == 2
    assert "foreign import-profile authority" in result.output


def test_in_memory_parameter_charter_tampering_fails_closed(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    changed_emission = matching_profile.parameter_charter.emission.model_copy(
        update={"off_map_log_likelihood": -500.0}
    )
    changed_charter = matching_profile.parameter_charter.model_copy(
        update={"emission": changed_emission}
    )
    changed_profile = matching_profile.model_copy(
        update={"parameter_charter": changed_charter}
    )
    observation = _observation(
        graph,
        _position(_arc(graph, 140, ArcDirection.FORWARD), 0.5),
        time="1",
    )
    with pytest.raises(ValueError, match="profile identity"):
        generate_road_candidates(graph, observation, profile=changed_profile)


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"value":NaN}',
        b"[" * 65 + b"0" + b"]" * 65,
        b" " * (256 * 1024 + 1),
    ],
)
def test_parameter_charter_rejects_hostile_json(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "hostile.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        load_map_matching_profile(path)


def test_directed_projection_heading_sign_and_low_speed_disable(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    forward = _arc(graph, 140, ArcDirection.FORWARD)
    position = _position(forward, 0.5)
    moving = _observation(graph, position, time="1", heading=0.0, speed=5.0)
    candidates = generate_road_candidates(graph, moving, profile=matching_profile)
    forward_candidate = _candidate(
        graph, matching_profile, moving, 140, ArcDirection.FORWARD
    )
    reverse_candidate = _candidate(
        graph, matching_profile, moving, 140, ArcDirection.REVERSE
    )
    assert forward_candidate.lateral_distance_m == 0.0
    assert forward_candidate.emission.heading_used is True
    assert forward_candidate.emission.heading_difference_rad < 0.001
    assert reverse_candidate.emission.heading_difference_rad > math.pi - 0.001
    assert forward_candidate.emission.total_log_likelihood > (
        reverse_candidate.emission.total_log_likelihood
    )
    assert best_emission_candidate(candidates).directed_arc_id == forward.arc_id

    stopped = _observation(graph, position, time="2", heading=0.0, speed=0.5)
    stopped_forward = _candidate(
        graph, matching_profile, stopped, 140, ArcDirection.FORWARD
    )
    stopped_reverse = _candidate(
        graph, matching_profile, stopped, 140, ArcDirection.REVERSE
    )
    assert stopped_forward.emission.heading_used is False
    assert stopped_reverse.emission.heading_used is False
    assert stopped_forward.emission.heading_weight == 0.0
    assert stopped_forward.emission.total_log_likelihood == (
        stopped_reverse.emission.total_log_likelihood
    )

    partial = _observation(graph, position, time="3", heading=0.0, speed=2.0)
    partial_forward = _candidate(
        graph, matching_profile, partial, 140, ArcDirection.FORWARD
    )
    assert partial_forward.emission.heading_used is True
    assert partial_forward.emission.heading_weight == 0.5


def test_uncertainty_expands_bounded_search_and_scales_lateral_variance(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    forward = _arc(graph, 100, ArcDirection.FORWARD)
    position = _position(forward, 0.5, lateral_m=25.0)
    no_uncertainty = _observation(graph, position, time="1", uncertainty=None)
    no_uncertainty_candidates = generate_road_candidates(
        graph, no_uncertainty, profile=matching_profile
    )
    assert {item.state for item in no_uncertainty_candidates} == {
        CandidateState.OFF_MAP
    }
    uncertain = _observation(graph, position, time="1", uncertainty=10.0)
    uncertain_candidates = generate_road_candidates(
        graph, uncertain, profile=matching_profile
    )
    road = next(item for item in uncertain_candidates if item.source_way_id == 100)
    assert road.search_radius_m == 30.0
    assert road.emission.lateral_sigma_m > (
        matching_profile.parameter_charter.emission.base_lateral_sigma_m
    )
    extreme = _observation(graph, position, time="1", uncertainty=100.0)
    assert all(
        item.search_radius_m == 75.0
        for item in generate_road_candidates(graph, extreme, profile=matching_profile)
    )


def test_off_map_is_always_available_and_prevents_forced_match(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    forward = _arc(graph, 100, ArcDirection.FORWARD)
    far_but_searched = _observation(
        graph,
        _position(forward, 0.5, lateral_m=14.0),
        time="1",
        heading=None,
    )
    candidates = generate_road_candidates(
        graph, far_but_searched, profile=matching_profile
    )
    assert any(item.state is CandidateState.ON_ROAD for item in candidates)
    assert best_emission_candidate(candidates).state is CandidateState.OFF_MAP

    remote = _observation(graph, (10_000.0, 10_000.0), time="1")
    remote_candidates = generate_road_candidates(
        graph, remote, profile=matching_profile
    )
    assert len(remote_candidates) == 1
    assert remote_candidates[0].state is CandidateState.OFF_MAP


def test_candidate_identity_order_and_wrong_frame_fail_closed(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    arc = _arc(graph, 150, ArcDirection.FORWARD)
    observation = _observation(graph, _position(arc, 0.5), time="1")
    first = generate_road_candidates(graph, observation, profile=matching_profile)
    second = generate_road_candidates(graph, observation, profile=matching_profile)
    assert first == second
    assert len({item.candidate_id for item in first}) == len(first)
    wrong_frame = make_road_match_observation(
        time=observation.time,
        local_frame_id="wrong-frame",
        position_local_m=observation.position_local_m,
        heading_rad=observation.heading_rad,
        speed_mps=observation.speed_mps,
        horizontal_uncertainty_m=None,
    )
    with pytest.raises(ValueError, match="wrong local frame"):
        generate_road_candidates(graph, wrong_frame, profile=matching_profile)

    changed_source = graph.source.model_copy(
        update={"source_object_key": "tests/fixtures/other-topology.osm"}
    )
    other_graph = _rehash_graph(graph.model_copy(update={"source": changed_source}))
    with pytest.raises(ValueError, match="spatial index does not belong"):
        generate_road_candidates(
            graph,
            observation,
            profile=matching_profile,
            spatial_index=RoadGraphSpatialIndex(other_graph),
        )


def test_observation_identity_binds_matching_features_and_rejects_tampering(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    arc = _arc(graph, 140, ArcDirection.FORWARD)
    source_id = f"observation-sha256-{'a' * 64}"
    common = {
        "time": _time("1"),
        "local_frame_id": graph.local_frame.frame.frame_id,
        "position_local_m": _position(arc, 0.5),
        "horizontal_uncertainty_m": None,
        "source_observation_id": source_id,
    }
    slow = make_road_match_observation(heading_rad=0.0, speed_mps=1.0, **common)
    fast = make_road_match_observation(heading_rad=0.2, speed_mps=5.0, **common)
    assert slow.source_observation_id == fast.source_observation_id
    assert slow.observation_id != fast.observation_id
    slow_candidates = generate_road_candidates(graph, slow, profile=matching_profile)
    fast_candidates = generate_road_candidates(graph, fast, profile=matching_profile)
    assert {item.candidate_id for item in slow_candidates}.isdisjoint(
        item.candidate_id for item in fast_candidates
    )

    tampered = slow.model_copy(update={"speed_mps": 99.0})
    with pytest.raises(ValueError, match="observation identity"):
        generate_road_candidates(graph, tampered, profile=matching_profile)
    with pytest.raises(ValueError, match="observation identity"):
        type(slow).model_validate_json(tampered.model_dump_json())


def test_uncertainty_requires_an_explicit_trustworthy_basis(
    graph: DirectedRoadGraph,
) -> None:
    arc = _arc(graph, 140, ArcDirection.FORWARD)
    common = {
        "time": _time("1"),
        "local_frame_id": graph.local_frame.frame.frame_id,
        "position_local_m": _position(arc, 0.5),
        "heading_rad": None,
        "speed_mps": 0.0,
    }
    with pytest.raises(ValueError, match="trustworthy basis"):
        make_road_match_observation(
            horizontal_uncertainty_m=2.0,
            **common,
        )
    with pytest.raises(ValueError, match="trustworthy basis"):
        make_road_match_observation(
            horizontal_uncertainty_m=None,
            horizontal_uncertainty_basis="DECLARED_TRUSTWORTHY",
            **common,
        )


def test_observation_builder_canonicalizes_integer_numeric_inputs() -> None:
    observation = make_road_match_observation(
        time=_time("1"),
        local_frame_id="integer-input-frame",
        position_local_m=(1, 2),
        heading_rad=0,
        speed_mps=3,
        horizontal_uncertainty_m=None,
    )
    observation.assert_identity()
    assert observation.position_local_m == (1.0, 2.0)
    assert isinstance(observation.speed_mps, float)
    with pytest.raises(ValueError, match="cannot be booleans"):
        make_road_match_observation(
            time=_time("1"),
            local_frame_id="boolean-input-frame",
            position_local_m=(1, 2),
            heading_rad=None,
            speed_mps=True,
            horizontal_uncertainty_m=None,
        )


def test_antipodal_heading_difference_stays_within_closed_domain(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    arc = _arc(graph, 140, ArcDirection.FORWARD)
    for index in range(10):
        observation = _observation(
            graph,
            _position(arc, 0.5),
            time=str(index + 1),
            speed=10.0,
            heading=-2.0 + 0.4 * index,
        )

        candidates = generate_road_candidates(
            graph, observation, profile=matching_profile
        )

        assert all(
            item.emission.heading_difference_rad is None
            or item.emission.heading_difference_rad <= math.pi
            for item in candidates
        )


def test_same_arc_transition_units_sign_and_speed_support(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    arc = _arc(graph, 140, ArcDirection.FORWARD)
    previous_observation = _observation(
        graph, _position(arc, 0.1), time="1", speed=20.0
    )
    current_observation = _observation(graph, _position(arc, 0.3), time="3", speed=20.0)
    previous = _candidate(
        graph,
        matching_profile,
        previous_observation,
        140,
        ArcDirection.FORWARD,
    )
    current = _candidate(
        graph,
        matching_profile,
        current_observation,
        140,
        ArcDirection.FORWARD,
    )
    supported = score_road_transition(
        graph,
        previous_observation,
        previous,
        current_observation,
        current,
        profile=matching_profile,
    )
    assert supported.possible is True
    assert supported.elapsed_seconds == 2.0
    assert supported.graph_distance_m == pytest.approx(arc.length_m * 0.2, abs=1e-5)
    assert supported.implied_graph_speed_mps == pytest.approx(
        supported.graph_distance_m / 2.0
    )
    assert supported.path_discrepancy_m < 1e-5
    assert supported.speed_log_likelihood == 0.0
    assert supported.total_log_likelihood <= 0.0

    slow_previous = _observation(graph, _position(arc, 0.1), time="1", speed=0.0)
    slow_current = _observation(graph, _position(arc, 0.3), time="3", speed=0.0)
    slow_previous_candidate = _candidate(
        graph,
        matching_profile,
        slow_previous,
        140,
        ArcDirection.FORWARD,
    )
    slow_current_candidate = _candidate(
        graph,
        matching_profile,
        slow_current,
        140,
        ArcDirection.FORWARD,
    )
    penalized = score_road_transition(
        graph,
        slow_previous,
        slow_previous_candidate,
        slow_current,
        slow_current_candidate,
        profile=matching_profile,
    )
    assert penalized.speed_log_likelihood < supported.speed_log_likelihood
    assert penalized.total_log_likelihood < supported.total_log_likelihood


def test_path_discrepancy_has_the_expected_penalty_sign(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    arc = _arc(graph, 140, ArcDirection.FORWARD)
    aligned_left = _observation(graph, _position(arc, 0.2), time="1", speed=10.0)
    aligned_right = _observation(graph, _position(arc, 0.3), time="3", speed=10.0)
    offset_left = _observation(
        graph, _position(arc, 0.2, lateral_m=10.0), time="1", speed=10.0
    )
    offset_right = _observation(
        graph, _position(arc, 0.3, lateral_m=-10.0), time="3", speed=10.0
    )
    aligned_candidates = (
        _candidate(graph, matching_profile, aligned_left, 140, ArcDirection.FORWARD),
        _candidate(graph, matching_profile, aligned_right, 140, ArcDirection.FORWARD),
    )
    offset_candidates = (
        _candidate(graph, matching_profile, offset_left, 140, ArcDirection.FORWARD),
        _candidate(graph, matching_profile, offset_right, 140, ArcDirection.FORWARD),
    )
    aligned = score_road_transition(
        graph,
        aligned_left,
        aligned_candidates[0],
        aligned_right,
        aligned_candidates[1],
        profile=matching_profile,
    )
    offset = score_road_transition(
        graph,
        offset_left,
        offset_candidates[0],
        offset_right,
        offset_candidates[1],
        profile=matching_profile,
    )
    assert offset.path_discrepancy_m > aligned.path_discrepancy_m
    assert offset.path_log_likelihood < aligned.path_log_likelihood
    assert offset.total_log_likelihood < aligned.total_log_likelihood


def test_forbidden_unknown_and_one_way_transitions_are_negative_infinity(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    from_arc = _arc(graph, 160, ArcDirection.FORWARD)
    previous_observation = _observation(
        graph, _position(from_arc, 0.99), time="1", heading=None
    )
    previous = _candidate(
        graph,
        matching_profile,
        previous_observation,
        160,
        ArcDirection.FORWARD,
    )
    for way_id, reason in (
        (162, TransitionRejection.FORBIDDEN_TURN),
        (161, TransitionRejection.UNKNOWN_RESTRICTION),
    ):
        to_arc = _arc(graph, way_id, ArcDirection.FORWARD)
        current_observation = _observation(
            graph, _position(to_arc, 0.01), time="3", heading=None
        )
        current = _candidate(
            graph,
            matching_profile,
            current_observation,
            way_id,
            ArcDirection.FORWARD,
        )
        score = score_road_transition(
            graph,
            previous_observation,
            previous,
            current_observation,
            current,
            profile=matching_profile,
        )
        assert score.possible is False
        assert score.rejection_reason == reason
        assert score.score == NEGATIVE_INFINITY
        assert math.isinf(score.score) and score.score < 0.0
        assert "Infinity" not in score.model_dump_json()

    one_way = _arc(graph, 100, ArcDirection.FORWARD)
    near_end = _observation(graph, _position(one_way, 0.9), time="1")
    near_start = _observation(graph, _position(one_way, 0.1), time="3")
    one_way_score = score_road_transition(
        graph,
        near_end,
        _candidate(graph, matching_profile, near_end, 100, ArcDirection.FORWARD),
        near_start,
        _candidate(graph, matching_profile, near_start, 100, ArcDirection.FORWARD),
        profile=matching_profile,
    )
    assert one_way_score.rejection_reason == TransitionRejection.NO_DIRECTED_PATH
    assert one_way_score.score == NEGATIVE_INFINITY


def test_forbidden_direct_turn_does_not_hide_a_legal_directed_detour(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    restrictions = tuple(
        item for item in graph.restrictions if item.source_relation_id == 1000
    )
    rules = tuple(
        item for item in graph.transition_rules if item.source_relation_id == 1000
    )
    statistics = graph.statistics.model_copy(
        update={
            "parsed_relation_count": 1,
            "transition_rule_count": len(rules),
            "applied_restriction_count": 1,
            "unknown_restriction_count": 0,
        }
    )
    detour_graph = _rehash_graph(
        graph.model_copy(
            update={
                "restrictions": restrictions,
                "transition_rules": rules,
                "statistics": statistics,
            }
        )
    )
    from_arc = _arc(detour_graph, 160, ArcDirection.FORWARD)
    to_arc = _arc(detour_graph, 162, ArcDirection.FORWARD)
    previous_observation = _observation(
        detour_graph,
        _position(from_arc, 0.99),
        time="1",
        heading=None,
        speed=20.0,
    )
    current_observation = _observation(
        detour_graph,
        _position(to_arc, 0.01),
        time="20",
        heading=None,
        speed=20.0,
    )
    previous = _candidate(
        detour_graph,
        matching_profile,
        previous_observation,
        160,
        ArcDirection.FORWARD,
    )
    current = _candidate(
        detour_graph,
        matching_profile,
        current_observation,
        162,
        ArcDirection.FORWARD,
    )
    score = score_road_transition(
        detour_graph,
        previous_observation,
        previous,
        current_observation,
        current,
        profile=matching_profile,
    )
    arc_lookup = {item.arc_id: item for item in detour_graph.arcs}
    assert score.possible is True
    assert tuple(arc_lookup[item].source_way_id for item in score.path_arc_ids) == (
        160,
        161,
        161,
        162,
    )
    assert score.turn_count == 3
    assert score.u_turn_count == 1


def test_nonpositive_time_and_implausible_speed_are_impossible(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    arc = _arc(graph, 140, ArcDirection.FORWARD)
    previous_observation = _observation(graph, _position(arc, 0.1), time="1")
    same_time = _observation(graph, _position(arc, 0.2), time="1")
    previous = _candidate(
        graph,
        matching_profile,
        previous_observation,
        140,
        ArcDirection.FORWARD,
    )
    current = _candidate(graph, matching_profile, same_time, 140, ArcDirection.FORWARD)
    nonpositive = score_road_transition(
        graph,
        previous_observation,
        previous,
        same_time,
        current,
        profile=matching_profile,
    )
    assert nonpositive.rejection_reason == (
        TransitionRejection.NON_POSITIVE_ELAPSED_TIME
    )

    fast_observation = _observation(graph, _position(arc, 0.9), time="2")
    fast = _candidate(
        graph, matching_profile, fast_observation, 140, ArcDirection.FORWARD
    )
    implausible = score_road_transition(
        graph,
        previous_observation,
        previous,
        fast_observation,
        fast,
        profile=matching_profile,
    )
    assert implausible.rejection_reason == (
        TransitionRejection.IMPLAUSIBLE_ABSOLUTE_SPEED
    )
    assert implausible.implied_graph_speed_mps > 60.0


def test_allowed_graph_path_and_off_map_transitions_are_finite(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    from_arc = _arc(graph, 120, ArcDirection.FORWARD)
    to_arc = _arc(graph, 121, ArcDirection.FORWARD)
    previous_observation = _observation(
        graph, _position(from_arc, 0.9), time="1", speed=15.0
    )
    current_observation = _observation(
        graph, _position(to_arc, 0.1), time="3", speed=15.0
    )
    previous = _candidate(
        graph,
        matching_profile,
        previous_observation,
        120,
        ArcDirection.FORWARD,
    )
    current_candidates = generate_road_candidates(
        graph, current_observation, profile=matching_profile
    )
    current = _candidate(
        graph,
        matching_profile,
        current_observation,
        121,
        ArcDirection.FORWARD,
    )
    allowed = score_road_transition(
        graph,
        previous_observation,
        previous,
        current_observation,
        current,
        profile=matching_profile,
    )
    assert allowed.possible is True
    assert allowed.path_arc_ids == (from_arc.arc_id, to_arc.arc_id)
    assert allowed.turn_count == 1
    assert math.isfinite(allowed.score)

    off_map = next(
        item for item in current_candidates if item.state is CandidateState.OFF_MAP
    )
    enter = score_road_transition(
        graph,
        previous_observation,
        previous,
        current_observation,
        off_map,
        profile=matching_profile,
    )
    assert enter.possible is True
    assert enter.off_map_log_likelihood == -2.5
    assert enter.score == -2.5


def test_candidate_observation_ownership_is_enforced(
    graph: DirectedRoadGraph, matching_profile
) -> None:
    arc = _arc(graph, 140, ArcDirection.FORWARD)
    previous_observation = _observation(graph, _position(arc, 0.1), time="1")
    current_observation = _observation(graph, _position(arc, 0.2), time="2")
    previous = _candidate(
        graph,
        matching_profile,
        previous_observation,
        140,
        ArcDirection.FORWARD,
    )
    current = _candidate(
        graph, matching_profile, current_observation, 140, ArcDirection.FORWARD
    )
    with pytest.raises(ValueError, match="do not belong"):
        score_road_transition(
            graph,
            current_observation,
            previous,
            previous_observation,
            current,
            profile=matching_profile,
        )

    wrong_graph = previous.model_copy(
        update={"graph_id": f"road-graph-sha256-{'f' * 64}"}
    )
    with pytest.raises(ValueError, match="directed graph"):
        score_road_transition(
            graph,
            previous_observation,
            wrong_graph,
            current_observation,
            current,
            profile=matching_profile,
        )

    tampered_position = previous.model_copy(
        update={"projected_position_local_m": (1.0, 2.0)}
    )
    with pytest.raises(ValueError, match="candidate identity"):
        score_road_transition(
            graph,
            previous_observation,
            tampered_position,
            current_observation,
            current,
            profile=matching_profile,
        )


def test_public_candidate_scoring_cli(tmp_path: Path, graph: DirectedRoadGraph) -> None:
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(graph.model_dump_json(), encoding="utf-8")
    arc = _arc(graph, 140, ArcDirection.FORWARD)
    x_m, y_m = _position(arc, 0.5)
    output = tmp_path / "candidates.json"
    result = CliRunner().invoke(
        app,
        [
            "score-road-candidates",
            str(graph_path),
            "--x-m",
            str(x_m),
            "--y-m",
            str(y_m),
            "--time-seconds",
            "1.25",
            "--speed-mps",
            "5.0",
            "--heading-rad",
            "0.0",
            "--source-observation-id",
            f"observation-sha256-{'a' * 64}",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == "cartosentry.road-candidate-report.v1"
    assert report["algorithm_backend"] == ALGORITHM_BACKEND
    assert report["best_emission_state"] == "ON_ROAD"
    assert report["observation"]["source_observation_id"] == (
        f"observation-sha256-{'a' * 64}"
    )
    assert {item["state"] for item in report["candidates"]} == {
        "ON_ROAD",
        "OFF_MAP",
    }
    assert all(item["search_radius_m"] == 15.0 for item in report["candidates"])


def test_public_transition_scoring_cli_reports_possible_and_impossible_paths(
    tmp_path: Path, graph: DirectedRoadGraph
) -> None:
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(graph.model_dump_json(), encoding="utf-8")

    from_arc = _arc(graph, 120, ArcDirection.FORWARD)
    to_arc = _arc(graph, 121, ArcDirection.FORWARD)
    from_x, from_y = _position(from_arc, 0.9)
    to_x, to_y = _position(to_arc, 0.1)
    output = tmp_path / "allowed-transition.json"
    result = CliRunner().invoke(
        app,
        [
            "score-road-transition",
            str(graph_path),
            "--from-x-m",
            str(from_x),
            "--from-y-m",
            str(from_y),
            "--from-time-seconds",
            "1",
            "--from-speed-mps",
            "15",
            "--to-x-m",
            str(to_x),
            "--to-y-m",
            str(to_y),
            "--to-time-seconds",
            "20",
            "--to-speed-mps",
            "15",
            "--from-candidate",
            from_arc.arc_id,
            "--to-candidate",
            to_arc.arc_id,
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == "cartosentry.road-transition-report.v1"
    assert report["algorithm_backend"] == ALGORITHM_BACKEND
    assert report["transition"]["possible"] is True
    assert report["transition"]["path_arc_ids"] == [
        from_arc.arc_id,
        to_arc.arc_id,
    ]
    assert report["runtime_score_is_negative_infinity"] is False

    forbidden_from = _arc(graph, 160, ArcDirection.FORWARD)
    forbidden_to = _arc(graph, 162, ArcDirection.FORWARD)
    from_x, from_y = _position(forbidden_from, 0.99)
    to_x, to_y = _position(forbidden_to, 0.01)
    forbidden_output = tmp_path / "forbidden-transition.json"
    forbidden_result = CliRunner().invoke(
        app,
        [
            "score-road-transition",
            str(graph_path),
            "--from-x-m",
            str(from_x),
            "--from-y-m",
            str(from_y),
            "--from-time-seconds",
            "1",
            "--from-speed-mps",
            "5",
            "--to-x-m",
            str(to_x),
            "--to-y-m",
            str(to_y),
            "--to-time-seconds",
            "20",
            "--to-speed-mps",
            "5",
            "--from-candidate",
            forbidden_from.arc_id,
            "--to-candidate",
            forbidden_to.arc_id,
            "--output",
            str(forbidden_output),
        ],
    )
    assert forbidden_result.exit_code == 0, forbidden_result.output
    forbidden_report = json.loads(forbidden_output.read_text(encoding="utf-8"))
    assert forbidden_report["transition"]["possible"] is False
    assert forbidden_report["transition"]["rejection_reason"] == "FORBIDDEN_TURN"
    assert forbidden_report["transition"]["total_log_likelihood"] is None
    assert forbidden_report["runtime_score_is_negative_infinity"] is True
