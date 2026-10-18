"""Glue: discover -> extract -> resolve -> load into FalkorDB."""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from preflight import schema as S
from preflight.events import emit

from .discover import detect_relaxed, discover
from .extract import extract_file
from .resolve import Resolved, resolve_all

log = logging.getLogger("preflight.ingest")
META = "Meta"


@dataclass
class IngestResult:
    resolved: Resolved
    file_hashes: dict[str, str]     # module qname -> sha256
    workspace_hash: str
    timings_ms: dict[str, float]
    skipped: dict[str, int]


def ingest_workspace(root: Path) -> IngestResult:
    """Pure Python work (no database). Safe to call from tests."""
    t0 = time.perf_counter()
    files, skipped = discover(root)
    t1 = time.perf_counter()
    relaxed = detect_relaxed(root.resolve())
    facts = [extract_file(f, relaxed) for f in files]
    t2 = time.perf_counter()
    resolved = resolve_all(facts)
    resolved.stats["relaxed_tests"] = int(relaxed)
    t3 = time.perf_counter()
    hashes = {m.qname: m.sha256 for m in resolved.modules}
    digest = hashlib.sha256("\n".join(f"{k}:{v}" for k, v in sorted(hashes.items())).encode()).hexdigest()
    return IngestResult(
        resolved=resolved, file_hashes=hashes, workspace_hash=digest, skipped=skipped,
        timings_ms={"discover": round((t1 - t0) * 1000, 1), "extract": round((t2 - t1) * 1000, 1),
                    "resolve": round((t3 - t2) * 1000, 1)},
    )


def load_into_graph(db, canon: str, result: IngestResult, root: Path) -> float:
    """Full re-ingest: delete the graph, rebuild it. Returns load time in ms."""
    t = time.perf_counter()
    g = S.reset_graph(db, canon)
    r = result.resolved
    S.load_batch(g, S.LOAD_MODULES, r.modules)
    S.load_batch(g, S.LOAD_FUNCTIONS, r.functions)
    S.load_batch(g, S.LOAD_DEFINED_IN, r.functions)
    S.load_batch(g, S.LOAD_IMPORTS, r.imports)
    S.load_batch(g, S.LOAD_CALLS, r.calls)
    S.load_batch(g, S.LOAD_REFS, r.refs)
    S.load_batch(g, S.LOAD_MOCKS, r.mocks)
    g.query(f"CREATE (:{META} {{name: 'workspace', hash: $h, root: $root}})",
            params={"h": result.workspace_hash, "root": str(root)})
    return round((time.perf_counter() - t) * 1000, 1)


def _stored_state(db, canon: str) -> tuple[str, dict[str, str]]:
    """(workspace hash, {module: sha256}) of what is already in the graph."""
    names = [n.decode() if isinstance(n, bytes) else n for n in db.list_graphs()]
    if canon not in names:
        return "", {}
    g = db.select_graph(canon)
    try:
        h = g.ro_query(f"MATCH (m:{META}) RETURN m.hash").result_set
        mods = g.ro_query(f"MATCH (m:{S.Label.MODULE}) RETURN m.qname, m.sha256").result_set
        return (h[0][0] if h else ""), {q: s for q, s in mods}
    except Exception:  # noqa: BLE001
        return "", {}


def graph_counts(db, canon: str) -> dict[str, int]:
    g = db.select_graph(canon)
    q = lambda c: g.ro_query(c).result_set[0][0]  # noqa: E731
    return {
        "nodes": q("MATCH (n) RETURN count(n)"),
        "edges": q("MATCH ()-[r]->() RETURN count(r)"),
        "calls": q("MATCH ()-[r:CALLS]->() RETURN count(r)"),
        "mocks": q("MATCH ()-[r:MOCKS]->() RETURN count(r)"),
        "refs": q("MATCH ()-[r:REFERENCES]->() RETURN count(r)"),
    }


def sync_workspace(workspace: Path, canon: str, db=None) -> dict[str, Any]:
    """Entry point for the MCP tool `sync_workspace_graph`."""
    from preflight.db import get_db
    db = db or get_db()
    result = ingest_workspace(workspace)
    old_hash, old_files = _stored_state(db, canon)
    changed = sum(1 for q, h in result.file_hashes.items() if old_files.get(q) != h)
    st = result.resolved.stats
    report = {
        "files_total": st["files"], "files_changed": changed,
        "functions": st["functions"], "tests": st["tests"],
        "calls_resolved": st["calls_resolved"], "calls_unresolved": st["calls_unresolved"],
        "unresolved_call_pct": round(100 - st["internal_resolution_pct"], 1),
        "mocks_found": st["mocks_found"], "mocks_resolved": st["mocks_resolved"],
        "mocks_external": st["mocks_external"], "mocks_unresolved": st["mocks_unresolved"],
        "mock_edges": st["mock_edges_direct"] + st["mock_edges_derived"],
        "test_style": "pytest-relaxed" if st["relaxed_tests"] else "standard",
        "parse_errors": st["parse_errors"], "skipped_files": result.skipped,
        "parse_ms": sum(result.timings_ms.values()),
    }
    if old_hash and old_hash == result.workspace_hash:
        out = {"status": "unchanged", **report, **graph_counts(db, canon)}
    else:
        report["load_ms"] = load_into_graph(db, canon, result, workspace)
        out = {"status": "loaded", **report, **graph_counts(db, canon)}
    emit("sync", graph=canon, status=out["status"], files=out["files_total"], changed=out["files_changed"],
         functions=out["functions"], tests=out["tests"], nodes=out["nodes"], edges=out["edges"],
         calls=out["calls"], mocks=out["mocks"], refs=out["refs"], parse_ms=round(out["parse_ms"]),
         load_ms=round(out.get("load_ms", 0)))
    return out
