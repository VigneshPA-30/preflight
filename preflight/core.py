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


def blast_radius(canon: str, qualified_name: str, max_hops: int) -> dict[str, Any]:
    """MODULES 3+4. Read-only query (use graph.ro_query) on graph `canon`.

    Return: {"found": bool, "target": str,
             "hops": [{"hop": 1, "callers": [{"name", "file", "line"}]}],
             "mocking_tests": [{"test", "file", "line", "mocks"}],
             "hub_score": float | None}
    """
    return {
        "status": "stub",
        "found": False,
        "target": qualified_name,
        "hops": [],
        "mocking_tests": [],
        "hub_score": None,
    }
