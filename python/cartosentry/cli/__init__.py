"""CartoSentry public command-line interface."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from cartosentry.adapters import (
    BoreasAdapter,
    inspect_boreas,
    qualify_boreas_adapter,
    source_group_for_sequence,
)
from cartosentry.artifacts import (
    canonicalize_portable_artifact,
    validate_artifact_json,
)
from cartosentry.contracts import TimeEpoch, TimePoint, TimeReference
from cartosentry.faults import (
    FaultOperatorId,
    FaultRequest,
    inject_fault,
    load_fault_registry,
    materialize_fault_result,
    verify_fault_result,
)
from cartosentry.ingestion import (
    index_boreas_recording,
    load_ingestion_budget,
)
from cartosentry.lidar_integrity_qualification import qualify_lidar_integrity
from cartosentry.manifest_boundaries import (
    MAXIMUM_ARTIFACT_JSON_BYTES,
    read_bounded_regular_bytes,
)
from cartosentry.motion_alignment_qualification import qualify_motion_alignment
from cartosentry.public_road_matching_qualification import (
    prepare_public_route_review,
    qualify_public_road_matching,
)
from cartosentry.qualification import qualify_contracts
from cartosentry.recovery import qualify_run_recovery, resume_registered_run
from cartosentry.road_bins_qualification import qualify_directed_road_bins
from cartosentry.road_decoder_qualification import qualify_synthetic_road_matching
from cartosentry.road_graph import (
    DirectedRoadGraph,
    import_osm_road_graph,
    load_graph_import_profile,
    validate_graph_identity,
)
from cartosentry.road_graph_qualification import qualify_directed_road_graph
from cartosentry.road_matching import (
    ALGORITHM_BACKEND,
    CandidateState,
    RoadCandidate,
    best_emission_candidate,
    generate_road_candidates,
    load_map_matching_profile,
    make_road_match_observation,
    score_road_transition,
    validate_matching_graph_authority,
)
from cartosentry.scheduler import qualify_scheduler
from cartosentry.spikes import qualify_observability
from cartosentry.synthetic import materialize_fixture_set, qualify_fixture_set
from cartosentry.temporal_checkpoint import qualify_temporal_checkpoint
from cartosentry.topology_hypotheses_qualification import (
    qualify_topology_hypotheses,
)
from cartosentry.trajectory import qualify_reference_trajectory
from cartosentry.trajectory_integrity_qualification import (
    qualify_trajectory_integrity,
)

app = typer.Typer(
    name="cartosentry",
    help="Evidence-backed mapping-readiness analysis and recollection planning.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Run one of CartoSentry's checked public workflows."""


def _write_text_atomically(serialized: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _write_report(report: dict[str, object], output: Path | None) -> None:
    serialized = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if output is None:
        typer.echo(serialized, nl=False)
        return
    _write_text_atomically(serialized, output)


def _read_artifact(path: Path) -> str:
    content = read_bounded_regular_bytes(
        path,
        maximum_bytes=MAXIMUM_ARTIFACT_JSON_BYTES,
        context="artifact",
    )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("artifact is not valid UTF-8") from error


@app.command("import-road-graph")
def import_road_graph_command(
    source: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Manifest-pinned OpenStreetMap XML extract.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Destination for the portable directed graph JSON.",
        ),
    ],
    profile_path: Annotated[
        Path,
        typer.Option(
            "--profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen graph import profile.",
        ),
    ] = Path("profiles/graph_import_v1.yaml"),
) -> None:
    """Import the pinned public OSM extract as a portable directed graph."""

    try:
        profile, profile_file_sha256 = load_graph_import_profile(profile_path)
        graph = import_osm_road_graph(
            source,
            profile=profile,
            profile_file_sha256=profile_file_sha256,
            source_object_key=profile.authorities.public_object_key,
            expected_source_sha256=profile.authorities.public_object_sha256,
        )
        _write_report(graph.model_dump(mode="json"), output)
        _write_report(
            {
                "accepted": True,
                "directed_arc_count": graph.statistics.directed_arc_count,
                "graph_id": graph.graph_id,
                "output_object_key": output.name,
                "source_sha256": graph.source.source_sha256,
            },
            None,
        )
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Road-graph import failed: {error}", err=True)
        raise typer.Exit(code=2) from error


