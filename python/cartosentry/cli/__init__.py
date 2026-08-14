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
from cartosentry.faults import (
    FaultOperatorId,
    FaultRequest,
    inject_fault,
    load_fault_registry,
    materialize_fault_result,
    verify_fault_result,
)
from cartosentry.qualification import qualify_contracts
from cartosentry.spikes import qualify_observability
from cartosentry.synthetic import materialize_fixture_set, qualify_fixture_set

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
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("artifact exceeds the 16 MiB validation limit")
    return path.read_text(encoding="utf-8")


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
