"""Deterministic directed OpenStreetMap graph import and spatial indexing."""

from __future__ import annotations

import hashlib
import json
import math
import xml.etree.ElementTree as ElementTree
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from shapely import LineString, Point, STRtree  # type: ignore[import-untyped]

from cartosentry.adapters.base import SourceProvenance, TrajectorySample
from cartosentry.contracts import (
    GlobalCoordinate,
    LocalOrigin,
    NamedFrame,
    TimePoint,
    VerticalDatum,
)
from cartosentry.manifest_boundaries import (
    ManifestBoundaryError,
    decode_bounded_json,
    read_bounded_regular_bytes,
)

PROFILE_IMMUTABLE_SHA256 = (
    "89f4b306dce91ba9adcfc4da5b651ef1acaa247a33009cc03d96d85d632f20fb"
)
MAXIMUM_PROFILE_BYTES = 256 * 1024

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class GraphAuthorities(StrictModel):
    data_manifest_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    public_artifact_id: Literal["osm-toronto-glen-shields-v1"]
    public_object_key: Literal["road_graphs/toronto-glen-shields-v1.osm"]
    public_object_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    snapshot_utc: Literal["2026-08-14T00:47:44Z"]
    attribution: Annotated[str, Field(min_length=1)]
    license_url: Literal["https://opendatacommons.org/licenses/odbl/1-0/"]
    database_classification: Literal["ODBL_DERIVATIVE_DATABASE"]


class AccessProfile(StrictModel):
    generic_keys: tuple[str, ...]
    allowed_values: tuple[str, ...]
    denied_values: tuple[str, ...]
    missing_value_interpretation: Literal["INCLUDED_WITHOUT_ROAD_LEGALITY_CLAIM"]
    unknown_value_interpretation: Literal["EXCLUDE"]
    conditional_interpretation: Literal["EXCLUDE"]
    construction_interpretation: Literal["EXCLUDE"]
    private_interpretation: Literal["EXCLUDE"]
    ferry_interpretation: Literal["EXCLUDE"]


class DirectionalityProfile(StrictModel):
    oneway_keys: tuple[str, ...]
    forward_values: tuple[str, ...]
    reverse_values: tuple[str, ...]
    bidirectional_values: tuple[str, ...]
    implicit_forward_junction_values: tuple[str, ...]
    implicit_forward_highway_classes: tuple[str, ...]
    directional_access_suffixes: tuple[Literal["forward", "backward"], ...]
    unknown_oneway_interpretation: Literal["EXCLUDE"]


class TurnRestrictionProfile(StrictModel):
    relation_type: Literal["restriction"]
    restriction_tag_keys: tuple[str, ...]
    supported_via: Literal["SINGLE_NODE_ONLY"]
    supported_prefixes: tuple[Literal["no_", "only_"], ...]
    conditional_interpretation: Literal["UNKNOWN_RESTRICTION"]
    via_way_interpretation: Literal["UNKNOWN_RESTRICTION"]
    unknown_value_interpretation: Literal["UNKNOWN_RESTRICTION"]
    except_interpretation: Literal["UNKNOWN_RESTRICTION"]


class GeometryProfile(StrictModel):
    origin_policy: Literal["OSM_BOUNDS_CENTER_WGS84_E8_ZERO_ELLIPSOID"]
    coordinate_provenance: Literal["WGS84_SOURCE_DERIVED_LOCAL_WORLD"]
    local_frame_axes: tuple[Literal["east", "north", "up"], ...]
    road_geometry_altitude_policy: Literal["FLATTEN_TO_LOCAL_Z_ZERO"]
    local_coordinate_rounding_decimal_places: Annotated[int, Field(ge=0, le=12)]
    length_rounding_decimal_places: Annotated[int, Field(ge=0, le=12)]
    minimum_arc_length_m: Annotated[float, Field(gt=0.0)]
    split_policy: Literal["WAY_ENDPOINT_SHARED_NODE_AND_RESTRICTION_VIA_NODE"]
    retain_source_shape_nodes: Literal[True]


class SpatialIndexProfile(StrictModel):
    implementation: Literal["SHAPELY_STRTREE_IMMUTABLE_V1"]
    input_order: Literal["ARC_ID_ASCENDING"]
    query_result_order: Literal["ARC_ID_ASCENDING"]


class GraphLimits(StrictModel):
    maximum_source_bytes: Annotated[int, Field(gt=0)]
    maximum_nodes: Annotated[int, Field(gt=0)]
    maximum_ways: Annotated[int, Field(gt=0)]
    maximum_relations: Annotated[int, Field(gt=0)]
    maximum_way_node_references: Annotated[int, Field(gt=0)]
    maximum_tags_per_element: Annotated[int, Field(gt=0)]
    maximum_geometry_points_per_arc: Annotated[int, Field(gt=1)]


