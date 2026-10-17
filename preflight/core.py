"""Modules 2-4 live here: ingest and read-only queries.

STUBS. Each function has a fixed contract (inputs and output shape), so
server.py works today. Replace the bodies, keep the shapes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any



def sync_workspace(workspace: Path, canon: str) -> dict[str, Any]:
    """MODULE 2 (Python). Parse with `ast`, resolve names, rebuild the graph.
    Skips all work when no file changed (workspace hash)."""
    from preflight.ingestion.python.pipeline import sync_workspace as _sync

    return _sync(workspace, canon)


def blast_radius(canon: str, qname: str, max_hops: int, db=None, max_items: int = 25) -> dict[str, Any]:
    """MODULE 3. Read-only multi-hop query. Implemented in preflight/queries.py."""
    from preflight.queries import blast_radius as _blast

    return _blast(canon, qname, max_hops, db=db, max_items=max_items)
