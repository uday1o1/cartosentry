# CartoSentry

CartoSentry is under active implementation.
The verified public workflow and evidence-backed project description will be completed at the portfolio release-candidate milestone.

The current foundation can be installed and checked with the following commands.

```console
uv sync --frozen
uv run python -c "import cartosentry; print(cartosentry.native_self_check())"
uv run pytest
```

See `BUILD_PLAN.md` for the authoritative scope and acceptance gates.
