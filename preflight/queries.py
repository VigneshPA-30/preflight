"""MODULE 3: read-only questions about the canonical graph.

Everything here uses ro_query. Nothing here changes the graph.
`blast_radius` is the engine behind the MCP tool `inspect_blast_radius`.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from preflight import schema as S
from preflight.db import get_db

log = logging.getLogger("preflight.queries")

_PR_CACHE: dict[str, tuple[str, dict[str, int], dict[str, float]]] = {}   # canon -> (hash, rank, score)


# ------------------------------------------------------------------ helpers
def _graph(db, canon: str):
    return (db or get_db()).select_graph(canon)


def _rows(g, cypher: str, **params) -> list[list[Any]]:
    return g.ro_query(cypher, params=params).result_set


def suggest_names(g, qname: str, limit: int = 5) -> list[str]:
    """Close matches for a name that was not found (agents often forget the module path)."""
    last = qname.rsplit(".", 1)[-1]
    rows = _rows(g, "MATCH (f:Function) WHERE f.qname ENDS WITH $s OR f.qname STARTS WITH $p "
                    "RETURN f.qname LIMIT 200", s="." + last, p=qname + ".")
    want = qname.split(".")

    def score(name: str) -> tuple[int, int, int]:
        parts = name.split(".")
        tail_run = 0   # how many trailing names match: the best signal
        for a, b in zip(reversed(parts), reversed(want)):
            if a != b:
                break
            tail_run += 1
        head = 0
        for a, b in zip(parts, want):
            if a != b:
                break
            head += 1
        return (-tail_run, -head, len(name))

    return [r[0] for r in sorted(rows, key=lambda r: score(r[0]))[:limit]]


def hub_info(g, canon: str, qname: str) -> dict[str, Any] | None:
    """PageRank over the call graph: how central is this function? (FalkorDB algo.pageRank)"""
    try:
        meta = _rows(g, "MATCH (m:Meta) RETURN m.hash")
        key = meta[0][0] if meta else ""
        cached = _PR_CACHE.get(canon)
        if not cached or cached[0] != key or not key:
            rows = _rows(g, "CALL algo.pageRank('Function','CALLS') YIELD node, score "
                            "RETURN node.qname, score ORDER BY score DESC")
            rank = {r[0]: i + 1 for i, r in enumerate(rows)}
            score = {r[0]: r[1] for r in rows}
            cached = _PR_CACHE[canon] = (key, rank, score)
        _, rank, score = cached
        if qname not in rank:
            return None
        n = len(rank)
        return {"rank": rank[qname], "of": n, "pagerank": round(score[qname], 4),
                "is_hub": rank[qname] <= max(3, n // 20)}
    except Exception:  # noqa: BLE001
        log.exception("pagerank failed (continuing without hub info)")
        return None


# ------------------------------------------------------------------ the main query
def blast_radius(canon: str, qname: str, max_hops: int = 5, db=None, max_items: int = 25) -> dict[str, Any]:
    """Who is affected if `qname` changes, hop by hop, and which tests lie about it?

    Production callers are grouped by hop (shortest distance). Tests are summarised separately.
    Every direct caller says how many tests reach it and how many of those mock the call away.
    """
    t0 = time.perf_counter()
    g = _graph(db, canon)
    max_hops = max(1, min(int(max_hops), 8))
    truncated = 0

    me = _rows(g, "MATCH (f:Function {qname:$q}) RETURN f.file, f.line, f.end_line, f.params, f.required, "
                  "f.kwonly, f.returns, f.is_async, f.is_test, f.has_varargs, f.has_varkw", q=qname)
    if not me:
        return {"found": False, "target": qname, "suggestions": suggest_names(g, qname),
                "hint": "use the fully qualified name, e.g. package.module.function or package.module.Class.method"}
    f_file, f_line, f_end, params, required, kwonly, returns, is_async, f_is_test, varargs, varkw = me[0]

    # 1. every caller, with its shortest hop distance (one multi-hop query)
    callers = _rows(
        g, f"MATCH p=(c:Function)-[:CALLS*1..{max_hops}]->(f:Function {{qname:$q}}) "
           "RETURN c.qname, c.file, c.line, c.is_test, c.in_test_file, min(length(p)) AS hop "
           "ORDER BY hop, c.qname", q=qname)
    prod = [r for r in callers if not r[4] and r[0] != qname]
    tests = [r for r in callers if r[3]]

    # 2. direct call sites (these are what a signature change breaks)
    sites: dict[str, list[dict]] = {}
    for c, line, n_pos, kw, dyn in _rows(
            g, "MATCH (c:Function)-[k:CALLS]->(f:Function {qname:$q}) "
               "RETURN c.qname, k.line, k.n_pos, k.kw_names, k.dynamic_args ORDER BY c.file, k.line", q=qname):
        sites.setdefault(c, []).append({"line": line, "n_pos": n_pos, "kw": kw, "dynamic_args": dyn})

    # 3. tests that mock it, and which of them LIE (mock the call and still reach the caller)
    lying: dict[tuple, dict] = {}
    for (t, t_file, m_line, m_file, via, scope, caller, c_file, c_line) in _rows(g, S.Q_LYING_MOCKS, q=qname):
        e = lying.setdefault((t, m_file, m_line), {"via": via, "scope": scope, "test_file": t_file, "hides": []})
        e["hides"].append({"caller": caller, "file": c_file, "line": c_line})
    mocking = []
    for t, t_file, via, scope, m_file, m_line in _rows(
            g, "MATCH (t:Function)-[m:MOCKS]->(f:Function {qname:$q}) "
               "RETURN t.qname, t.file, m.via, m.scope, m.file, m.line ORDER BY t.qname, m.line", q=qname):
        lie = lying.get((t, m_file, m_line))
        mocking.append({"test": t, "test_file": t_file, "mock_file": m_file, "mock_line": m_line,
                        "via": via, "scope": scope, "lying": bool(lie),
                        **({"hides": lie["hides"]} if lie else {})})
    lying_tests = {m["test"] for m in mocking if m["lying"]}

    # 4. for each direct production caller: how many tests really exercise it?
    direct = [r for r in prod if r[5] == 1]
    reach: dict[str, set[str]] = {}
    if direct:
        for cq, ts in _rows(g, "UNWIND $cs AS cq MATCH (c:Function {qname: cq}) "
                               "OPTIONAL MATCH (t:Function {is_test:true})-[:CALLS*1..4]->(c) "
                               "RETURN cq, collect(DISTINCT t.qname)", cs=[r[0] for r in direct]):
            reach[cq] = set(ts)
    mocked_by: dict[str, set[str]] = {}
    for m in mocking:
        for h in m.get("hides", []):
            mocked_by.setdefault(h["caller"], set()).add(m["test"])

    # 5. witness path for deeper callers, rebuilt from ONE edge query
    hop_of = {r[0]: r[5] for r in prod}
    names = list(hop_of)
    parent: dict[str, str] = {}
    if any(h > 1 for h in hop_of.values()):
        edges = _rows(g, "MATCH (a:Function)-[:CALLS]->(b:Function) WHERE a.qname IN $n "
                         "RETURN DISTINCT a.qname, b.qname", n=names)
        for a, b in edges:
            if hop_of.get(a, 0) >= 2 and hop_of.get(b) == hop_of[a] - 1 and a not in parent:
                parent[a] = b

    def witness(c: str) -> list[str]:
        path, cur = [c], c
        while hop_of.get(cur, 0) > 1 and cur in parent:
            cur = parent[cur]
            path.append(cur)
        return path + [qname]

    # 6. assemble (capped: output goes into an agent's context window)
    hops: list[dict] = []
    for h in sorted({r[5] for r in prod}):
        rows = [r for r in prod if r[5] == h]
        items = []
        for name, file, line, _, _, _ in rows[:max_items]:
            e: dict[str, Any] = {"name": name, "file": file, "line": line}
            if h == 1:
                e["call_sites"] = sites.get(name, [])[:10]
                rt, mk = reach.get(name, set()), mocked_by.get(name, set())
                e.update(tests_reaching=len(rt), tests_mocking=len(mk & rt) if rt else len(mk),
                         masked=bool(rt) and rt <= mk, untested=not rt)
            else:
                e["via"] = witness(name)
            items.append(e)
        truncated += max(0, len(rows) - max_items)
        hops.append({"hop": h, "count": len(rows), "callers": items})

    files: dict[str, dict] = {}
    for name, file, line, _, _, h in prod:
        e = files.setdefault(file, {"file": file, "nearest_hop": h, "functions": 0})
        e["functions"] += 1
    file_list = sorted(files.values(), key=lambda e: (e["nearest_hop"], e["file"]))
    truncated += max(0, len(file_list) - max_items)

    mock_out = mocking[:max_items]
    for m in mock_out:
        if "hides" in m:
            truncated += max(0, len(m["hides"]) - 10)
            m["hides"] = m["hides"][:10]
    truncated += max(0, len(mocking) - max_items)

    masked = [e["name"] for hp in hops if hp["hop"] == 1 for e in hp["callers"] if e.get("masked")]
    return {
        "found": True, "target": qname,
        "function": {"file": f_file, "line": f_line, "end_line": f_end, "params": params, "required": required,
                     "kwonly": kwonly, "returns": returns, "is_async": is_async,
                     "has_varargs": varargs, "has_varkw": varkw, "is_test": f_is_test},
        "summary": {"direct_callers": len(direct), "affected_functions": len(prod),
                    "affected_files": len(files), "tests_reaching": len(tests),
                    "mocking_tests": len(mocking), "lying_tests": len(lying_tests),
                    "fully_masked_callers": len(masked),
                    "untested_callers": sum(1 for r in direct if not reach.get(r[0]))},
        "hops": hops,
        "files": file_list[:max_items],
        "tests_reaching": [t[0] for t in tests[:max_items]],
        "mocking_tests": mock_out,
        "hub": hub_info(g, canon, qname),
        "truncated_items": truncated,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
