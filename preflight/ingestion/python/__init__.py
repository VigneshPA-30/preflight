"""Python ingestion (uses the stdlib `ast`; never imports the code it reads).

Stages:  discover -> extract (per file) -> resolve (whole workspace) -> load.
Use `pipeline.sync_workspace(path, canon)` from the server, or run the CLI:
    uv run python -m preflight.ingestion.python.cli <workspace> --target <qname>
"""
