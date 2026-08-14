"""Deterministic Viterbi road-path decoder tests."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest
from cartosentry.contracts import TimeEpoch, TimePoint, TimeReference
from cartosentry.road_decoder import (
    MATCHING_PROFILE_FILE_SHA256,
    PROFILE_FILE_SHA256,
    PROFILE_IMMUTABLE_SHA256,
    MatchConfidence,
    decode_road_path,
    load_map_decoder_profile,
)
from cartosentry.road_graph import (
    ArcDirection,
    DirectedRoadArc,
    DirectedRoadGraph,
    GraphSourceKind,
    import_osm_road_graph,
    load_graph_import_profile,
)
from cartosentry.road_matching import (
    ALGORITHM_BACKEND,
    CandidateState,
    load_map_matching_profile,
    make_road_match_observation,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GRAPH_PROFILE_PATH = REPOSITORY_ROOT / "profiles/graph_import_v1.yaml"
MATCH_PROFILE_PATH = REPOSITORY_ROOT / "profiles/map_matching_v1.yaml"
DECODER_PROFILE_PATH = REPOSITORY_ROOT / "profiles/map_decoder_v1.yaml"
NUMERICAL_CHARTER_PATH = REPOSITORY_ROOT / "benchmarks/numerical_charter.yaml"
FIXTURE_PATH = REPOSITORY_ROOT / "tests/fixtures/road_graphs/topology_v1.osm"
FIXTURE_SHA256 = "eda30cc433fae67cd95584d89e7d6de0f124aed4b396990b0b3da6c3489a4616"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


@pytest.fixture(scope="module")
def profiles():
    matching, matching_file_sha256 = load_map_matching_profile(MATCH_PROFILE_PATH)
    decoder, decoder_file_sha256 = load_map_decoder_profile(DECODER_PROFILE_PATH)
    return matching, matching_file_sha256, decoder, decoder_file_sha256


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
        source_key="tests/road-decoder",
        field="time",
        epoch=TimeEpoch.UNIX_UTC,
        clock_id="road-decoder-clock",
        reference=TimeReference.SAMPLE,
    )


def _arc(
    graph: DirectedRoadGraph, way_id: int, direction: ArcDirection
) -> DirectedRoadArc:
    return next(
        item
        for item in graph.arcs
        if item.source_way_id == way_id and item.direction == direction
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


def _heading(arc: DirectedRoadArc) -> float:
    left, right = arc.geometry_local_m[0], arc.geometry_local_m[-1]
    return math.atan2(right[1] - left[1], right[0] - left[0])


def _observation(
    graph: DirectedRoadGraph,
    position: tuple[float, float],
    *,
    time: str,
    speed: float,
    heading: float | None,
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


def _decode(graph, profiles, observations, sequence_id="synthetic-decoder"):
    matching, matching_file_sha256, decoder, decoder_file_sha256 = profiles
    return decode_road_path(
        graph,
        tuple(observations),
        sequence_id=sequence_id,
        source_group_id=f"{sequence_id}-family",
        partition="synthetic",
        matching_profile=matching,
        matching_profile_file_sha256=matching_file_sha256,
        decoder_profile=decoder,
        decoder_profile_file_sha256=decoder_file_sha256,
    )


def test_decoder_profile_is_self_hashed_and_binds_authorities(profiles) -> None:
    matching, matching_file_sha256, decoder, decoder_file_sha256 = profiles
    assert decoder.immutable_sha256 == PROFILE_IMMUTABLE_SHA256
    assert decoder_file_sha256 == PROFILE_FILE_SHA256
    assert matching_file_sha256 == MATCHING_PROFILE_FILE_SHA256
    assert decoder.authorities.map_matching_profile_file_sha256 == matching_file_sha256
    assert (
        decoder.authorities.map_matching_profile_immutable_sha256
        == matching.immutable_sha256
    )
    assert decoder.authorities.numerical_charter_file_sha256 == _sha256(
        NUMERICAL_CHARTER_PATH
    )
    assert set(decoder.parameter_charter.model_dump()) == {
        "decoding_mode",
        "beam_width",
        "hypotheses_per_terminal_candidate",
        "beam_score_delta_log_likelihood",
        "ambiguity_path_separation_log_likelihood",
        "stationary_speed_threshold_mps",
        "stationary_position_tolerance_m",
        "stationary_minimum_observations",
        "maximum_sequence_observations",
        "score_rounding_decimal_places",
    }


def test_exact_ramp_path_intervals_and_identity_are_deterministic(
    graph: DirectedRoadGraph, profiles
) -> None:
    ramp_in = _arc(graph, 120, ArcDirection.FORWARD)
    ramp_out = _arc(graph, 121, ArcDirection.FORWARD)
    observations = (
        _observation(
            graph,
            _position(ramp_in, 0.2),
            time="1",
            speed=15.0,
            heading=_heading(ramp_in),
        ),
        _observation(
            graph,
            _position(ramp_in, 0.9),
            time="10",
            speed=15.0,
            heading=_heading(ramp_in),
        ),
        _observation(
            graph,
            _position(ramp_out, 0.3),
            time="20",
            speed=15.0,
            heading=_heading(ramp_out),
        ),
    )
    first = _decode(graph, profiles, observations, "ramp-merge")
    second = _decode(graph, profiles, observations, "ramp-merge")
    assert first == second
    first.assert_identity()
    assert first.algorithm_backend == ALGORITHM_BACKEND
    assert first.decode_mode == "OFFLINE_FULL_WINDOW"
    assert first.confidence == MatchConfidence.CONFIDENT
    assert tuple(item.candidate.directed_arc_id for item in first.points) == (
        ramp_in.arc_id,
        ramp_in.arc_id,
        ramp_out.arc_id,
    )
    assert tuple(item.confidence for item in first.points) == (
        MatchConfidence.CONFIDENT,
        MatchConfidence.CONFIDENT,
        MatchConfidence.CONFIDENT,
    )
    assert len(first.intervals) == 2
    assert first.intervals[0].usable_distance_m > 0.0
    assert first.intervals[1].usable_distance_m == 0.0


def test_parallel_equal_paths_are_labeled_ambiguous(
    graph: DirectedRoadGraph, profiles
) -> None:
    south = _arc(graph, 140, ArcDirection.FORWARD)
    north = _arc(graph, 141, ArcDirection.FORWARD)
    positions = tuple(
        tuple(
            (left + right) / 2.0
            for left, right in zip(
                _position(south, fraction), _position(north, fraction)
            )
        )
        for fraction in (0.2, 0.5, 0.8)
    )
    observations = tuple(
        _observation(
            graph,
            position,
            time=str(1 + index * 10),
            speed=10.0,
            heading=_heading(south),
            uncertainty=5.0,
        )
        for index, position in enumerate(positions)
    )
    result = _decode(graph, profiles, observations, "parallel-ambiguous")
    assert result.confidence == MatchConfidence.AMBIGUOUS
    assert result.path_separation_log_likelihood == pytest.approx(0.0, abs=1e-9)
    assert set(item.confidence for item in result.points) == {MatchConfidence.AMBIGUOUS}
    assert {
        tuple(item.candidate.directed_arc_id for item in result.points),
        tuple(item.directed_arc_id for item in result.runner_up_candidates),
    } == {
        (south.arc_id, south.arc_id, south.arc_id),
        (north.arc_id, north.arc_id, north.arc_id),
    }
    assert all(item.usable_distance_m == 0.0 for item in result.intervals)


def test_off_map_path_is_not_forced_onto_roads(
    graph: DirectedRoadGraph, profiles
) -> None:
    observations = tuple(
        _observation(
            graph,
            (10_000.0 + index * 10.0, 10_000.0),
            time=str(1 + index * 5),
            speed=2.0,
            heading=0.0,
        )
        for index in range(4)
    )
    result = _decode(graph, profiles, observations, "missing-edge")
    assert {item.candidate.state for item in result.points} == {CandidateState.OFF_MAP}
    assert len(result.intervals) == 1
    assert result.intervals[0].state == CandidateState.OFF_MAP
    assert result.intervals[0].usable_distance_m == 0.0


def test_stationary_run_is_one_noncoverage_interval(
    graph: DirectedRoadGraph, profiles
) -> None:
    arc = _arc(graph, 140, ArcDirection.FORWARD)
    observations = tuple(
        _observation(
            graph,
            _position(arc, 0.5, lateral_m=(index % 2) * 0.1),
            time=str(index + 1),
            speed=0.0,
            heading=(-2.0 + index),
        )
        for index in range(4)
    )
    result = _decode(graph, profiles, observations, "stopped-vehicle")
    assert all(item.stationary for item in result.points)
    assert len(result.intervals) == 1
    assert result.intervals[0].stationary is True
    assert result.intervals[0].usable_distance_m == 0.0


def test_decoder_rejects_time_order_budget_and_foreign_profiles(
    graph: DirectedRoadGraph, profiles
) -> None:
    matching, matching_file_sha256, decoder, decoder_file_sha256 = profiles
    arc = _arc(graph, 140, ArcDirection.FORWARD)
    first = _observation(
        graph, _position(arc, 0.1), time="2", speed=5.0, heading=_heading(arc)
    )
    second = _observation(
        graph, _position(arc, 0.2), time="1", speed=5.0, heading=_heading(arc)
    )
    common = {
        "graph": graph,
        "sequence_id": "invalid-sequence",
        "source_group_id": "invalid-family",
        "partition": "synthetic",
        "matching_profile": matching,
        "matching_profile_file_sha256": matching_file_sha256,
        "decoder_profile": decoder,
        "decoder_profile_file_sha256": decoder_file_sha256,
    }
    with pytest.raises(ValueError, match="strictly time ordered"):
        decode_road_path(observations=(first, second), **common)

    parameters = decoder.parameter_charter.model_copy(
        update={"maximum_sequence_observations": 1}
    )
    changed_decoder = decoder.model_copy(update={"parameter_charter": parameters})
    with pytest.raises(ValueError, match="profile identity"):
        decode_road_path(
            observations=(second,),
            **{**common, "decoder_profile": changed_decoder},
        )

    with pytest.raises(ValueError, match="foreign matching-profile authority"):
        decode_road_path(
            observations=(second,),
            **{**common, "matching_profile_file_sha256": "f" * 64},
        )

    with pytest.raises(ValueError, match="foreign decoder-profile authority"):
        decode_road_path(
            observations=(second,),
            **{**common, "decoder_profile_file_sha256": "f" * 64},
        )


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"value":NaN}',
        b"[" * 65 + b"0" + b"]" * 65,
        b" " * (256 * 1024 + 1),
    ],
)
def test_decoder_profile_rejects_hostile_json(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "hostile.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        load_map_decoder_profile(path)


def test_decoded_identity_rejects_portable_tampering(
    graph: DirectedRoadGraph, profiles
) -> None:
    arc = _arc(graph, 140, ArcDirection.FORWARD)
    observation = _observation(
        graph, _position(arc, 0.5), time="1", speed=5.0, heading=_heading(arc)
    )
    result = _decode(graph, profiles, (observation,), "identity-control")
    raw = json.loads(result.model_dump_json())
    raw["best_total_log_likelihood"] -= 1.0
    with pytest.raises(ValueError, match="runner-up path evidence"):
        type(result).model_validate_json(json.dumps(raw))

    raw = json.loads(result.model_dump_json())
    raw["sequence_id"] = "tampered-sequence"
    with pytest.raises(ValueError, match="identity"):
        type(result).model_validate_json(json.dumps(raw))

    raw = json.loads(result.model_dump_json())
    raw["algorithm_backend"] = "PYTHON"
    with pytest.raises(ValueError, match="algorithm_backend"):
        type(result).model_validate_json(json.dumps(raw))

    raw = json.loads(result.model_dump_json())
    interval = raw["intervals"][0]
    interval["source_observation_ids"][0] = f"observation-sha256-{'0' * 64}"
    interval_payload = {
        key: value for key, value in interval.items() if key != "interval_id"
    }
    interval["interval_id"] = (
        f"road-interval-sha256-{_canonical_hash(interval_payload)}"
    )
    road_match_payload = {
        key: value for key, value in raw.items() if key != "road_match_id"
    }
    raw["road_match_id"] = f"road-match-sha256-{_canonical_hash(road_match_payload)}"
    with pytest.raises(ValueError, match="interval evidence is inconsistent"):
        type(result).model_validate_json(json.dumps(raw))
