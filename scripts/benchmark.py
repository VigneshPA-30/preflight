"""Measure the tool on real projects and write docs/BENCHMARK.md (numbers for the README).

    uv run python scripts/benchmark.py ~/graphhacks/targets/cookiecutter ~/graphhacks/targets/invoke
"""
from __future__ import annotations

import platform
import statistics
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # run from anywhere

from redislite.falkordb_client import FalkorDB  # noqa: E402

import preflight.db as D  # noqa: E402
from preflight.ingestion.python.pipeline import sync_workspace  # noqa: E402
from preflight.queries import blast_radius, hub_info  # noqa: E402
from preflight.simulate import run_simulation  # noqa: E402


def ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def med(xs: list[float]) -> float:
    return round(statistics.median(xs), 1)


def bench(root: Path, db) -> dict:
    canon = D.canon_name(root)
    rep = sync_workspace(root, canon, db=db)
    g = db.select_graph(canon)
    row = {"project": root.name, "files": rep["files_total"], "functions": rep["functions"], "tests": rep["tests"],
           "nodes": rep["nodes"], "edges": rep["edges"], "parse_ms": round(rep["parse_ms"]), "load_ms": round(rep["load_ms"])}

    again = []
    for _ in range(3):
        s = time.perf_counter(); sync_workspace(root, canon, db=db); again.append(ms(s))
    row["resync_unchanged_ms"] = med(again)

    clones = []
    for i in range(5):
        s = time.perf_counter(); c = g.copy(f"uni_bench_{i}"); clones.append(ms(s)); c.delete()
    row["clone_ms"] = med(clones)

    hub = g.ro_query("MATCH (c:Function)-[:CALLS]->(f:Function) WHERE NOT f.in_test_file AND NOT c.in_test_file "
                     "AND size(f.kwonly) = 0 AND NOT f.has_varargs AND NOT f.has_varkw "
                     "RETURN f.qname, count(c) AS n ORDER BY n DESC LIMIT 1").result_set[0][0]
    times, last = [], None
    for _ in range(5):
        s = time.perf_counter(); last = blast_radius(canon, hub, db=db); times.append(ms(s))
    row.update(hub=hub, hub_callers=last["summary"]["affected_functions"], blast_ms=med(times))

    s = time.perf_counter()
    D_cache = hub_info(g, canon + "_pr", hub)  # first call computes PageRank
    row["pagerank_ms"] = round(ms(s), 1)

    node = last["function"]
    spec = [p if p in node["required"] else p + "=" for p in node["params"]]
    mk = lambda n, m: {"name": n, "mutations": [m]}
    cands = [
        mk("append_optional", {"kind": "change_params", "target": hub, "new_params": spec + ["zz="]}),
        mk("append_required", {"kind": "change_params", "target": hub, "new_params": spec + ["zz"]}),
        mk("rename", {"kind": "rename", "target": hub, "new_name": hub.rsplit(".", 1)[-1] + "_v2"}),
    ]
    walls = []
    for k in range(3):
        out = run_simulation(canon, hub, cands, f"b{k}", parallel=True); walls.append(out["wall_ms"])
    row["simulate3_ms"] = med(walls)
    row["sim"] = [(c["candidate"], c["verdict"], c["broken"], c["silent"]) for c in out["comparison"]]
    return row


def main() -> None:
    db = FalkorDB(str(Path(tempfile.mkdtemp()) / "bench.db"))
    D._db = db
    rows = [bench(Path(a).expanduser().resolve(), db) for a in sys.argv[1:]]
    L = ["# Benchmark", "",
         f"Measured on: {platform.platform()}, Python {platform.python_version()}, "
         f"falkordblite {version('falkordblite')}. Median of several runs. Re-create with `scripts/benchmark.py`.", "",
         "| Project | Files | Functions | Tests | Graph nodes / edges | Parse (ms) | Load graph (ms) | Re-sync, nothing changed (ms) |",
         "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['project']} | {r['files']} | {r['functions']} | {r['tests']} | {r['nodes']} / {r['edges']} | "
                 f"{r['parse_ms']} | {r['load_ms']} | {r['resync_unchanged_ms']} |")
    L += ["", "| Project | `GRAPH.COPY` clone (ms) | Most-called function | Affected functions | Blast radius query (ms) | PageRank (ms) | 3 rival plans, simulated (ms) |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['project']} | {r['clone_ms']} | `{r['hub']}` | {r['hub_callers']} | {r['blast_ms']} | "
                 f"{r['pagerank_ms']} | {r['simulate3_ms']} |")
    L += ["", "Plans simulated per project: append an optional parameter, append a required parameter, rename (3 clones, evaluated in parallel after serialized `GRAPH.COPY`)."]
    Path("docs").mkdir(exist_ok=True)
    Path("docs/BENCHMARK.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
