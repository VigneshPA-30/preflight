"""Modules 2-4 live here: ingest and read-only queries.

STUBS. Each function has a fixed contract (inputs and output shape), so
server.py works today. Replace the bodies, keep the shapes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from preflight.db import get_db


def sync_workspace(workspace: Path, canon: str) -> dict[str, Any]:
    """MODULE 2. Parse .py files with `ast`, hash files (SHA-256), and load
    only changed files into graph `canon`.

    Return: {"files_total", "files_changed", "nodes", "edges",
             "unresolved_call_pct"}
    """
    # Stub: only make sure the canonical graph exists, so the rest of the chain runs.
    get_db().select_graph(canon).query("MERGE (:Meta {name: 'placeholder'})")
    return {
        "status": "stub",
        "files_total": 0,
        "files_changed": 0,
        "nodes": 0,
        "edges": 0,
        "unresolved_call_pct": None,
    }


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