@app.command("qualify-road-graph")
def qualify_road_graph_command(
    public_data_root: Annotated[
        Path,
        typer.Option(
            "--public-data-root",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Root containing the manifest-pinned public objects.",
        ),
    ] = Path("data/public"),
    profile_path: Annotated[
        Path,
        typer.Option(
            "--profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen graph import profile.",
        ),
    ] = Path("profiles/graph_import_v1.yaml"),
    gate_path: Annotated[
        Path,
        typer.Option(
            "--gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M5.1 acceptance gate.",
        ),
    ] = Path("benchmarks/m5_1_graph_gate.yaml"),
    data_manifest: Annotated[
        Path,
        typer.Option(
            "--data-manifest",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Manifest-bound public data authority.",
        ),
    ] = Path("benchmarks/data_manifest.yaml"),
    fixture: Annotated[
        Path,
        typer.Option(
            "--fixture",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen independent topology fixture.",
        ),
    ] = Path("tests/fixtures/road_graphs/topology_v1.osm"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the machine-readable qualification report atomically.",
        ),
    ] = None,
) -> None:
    """Qualify deterministic graph import and real trajectory provenance."""

    try:
        report = qualify_directed_road_graph(
            profile_path=profile_path,
            gate_path=gate_path,
            data_manifest_path=data_manifest,
            fixture_path=fixture,
            public_graph_path=(
                public_data_root / "road_graphs/toronto-glen-shields-v1.osm"
            ),
            public_sequence_root=(public_data_root / "boreas-2021-09-02-11-42"),
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Road-graph qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("qualify-road-matching")
def qualify_road_matching_command(
    graph_profile_path: Annotated[
        Path,
        typer.Option(
            "--graph-profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen directed-road graph import profile.",
        ),
    ] = Path("profiles/graph_import_v1.yaml"),
    matching_profile_path: Annotated[
        Path,
        typer.Option(
            "--matching-profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen road-candidate and transition profile.",
        ),
    ] = Path("profiles/map_matching_v1.yaml"),
    decoder_profile_path: Annotated[
        Path,
        typer.Option(
            "--decoder-profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen offline map-decoder profile.",
        ),
    ] = Path("profiles/map_decoder_v1.yaml"),
    gate_path: Annotated[
        Path,
        typer.Option(
            "--gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M5.3 synthetic qualification gate.",
        ),
    ] = Path("benchmarks/m5_3_map_matching_gate.yaml"),
    truth_path: Annotated[
        Path,
        typer.Option(
            "--truth",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen independent M5.3 scenario truth.",
        ),
    ] = Path("benchmarks/m5_3_map_matching_truth.yaml"),
    numerical_charter_path: Annotated[
        Path,
        typer.Option(
            "--numerical-charter",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen numerical acceptance authority.",
        ),
    ] = Path("benchmarks/numerical_charter.yaml"),
    fixture_path: Annotated[
        Path,
        typer.Option(
            "--fixture",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen independent road-topology fixture.",
        ),
    ] = Path("tests/fixtures/road_graphs/topology_v1.osm"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the deterministic qualification report atomically.",
        ),
    ] = None,
) -> None:
    """Qualify offline road decoding on the frozen synthetic topology suite."""

    try:
        report = qualify_synthetic_road_matching(
            graph_profile_path=graph_profile_path,
            matching_profile_path=matching_profile_path,
            decoder_profile_path=decoder_profile_path,
            gate_path=gate_path,
            truth_path=truth_path,
            numerical_charter_path=numerical_charter_path,
            fixture_path=fixture_path,
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Road-matching qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("prepare-public-road-review")
def prepare_public_road_review_command(
    public_data_root: Annotated[
        Path,
        typer.Option(
            "--public-data-root",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Root containing the manifest-pinned public objects.",
        ),
    ] = Path("data/public"),
    gate_path: Annotated[
        Path,
        typer.Option(
            "--gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen pre-review M5.6 gate and sampling contract.",
        ),
    ] = Path("benchmarks/m5_6_public_road_matching_gate.yaml"),
    protocol_path: Annotated[
        Path,
        typer.Option(
            "--protocol",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen blind manual-review instructions.",
        ),
    ] = Path("docs/public_route_adjudication.md"),
    data_manifest_path: Annotated[
        Path,
        typer.Option("--data-manifest", exists=True, dir_okay=False),
    ] = Path("benchmarks/data_manifest.yaml"),
    source_groups_path: Annotated[
        Path,
        typer.Option("--source-groups", exists=True, dir_okay=False),
    ] = Path("benchmarks/source_groups.yaml"),
    split_manifest_path: Annotated[
        Path,
        typer.Option("--split-manifest", exists=True, dir_okay=False),
    ] = Path("benchmarks/split_manifest.yaml"),
    numerical_charter_path: Annotated[
        Path,
        typer.Option("--numerical-charter", exists=True, dir_okay=False),
    ] = Path("benchmarks/numerical_charter.yaml"),
    graph_profile_path: Annotated[
        Path,
        typer.Option("--graph-profile", exists=True, dir_okay=False),
    ] = Path("profiles/graph_import_v1.yaml"),
    matching_profile_path: Annotated[
        Path,
        typer.Option("--matching-profile", exists=True, dir_okay=False),
    ] = Path("profiles/map_matching_v1.yaml"),
    decoder_profile_path: Annotated[
        Path,
        typer.Option("--decoder-profile", exists=True, dir_okay=False),
    ] = Path("profiles/map_decoder_v1.yaml"),
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Ignored destination for the blind derived-data review packet.",
        ),
    ] = Path("benchmark-results/m5_6_public_route_review_packet.json"),
) -> None:
    """Prepare the blind public-route packet without running the decoder."""

    try:
        packet = prepare_public_route_review(
            public_data_root=public_data_root,
            gate_path=gate_path,
            protocol_path=protocol_path,
            data_manifest_path=data_manifest_path,
            source_groups_path=source_groups_path,
            split_manifest_path=split_manifest_path,
            numerical_charter_path=numerical_charter_path,
            graph_profile_path=graph_profile_path,
            matching_profile_path=matching_profile_path,
            decoder_profile_path=decoder_profile_path,
        )
        _write_report(packet, output)
        _write_report(
            {
                "accepted": True,
                "output_object_key": output.name,
                "packet_immutable_sha256": packet["packet_immutable_sha256"],
                "moving_review_observation_count": packet[
                    "moving_review_observation_count"
                ],
                "production_decoder_output_included": False,
                "final_test_material_included": False,
            },
            None,
        )
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Public road-review preparation failed: {error}", err=True)
        raise typer.Exit(code=2) from error


