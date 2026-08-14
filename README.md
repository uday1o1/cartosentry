# CartoSentry

CartoSentry is under active implementation.
The verified public workflow and evidence-backed project description will be completed at the portfolio release-candidate milestone.

The current foundation can be installed and checked with the following commands.

```console
uv sync --frozen
uv run python -c "import cartosentry; print(cartosentry.native_self_check())"
uv run pytest
```

The current versioned artifact contracts, deterministic identifiers, portable export rules, and validation commands are documented in `docs/artifact_schemas.md`.
Representative portable artifacts and their JSON Schemas are committed under `schemas`.

See `BUILD_PLAN.md` for the authoritative scope and acceptance gates.
