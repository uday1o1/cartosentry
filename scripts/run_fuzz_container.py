#!/usr/bin/env python3
"""Run a fuzz image while preserving evidence from passing and failing containers."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def _source_revision() -> str:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return f"{commit}-working-tree" if status else commit


def run_container(
    *, image: str, suite: str, output_root: Path, source_revision: str
) -> int:
    if output_root.exists():
        raise ValueError("fuzz evidence output must not already exist")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    image_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise RuntimeError("Docker did not return an immutable image identity")
    created = subprocess.run(
        [
            "docker",
            "create",
            "--platform",
            "linux/amd64",
            "--env",
            f"CARTOSENTRY_SOURCE_REVISION={source_revision}",
            "--env",
            f"CARTOSENTRY_CONTAINER_IMAGE_ID={image_id}",
            image_id,
            "--suite",
            suite,
            "--build-dir",
            "build/fuzz",
            "--output-root",
            "/fuzz-evidence",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    container_id = created.stdout.strip()
    if not container_id:
        raise RuntimeError("Docker did not return a fuzz container identity")
    try:
        created_image_id = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", container_id],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if created_image_id != image_id:
            raise RuntimeError("created fuzz container image identity changed")
        subprocess.run(["docker", "start", "--attach", container_id], check=False)
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.ExitCode}}",
                container_id,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        container_status = int(inspected.stdout.strip())
        output_root.mkdir()
        copied = subprocess.run(
            ["docker", "cp", f"{container_id}:/fuzz-evidence/.", str(output_root)],
            check=False,
        )
        if copied.returncode != 0 and container_status == 0:
            raise RuntimeError("accepted fuzz container evidence could not be copied")
        return container_status if container_status != 0 else copied.returncode
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container_id],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--suite", choices=("local", "nightly"), required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-revision", default=None)
    arguments = parser.parse_args()
    return run_container(
        image=arguments.image,
        suite=arguments.suite,
        output_root=arguments.output_root.resolve(),
        source_revision=arguments.source_revision or _source_revision(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
