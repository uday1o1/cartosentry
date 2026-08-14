"""Directed road-graph import, provenance, and qualification tests."""

from __future__ import annotations

import hashlib
import json
from itertools import islice
from pathlib import Path

import pytest
from cartosentry.adapters.boreas_v1 import BoreasAdapter
from cartosentry.cli import app
from cartosentry.road_graph import (
    PROFILE_IMMUTABLE_SHA256,
    ArcDirection,
    DirectedRoadGraph,
    GraphSourceKind,
    RestrictionInterpretation,
    RoadGraphSpatialIndex,
    import_osm_road_graph,
    load_graph_import_profile,
    normalize_matching_observation,
    validate_graph_identity,
)
from cartosentry.road_graph_qualification import (
    GATE_IMMUTABLE_SHA256,
    load_directed_road_graph_gate,
    qualify_directed_road_graph,
)
from typer.testing import CliRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPOSITORY_ROOT / "profiles/graph_import_v1.yaml"
GATE_PATH = REPOSITORY_ROOT / "benchmarks/m5_1_graph_gate.yaml"
DATA_MANIFEST_PATH = REPOSITORY_ROOT / "benchmarks/data_manifest.yaml"
FIXTURE_PATH = REPOSITORY_ROOT / "tests/fixtures/road_graphs/topology_v1.osm"
PUBLIC_DATA_ROOT = REPOSITORY_ROOT / "data/public"
PUBLIC_GRAPH_PATH = PUBLIC_DATA_ROOT / "road_graphs/toronto-glen-shields-v1.osm"
PUBLIC_SEQUENCE_ROOT = PUBLIC_DATA_ROOT / "boreas-2021-09-02-11-42"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def profile_and_hash():
    return load_graph_import_profile(PROFILE_PATH)


@pytest.fixture(scope="module")
def fixture_graph(profile_and_hash):
    profile, profile_file_sha256 = profile_and_hash
    gate, _ = load_directed_road_graph_gate(GATE_PATH)
    return import_osm_road_graph(
        FIXTURE_PATH,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_object_key=gate.authorities.fixture_object_key,
        expected_source_sha256=gate.authorities.fixture_sha256,
        source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
    )