@app.command("qualify-public-road-matching")
def qualify_public_road_matching_command(
    public_data_root: Annotated[
        Path,
        typer.Option(
            "--public-data-root",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ] = Path("data/public"),
    adjudication_path: Annotated[
        Path,
        typer.Option(
            "--adjudication",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Completed frozen blind public-route decisions.",
        ),
    ] = Path("benchmarks/m5_6_public_route_adjudication.yaml"),
    gate_path: Annotated[
        Path,
        typer.Option("--gate", exists=True, dir_okay=False),
    ] = Path("benchmarks/m5_6_public_road_matching_gate.yaml"),
    protocol_path: Annotated[
        Path,
        typer.Option("--protocol", exists=True, dir_okay=False),
    ] = Path("docs/public_route_adjudication.md"),
    data_manifest_path: Annotated[
        Path,
        typer.Option("--data-manifest", exists=True, dir_okay=False),
    ] = Path("benchmarks/data_manifest.yaml"),
    source_groups_path: Annotated[
        Path,
        typer.Option("--source-groups", exists=True, dir_okay=False),
    ] = Path("benchmarks/source_groups.yaml"),
    split_manifest_path: Annotated[
        Path,
        typer.Option("--split-manifest", exists=True, dir_okay=False),
    ] = Path("benchmarks/split_manifest.yaml"),
    numerical_charter_path: Annotated[
        Path,
        typer.Option("--numerical-charter", exists=True, dir_okay=False),
    ] = Path("benchmarks/numerical_charter.yaml"),
    graph_profile_path: Annotated[
        Path,
        typer.Option("--graph-profile", exists=True, dir_okay=False),
    ] = Path("profiles/graph_import_v1.yaml"),
    matching_profile_path: Annotated[
        Path,
        typer.Option("--matching-profile", exists=True, dir_okay=False),
    ] = Path("profiles/map_matching_v1.yaml"),
    decoder_profile_path: Annotated[
        Path,
        typer.Option("--decoder-profile", exists=True, dir_okay=False),
    ] = Path("profiles/map_decoder_v1.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the deterministic public-route qualification atomically.",
        ),
    ] = None,
) -> None:
    """Qualify production matching against frozen blind route decisions."""

    try:
        report = qualify_public_road_matching(
            public_data_root=public_data_root,
            gate_path=gate_path,
            adjudication_path=adjudication_path,
            protocol_path=protocol_path,
            data_manifest_path=data_manifest_path,
            source_groups_path=source_groups_path,
            split_manifest_path=split_manifest_path,
            numerical_charter_path=numerical_charter_path,
            graph_profile_path=graph_profile_path,
            matching_profile_path=matching_profile_path,
            decoder_profile_path=decoder_profile_path,
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Public road-matching qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("qualify-road-bins")
def qualify_road_bins_command(
    graph_profile_path: Annotated[
        Path,
        typer.Option(
            "--graph-profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen directed-road graph import profile.",
        ),
    ] = Path("profiles/graph_import_v1.yaml"),
    matching_profile_path: Annotated[
        Path,
        typer.Option(
            "--matching-profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen road-candidate and transition profile.",
        ),
    ] = Path("profiles/map_matching_v1.yaml"),
    decoder_profile_path: Annotated[
        Path,
        typer.Option(
            "--decoder-profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen offline map-decoder profile.",
        ),
    ] = Path("profiles/map_decoder_v1.yaml"),
    binning_profile_path: Annotated[
        Path,
        typer.Option(
            "--binning-profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen directed-road bin aggregation profile.",
        ),
    ] = Path("profiles/road_binning_v1.yaml"),
    gate_path: Annotated[
        Path,
        typer.Option(
            "--gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M5.4 directed-road bin acceptance gate.",
        ),
    ] = Path("benchmarks/m5_4_road_bins_gate.yaml"),
    numerical_charter_path: Annotated[
        Path,
        typer.Option(
            "--numerical-charter",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen numerical acceptance authority.",
        ),
    ] = Path("benchmarks/numerical_charter.yaml"),
    fixture_path: Annotated[
        Path,
        typer.Option(
            "--fixture",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen independent road-topology fixture.",
        ),
    ] = Path("tests/fixtures/road_graphs/topology_v1.osm"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the deterministic qualification report atomically.",
        ),
    ] = None,
) -> None:
    """Qualify directed bins, pass identity, evidence joins, and localization."""

    try:
        report = qualify_directed_road_bins(
            graph_profile_path=graph_profile_path,
            matching_profile_path=matching_profile_path,
            decoder_profile_path=decoder_profile_path,
            binning_profile_path=binning_profile_path,
            gate_path=gate_path,
            numerical_charter_path=numerical_charter_path,
            fixture_path=fixture_path,
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Road-bin qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("qualify-topology-hypotheses")
def qualify_topology_hypotheses_command(
    profile_path: Annotated[
        Path,
        typer.Option(
            "--profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen repeated-trajectory topology-hypothesis profile.",
        ),
    ] = Path("profiles/topology_hypotheses_v1.yaml"),
    gate_path: Annotated[
        Path,
        typer.Option(
            "--gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M5.5 supported synthetic acceptance gate.",
        ),
    ] = Path("benchmarks/m5_5_topology_hypotheses_gate.yaml"),
    numerical_charter_path: Annotated[
        Path,
        typer.Option(
            "--numerical-charter",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen numerical acceptance authority.",
        ),
    ] = Path("benchmarks/numerical_charter.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the deterministic qualification report atomically.",
        ),
    ] = None,
) -> None:
    """Qualify review-only repeated-trajectory topology hypotheses."""

    try:
        report = qualify_topology_hypotheses(
            profile_path=profile_path,
            gate_path=gate_path,
            numerical_charter_path=numerical_charter_path,
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Topology-hypothesis qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("score-road-candidates")
def score_road_candidates_command(
    graph_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Portable directed road-graph JSON.",
        ),
    ],
    x_m: Annotated[float, typer.Option("--x-m", help="Graph-local east position.")],
    y_m: Annotated[float, typer.Option("--y-m", help="Graph-local north position.")],
    time_seconds: Annotated[
        str,
        typer.Option(
            "--time-seconds",
            help="Plain decimal Unix UTC observation time.",
        ),
    ],
    speed_mps: Annotated[
        float,
        typer.Option("--speed-mps", min=0.0, help="Observed horizontal speed."),
    ],
    heading_rad: Annotated[
        float | None,
        typer.Option(
            "--heading-rad",
            min=-3.141592653589793,
            max=3.141592653589793,
            help="Optional graph-local heading in radians.",
        ),
    ] = None,
    uncertainty_m: Annotated[
        float | None,
        typer.Option(
            "--uncertainty-m",
            min=0.000000001,
            help="Optional trustworthy horizontal one-sigma uncertainty.",
        ),
    ] = None,
    source_observation_id: Annotated[
        str | None,
        typer.Option(
            "--source-observation-id",
            help="Optional upstream normalized-observation identity.",
        ),
    ] = None,
    profile_path: Annotated[
        Path,
        typer.Option(
            "--profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen map-matching parameter charter.",
        ),
    ] = Path("profiles/map_matching_v1.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write deterministic candidate evidence atomically.",
        ),
    ] = None,
) -> None:
    """Project one local observation onto directed roads and the off-map state."""

    try:
        graph = DirectedRoadGraph.model_validate_json(_read_artifact(graph_path))
        validate_graph_identity(graph)
        profile, profile_file_sha256 = load_map_matching_profile(profile_path)
        validate_matching_graph_authority(graph, profile)
        time = TimePoint.from_decimal_seconds(
            time_seconds,
            source_key="cli/road-observation",
            field="time_seconds",
            epoch=TimeEpoch.UNIX_UTC,
            clock_id="cli-unix-utc",
            reference=TimeReference.SAMPLE,
        )
        observation = make_road_match_observation(
            time=time,
            local_frame_id=graph.local_frame.frame.frame_id,
            position_local_m=(x_m, y_m),
            heading_rad=heading_rad,
            speed_mps=speed_mps,
            horizontal_uncertainty_m=uncertainty_m,
            horizontal_uncertainty_basis=(
                "DECLARED_TRUSTWORTHY" if uncertainty_m is not None else None
            ),
            source_observation_id=source_observation_id,
        )
        candidates = generate_road_candidates(
            graph,
            observation,
            profile=profile,
        )
        best = best_emission_candidate(candidates)
        _write_report(
            {
                "schema_version": "cartosentry.road-candidate-report.v1",
                "algorithm_backend": ALGORITHM_BACKEND,
                "graph_id": graph.graph_id,
                "profile_file_sha256": profile_file_sha256,
                "profile_immutable_sha256": profile.immutable_sha256,
                "observation": observation.model_dump(mode="json"),
                "candidate_count": len(candidates),
                "candidates": [item.model_dump(mode="json") for item in candidates],
                "best_emission_candidate_id": best.candidate_id,
                "best_emission_state": best.state.value,
            },
            output,
        )
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Road-candidate scoring failed: {error}", err=True)
        raise typer.Exit(code=2) from error


