"""CartoSentry public command-line interface."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from cartosentry.adapters import inspect_boreas

app = typer.Typer(
    name="cartosentry",
    help="Evidence-backed mapping-readiness analysis and recollection planning.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Run one of CartoSentry's checked public workflows."""


def _write_report(report: dict[str, object], output: Path | None) -> None:
    serialized = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if output is None:
        typer.echo(serialized, nl=False)
        return
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
