"""Run ingestion from the terminal and look at the lying mocks.

    uv run python -m preflight.ingestion.python.cli ~/graphhacks/targets/cookiecutter
    uv run python -m preflight.ingestion.python.cli <workspace> --target cookiecutter.main.generate_context
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from redislite.falkordb_client import FalkorDB

from preflight import schema as S
from preflight.db import canon_name

from .pipeline import sync_workspace

TOP_LYING = (
    "MATCH (t:Function {is_test:true})-[m:MOCKS]->(f:Function) "
    "MATCH (c:Function)-[k:CALLS]->(f) "
    "WHERE k.ns = m.ns AND (c.qname = t.qname OR (t)-[:CALLS*1..4]->(c)) "
    "RETURN f.qname AS fn, count(DISTINCT t) AS tests, count(DISTINCT c) AS callers "
    "ORDER BY tests DESC, fn LIMIT $n"
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("workspace")
    ap.add_argument("--target", help="qualified function name to inspect")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--db", help="database file (default: a temporary one)")
    a = ap.parse_args()

    ws = Path(a.workspace).expanduser().resolve()
    db = FalkorDB(a.db or str(Path(tempfile.mkdtemp()) / "cli.db"))
    canon = canon_name(ws)
    rep = sync_workspace(ws, canon, db=db)

    print(f"\n== ingest: {ws.name} ({rep['status']}) ==")
    for k in ("files_total", "functions", "tests", "nodes", "edges", "calls", "mocks"):
        print(f"  {k:<18}{rep[k]}")
    print(f"  {'call resolution':<18}{100 - rep['unresolved_call_pct']}% of project-internal calls "
          f"({rep['calls_resolved']} resolved, {rep['calls_unresolved']} not)")
    print(f"  {'mock statements':<18}{rep['mocks_found']} found: {rep['mocks_resolved']} resolved, "
          f"{rep['mocks_external']} library, {rep['mocks_unresolved']} unresolved")
    print(f"  {'timing (ms)':<18}parse {rep['parse_ms']:.0f}, load {rep.get('load_ms', 0):.0f}")

    g = db.select_graph(canon)
    if a.target:
        print(f"\n== tests that mock {a.target} ==")
        for r in g.ro_query("MATCH (t:Function)-[m:MOCKS]->(f:Function {qname:$q}) "
                            "RETURN t.qname, m.via, m.scope, m.file, m.line ORDER BY t.qname", params={"q": a.target}).result_set:
            print(f"  {r[0]}  [{r[1]}, {r[2]}]  {r[3]}:{r[4]}")
        print(f"\n== LYING MOCKS for {a.target} ==")
        rows = g.ro_query(S.Q_LYING_MOCKS, params={"q": a.target}).result_set
        for t, tf, ml, mf, via, scope, c, cf, cl in rows:
            print(f"  {t}\n      mocks it ({via}, {scope}) at {mf}:{ml}\n      but reaches caller {c} ({cf}:{cl})")
        print(f"  -> {len(rows)} lying path(s)")
    else:
        print(f"\n== functions with the most lying-mock tests (top {a.top}) ==")
        for fn, tests, callers in g.ro_query(TOP_LYING, params={"n": a.top}).result_set:
            print(f"  {tests:>3} tests  {callers:>2} callers  {fn}")


if __name__ == "__main__":
    main()