def _select_transition_candidate(
    candidates: tuple[RoadCandidate, ...], selector: str | None
) -> RoadCandidate:
    if selector is None:
        return best_emission_candidate(candidates)
    if selector == CandidateState.OFF_MAP:
        return next(item for item in candidates if item.state == CandidateState.OFF_MAP)
    try:
        return next(item for item in candidates if item.directed_arc_id == selector)
    except StopIteration as error:
        raise ValueError(
            "requested directed arc is absent from the bounded candidate set"
        ) from error


@app.command("score-road-transition")
def score_road_transition_command(
    graph_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Portable directed road-graph JSON.",
        ),
    ],
    from_x_m: Annotated[float, typer.Option("--from-x-m")],
    from_y_m: Annotated[float, typer.Option("--from-y-m")],
    from_time_seconds: Annotated[str, typer.Option("--from-time-seconds")],
    from_speed_mps: Annotated[float, typer.Option("--from-speed-mps", min=0.0)],
    to_x_m: Annotated[float, typer.Option("--to-x-m")],
    to_y_m: Annotated[float, typer.Option("--to-y-m")],
    to_time_seconds: Annotated[str, typer.Option("--to-time-seconds")],
    to_speed_mps: Annotated[float, typer.Option("--to-speed-mps", min=0.0)],
    from_heading_rad: Annotated[
        float | None,
        typer.Option(
            "--from-heading-rad",
            min=-3.141592653589793,
            max=3.141592653589793,
        ),
    ] = None,
    to_heading_rad: Annotated[
        float | None,
        typer.Option(
            "--to-heading-rad",
            min=-3.141592653589793,
            max=3.141592653589793,
        ),
    ] = None,
    from_uncertainty_m: Annotated[
        float | None, typer.Option("--from-uncertainty-m", min=0.000000001)
    ] = None,
    to_uncertainty_m: Annotated[
        float | None, typer.Option("--to-uncertainty-m", min=0.000000001)
    ] = None,
    from_source_observation_id: Annotated[
        str | None, typer.Option("--from-source-observation-id")
    ] = None,
    to_source_observation_id: Annotated[
        str | None, typer.Option("--to-source-observation-id")
    ] = None,
    from_candidate: Annotated[
        str | None,
        typer.Option(
            "--from-candidate",
            help="Directed arc ID or OFF_MAP; defaults to the best emission.",
        ),
    ] = None,
    to_candidate: Annotated[
        str | None,
        typer.Option(
            "--to-candidate",
            help="Directed arc ID or OFF_MAP; defaults to the best emission.",
        ),
    ] = None,
    profile_path: Annotated[
        Path,
        typer.Option(
            "--profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen map-matching parameter charter.",
        ),
    ] = Path("profiles/map_matching_v1.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write deterministic transition evidence atomically.",
        ),
    ] = None,
) -> None:
    """Score one explicit transition between bounded road candidates."""

    try:
        graph = DirectedRoadGraph.model_validate_json(_read_artifact(graph_path))
        validate_graph_identity(graph)
        profile, profile_file_sha256 = load_map_matching_profile(profile_path)
        validate_matching_graph_authority(graph, profile)
        frame_id = graph.local_frame.frame.frame_id
        previous_observation = make_road_match_observation(
            time=TimePoint.from_decimal_seconds(
                from_time_seconds,
                source_key="cli/road-transition/from",
                field="time_seconds",
                epoch=TimeEpoch.UNIX_UTC,
                clock_id="cli-unix-utc",
                reference=TimeReference.SAMPLE,
            ),
            local_frame_id=frame_id,
            position_local_m=(from_x_m, from_y_m),
            heading_rad=from_heading_rad,
            speed_mps=from_speed_mps,
            horizontal_uncertainty_m=from_uncertainty_m,
            horizontal_uncertainty_basis=(
                "DECLARED_TRUSTWORTHY" if from_uncertainty_m is not None else None
            ),
            source_observation_id=from_source_observation_id,
        )
        current_observation = make_road_match_observation(
            time=TimePoint.from_decimal_seconds(
                to_time_seconds,
                source_key="cli/road-transition/to",
                field="time_seconds",
                epoch=TimeEpoch.UNIX_UTC,
                clock_id="cli-unix-utc",
                reference=TimeReference.SAMPLE,
            ),
            local_frame_id=frame_id,
            position_local_m=(to_x_m, to_y_m),
            heading_rad=to_heading_rad,
            speed_mps=to_speed_mps,
            horizontal_uncertainty_m=to_uncertainty_m,
            horizontal_uncertainty_basis=(
                "DECLARED_TRUSTWORTHY" if to_uncertainty_m is not None else None
            ),
            source_observation_id=to_source_observation_id,
        )
        previous = _select_transition_candidate(
            generate_road_candidates(graph, previous_observation, profile=profile),
            from_candidate,
        )
        current = _select_transition_candidate(
            generate_road_candidates(graph, current_observation, profile=profile),
            to_candidate,
        )
        transition = score_road_transition(
            graph,
            previous_observation,
            previous,
            current_observation,
            current,
            profile=profile,
        )
        _write_report(
            {
                "schema_version": "cartosentry.road-transition-report.v1",
                "algorithm_backend": ALGORITHM_BACKEND,
                "graph_id": graph.graph_id,
                "profile_file_sha256": profile_file_sha256,
                "profile_immutable_sha256": profile.immutable_sha256,
                "previous_observation": previous_observation.model_dump(mode="json"),
                "previous_candidate": previous.model_dump(mode="json"),
                "current_observation": current_observation.model_dump(mode="json"),
                "current_candidate": current.model_dump(mode="json"),
                "transition": transition.model_dump(mode="json"),
                "runtime_score_is_negative_infinity": (
                    not transition.possible and transition.score == float("-inf")
                ),
            },
            output,
        )
    except (OSError, StopIteration, ValueError, ValidationError) as error:
        typer.echo(f"Road-transition scoring failed: {error}", err=True)
        raise typer.Exit(code=2) from error