class GraphImportProfile(StrictModel):
    schema_version: Literal[1]
    profile_id: Literal["graph-import-v1"]
    profile_version: Literal["1.0.0"]
    freeze_state: Literal["FROZEN_BEFORE_M5_1_ACCEPTANCE"]
    hash_contract: Literal[
        "SHA-256 of canonical UTF-8 JSON with immutable_sha256 omitted"
    ]
    immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    authorities: GraphAuthorities
    included_highway_classes: tuple[str, ...]
    access: AccessProfile
    directionality: DirectionalityProfile
    turn_restrictions: TurnRestrictionProfile
    geometry: GeometryProfile
    spatial_index: SpatialIndexProfile
    limits: GraphLimits

    @model_validator(mode="after")
    def validate_identity_and_sets(self) -> Self:
        if self.immutable_sha256 != PROFILE_IMMUTABLE_SHA256:
            raise ValueError("graph import profile identity is not pinned")
        collections = (
            self.included_highway_classes,
            self.access.generic_keys,
            self.access.allowed_values,
            self.access.denied_values,
            self.directionality.oneway_keys,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("graph import profile sets must be unique")
        if set(self.access.allowed_values) & set(self.access.denied_values):
            raise ValueError("graph import access values must not overlap")
        if self.geometry.local_frame_axes != ("east", "north", "up"):
            raise ValueError("graph local frame axes are not exact")
        return self


class ArcDirection(StrEnum):
    FORWARD = "FORWARD"
    REVERSE = "REVERSE"


class TransitionState(StrEnum):
    FORBIDDEN = "FORBIDDEN"
    UNKNOWN_RESTRICTION = "UNKNOWN_RESTRICTION"


class RestrictionInterpretation(StrEnum):
    APPLIED_NO = "APPLIED_NO"
    APPLIED_ONLY = "APPLIED_ONLY"
    UNKNOWN_CONDITIONAL = "UNKNOWN_CONDITIONAL"
    UNKNOWN_VIA_WAY = "UNKNOWN_VIA_WAY"
    UNKNOWN_VALUE = "UNKNOWN_VALUE"
    UNKNOWN_EXCEPT = "UNKNOWN_EXCEPT"
    UNKNOWN_MALFORMED = "UNKNOWN_MALFORMED"
    UNKNOWN_DISCONNECTED = "UNKNOWN_DISCONNECTED"
    INACTIVE_SOURCE_EXCLUDED = "INACTIVE_SOURCE_EXCLUDED"


class GraphSourceKind(StrEnum):
    OPENSTREETMAP_EXTRACT = "OPENSTREETMAP_EXTRACT"
    HAND_AUTHORED_FIXTURE = "HAND_AUTHORED_FIXTURE"


class Wgs84E7(StrictModel):
    latitude_e7: Annotated[int, Field(ge=-900_000_000, le=900_000_000)]
    longitude_e7: Annotated[int, Field(ge=-1_800_000_000, le=1_800_000_000)]


class OsmTag(StrictModel):
    key: Annotated[str, Field(min_length=1)]
    value: str


class RoadGraphSource(StrictModel):
    source_kind: GraphSourceKind
    source_object_key: Annotated[str, Field(min_length=1)]
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    snapshot_utc: str
    attribution: Annotated[str, Field(min_length=1)]
    license_url: Annotated[str, Field(min_length=1)]
    database_classification: Literal["ODBL_DERIVATIVE_DATABASE", "PROJECT_TEST_FIXTURE"]

    @model_validator(mode="after")
    def validate_source_declaration(self) -> Self:
        normalized = self.source_object_key.replace("\\", "/")
        if normalized.startswith(("/", "~/", "//")) or ".." in normalized.split("/"):
            raise ValueError("road graph source key must be portable and relative")
        expected_classification = {
            GraphSourceKind.OPENSTREETMAP_EXTRACT: "ODBL_DERIVATIVE_DATABASE",
            GraphSourceKind.HAND_AUTHORED_FIXTURE: "PROJECT_TEST_FIXTURE",
        }[self.source_kind]
        if self.database_classification != expected_classification:
            raise ValueError("road graph source classification is inconsistent")
        return self


class RoadGraphLocalFrame(StrictModel):
    frame: NamedFrame
    origin_latitude_e8: Annotated[int, Field(ge=-9_000_000_000, le=9_000_000_000)]
    origin_longitude_e8: Annotated[int, Field(ge=-18_000_000_000, le=18_000_000_000)]
    origin_altitude_m: Annotated[float, Field(ge=0.0, le=0.0)]
    origin_policy: Literal["OSM_BOUNDS_CENTER_WGS84_E8_ZERO_ELLIPSOID"]
    coordinate_provenance: Literal["WGS84_SOURCE_DERIVED_LOCAL_WORLD"]

    def local_origin(self) -> LocalOrigin:
        return LocalOrigin(
            frame=self.frame,
            global_coordinate=GlobalCoordinate(
                latitude_deg=self.origin_latitude_e8 / 100_000_000.0,
                longitude_deg=self.origin_longitude_e8 / 100_000_000.0,
                altitude_m=0.0,
                vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
            ),
        )


class RoadNode(StrictModel):
    node_id: Identifier
    osm_node_id: Annotated[int, Field(gt=0)]
    position_wgs84_e7: Wgs84E7
    position_local_m: tuple[float, float, float]


class DirectedRoadArc(StrictModel):
    arc_id: Annotated[str, Field(pattern=r"^osm-arc-sha256-[0-9a-f]{64}$")]
    from_node_id: Identifier
    to_node_id: Identifier
    source_way_id: Annotated[int, Field(gt=0)]
    source_chain_index: Annotated[int, Field(ge=0)]
    direction: ArcDirection
    highway_class: str
    source_geometry_node_ids: tuple[Annotated[int, Field(gt=0)], ...]
    geometry_wgs84_e7: tuple[Wgs84E7, ...]
    geometry_local_m: tuple[tuple[float, float, float], ...]
    length_m: Annotated[float, Field(gt=0.0)]
    layer: int
    bridge: bool
    tunnel: bool
    access_interpretation: Literal["INCLUDED_WITHOUT_ROAD_LEGALITY_CLAIM"]
    source_tags: tuple[OsmTag, ...]

    @model_validator(mode="after")
    def validate_geometry_contract(self) -> Self:
        point_count = len(self.source_geometry_node_ids)
        if point_count < 2 or (
            len(self.geometry_wgs84_e7) != point_count
            or len(self.geometry_local_m) != point_count
        ):
            raise ValueError("road arc geometries must have matching point counts")
        if self.from_node_id != _node_name(
            self.source_geometry_node_ids[0]
        ) or self.to_node_id != _node_name(self.source_geometry_node_ids[-1]):
            raise ValueError("road arc endpoints must match source geometry endpoints")
        return self


class TransitionRule(StrictModel):
    from_arc_id: Annotated[str, Field(pattern=r"^osm-arc-sha256-[0-9a-f]{64}$")]
    to_arc_id: Annotated[str, Field(pattern=r"^osm-arc-sha256-[0-9a-f]{64}$")]
    state: TransitionState
    source_relation_id: Annotated[int, Field(gt=0)]


class TurnRestrictionEvidence(StrictModel):
    source_relation_id: Annotated[int, Field(gt=0)]
    restriction_value: str
    interpretation: RestrictionInterpretation
    from_way_ids: tuple[Annotated[int, Field(gt=0)], ...]
    via_node_ids: tuple[Annotated[int, Field(gt=0)], ...]
    via_way_ids: tuple[Annotated[int, Field(gt=0)], ...]
    to_way_ids: tuple[Annotated[int, Field(gt=0)], ...]
    generated_rule_count: Annotated[int, Field(ge=0)]
    source_tags: tuple[OsmTag, ...]

    @model_validator(mode="after")
    def validate_applied_evidence(self) -> Self:
        if (
            self.interpretation
            in {
                RestrictionInterpretation.APPLIED_NO,
                RestrictionInterpretation.APPLIED_ONLY,
            }
            and self.generated_rule_count == 0
        ):
            raise ValueError("applied turn restrictions must generate transition rules")
        return self


class RoadGraphStatistics(StrictModel):
    parsed_node_count: Annotated[int, Field(ge=0)]
    parsed_way_count: Annotated[int, Field(ge=0)]
    parsed_relation_count: Annotated[int, Field(ge=0)]
    included_way_count: Annotated[int, Field(ge=0)]
    excluded_way_count: Annotated[int, Field(ge=0)]
    topology_node_count: Annotated[int, Field(ge=0)]
    directed_arc_count: Annotated[int, Field(ge=0)]
    forward_arc_count: Annotated[int, Field(ge=0)]
    reverse_arc_count: Annotated[int, Field(ge=0)]
    transition_rule_count: Annotated[int, Field(ge=0)]
    applied_restriction_count: Annotated[int, Field(ge=0)]
    unknown_restriction_count: Annotated[int, Field(ge=0)]
    exclusion_reason_counts: dict[str, Annotated[int, Field(ge=0)]]


class DirectedRoadGraph(StrictModel):
    schema_version: Literal["cartosentry.directed-road-graph.v1"]
    graph_id: Annotated[str, Field(pattern=r"^road-graph-sha256-[0-9a-f]{64}$")]
    profile_immutable_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    profile_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source: RoadGraphSource
    local_frame: RoadGraphLocalFrame
    nodes: tuple[RoadNode, ...]
    arcs: tuple[DirectedRoadArc, ...]
    transition_rules: tuple[TransitionRule, ...]
    restrictions: tuple[TurnRestrictionEvidence, ...]
    statistics: RoadGraphStatistics

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        node_ids = {item.node_id for item in self.nodes}
        arc_ids = {item.arc_id for item in self.arcs}
        if len(node_ids) != len(self.nodes) or len(arc_ids) != len(self.arcs):
            raise ValueError("road graph node and arc identifiers must be unique")
        if any(
            item.from_node_id not in node_ids or item.to_node_id not in node_ids
            for item in self.arcs
        ):
            raise ValueError("road graph arcs must reference topology nodes")
        if any(
            item.from_arc_id not in arc_ids or item.to_arc_id not in arc_ids
            for item in self.transition_rules
        ):
            raise ValueError("road graph transition rules must reference arcs")
        if tuple(item.arc_id for item in self.arcs) != tuple(
            sorted(item.arc_id for item in self.arcs)
        ):
            raise ValueError("road graph arcs must have deterministic identity order")
        restriction_ids = [item.source_relation_id for item in self.restrictions]
        rule_keys = [
            (
                item.from_arc_id,
                item.to_arc_id,
                item.state,
                item.source_relation_id,
            )
            for item in self.transition_rules
        ]
        if len(restriction_ids) != len(set(restriction_ids)) or len(rule_keys) != len(
            set(rule_keys)
        ):
            raise ValueError("road graph restriction evidence must be unique")
        statistics = self.statistics
        if (
            statistics.topology_node_count != len(self.nodes)
            or statistics.directed_arc_count != len(self.arcs)
            or statistics.forward_arc_count
            != sum(item.direction is ArcDirection.FORWARD for item in self.arcs)
            or statistics.reverse_arc_count
            != sum(item.direction is ArcDirection.REVERSE for item in self.arcs)
            or statistics.transition_rule_count != len(self.transition_rules)
            or statistics.included_way_count + statistics.excluded_way_count
            != statistics.parsed_way_count
        ):
            raise ValueError("road graph statistics do not match portable content")
        return self


class MatchingObservation(StrictModel):
    observation_id: Annotated[str, Field(pattern=r"^observation-sha256-[0-9a-f]{64}$")]
    time: TimePoint
    coordinate_wgs84: GlobalCoordinate
    position_local_m: tuple[float, float, float]
    local_frame_id: Identifier
    coordinate_provenance: Literal["WGS84_SOURCE_DERIVED_LOCAL_WORLD"]
    local_altitude_policy: Literal["ZERO_ELLIPSOID_FOR_HORIZONTAL_MATCHING"]
    source_provenance: SourceProvenance


@dataclass(frozen=True)
class _RawNode:
    node_id: int
    coordinate: Wgs84E7


@dataclass(frozen=True)
class _RawWay:
    way_id: int
    node_ids: tuple[int, ...]
    tags: dict[str, str]


@dataclass(frozen=True)
class _RawMember:
    member_type: str
    member_id: int
    role: str


@dataclass(frozen=True)
class _RawRelation:
    relation_id: int
    members: tuple[_RawMember, ...]
    tags: dict[str, str]


@dataclass(frozen=True)
class _IncludedWay:
    raw: _RawWay
    forward: bool
    reverse: bool


def _canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_graph_import_profile(path: Path) -> tuple[GraphImportProfile, str]:
    """Load and self-authenticate the frozen graph import profile."""

    try:
        content = read_bounded_regular_bytes(
            path,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="graph import profile",
        )
        decoded = decode_bounded_json(
            content,
            maximum_bytes=MAXIMUM_PROFILE_BYTES,
            context="graph import profile",
        )
    except ManifestBoundaryError as error:
        raise ValueError("graph import profile is unavailable or malformed") from error
    if not isinstance(decoded, dict):
        raise ValueError("graph import profile must be an object")
    raw = cast(dict[str, object], decoded)
    canonical = {key: value for key, value in raw.items() if key != "immutable_sha256"}
    if raw.get("immutable_sha256") != _canonical_hash(canonical):
        raise ValueError("graph import profile immutable hash is invalid")
    return GraphImportProfile.model_validate_json(content), hashlib.sha256(
        content
    ).hexdigest()


def _positive_id(value: str, *, context: str) -> int:
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise ValueError(f"OSM {context} identifier must be a positive integer")
    return int(value)


def _coordinate_e7(value: str, *, latitude: bool) -> int:
    try:
        scaled = Decimal(value) * Decimal(10_000_000)
    except InvalidOperation as error:
        raise ValueError("OSM coordinate must be finite decimal degrees") from error
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError("OSM coordinates must have at most seven decimal places")
    result = int(integral)
    limit = 900_000_000 if latitude else 1_800_000_000
    if not -limit <= result <= limit:
        raise ValueError("OSM coordinate is outside WGS84 bounds")
    return result


def _tags(element: ElementTree.Element, maximum: int) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in element.findall("tag"):
        key = item.attrib.get("k")
        value = item.attrib.get("v")
        if not key or value is None or key in result:
            raise ValueError("OSM tags must have unique nonempty keys and values")
        result[key] = value
        if len(result) > maximum:
            raise ValueError("OSM element tag count exceeds the frozen budget")
    return result


def _parse_osm(
    content: bytes, profile: GraphImportProfile
) -> tuple[
    dict[int, _RawNode],
    tuple[_RawWay, ...],
    tuple[_RawRelation, ...],
    tuple[int, int, int, int],
]:
    upper = content.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("OSM XML declarations and entities are not supported")
    try:
        root = ElementTree.parse(BytesIO(content)).getroot()
    except (ElementTree.ParseError, UnicodeError) as error:
        raise ValueError("OSM XML is malformed") from error
    if root.tag != "osm" or root.attrib.get("version") != "0.6":
        raise ValueError("OSM XML must use the version 0.6 root contract")
    node_elements = root.findall("node")
    way_elements = root.findall("way")
    relation_elements = root.findall("relation")
    limits = profile.limits
    if len(node_elements) > limits.maximum_nodes:
        raise ValueError("OSM node count exceeds the frozen budget")
    if len(way_elements) > limits.maximum_ways:
        raise ValueError("OSM way count exceeds the frozen budget")
    if len(relation_elements) > limits.maximum_relations:
        raise ValueError("OSM relation count exceeds the frozen budget")
    nodes: dict[int, _RawNode] = {}
    for element in node_elements:
        node_id = _positive_id(element.attrib.get("id", ""), context="node")
        if node_id in nodes:
            raise ValueError("OSM node identifiers must be unique")
        nodes[node_id] = _RawNode(
            node_id=node_id,
            coordinate=Wgs84E7(
                latitude_e7=_coordinate_e7(
                    element.attrib.get("lat", ""), latitude=True
                ),
                longitude_e7=_coordinate_e7(
                    element.attrib.get("lon", ""), latitude=False
                ),
            ),
        )
        _tags(element, limits.maximum_tags_per_element)
    ways: list[_RawWay] = []
    reference_count = 0
    way_ids: set[int] = set()
    for element in way_elements:
        way_id = _positive_id(element.attrib.get("id", ""), context="way")
        if way_id in way_ids:
            raise ValueError("OSM way identifiers must be unique")
        way_ids.add(way_id)
        node_ids = tuple(
            _positive_id(item.attrib.get("ref", ""), context="node reference")
            for item in element.findall("nd")
        )
        reference_count += len(node_ids)
        if reference_count > limits.maximum_way_node_references:
            raise ValueError("OSM way references exceed the frozen budget")
        if any(item not in nodes for item in node_ids):
            raise ValueError("OSM way references an unavailable node")
        ways.append(
            _RawWay(
                way_id=way_id,
                node_ids=node_ids,
                tags=_tags(element, limits.maximum_tags_per_element),
            )
        )
    relations: list[_RawRelation] = []
    relation_ids: set[int] = set()
    for element in relation_elements:
        relation_id = _positive_id(element.attrib.get("id", ""), context="relation")
        if relation_id in relation_ids:
            raise ValueError("OSM relation identifiers must be unique")
        relation_ids.add(relation_id)
        members = tuple(
            _RawMember(
                member_type=item.attrib.get("type", ""),
                member_id=_positive_id(
                    item.attrib.get("ref", ""), context="relation member"
                ),
                role=item.attrib.get("role", ""),
            )
            for item in element.findall("member")
        )
        relations.append(
            _RawRelation(
                relation_id=relation_id,
                members=members,
                tags=_tags(element, limits.maximum_tags_per_element),
            )
        )
    bounds = root.find("bounds")
    if bounds is not None:
        minimum_latitude_e7 = _coordinate_e7(
            bounds.attrib.get("minlat", ""), latitude=True
        )
        minimum_longitude_e7 = _coordinate_e7(
            bounds.attrib.get("minlon", ""), latitude=False
        )
        maximum_latitude_e7 = _coordinate_e7(
            bounds.attrib.get("maxlat", ""), latitude=True
        )
        maximum_longitude_e7 = _coordinate_e7(
            bounds.attrib.get("maxlon", ""), latitude=False
        )
    elif nodes:
        minimum_latitude_e7 = min(
            item.coordinate.latitude_e7 for item in nodes.values()
        )
        minimum_longitude_e7 = min(
            item.coordinate.longitude_e7 for item in nodes.values()
        )
        maximum_latitude_e7 = max(
            item.coordinate.latitude_e7 for item in nodes.values()
        )
        maximum_longitude_e7 = max(
            item.coordinate.longitude_e7 for item in nodes.values()
        )
    else:
        raise ValueError("OSM XML has neither bounds nor nodes")
    if (
        minimum_latitude_e7 > maximum_latitude_e7
        or minimum_longitude_e7 > maximum_longitude_e7
    ):
        raise ValueError("OSM bounds are reversed")
    return (
        nodes,
        tuple(ways),
        tuple(relations),
        (
            minimum_latitude_e7,
            minimum_longitude_e7,
            maximum_latitude_e7,
            maximum_longitude_e7,
        ),
    )


def _directional_access(
    tags: dict[str, str],
    suffix: Literal["forward", "backward"],
    profile: GraphImportProfile,
) -> tuple[bool, bool]:
    for key in profile.access.generic_keys:
        directional_key = f"{key}:{suffix}"
        if directional_key not in tags:
            continue
        value = tags[directional_key]
        if value in profile.access.allowed_values:
            return True, False
        if value in profile.access.denied_values:
            return False, False
        return False, True
    return True, False


def _include_way(
    way: _RawWay, profile: GraphImportProfile
) -> tuple[_IncludedWay | None, str | None]:
    tags = way.tags
    highway = tags.get("highway", "")
    if highway not in profile.included_highway_classes:
        return None, "unsupported_highway"
    if tags.get("area") == "yes":
        return None, "area"
    if highway == "construction" or "construction" in tags or "proposed" in tags:
        return None, "construction"
    if tags.get("route") == "ferry" or "ferry" in tags:
        return None, "ferry"
    if any(key.endswith(":conditional") for key in tags):
        return None, "conditional_access"
    for key in profile.access.generic_keys:
        if key not in tags:
            continue
        value = tags[key]
        if value in profile.access.allowed_values:
            break
        if value in profile.access.denied_values:
            return None, "denied_or_limited_access"
        return None, "unknown_access"
    if len(way.node_ids) < 2:
        return None, "insufficient_geometry"
    oneway_value: str | None = None
    for key in profile.directionality.oneway_keys:
        if key in tags:
            oneway_value = tags[key]
            break
    if oneway_value is None:
        forward = True
        reverse = not (
            tags.get("junction")
            in profile.directionality.implicit_forward_junction_values
            or highway in profile.directionality.implicit_forward_highway_classes
        )
    elif oneway_value in profile.directionality.forward_values:
        forward, reverse = True, False
    elif oneway_value in profile.directionality.reverse_values:
        forward, reverse = False, True
    elif oneway_value in profile.directionality.bidirectional_values:
        forward, reverse = True, True
    else:
        return None, "unknown_oneway"
    forward_access, forward_unknown = _directional_access(tags, "forward", profile)
    reverse_access, reverse_unknown = _directional_access(tags, "backward", profile)
    if forward_unknown or reverse_unknown:
        return None, "unknown_directional_access"
    forward = forward and forward_access
    reverse = reverse and reverse_access
    if not forward and not reverse:
        return None, "no_allowed_direction"
    layer = tags.get("layer")
    if layer is not None:
        try:
            int(layer)
        except ValueError:
            return None, "unknown_layer"
    return _IncludedWay(raw=way, forward=forward, reverse=reverse), None


def _tag_models(tags: dict[str, str]) -> tuple[OsmTag, ...]:
    return tuple(OsmTag(key=key, value=value) for key, value in sorted(tags.items()))


def _node_name(node_id: int) -> str:
    return f"osm-node-{node_id}"


def _local_frame(bounds: tuple[int, int, int, int]) -> RoadGraphLocalFrame:
    (
        minimum_latitude_e7,
        minimum_longitude_e7,
        maximum_latitude_e7,
        maximum_longitude_e7,
    ) = bounds
    origin_latitude_e8 = (minimum_latitude_e7 + maximum_latitude_e7) * 5
    origin_longitude_e8 = (minimum_longitude_e7 + maximum_longitude_e7) * 5
    identity = _canonical_hash(
        {"latitude_e8": origin_latitude_e8, "longitude_e8": origin_longitude_e8}
    )[:16]
    return RoadGraphLocalFrame(
        frame=NamedFrame(
            frame_id=f"road_graph_local_{identity}",
            x_axis="east",
            y_axis="north",
            z_axis="up",
        ),
        origin_latitude_e8=origin_latitude_e8,
        origin_longitude_e8=origin_longitude_e8,
        origin_altitude_m=0.0,
        origin_policy="OSM_BOUNDS_CENTER_WGS84_E8_ZERO_ELLIPSOID",
        coordinate_provenance="WGS84_SOURCE_DERIVED_LOCAL_WORLD",
    )


def _local_position(
    coordinate: Wgs84E7,
    local_frame: RoadGraphLocalFrame,
    decimal_places: int,
) -> tuple[float, float, float]:
    converted = local_frame.local_origin().to_local(
        GlobalCoordinate(
            latitude_deg=coordinate.latitude_e7 / 10_000_000.0,
            longitude_deg=coordinate.longitude_e7 / 10_000_000.0,
            altitude_m=0.0,
            vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
        )
    )
    return (
        round(converted.position_m[0], decimal_places),
        round(converted.position_m[1], decimal_places),
        0.0,
    )


def _arc_length(
    geometry: tuple[tuple[float, float, float], ...], decimal_places: int
) -> float:
    return round(
        sum(math.dist(left, right) for left, right in pairwise(geometry)),
        decimal_places,
    )


def _arc_id(
    profile_sha256: str,
    way_id: int,
    chain_index: int,
    direction: ArcDirection,
    node_ids: tuple[int, ...],
) -> str:
    digest = _canonical_hash(
        {
            "profile_immutable_sha256": profile_sha256,
            "source_way_id": way_id,
            "source_chain_index": chain_index,
            "direction": direction,
            "source_geometry_node_ids": node_ids,
        }
    )
    return f"osm-arc-sha256-{digest}"


def _build_arcs(
    included: tuple[_IncludedWay, ...],
    nodes: dict[int, _RawNode],
    restriction_via_nodes: set[int],
    local_frame: RoadGraphLocalFrame,
    profile: GraphImportProfile,
) -> tuple[tuple[RoadNode, ...], tuple[DirectedRoadArc, ...]]:
    usage: dict[int, set[int]] = defaultdict(set)
    for item in included:
        for node_id in set(item.raw.node_ids):
            usage[node_id].add(item.raw.way_id)
    topology = {
        node_id for node_id, way_ids in usage.items() if len(way_ids) > 1
    } | restriction_via_nodes
    for item in included:
        topology.add(item.raw.node_ids[0])
        topology.add(item.raw.node_ids[-1])
    local_positions = {
        node_id: _local_position(
            nodes[node_id].coordinate,
            local_frame,
            profile.geometry.local_coordinate_rounding_decimal_places,
        )
        for node_id in {node_id for item in included for node_id in item.raw.node_ids}
    }
    arcs: list[DirectedRoadArc] = []
    for item in sorted(included, key=lambda value: value.raw.way_id):
        raw = item.raw
        split_positions = [
            index for index, node_id in enumerate(raw.node_ids) if node_id in topology
        ]
        if not split_positions or split_positions[0] != 0:
            split_positions.insert(0, 0)
        if split_positions[-1] != len(raw.node_ids) - 1:
            split_positions.append(len(raw.node_ids) - 1)
        for chain_index, (start, end) in enumerate(pairwise(split_positions)):
            if end <= start:
                continue
            source_node_ids = raw.node_ids[start : end + 1]
            if len(source_node_ids) > profile.limits.maximum_geometry_points_per_arc:
                raise ValueError("road arc geometry exceeds the frozen point budget")
            geometry = tuple(local_positions[node_id] for node_id in source_node_ids)
            length = _arc_length(
                geometry, profile.geometry.length_rounding_decimal_places
            )
            if length < profile.geometry.minimum_arc_length_m:
                continue
            layer = int(raw.tags.get("layer", "0"))
            bridge = raw.tags.get("bridge") in {"yes", "true", "1"}
            tunnel = raw.tags.get("tunnel") in {"yes", "true", "1"}
            source_tags = _tag_models(raw.tags)
            if item.forward:
                arcs.append(
                    DirectedRoadArc(
                        arc_id=_arc_id(
                            profile.immutable_sha256,
                            raw.way_id,
                            chain_index,
                            ArcDirection.FORWARD,
                            source_node_ids,
                        ),
                        from_node_id=_node_name(source_node_ids[0]),
                        to_node_id=_node_name(source_node_ids[-1]),
                        direction=ArcDirection.FORWARD,
                        source_way_id=raw.way_id,
                        source_chain_index=chain_index,
                        highway_class=raw.tags["highway"],
                        source_geometry_node_ids=source_node_ids,
                        geometry_wgs84_e7=tuple(
                            nodes[node_id].coordinate for node_id in source_node_ids
                        ),
                        geometry_local_m=geometry,
                        length_m=length,
                        layer=layer,
                        bridge=bridge,
                        tunnel=tunnel,
                        access_interpretation="INCLUDED_WITHOUT_ROAD_LEGALITY_CLAIM",
                        source_tags=source_tags,
                    )
                )
            if item.reverse:
                reversed_node_ids = tuple(reversed(source_node_ids))
                arcs.append(
                    DirectedRoadArc(
                        arc_id=_arc_id(
                            profile.immutable_sha256,
                            raw.way_id,
                            chain_index,
                            ArcDirection.REVERSE,
                            reversed_node_ids,
                        ),
                        from_node_id=_node_name(reversed_node_ids[0]),
                        to_node_id=_node_name(reversed_node_ids[-1]),
                        direction=ArcDirection.REVERSE,
                        source_way_id=raw.way_id,
                        source_chain_index=chain_index,
                        highway_class=raw.tags["highway"],
                        source_geometry_node_ids=reversed_node_ids,
                        geometry_wgs84_e7=tuple(
                            nodes[node_id].coordinate for node_id in reversed_node_ids
                        ),
                        geometry_local_m=tuple(reversed(geometry)),
                        length_m=length,
                        layer=layer,
                        bridge=bridge,
                        tunnel=tunnel,
                        access_interpretation="INCLUDED_WITHOUT_ROAD_LEGALITY_CLAIM",
                        source_tags=source_tags,
                    )
                )
    road_nodes = tuple(
        RoadNode(
            node_id=_node_name(node_id),
            osm_node_id=node_id,
            position_wgs84_e7=nodes[node_id].coordinate,
            position_local_m=local_positions[node_id],
        )
        for node_id in sorted(topology & set(local_positions))
    )
    return road_nodes, tuple(sorted(arcs, key=lambda item: item.arc_id))


def _restriction_members(
    relation: _RawRelation, member_type: str, role: str
) -> tuple[int, ...]:
    return tuple(
        item.member_id
        for item in relation.members
        if item.member_type == member_type and item.role == role
    )


def _build_restrictions(
    relations: tuple[_RawRelation, ...],
    arcs: tuple[DirectedRoadArc, ...],
    included_way_ids: set[int],
    profile: GraphImportProfile,
) -> tuple[tuple[TransitionRule, ...], tuple[TurnRestrictionEvidence, ...]]:
    incoming_by_way_node: dict[tuple[int, str], list[DirectedRoadArc]] = defaultdict(
        list
    )
    outgoing_by_way_node: dict[tuple[int, str], list[DirectedRoadArc]] = defaultdict(
        list
    )
    outgoing_by_node: dict[str, list[DirectedRoadArc]] = defaultdict(list)
    for arc in arcs:
        incoming_by_way_node[(arc.source_way_id, arc.to_node_id)].append(arc)
        outgoing_by_way_node[(arc.source_way_id, arc.from_node_id)].append(arc)
        outgoing_by_node[arc.from_node_id].append(arc)
    rules: dict[tuple[str, str, TransitionState, int], TransitionRule] = {}
    evidence: list[TurnRestrictionEvidence] = []
    for relation in sorted(relations, key=lambda item: item.relation_id):
        if relation.tags.get("type") != profile.turn_restrictions.relation_type:
            continue
        from_way_ids = _restriction_members(relation, "way", "from")
        to_way_ids = _restriction_members(relation, "way", "to")
        via_node_ids = _restriction_members(relation, "node", "via")
        via_way_ids = _restriction_members(relation, "way", "via")
        restriction_value = next(
            (
                relation.tags[key]
                for key in profile.turn_restrictions.restriction_tag_keys
                if key in relation.tags
            ),
            relation.tags.get("restriction:conditional", ""),
        )
        conditional = any(key.endswith(":conditional") for key in relation.tags)
        simple_member_contract = (
            len(from_way_ids) == 1
            and len(to_way_ids) == 1
            and len(via_node_ids) == 1
            and not via_way_ids
        )
        if "except" in relation.tags:
            interpretation = RestrictionInterpretation.UNKNOWN_EXCEPT
        elif conditional:
            interpretation = RestrictionInterpretation.UNKNOWN_CONDITIONAL
        elif via_way_ids:
            interpretation = RestrictionInterpretation.UNKNOWN_VIA_WAY
        elif not simple_member_contract:
            interpretation = RestrictionInterpretation.UNKNOWN_MALFORMED
        elif not restriction_value.startswith(
            profile.turn_restrictions.supported_prefixes
        ):
            interpretation = RestrictionInterpretation.UNKNOWN_VALUE
        elif not set((*from_way_ids, *to_way_ids)) <= included_way_ids:
            interpretation = RestrictionInterpretation.INACTIVE_SOURCE_EXCLUDED
        elif restriction_value.startswith("no_"):
            interpretation = RestrictionInterpretation.APPLIED_NO
        else:
            interpretation = RestrictionInterpretation.APPLIED_ONLY
        generated: list[TransitionRule] = []
        if interpretation is not RestrictionInterpretation.INACTIVE_SOURCE_EXCLUDED:
            if len(via_node_ids) == 1:
                candidate_nodes: tuple[str, ...] = (_node_name(via_node_ids[0]),)
                incoming = [
                    arc
                    for way_id in from_way_ids
                    for arc in incoming_by_way_node.get(
                        (way_id, candidate_nodes[0]), []
                    )
                ]
            else:
                incoming = [arc for arc in arcs if arc.source_way_id in from_way_ids]
                candidate_nodes = tuple(sorted({arc.to_node_id for arc in incoming}))
            if interpretation is RestrictionInterpretation.APPLIED_NO:
                for node_id in candidate_nodes:
                    for from_arc in [
                        item for item in incoming if item.to_node_id == node_id
                    ]:
                        for to_way_id in to_way_ids:
                            for to_arc in outgoing_by_way_node.get(
                                (to_way_id, node_id), []
                            ):
                                generated.append(
                                    TransitionRule(
                                        from_arc_id=from_arc.arc_id,
                                        to_arc_id=to_arc.arc_id,
                                        state=TransitionState.FORBIDDEN,
                                        source_relation_id=relation.relation_id,
                                    )
                                )
                if not generated:
                    interpretation = RestrictionInterpretation.UNKNOWN_DISCONNECTED
            elif interpretation is RestrictionInterpretation.APPLIED_ONLY:
                allowed = {
                    item.arc_id
                    for node_id in candidate_nodes
                    for to_way_id in to_way_ids
                    for item in outgoing_by_way_node.get((to_way_id, node_id), [])
                }
                if allowed:
                    for from_arc in incoming:
                        for to_arc in outgoing_by_node.get(from_arc.to_node_id, []):
                            if to_arc.arc_id not in allowed:
                                generated.append(
                                    TransitionRule(
                                        from_arc_id=from_arc.arc_id,
                                        to_arc_id=to_arc.arc_id,
                                        state=TransitionState.FORBIDDEN,
                                        source_relation_id=relation.relation_id,
                                    )
                                )
                else:
                    interpretation = RestrictionInterpretation.UNKNOWN_DISCONNECTED
                if not generated:
                    interpretation = RestrictionInterpretation.UNKNOWN_DISCONNECTED
            if interpretation in {
                RestrictionInterpretation.UNKNOWN_CONDITIONAL,
                RestrictionInterpretation.UNKNOWN_VIA_WAY,
                RestrictionInterpretation.UNKNOWN_VALUE,
                RestrictionInterpretation.UNKNOWN_EXCEPT,
                RestrictionInterpretation.UNKNOWN_MALFORMED,
                RestrictionInterpretation.UNKNOWN_DISCONNECTED,
            }:
                for from_arc in incoming:
                    for to_arc in outgoing_by_node.get(from_arc.to_node_id, []):
                        generated.append(
                            TransitionRule(
                                from_arc_id=from_arc.arc_id,
                                to_arc_id=to_arc.arc_id,
                                state=TransitionState.UNKNOWN_RESTRICTION,
                                source_relation_id=relation.relation_id,
                            )
                        )
        for rule in generated:
            rules[
                (
                    rule.from_arc_id,
                    rule.to_arc_id,
                    rule.state,
                    rule.source_relation_id,
                )
            ] = rule
        evidence.append(
            TurnRestrictionEvidence(
                source_relation_id=relation.relation_id,
                restriction_value=restriction_value,
                interpretation=interpretation,
                from_way_ids=from_way_ids,
                via_node_ids=via_node_ids,
                via_way_ids=via_way_ids,
                to_way_ids=to_way_ids,
                generated_rule_count=len(generated),
                source_tags=_tag_models(relation.tags),
            )
        )
    return (
        tuple(
            sorted(
                rules.values(),
                key=lambda item: (
                    item.from_arc_id,
                    item.to_arc_id,
                    item.state,
                    item.source_relation_id,
                ),
            )
        ),
        tuple(evidence),
    )


def _graph_identity_payload(graph: DirectedRoadGraph) -> dict[str, object]:
    return cast(
        dict[str, object],
        graph.model_dump(mode="json", exclude={"graph_id"}),
    )


def import_osm_road_graph(
    source_path: Path,
    *,
    profile: GraphImportProfile,
    profile_file_sha256: str,
    source_object_key: str,
    expected_source_sha256: str,
    source_kind: GraphSourceKind = GraphSourceKind.OPENSTREETMAP_EXTRACT,
) -> DirectedRoadGraph:
    """Import one bounded OSM XML extract into an exact directed multigraph."""

    try:
        content = read_bounded_regular_bytes(
            source_path,
            maximum_bytes=profile.limits.maximum_source_bytes,
            context="OSM road graph",
        )
    except ManifestBoundaryError as error:
        raise ValueError("OSM road graph is unavailable or malformed") from error
    source_sha256 = _file_sha256(content)
    if source_sha256 != expected_source_sha256:
        raise ValueError("OSM road graph does not match its pinned object hash")
    nodes, ways, relations, bounds = _parse_osm(content, profile)
    exclusion_reasons: Counter[str] = Counter()
    included: list[_IncludedWay] = []
    for way in ways:
        result, reason = _include_way(way, profile)
        if result is None:
            exclusion_reasons[cast(str, reason)] += 1
        else:
            included.append(result)
    restriction_via_nodes = {
        item.member_id
        for relation in relations
        if relation.tags.get("type") == profile.turn_restrictions.relation_type
        for item in relation.members
        if item.member_type == "node" and item.role == "via"
    }
    local_frame = _local_frame(bounds)
    road_nodes, arcs = _build_arcs(
        tuple(included),
        nodes,
        restriction_via_nodes,
        local_frame,
        profile,
    )
    if not arcs:
        raise ValueError("OSM import produced no directed road arcs")
    rules, restrictions = _build_restrictions(
        relations,
        arcs,
        {item.raw.way_id for item in included},
        profile,
    )
    applied = sum(
        item.interpretation
        in {
            RestrictionInterpretation.APPLIED_NO,
            RestrictionInterpretation.APPLIED_ONLY,
        }
        for item in restrictions
    )
    unknown = sum(
        item.interpretation.value.startswith("UNKNOWN_") for item in restrictions
    )
    statistics = RoadGraphStatistics(
        parsed_node_count=len(nodes),
        parsed_way_count=len(ways),
        parsed_relation_count=len(relations),
        included_way_count=len(included),
        excluded_way_count=len(ways) - len(included),
        topology_node_count=len(road_nodes),
        directed_arc_count=len(arcs),
        forward_arc_count=sum(item.direction is ArcDirection.FORWARD for item in arcs),
        reverse_arc_count=sum(item.direction is ArcDirection.REVERSE for item in arcs),
        transition_rule_count=len(rules),
        applied_restriction_count=applied,
        unknown_restriction_count=unknown,
        exclusion_reason_counts=dict(sorted(exclusion_reasons.items())),
    )
    graph = DirectedRoadGraph(
        schema_version="cartosentry.directed-road-graph.v1",
        graph_id=f"road-graph-sha256-{'0' * 64}",
        profile_immutable_sha256=profile.immutable_sha256,
        profile_file_sha256=profile_file_sha256,
        source=RoadGraphSource(
            source_kind=source_kind,
            source_object_key=source_object_key,
            source_sha256=source_sha256,
            snapshot_utc=(
                profile.authorities.snapshot_utc
                if source_kind is GraphSourceKind.OPENSTREETMAP_EXTRACT
                else "source-controlled"
            ),
            attribution=(
                profile.authorities.attribution
                if source_kind is GraphSourceKind.OPENSTREETMAP_EXTRACT
                else "CartoSentry hand-authored graph fixture."
            ),
            license_url=(
                profile.authorities.license_url
                if source_kind is GraphSourceKind.OPENSTREETMAP_EXTRACT
                else "https://www.apache.org/licenses/LICENSE-2.0"
            ),
            database_classification=(
                profile.authorities.database_classification
                if source_kind is GraphSourceKind.OPENSTREETMAP_EXTRACT
                else "PROJECT_TEST_FIXTURE"
            ),
        ),
        local_frame=local_frame,
        nodes=road_nodes,
        arcs=arcs,
        transition_rules=rules,
        restrictions=restrictions,
        statistics=statistics,
    )
    identity = _canonical_hash(_graph_identity_payload(graph))
    return graph.model_copy(update={"graph_id": f"road-graph-sha256-{identity}"})


def validate_graph_identity(graph: DirectedRoadGraph) -> None:
    """Reject any graph whose complete portable content differs from its identity."""

    expected = f"road-graph-sha256-{_canonical_hash(_graph_identity_payload(graph))}"
    if graph.graph_id != expected:
        raise ValueError("directed road graph identity is invalid")


class RoadGraphSpatialIndex:
    """Immutable deterministic radius index over directed arc geometries."""

    def __init__(self, graph: DirectedRoadGraph) -> None:
        self.arcs = graph.arcs
        self.geometries = tuple(
            LineString([(point[0], point[1]) for point in arc.geometry_local_m])
            for arc in self.arcs
        )
        self.tree = STRtree(self.geometries)

    def query_radius(
        self, position_m: tuple[float, float], radius_m: float
    ) -> tuple[DirectedRoadArc, ...]:
        if (
            not all(math.isfinite(value) for value in (*position_m, radius_m))
            or radius_m < 0.0
        ):
            raise ValueError("road graph spatial query must be finite and nonnegative")
        point = Point(position_m)
        indices = self.tree.query(point.buffer(radius_m).envelope)
        selected = {
            int(index)
            for index in indices
            if self.geometries[int(index)].distance(point) <= radius_m
        }
        return tuple(self.arcs[index] for index in sorted(selected))


def normalize_matching_observation(
    sample: TrajectorySample,
    graph: DirectedRoadGraph,
    *,
    decimal_places: int = 6,
) -> MatchingObservation:
    """Preserve source WGS84 provenance while deriving graph-local coordinates."""

    coordinate = sample.geographic.coordinate
    horizontal_coordinate = GlobalCoordinate(
        latitude_deg=coordinate.latitude_deg,
        longitude_deg=coordinate.longitude_deg,
        altitude_m=0.0,
        vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
    )
    local = graph.local_frame.local_origin().to_local(horizontal_coordinate)
    position = cast(
        tuple[float, float, float],
        tuple(round(value, decimal_places) for value in local.position_m),
    )
    payload = {
        "time": sample.time.model_dump(mode="json"),
        "coordinate_wgs84": coordinate.model_dump(mode="json"),
        "position_local_m": position,
        "local_frame_id": graph.local_frame.frame.frame_id,
        "coordinate_provenance": "WGS84_SOURCE_DERIVED_LOCAL_WORLD",
        "local_altitude_policy": "ZERO_ELLIPSOID_FOR_HORIZONTAL_MATCHING",
        "source_provenance": sample.provenance.model_dump(mode="json"),
    }
    return MatchingObservation(
        observation_id=f"observation-sha256-{_canonical_hash(payload)}",
        time=sample.time,
        coordinate_wgs84=coordinate,
        position_local_m=position,
        local_frame_id=graph.local_frame.frame.frame_id,
        coordinate_provenance="WGS84_SOURCE_DERIVED_LOCAL_WORLD",
        local_altitude_policy="ZERO_ELLIPSOID_FOR_HORIZONTAL_MATCHING",
        source_provenance=sample.provenance,
    )


__all__ = [
    "PROFILE_IMMUTABLE_SHA256",
    "ArcDirection",
    "DirectedRoadArc",
    "DirectedRoadGraph",
    "GraphImportProfile",
    "GraphSourceKind",
    "MatchingObservation",
    "RestrictionInterpretation",
    "RoadGraphSpatialIndex",
    "TransitionState",
    "import_osm_road_graph",
    "load_graph_import_profile",
    "normalize_matching_observation",
    "validate_graph_identity",
]
