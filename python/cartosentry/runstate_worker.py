"""Internal process-crash worker used by the run recovery qualification."""

from __future__ import annotations

import argparse
from pathlib import Path

from cartosentry.recovery import run_demo_pipeline
from cartosentry.runstate import CommitBoundary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument(
        "--boundary",
        choices=[item.value for item in CommitBoundary],
        required=True,
    )
    arguments = parser.parse_args()
    run_demo_pipeline(
        arguments.root,
        crash_stage=arguments.stage,
        crash_boundary=CommitBoundary(arguments.boundary),
    )


if __name__ == "__main__":
    main()