def _artifact_identifier(value: dict[str, object]) -> str:
    for key in (
        "sequence_id",
        "run_id",
        "finding_id",
        "profile_id",
        "recapture_plan_id",
        "bundle_id",
    ):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    raise ValueError("artifact has no public identifier")


@app.command("validate-artifact")
def validate_artifact_command(
    artifact_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Versioned CartoSentry JSON artifact.",
        ),
    ],
) -> None:
    """Validate and cross-language round-trip a portable artifact."""

    try:
        artifact = validate_artifact_json(_read_artifact(artifact_path))
        canonical = canonicalize_portable_artifact(artifact)
        portable = artifact.portable_dict()
        _write_report(
            {
                "accepted": True,
                "artifact_id": _artifact_identifier(portable),
                "canonical_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                "schema_version": artifact.schema_name,
            },
            None,
        )
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Artifact validation failed: {error}", err=True)
        raise typer.Exit(code=2) from error


@app.command("export-portable-artifact")
def export_portable_artifact_command(
    artifact_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Local or already portable CartoSentry JSON artifact.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Argument(
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Destination for deterministic portable JSON.",
        ),
    ],
) -> None:
    """Strip local run context and atomically export portable JSON."""

    try:
        if artifact_path == output:
            raise ValueError("portable export destination must differ from its input")
        artifact = validate_artifact_json(_read_artifact(artifact_path))
        canonical = canonicalize_portable_artifact(artifact)
        _write_text_atomically(canonical + "\n", output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Portable artifact export failed: {error}", err=True)
        raise typer.Exit(code=2) from error


@app.command("inspect-boreas")
def inspect_boreas_command(
    sequence_root: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Root of one downloaded Boreas sequence.",
        ),
    ],
    route_html: Annotated[
        Path | None,
        typer.Option(
            "--route-html",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Official sequence route.html; defaults to SEQUENCE_ROOT/route.html.",
        ),
    ] = None,
    road_region: Annotated[
        Path,
        typer.Option(
            "--road-region",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen EPSG:4326 road-region polygon.",
        ),
    ] = Path("benchmarks/road_graphs/toronto_glen_shields_v1.polygon.json"),
    gate: Annotated[
        Path,
        typer.Option(
            "--gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen Boreas adapter acceptance gate.",
        ),
    ] = Path("benchmarks/m0_4_adapter_gate.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the JSON report atomically instead of printing it.",
        ),
    ] = None,
) -> None:
    """Inspect actual Boreas trajectory, calibration, and lidar contracts."""

    selected_route = route_html or sequence_root / "route.html"
    try:
        report = inspect_boreas(
            sequence_root,
            route_html=selected_route,
            road_region_path=road_region,
            gate_path=gate,
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Boreas inspection failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("qualify-boreas-adapter")
def qualify_boreas_adapter_command(
    sequence_root: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Root of one locally materialized Boreas sequence.",
        ),
    ],
    split_manifest: Annotated[
        Path,
        typer.Option(
            "--split-manifest",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen source-group assignment.",
        ),
    ] = Path("benchmarks/split_manifest.yaml"),
    maximum_lidar_frames: Annotated[
        int | None,
        typer.Option(
            "--maximum-lidar-frames",
            min=1,
            help="Optional bounded smoke subset; defaults to every local frame.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the deterministic qualification report atomically.",
        ),
    ] = None,
) -> None:
    """Exercise the production Boreas adapter through sequential views."""

    try:
        source_group_id = source_group_for_sequence(sequence_root.name, split_manifest)
        report = qualify_boreas_adapter(
            BoreasAdapter(sequence_root, source_group_id=source_group_id),
            maximum_lidar_frames=maximum_lidar_frames,
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Boreas adapter qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("index-boreas")
def index_boreas_command(
    sequence_root: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Root of one locally materialized Boreas sequence.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Argument(
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="New directory for the atomically published manifest and index.",
        ),
    ],
    split_manifest: Annotated[
        Path,
        typer.Option(
            "--split-manifest",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen source-group assignment.",
        ),
    ] = Path("benchmarks/split_manifest.yaml"),
    budget_path: Annotated[
        Path,
        typer.Option(
            "--budget",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen ingestion memory and safety budget.",
        ),
    ] = Path("benchmarks/ingestion_budget.yaml"),
) -> None:
    """Hash, inspect, and atomically index an immutable Boreas recording."""

    try:
        source_group_id = source_group_for_sequence(sequence_root.name, split_manifest)
        budget, budget_sha256 = load_ingestion_budget(budget_path)
        report = index_boreas_recording(
            sequence_root,
            output,
            source_group_id=source_group_id,
            budget=budget,
            budget_sha256=budget_sha256,
        )
        _write_report(report.portable_dict(), None)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Boreas indexing failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report.accepted:
        raise typer.Exit(code=1)


