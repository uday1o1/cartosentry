from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cartosentry.road_graph import (
    GraphSourceKind,
    import_osm_road_graph,
    load_graph_import_profile,
)
from cartosentry.topology_hypotheses import (
    PROFILE_FILE_SHA256,
    PROFILE_IMMUTABLE_SHA256,
    TopologyGraphArc,
    TopologyGraphNode,
    TopologyGraphView,
    TopologyHypothesisKind,
    load_topology_hypothesis_profile,
    make_off_map_trajectory_interval,
    make_topology_graph_view,
    make_topology_graph_view_from_primitives,
    mine_topology_hypotheses,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
PROFILE_PATH = REPOSITORY_ROOT / "profiles/topology_hypotheses_v1.yaml"
GRAPH_PROFILE_PATH = REPOSITORY_ROOT / "profiles/graph_import_v1.yaml"
FIXTURE_PATH = REPOSITORY_ROOT / "tests/fixtures/road_graphs/topology_v1.osm"
FIXTURE_SHA256 = "eda30cc433fae67cd95584d89e7d6de0f124aed4b396990b0b3da6c3489a4616"


def _source_graph_id(name: str) -> str:
    return f"synthetic-road-graph-sha256-{hashlib.sha256(name.encode()).hexdigest()}"


def _graph(
    *,
    arc_middle_y_m: float | None,
    include_arc: bool,
) -> TopologyGraphView:
    start = TopologyGraphNode(node_id="start", position_local_m=(0.0, 0.0))
    end = TopologyGraphNode(node_id="end", position_local_m=(100.0, 0.0))
    arcs = ()
    if include_arc:
        geometry = (
            ((0.0, 0.0), (100.0, 0.0))
            if arc_middle_y_m is None
            else ((0.0, 0.0), (50.0, arc_middle_y_m), (100.0, 0.0))
        )
        arcs = (
            TopologyGraphArc(
                arc_id="arc",
                from_node_id="start",
                to_node_id="end",
                geometry_local_m=geometry,
            ),
        )
    return make_topology_graph_view_from_primitives(
        source_road_graph_id=_source_graph_id(f"{arc_middle_y_m}-{include_arc}"),
        coordinate_frame_id="test-local-world",
        nodes=(start, end),
        arcs=arcs,
    )


def _interval(
    index: int,
    y_m: float,
    *,
    traversal_id: str | None = None,
    off_map_state: bool = True,
    positioning_observable: bool = True,
    direction_confident: bool = True,
    stationary: bool = False,
    positioning_quality: float = 0.99,
):
    return make_off_map_trajectory_interval(
        sequence_id=f"sequence-{index}",
        traversal_id=traversal_id or f"pass-{index}",
        source_group_id="source-family",
        coordinate_frame_id="test-local-world",
        off_map_state=off_map_state,
        positioning_observable=positioning_observable,
        direction_confident=direction_confident,
        stationary=stationary,
        positioning_quality=positioning_quality,
        points_local_m=((0.0, y_m), (50.0, y_m), (100.0, y_m)),
    )


def test_profile_is_byte_and_semantically_frozen(tmp_path: Path) -> None:
    profile, file_sha256 = load_topology_hypothesis_profile(PROFILE_PATH)
    assert profile.immutable_sha256 == PROFILE_IMMUTABLE_SHA256
    assert file_sha256 == PROFILE_FILE_SHA256
    assert profile.parameter_charter.minimum_independent_traversals == 4

    altered = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    altered["parameter_charter"]["minimum_independent_traversals"] = 3
    altered_path = tmp_path / "altered-profile.json"
    altered_path.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable hash"):
        load_topology_hypothesis_profile(altered_path)


def test_missing_connection_uses_independent_pass_identity() -> None:
    profile, profile_sha256 = load_topology_hypothesis_profile(PROFILE_PATH)
    graph = _graph(arc_middle_y_m=None, include_arc=False)
    intervals = (
        _interval(0, -0.3),
        _interval(1, -0.1),
        _interval(2, 0.1),
        _interval(3, 0.3),
        _interval(4, 0.2, traversal_id="pass-3"),
    )
    report = mine_topology_hypotheses(
        graph,
        intervals,
        profile=profile,
        profile_file_sha256=profile_sha256,
    )
    assert len(report.clusters) == 1
    assert report.clusters[0].independent_traversal_count == 4
    assert len(report.hypotheses) == 1
    hypothesis = report.hypotheses[0]
    assert hypothesis.kind is TopologyHypothesisKind.POSSIBLE_MISSING_CONNECTION
    assert hypothesis.result_label == "REVIEW_HYPOTHESIS_NOT_GROUND_TRUTH"
    assert hypothesis.review_required
    assert not hypothesis.automatic_map_edit_permitted
    assert hypothesis.ground_truth_status == "NOT_GROUND_TRUTH"


def test_perturbed_geometry_is_a_review_only_hypothesis() -> None:
    profile, profile_sha256 = load_topology_hypothesis_profile(PROFILE_PATH)
    graph = _graph(arc_middle_y_m=15.0, include_arc=True)
    report = mine_topology_hypotheses(
        graph,
        tuple(
            _interval(index, offset)
            for index, offset in enumerate((-0.3, -0.1, 0.1, 0.3))
        ),
        profile=profile,
        profile_file_sha256=profile_sha256,
    )
    assert len(report.hypotheses) == 1
    hypothesis = report.hypotheses[0]
    assert hypothesis.kind is TopologyHypothesisKind.POSSIBLE_GEOMETRY_DISAGREEMENT
    assert hypothesis.compared_arc_id == "arc"
    assert hypothesis.geometry_corridor_error_m is not None
    assert hypothesis.geometry_corridor_error_m > 3.0


def test_unchanged_and_parallel_roads_do_not_create_hypotheses() -> None:
    profile, profile_sha256 = load_topology_hypothesis_profile(PROFILE_PATH)
    unchanged = _graph(arc_middle_y_m=None, include_arc=True)
    unchanged_report = mine_topology_hypotheses(
        unchanged,
        tuple(
            _interval(index, offset)
            for index, offset in enumerate((-0.3, -0.1, 0.1, 0.3))
        ),
        profile=profile,
        profile_file_sha256=profile_sha256,
    )
    assert unchanged_report.hypotheses == ()

    nodes = (
        TopologyGraphNode(node_id="lower-end", position_local_m=(100.0, 0.0)),
        TopologyGraphNode(node_id="lower-start", position_local_m=(0.0, 0.0)),
        TopologyGraphNode(node_id="upper-end", position_local_m=(100.0, 8.0)),
        TopologyGraphNode(node_id="upper-start", position_local_m=(0.0, 8.0)),
    )
    parallel = make_topology_graph_view_from_primitives(
        source_road_graph_id=_source_graph_id("parallel"),
        coordinate_frame_id="test-local-world",
        nodes=nodes,
        arcs=(
            TopologyGraphArc(
                arc_id="lower",
                from_node_id="lower-start",
                to_node_id="lower-end",
                geometry_local_m=((0.0, 0.0), (100.0, 0.0)),
            ),
            TopologyGraphArc(
                arc_id="upper",
                from_node_id="upper-start",
                to_node_id="upper-end",
                geometry_local_m=((0.0, 8.0), (100.0, 8.0)),
            ),
        ),
    )
    parallel_report = mine_topology_hypotheses(
        parallel,
        tuple(
            _interval(index, 8.0 + offset)
            for index, offset in enumerate((-0.3, -0.1, 0.1, 0.3))
        ),
        profile=profile,
        profile_file_sha256=profile_sha256,
    )
    assert parallel_report.hypotheses == ()


def test_high_quality_selection_is_explicit_and_exhaustive() -> None:
    profile, profile_sha256 = load_topology_hypothesis_profile(PROFILE_PATH)
    graph = _graph(arc_middle_y_m=None, include_arc=False)
    short = make_off_map_trajectory_interval(
        sequence_id="short",
        traversal_id="short",
        source_group_id="source-family",
        coordinate_frame_id="test-local-world",
        off_map_state=True,
        positioning_observable=True,
        direction_confident=True,
        stationary=False,
        positioning_quality=0.99,
        points_local_m=((0.0, 0.0), (10.0, 0.0)),
    )
    intervals = (
        _interval(0, 0.0, off_map_state=False),
        _interval(1, 0.0, positioning_observable=False),
        _interval(2, 0.0, direction_confident=False),
        _interval(3, 0.0, stationary=True),
        _interval(4, 0.0, positioning_quality=0.5),
        short,
    )
    report = mine_topology_hypotheses(
        graph,
        intervals,
        profile=profile,
        profile_file_sha256=profile_sha256,
    )
    assert report.selected_interval_ids == ()
    assert report.rejected_interval_counts.model_dump() == {
        "not_off_map": 1,
        "unobservable_positioning": 1,
        "uncertain_direction": 1,
        "stationary": 1,
        "insufficient_positioning_quality": 1,
        "short_interval": 1,
    }


def test_real_directed_graph_converts_to_comparison_view() -> None:
    graph_profile, graph_profile_sha256 = load_graph_import_profile(GRAPH_PROFILE_PATH)
    graph = import_osm_road_graph(
        FIXTURE_PATH,
        profile=graph_profile,
        profile_file_sha256=graph_profile_sha256,
        source_object_key="tests/fixtures/road_graphs/topology_v1.osm",
        expected_source_sha256=FIXTURE_SHA256,
        source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
    )
    view = make_topology_graph_view(graph)
    assert view.source_road_graph_id == graph.graph_id
    assert len(view.nodes) == len(graph.nodes)
    assert len(view.arcs) == len(graph.arcs)
    assert tuple(item.arc_id for item in view.arcs) == tuple(
        sorted(item.arc_id for item in graph.arcs)
    )


def test_frame_mismatch_fails_before_native_mining() -> None:
    profile, profile_sha256 = load_topology_hypothesis_profile(PROFILE_PATH)
    graph = _graph(arc_middle_y_m=None, include_arc=False)
    foreign = make_off_map_trajectory_interval(
        sequence_id="foreign",
        traversal_id="foreign",
        source_group_id="source-family",
        coordinate_frame_id="foreign-frame",
        off_map_state=True,
        positioning_observable=True,
        direction_confident=True,
        stationary=False,
        positioning_quality=0.99,
        points_local_m=((0.0, 0.0), (100.0, 0.0)),
    )
    with pytest.raises(ValueError, match="different frames"):
        mine_topology_hypotheses(
            graph,
            (foreign,),
            profile=profile,
            profile_file_sha256=profile_sha256,
        )