def test_profile_and_gate_are_self_hashed_strict_and_frozen(
    tmp_path: Path, profile_and_hash
) -> None:
    profile, profile_file_sha256 = profile_and_hash
    gate, gate_file_sha256 = load_directed_road_graph_gate(GATE_PATH)
    assert profile.immutable_sha256 == PROFILE_IMMUTABLE_SHA256
    assert gate.immutable_sha256 == GATE_IMMUTABLE_SHA256
    assert gate.authorities.profile_file_sha256 == profile_file_sha256
    assert len(gate_file_sha256) == 64
    assert profile.model_json_schema()["additionalProperties"] is False

    for source, field in (
        (PROFILE_PATH, "included_highway_classes"),
        (GATE_PATH, "required_topology_checks"),
    ):
        raw = json.loads(source.read_text(encoding="utf-8"))
        raw[field] = []
        modified = tmp_path / source.name
        modified.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ValueError, match="hash"):
            if source == PROFILE_PATH:
                load_graph_import_profile(modified)
            else:
                load_directed_road_graph_gate(modified)


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"value":NaN}',
        b"[" * 65 + b"0" + b"]" * 65,
        b" " * (256 * 1024 + 1),
    ],
)
def test_profile_and_gate_reject_hostile_json(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "hostile.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        load_graph_import_profile(path)
    with pytest.raises(ValueError):
        load_directed_road_graph_gate(path)


def test_fixture_import_is_exact_deterministic_and_independently_attributed(
    fixture_graph: DirectedRoadGraph,
    profile_and_hash,
) -> None:
    profile, profile_file_sha256 = profile_and_hash
    repeated = import_osm_road_graph(
        FIXTURE_PATH,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_object_key="tests/fixtures/road_graphs/topology_v1.osm",
        expected_source_sha256=_sha256(FIXTURE_PATH),
        source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
    )
    assert fixture_graph == repeated
    assert fixture_graph.graph_id == (
        "road-graph-sha256-"
        "c08949b5660b5f40eebcc16db519cee9dd91895670f4168f22ae41ca9fe67fe9"
    )
    assert fixture_graph.source.source_kind is GraphSourceKind.HAND_AUTHORED_FIXTURE
    assert fixture_graph.source.database_classification == "PROJECT_TEST_FIXTURE"
    assert "OpenStreetMap" not in fixture_graph.source.attribution
    validate_graph_identity(fixture_graph)

    decoded = DirectedRoadGraph.model_validate_json(fixture_graph.model_dump_json())
    assert decoded == fixture_graph
    validate_graph_identity(decoded)


def test_all_required_fixture_topologies_are_retained(
    fixture_graph: DirectedRoadGraph,
) -> None:
    by_way = {
        way_id: tuple(arc for arc in fixture_graph.arcs if arc.source_way_id == way_id)
        for way_id in range(100, 163)
    }
    assert len(by_way[100]) == 1
    assert by_way[100][0].direction is ArcDirection.FORWARD
    assert by_way[100][0].source_geometry_node_ids == (1, 2)
    assert len(by_way[101]) == 1
    assert by_way[101][0].direction is ArcDirection.REVERSE
    assert by_way[101][0].source_geometry_node_ids == (4, 3)
    assert {item.source_way_id for item in (*by_way[110], *by_way[111])} == {
        110,
        111,
    }
    assert all(item.direction is ArcDirection.FORWARD for item in by_way[121])
    assert by_way[121][0].highway_class == "motorway_link"
    assert len(by_way[130]) == 2
    assert all(item.direction is ArcDirection.FORWARD for item in by_way[130])
    assert len(by_way[140]) == len(by_way[141]) == 2

    bridge_nodes = {
        node for arc in by_way[150] for node in arc.source_geometry_node_ids
    }
    tunnel_nodes = {
        node for arc in by_way[151] for node in arc.source_geometry_node_ids
    }
    assert bridge_nodes.isdisjoint(tunnel_nodes)
    assert all(item.layer == 1 and item.bridge for item in by_way[150])
    assert all(item.layer == -1 and item.tunnel for item in by_way[151])

    restrictions = {
        item.source_relation_id: item for item in fixture_graph.restrictions
    }
    assert restrictions[1000].interpretation is RestrictionInterpretation.APPLIED_NO
    assert restrictions[1000].generated_rule_count == 1
    assert restrictions[1001].interpretation is (
        RestrictionInterpretation.UNKNOWN_CONDITIONAL
    )
    assert restrictions[1001].generated_rule_count == 3


def test_spatial_index_is_exact_ordered_and_retains_crossing_layers(
    fixture_graph: DirectedRoadGraph,
) -> None:
    bridge = next(item for item in fixture_graph.arcs if item.source_way_id == 150)
    left, right = bridge.geometry_local_m[0], bridge.geometry_local_m[-1]
    crossing = ((left[0] + right[0]) / 2.0, (left[1] + right[1]) / 2.0)
    index = RoadGraphSpatialIndex(fixture_graph)
    candidates = index.query_radius(crossing, 0.1)
    assert {item.source_way_id for item in candidates} == {150, 151}
    assert tuple(item.arc_id for item in candidates) == tuple(
        sorted(item.arc_id for item in candidates)
    )
    assert candidates == index.query_radius(crossing, 0.1)
    with pytest.raises(ValueError, match="finite and nonnegative"):
        index.query_radius(crossing, -1.0)


def test_access_direction_and_conservative_exclusion_profile(
    tmp_path: Path, profile_and_hash
) -> None:
    profile, profile_file_sha256 = profile_and_hash
    nodes = "".join(
        f'<node id="{index}" lat="43.700{index:02d}" lon="-79.4900000"/>'
        for index in range(1, 17)
    )
    cases = (
        (100, 1, 2, '<tag k="motorcar" v="yes"/><tag k="access" v="private"/>'),
        (101, 3, 4, '<tag k="access" v="private"/>'),
        (102, 5, 6, '<tag k="maxaxleload:conditional" v="5 @ wet"/>'),
        (103, 7, 8, '<tag k="oneway" v="unexpected"/>'),
        (104, 9, 10, '<tag k="motor_vehicle:forward" v="no"/>'),
        (105, 11, 12, '<tag k="construction" v="residential"/>'),
        (106, 13, 14, '<tag k="route" v="ferry"/>'),
        (107, 15, 16, '<tag k="access" v="unrecognized"/>'),
    )
    ways = "".join(
        (
            f'<way id="{way}"><nd ref="{left}"/><nd ref="{right}"/>'
            f'<tag k="highway" v="residential"/>{tags}</way>'
        )
        for way, left, right, tags in cases
    )
    content = (
        '<?xml version="1.0"?><osm version="0.6">'
        '<bounds minlat="43.7000000" minlon="-79.5000000" '
        'maxlat="43.7100000" maxlon="-79.4800000"/>'
        f"{nodes}{ways}</osm>"
    ).encode()
    path = tmp_path / "access.osm"
    path.write_bytes(content)
    graph = import_osm_road_graph(
        path,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_object_key="tests/generated/access.osm",
        expected_source_sha256=hashlib.sha256(content).hexdigest(),
        source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
    )
    assert {item.source_way_id for item in graph.arcs} == {100, 104}
    assert len([item for item in graph.arcs if item.source_way_id == 100]) == 2
    directional = [item for item in graph.arcs if item.source_way_id == 104]
    assert len(directional) == 1
    assert directional[0].direction is ArcDirection.REVERSE
    assert graph.statistics.exclusion_reason_counts == {
        "conditional_access": 1,
        "construction": 1,
        "denied_or_limited_access": 1,
        "ferry": 1,
        "unknown_access": 1,
        "unknown_oneway": 1,
    }


@pytest.mark.parametrize(
    "body",
    [
        '<!DOCTYPE osm [<!ENTITY x "boom">]><osm version="0.6"></osm>',
        '<osm version="0.6"><node id="1" lat="0" lon="0"/>'
        '<node id="1" lat="0" lon="0"/></osm>',
        '<osm version="0.6"><node id="1" lat="0" lon="0"/>'
        '<way id="1"><nd ref="2"/><nd ref="1"/>'
        '<tag k="highway" v="residential"/></way></osm>',
        '<osm version="0.6"><bounds minlat="1" minlon="1" '
        'maxlat="0" maxlon="0"/></osm>',
        '<osm version="0.6"><node id="1" lat="0.00000001" lon="0"/></osm>',
    ],
)
def test_osm_parser_rejects_hostile_or_ambiguous_input(
    tmp_path: Path, profile_and_hash, body: str
) -> None:
    profile, profile_file_sha256 = profile_and_hash
    path = tmp_path / "hostile.osm"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        import_osm_road_graph(
            path,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            source_object_key="tests/generated/hostile.osm",
            expected_source_sha256=_sha256(path),
            source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
        )


def test_source_hash_and_complete_graph_identity_fail_closed(
    fixture_graph: DirectedRoadGraph, profile_and_hash
) -> None:
    profile, profile_file_sha256 = profile_and_hash
    with pytest.raises(ValueError, match="pinned object hash"):
        import_osm_road_graph(
            FIXTURE_PATH,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            source_object_key="tests/fixtures/road_graphs/topology_v1.osm",
            expected_source_sha256="0" * 64,
            source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
        )
    changed_source = fixture_graph.source.model_copy(update={"attribution": "altered"})
    tampered = fixture_graph.model_copy(update={"source": changed_source})
    with pytest.raises(ValueError, match="identity is invalid"):
        validate_graph_identity(tampered)


def test_malformed_and_disconnected_restrictions_never_report_applied(
    tmp_path: Path, profile_and_hash
) -> None:
    profile, profile_file_sha256 = profile_and_hash
    content = b"""<?xml version="1.0"?>
<osm version="0.6">
  <bounds minlat="43.7000000" minlon="-79.5000000"
          maxlat="43.7100000" maxlon="-79.4800000"/>
  <node id="1" lat="43.7010000" lon="-79.4990000"/>
  <node id="2" lat="43.7010000" lon="-79.4980000"/>
  <node id="3" lat="43.7020000" lon="-79.4970000"/>
  <node id="4" lat="43.7020000" lon="-79.4960000"/>
  <way id="100"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way>
  <way id="101"><nd ref="3"/><nd ref="4"/><tag k="highway" v="residential"/></way>
  <relation id="1000">
    <member type="way" ref="100" role="from"/>
    <member type="node" ref="2" role="via"/>
    <member type="way" ref="101" role="to"/>
    <tag k="type" v="restriction"/><tag k="restriction" v="no_left_turn"/>
  </relation>
  <relation id="1001">
    <member type="node" ref="2" role="via"/><member type="way" ref="101" role="to"/>
    <tag k="type" v="restriction"/><tag k="restriction" v="no_right_turn"/>
  </relation>
</osm>
"""
    path = tmp_path / "invalid-restrictions.osm"
    path.write_bytes(content)
    graph = import_osm_road_graph(
        path,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_object_key="tests/generated/invalid-restrictions.osm",
        expected_source_sha256=hashlib.sha256(content).hexdigest(),
        source_kind=GraphSourceKind.HAND_AUTHORED_FIXTURE,
    )
    restrictions = {item.source_relation_id: item for item in graph.restrictions}
    assert restrictions[1000].interpretation is (
        RestrictionInterpretation.UNKNOWN_DISCONNECTED
    )
    assert restrictions[1001].interpretation is (
        RestrictionInterpretation.UNKNOWN_MALFORMED
    )
    assert all(
        item.interpretation
        not in {
            RestrictionInterpretation.APPLIED_NO,
            RestrictionInterpretation.APPLIED_ONLY,
        }
        for item in restrictions.values()
    )
    assert graph.statistics.applied_restriction_count == 0
    assert graph.statistics.unknown_restriction_count == 2


@pytest.mark.skipif(
    not PUBLIC_GRAPH_PATH.is_file() or not PUBLIC_SEQUENCE_ROOT.is_dir(),
    reason="verified public development objects unavailable",
)
def test_public_graph_and_real_observation_provenance(profile_and_hash) -> None:
    profile, profile_file_sha256 = profile_and_hash
    graph = import_osm_road_graph(
        PUBLIC_GRAPH_PATH,
        profile=profile,
        profile_file_sha256=profile_file_sha256,
        source_object_key=profile.authorities.public_object_key,
        expected_source_sha256=profile.authorities.public_object_sha256,
    )
    assert graph.graph_id == (
        "road-graph-sha256-"
        "da259ced68a1a3d17f7a25ea2e695151928ca73043d42f26c4d8abd89fa7b1f2"
    )
    assert graph.source.attribution == profile.authorities.attribution
    assert graph.statistics.directed_arc_count == 2398
    adapter = BoreasAdapter(
        PUBLIC_SEQUENCE_ROOT,
        source_group_id="boreas-glen-shields-family-v1",
    )
    observations = tuple(
        normalize_matching_observation(item, graph)
        for item in islice(adapter.pose_samples(), 64)
    )
    assert len(observations) == 64
    assert all(
        item.coordinate_provenance == "WGS84_SOURCE_DERIVED_LOCAL_WORLD"
        and item.local_altitude_policy == "ZERO_ELLIPSOID_FOR_HORIZONTAL_MATCHING"
        and item.source_provenance.source_key == "applanix/gps_post_process.csv"
        for item in observations
    )


@pytest.mark.skipif(
    not PUBLIC_GRAPH_PATH.is_file() or not PUBLIC_SEQUENCE_ROOT.is_dir(),
    reason="verified public development objects unavailable",
)
def test_complete_qualification_and_public_cli(tmp_path: Path) -> None:
    report = qualify_directed_road_graph(
        profile_path=PROFILE_PATH,
        gate_path=GATE_PATH,
        data_manifest_path=DATA_MANIFEST_PATH,
        fixture_path=FIXTURE_PATH,
        public_graph_path=PUBLIC_GRAPH_PATH,
        public_sequence_root=PUBLIC_SEQUENCE_ROOT,
    )
    assert report["accepted"] is True
    assert report["public_graph"]["attribution_survives_portable_export"] is True
    assert report["public_observations"]["provenance_valid_fraction"] == 1.0

    graph_output = tmp_path / "graph.json"
    imported = CliRunner().invoke(
        app,
        ["import-road-graph", str(PUBLIC_GRAPH_PATH), "--output", str(graph_output)],
    )
    assert imported.exit_code == 0, imported.output
    portable = json.loads(graph_output.read_text(encoding="utf-8"))
    assert portable["source"]["attribution"] == (
        "Contains information from OpenStreetMap, which is made available under "
        "the Open Database License."
    )
    validate_graph_identity(
        DirectedRoadGraph.model_validate_json(graph_output.read_text(encoding="utf-8"))
    )

    report_output = tmp_path / "report.json"
    qualified = CliRunner().invoke(
        app,
        [
            "qualify-road-graph",
            "--public-data-root",
            str(PUBLIC_DATA_ROOT),
            "--output",
            str(report_output),
        ],
    )
    assert qualified.exit_code == 0, qualified.output
    exported = json.loads(report_output.read_text(encoding="utf-8"))
    assert exported == report