@app.command("qualify-scheduler")
def qualify_scheduler_command(
    output_root: Annotated[
        Path,
        typer.Argument(
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="New output directory for cancellation safety evidence.",
        ),
    ],
    suite_path: Annotated[
        Path,
        typer.Option(
            "--suite",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen mixed-modality scheduler stress suite.",
        ),
    ] = Path("benchmarks/scheduler_stress.yaml"),
) -> None:
    """Stress the native byte-budgeted scheduler and cancellation path."""

    try:
        report = qualify_scheduler(output_root, suite_path)
        _write_report(report.portable_dict(), None)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Scheduler qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report.accepted:
        raise typer.Exit(code=1)


@app.command("resume-run")
def resume_run_command(
    run_root: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Existing CartoSentry run directory.",
        ),
    ],
    force_stage: Annotated[
        str | None,
        typer.Option(
            "--force-stage",
            help="Invalidate and execute only this stage and its dependency closure.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show the force-stage scope without changing the run.",
        ),
    ] = False,
) -> None:
    """Reconcile hashes and resume a registered persisted workflow."""

    try:
        if dry_run and force_stage is None:
            raise ValueError("--dry-run requires --force-stage")
        if force_stage is not None and not dry_run:
            preview = resume_registered_run(
                run_root,
                force_stage=force_stage,
                dry_run=True,
            )
            typer.echo(
                "Force-stage scope: " + ", ".join(preview.forced_scope),
                err=True,
            )
        report = resume_registered_run(
            run_root,
            force_stage=force_stage,
            dry_run=dry_run,
        )
        _write_report(report.portable_dict(), None)
    except (OSError, RuntimeError, ValueError, ValidationError) as error:
        typer.echo(f"Run resume failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not dry_run and not report.complete:
        raise typer.Exit(code=1)


@app.command("qualify-run-recovery")
def qualify_run_recovery_command(
    output_root: Annotated[
        Path,
        typer.Argument(
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="New output directory for interruption and recovery evidence.",
        ),
    ],
) -> None:
    """Kill stages at every commit boundary and verify exact resume semantics."""

    try:
        report = qualify_run_recovery(output_root)
        _write_report(report.portable_dict(), None)
    except (OSError, RuntimeError, ValueError, ValidationError) as error:
        typer.echo(f"Run recovery qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report.accepted:
        raise typer.Exit(code=1)


@app.command("qualify-observability")
def qualify_observability_command(
    sequence_root: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Root of the pinned Boreas development sequence.",
        ),
    ],
    road_graph: Annotated[
        Path,
        typer.Option(
            "--road-graph",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Pinned OSM XML road-graph extract.",
        ),
    ] = Path("data/public/road_graphs/toronto-glen-shields-v1.osm"),
    gate: Annotated[
        Path,
        typer.Option(
            "--gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M0.5 observability acceptance gate.",
        ),
    ] = Path("benchmarks/m0_5_observability_gate.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the JSON report atomically instead of printing it.",
        ),
    ] = None,
) -> None:
    """Qualify motion compensation, map candidates, and tiny routing."""

    try:
        report = qualify_observability(
            sequence_root,
            road_graph_path=road_graph,
            gate_path=gate,
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Observability qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("qualify-contracts")
def qualify_contracts_command(
    charter: Annotated[
        Path,
        typer.Option(
            "--charter",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen numerical acceptance charter.",
        ),
    ] = Path("benchmarks/numerical_charter.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the JSON report atomically instead of printing it.",
        ),
    ] = None,
) -> None:
    """Qualify canonical time, frame, transform, and coordinate contracts."""

    try:
        report = qualify_contracts(charter)
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Contract qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("generate-synthetic-fixtures")
def generate_synthetic_fixtures_command(
    output_root: Annotated[
        Path,
        typer.Argument(
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Destination directory for the deterministic fixture set.",
        ),
    ],
    split_manifest: Annotated[
        Path,
        typer.Option(
            "--split-manifest",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen source-group and synthetic-family assignment.",
        ),
    ] = Path("benchmarks/split_manifest.yaml"),
) -> None:
    """Generate the compact V1 analytic trajectory and lidar fixtures."""

    try:
        report = materialize_fixture_set(output_root, split_manifest)
        _write_report(report, None)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Synthetic fixture generation failed: {error}", err=True)
        raise typer.Exit(code=2) from error


@app.command("qualify-synthetic-fixtures")
def qualify_synthetic_fixtures_command(
    fixture_root: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Root containing a generated synthetic fixture set.",
        ),
    ],
    split_manifest: Annotated[
        Path,
        typer.Option(
            "--split-manifest",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen source-group and synthetic-family assignment.",
        ),
    ] = Path("benchmarks/split_manifest.yaml"),
    charter: Annotated[
        Path,
        typer.Option(
            "--charter",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen numerical acceptance charter.",
        ),
    ] = Path("benchmarks/numerical_charter.yaml"),
) -> None:
    """Qualify synthetic bytes, analytic geometry, and exact point time."""

    try:
        report = qualify_fixture_set(fixture_root, split_manifest, charter)
        _write_report(report, None)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Synthetic fixture qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("qualify-reference-trajectory")
