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
    "WHERE k.ns = m.ns AND (c.qname = t.qname OR (t)-[:CALLS|REFERENCES*1..4]->(c)) "
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
        from preflight.queries import blast_radius
        r = blast_radius(canon, a.target, max_hops=5, db=db)
        if not r["found"]:
            print(f"\nnot found: {a.target}\n  did you mean: {r['suggestions']}")
            return
        sm, fn = r["summary"], r["function"]
        print(f"\n== blast radius: {a.target} ==")
        print(f"  defined at {fn['file']}:{fn['line']}   params={fn['params']} required={fn['required']}")
        print(f"  {sm['direct_callers']} direct callers, {sm['affected_functions']} affected functions in "
              f"{sm['affected_files']} files, {sm['tests_reaching']} tests reach it ({r['elapsed_ms']} ms)")
        if r["hub"]:
            print(f"  hub rank {r['hub']['rank']} of {r['hub']['of']} (PageRank {r['hub']['pagerank']})")
        for h in r["hops"]:
            print(f"  hop {h['hop']}: {h['count']} function(s)")
            for c in h["callers"][:6]:
                extra = ""
                if h["hop"] == 1:
                    lines = ",".join(str(x["line"]) for x in c["call_sites"])
                    flag = "  <-- NO REAL TEST EXERCISES THIS CALL" if c["masked"] else ("  (untested)" if c["untested"] else "")
                    extra = f"  calls at line {lines}; {c['tests_reaching']} tests reach it, {c['tests_mocking']} mock it{flag}"
                else:
                    extra = "  via " + " -> ".join(x.rsplit(".", 1)[-1] for x in c["via"])
                print(f"      {c['name']} ({c['file']}:{c['line']}){extra}")
        print(f"\n== tests that mock it: {sm['mocking_tests']}  (lying: {sm['lying_tests']}) ==")
        for m in r["mocking_tests"][:8]:
            tag = "LIES" if m["lying"] else "ok  "
            hides = ", ".join(f"{h['caller'].rsplit('.', 1)[-1]}:{h['line']}" for h in m.get("hides", []))
            print(f"  [{tag}] {m['test']}  ({m['via']}, {m['scope']}) {m['mock_file']}:{m['mock_line']}"
                  + (f"\n          hides: {hides}" if hides else ""))
    else:
        print(f"\n== functions with the most lying-mock tests (top {a.top}) ==")
        for fn, tests, callers in g.ro_query(TOP_LYING, params={"n": a.top}).result_set:
            print(f"  {tests:>3} tests  {callers:>2} callers  {fn}")


if __name__ == "__main__":
    main()