def qualify_reference_trajectory_command(
    gate: Annotated[
        Path,
        typer.Option(
            "--gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen deterministic M3.1 trajectory acceptance gate.",
        ),
    ] = Path("benchmarks/m3_1_trajectory_gate.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the deterministic qualification report atomically.",
        ),
    ] = None,
) -> None:
    """Qualify interpolation, derivatives, stationarity, and gap handling."""

    try:
        report = qualify_reference_trajectory(gate)
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Reference trajectory qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("qualify-trajectory-integrity")
def qualify_trajectory_integrity_command(
    profile: Annotated[
        Path,
        typer.Option(
            "--profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M3.2 detector profile.",
        ),
    ] = Path("profiles/trajectory_integrity_v1.yaml"),
    trajectory_gate: Annotated[
        Path,
        typer.Option(
            "--trajectory-gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Pinned M3.1 continuous-trajectory gate.",
        ),
    ] = Path("benchmarks/m3_1_trajectory_gate.yaml"),
    split_manifest: Annotated[
        Path,
        typer.Option(
            "--split-manifest",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen source-group and partition assignment.",
        ),
    ] = Path("benchmarks/split_manifest.yaml"),
    fault_matrix: Annotated[
        Path,
        typer.Option(
            "--fault-matrix",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen cartosentry-v1-core fault matrix.",
        ),
    ] = Path("benchmarks/fault_matrix_v1.yaml"),
    charter: Annotated[
        Path,
        typer.Option(
            "--charter",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen numerical acceptance charter.",
        ),
    ] = Path("benchmarks/numerical_charter.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the machine-readable qualification report atomically.",
        ),
    ] = None,
) -> None:
    """Qualify M3.2 trajectory faults on exact development and calibration groups."""

    try:
        report = qualify_trajectory_integrity(
            profile_path=profile,
            trajectory_gate_path=trajectory_gate,
            split_manifest_path=split_manifest,
            fault_matrix_path=fault_matrix,
            charter_path=charter,
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Trajectory integrity qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("qualify-lidar-integrity")
def qualify_lidar_integrity_command(
    public_data_root: Annotated[
        Path,
        typer.Option(
            "--public-data-root",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Manifest-verified public development data root.",
        ),
    ] = Path("data/public"),
    gate: Annotated[
        Path,
        typer.Option(
            "--gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M4.1 LiDAR qualification gate.",
        ),
    ] = Path("benchmarks/m4_1_lidar_gate.yaml"),
    profile: Annotated[
        Path,
        typer.Option(
            "--profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen LiDAR integrity profile.",
        ),
    ] = Path("profiles/lidar_integrity_v1.yaml"),
    split_manifest: Annotated[
        Path,
        typer.Option(
            "--split-manifest",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen source-group and partition assignment.",
        ),
    ] = Path("benchmarks/split_manifest.yaml"),
    charter: Annotated[
        Path,
        typer.Option(
            "--charter",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen numerical acceptance charter.",
        ),
    ] = Path("benchmarks/numerical_charter.yaml"),
    fault_matrix: Annotated[
        Path,
        typer.Option(
            "--fault-matrix",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen representative V1 fault matrix authority.",
        ),
    ] = Path("benchmarks/fault_matrix_v1.yaml"),
    data_manifest: Annotated[
        Path,
        typer.Option(
            "--data-manifest",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Immutable public-data object manifest.",
        ),
    ] = Path("benchmarks/data_manifest.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the machine-readable qualification report atomically.",
        ),
    ] = None,
) -> None:
    """Qualify streaming LiDAR integrity on frozen synthetic and public inputs."""

    try:
        report = qualify_lidar_integrity(
            gate_path=gate,
            profile_path=profile,
            split_manifest_path=split_manifest,
            numerical_charter_path=charter,
            representative_fault_matrix_path=fault_matrix,
            data_manifest_path=data_manifest,
            public_data_root=public_data_root,
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"LiDAR integrity qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("qualify-lidar-alignment")
def qualify_lidar_alignment_command(
    gate: Annotated[
        Path,
        typer.Option(
            "--gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M4.2 motion-alignment qualification gate.",
        ),
    ] = Path("benchmarks/m4_2_alignment_gate.yaml"),
    profile: Annotated[
        Path,
        typer.Option(
            "--profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen motion-alignment profile.",
        ),
    ] = Path("profiles/lidar_alignment_v1.yaml"),
    trajectory_gate: Annotated[
        Path,
        typer.Option(
            "--trajectory-gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M3.1 continuous-trajectory gate.",
        ),
    ] = Path("benchmarks/m3_1_trajectory_gate.yaml"),
    lidar_integrity_gate: Annotated[
        Path,
        typer.Option(
            "--lidar-integrity-gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M4.1 LiDAR integrity gate.",
        ),
    ] = Path("benchmarks/m4_1_lidar_gate.yaml"),
    split_manifest: Annotated[
        Path,
        typer.Option(
            "--split-manifest",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen source-group and partition assignment.",
        ),
    ] = Path("benchmarks/split_manifest.yaml"),
    charter: Annotated[
        Path,
        typer.Option(
            "--charter",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen numerical acceptance charter.",
        ),
    ] = Path("benchmarks/numerical_charter.yaml"),
    fault_matrix: Annotated[
        Path,
        typer.Option(
            "--fault-matrix",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen representative V1 fault matrix authority.",
        ),
    ] = Path("benchmarks/fault_matrix_v1.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the machine-readable qualification report atomically.",
        ),
    ] = None,
) -> None:
    """Qualify per-point-time motion-compensated LiDAR alignment."""

    try:
        report = qualify_motion_alignment(
            gate_path=gate,
            profile_path=profile,
            trajectory_gate_path=trajectory_gate,
            lidar_integrity_gate_path=lidar_integrity_gate,
            split_manifest_path=split_manifest,
            numerical_charter_path=charter,
            representative_fault_matrix_path=fault_matrix,
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"LiDAR alignment qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("qualify-temporal-checkpoint")
def qualify_temporal_checkpoint_command(
    public_data_root: Annotated[
        Path,
        typer.Option(
            "--public-data-root",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Manifest-verified public development data root.",
        ),
    ] = Path("data/public"),
    checkpoint: Annotated[
        Path,
        typer.Option(
            "--checkpoint",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M3.5 checkpoint contract.",
        ),
    ] = Path("benchmarks/m3_5_temporal_checkpoint.yaml"),
    profile: Annotated[
        Path,
        typer.Option(
            "--profile",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M3.2 detector profile.",
        ),
    ] = Path("profiles/trajectory_integrity_v1.yaml"),
    trajectory_gate: Annotated[
        Path,
        typer.Option(
            "--trajectory-gate",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen M3.1 continuous-trajectory gate.",
        ),
    ] = Path("benchmarks/m3_1_trajectory_gate.yaml"),
    split_manifest: Annotated[
        Path,
        typer.Option(
            "--split-manifest",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen source-group and partition assignment.",
        ),
    ] = Path("benchmarks/split_manifest.yaml"),
    fault_matrix: Annotated[
        Path,
        typer.Option(
            "--fault-matrix",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen cartosentry-v1-core fault matrix.",
        ),
    ] = Path("benchmarks/fault_matrix_v1.yaml"),
    numerical_charter: Annotated[
        Path,
        typer.Option(
            "--numerical-charter",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen numerical acceptance charter.",
        ),
    ] = Path("benchmarks/numerical_charter.yaml"),
    charter_revisions: Annotated[
        Path,
        typer.Option(
            "--charter-revisions",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen aggregate charter revision history.",
        ),
    ] = Path("benchmarks/charter_revisions.yaml"),
    data_manifest: Annotated[
        Path,
        typer.Option(
            "--data-manifest",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Manifest-bound public data authority.",
        ),
    ] = Path("benchmarks/data_manifest.yaml"),
    source_groups: Annotated[
        Path,
        typer.Option(
            "--source-groups",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen source-group assignment authority.",
        ),
    ] = Path("benchmarks/source_groups.yaml"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Write the complete machine-readable checkpoint atomically.",
        ),
    ] = None,
) -> None:
    """Run all frozen M3 gates and review real public development clips."""

    try:
        report = qualify_temporal_checkpoint(
            checkpoint_path=checkpoint,
            public_data_root=public_data_root,
            profile_path=profile,
            trajectory_gate_path=trajectory_gate,
            split_manifest_path=split_manifest,
            fault_matrix_path=fault_matrix,
            numerical_charter_path=numerical_charter,
            charter_revisions_path=charter_revisions,
            data_manifest_path=data_manifest,
            source_groups_path=source_groups,
        )
        _write_report(report, output)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Temporal checkpoint qualification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)


@app.command("inject-synthetic-fault")
def inject_synthetic_fault_command(
    source_fixture: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Canonical clean synthetic fixture JSON.",
        ),
    ],
    output_root: Annotated[
        Path,
        typer.Argument(
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="New directory for derivative.json and manifest.json.",
        ),
    ],
    operator: Annotated[
        str,
        typer.Option("--operator", help="Exact cartosentry-v1-core operator ID."),
    ],
    case: Annotated[
        str,
        typer.Option("--case", help="Exact case ID from the frozen fault matrix."),
    ],
    seed: Annotated[
        int,
        typer.Option("--seed", min=0, help="Deterministic target-selection seed."),
    ],
    clean_source_truth: Annotated[
        Path,
        typer.Option(
            "--clean-source-truth",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Immutable clean-source truth artifact frozen before injection.",
        ),
    ],
    fault_matrix: Annotated[
        Path,
        typer.Option(
            "--fault-matrix",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen V1 operator registry and cases.",
        ),
    ] = Path("benchmarks/fault_matrix_v1.yaml"),
) -> None:
    """Create one deterministic fault derivative with immutable provenance."""

    try:
        registry = load_fault_registry(fault_matrix)
        request = FaultRequest(
            operator_id=FaultOperatorId(operator),
            case_id=case,
            seed=seed,
            clean_source_truth_sha256=hashlib.sha256(
                _read_artifact(clean_source_truth).encode()
            ).hexdigest(),
        )
        result = inject_fault(
            _read_artifact(source_fixture).encode(), request, registry
        )
        materialize_fault_result(output_root, result)
        _write_report(
            {
                "accepted": True,
                "attributed_change_count": len(result.manifest.changed_values),
                "fault_id": result.manifest.fault_id,
                "operator_id": result.manifest.operator_id.value,
                "output_root": output_root.as_posix(),
            },
            None,
        )
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Synthetic fault injection failed: {error}", err=True)
        raise typer.Exit(code=2) from error


@app.command("verify-synthetic-fault")
def verify_synthetic_fault_command(
    source_fixture: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Canonical clean synthetic fixture JSON.",
        ),
    ],
    derivative_root: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Fault output directory to verify independently.",
        ),
    ],
    fault_matrix: Annotated[
        Path,
        typer.Option(
            "--fault-matrix",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Frozen V1 operator registry and cases.",
        ),
    ] = Path("benchmarks/fault_matrix_v1.yaml"),
) -> None:
    """Reapply and verify a fault derivative and its attribution manifest."""

    try:
        report = verify_fault_result(
            _read_artifact(source_fixture).encode(),
            _read_artifact(derivative_root / "derivative.json").encode(),
            _read_artifact(derivative_root / "manifest.json").encode(),
            load_fault_registry(fault_matrix),
        )
        _write_report(report, None)
    except (OSError, ValueError, ValidationError) as error:
        typer.echo(f"Synthetic fault verification failed: {error}", err=True)
        raise typer.Exit(code=2) from error
    if not report["accepted"]:
        raise typer.Exit(code=1)
