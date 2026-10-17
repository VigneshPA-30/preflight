"""Module 3: blast radius. Checked against hand-made answers AND an independent computation."""
import os
import random
import textwrap
from collections import deque
from pathlib import Path

import pytest
from redislite.falkordb_client import FalkorDB

from preflight.ingestion.python.pipeline import ingest_workspace, sync_workspace
from preflight.queries import blast_radius, suggest_names

from test_ingest_python import NUANCE, make


@pytest.fixture()
def db(tmp_path_factory):
    return FalkorDB(str(tmp_path_factory.mktemp("db") / "t.db"))


@pytest.fixture()
def nuance(tmp_path, db):
    files = dict(NUANCE)
    files["pkg/api.py"] = "from pkg import b\ndef handler():\n    return b.run_b()\n"
    files["tests/test_api.py"] = textwrap.dedent("""
        from unittest import mock
        from pkg import api
        def test_handler():
            with mock.patch("pkg.util.compute"):
                api.handler()
    """)
    sync_workspace(make(tmp_path, files), "canon_q", db=db)
    return db


def br(db, q="pkg.util.compute", **kw):
    return blast_radius("canon_q", q, db=db, **kw)


def test_hops_and_call_sites(nuance):
    r = br(nuance)
    assert r["found"] and r["function"]["params"] == ["x", "y"] and r["function"]["required"] == ["x", "y"]
    hops = {h["hop"]: {c["name"]: c for c in h["callers"]} for h in r["hops"]}
    assert set(hops[1]) == {"pkg.a.run_a", "pkg.b.run_b", "pkg.c.run_c"}
    assert set(hops[2]) == {"pkg.api.handler"}                       # tests are NOT in production hops
    site = hops[1]["pkg.a.run_a"]["call_sites"][0]
    assert site["line"] == 3 and site["n_pos"] == 2 and site["kw"] == []
    assert hops[2]["pkg.api.handler"]["via"] == ["pkg.api.handler", "pkg.b.run_b", "pkg.util.compute"]
    assert r["summary"]["affected_functions"] == 4 and r["summary"]["affected_files"] == 4


def test_masked_untested_and_real_coverage(nuance):
    r = br(nuance)
    hop1 = {c["name"]: c for c in r["hops"][0]["callers"]}
    # run_a: 3 tests reach it, only 1 of them mocks that call -> real coverage remains
    assert hop1["pkg.a.run_a"]["tests_reaching"] == 3 and hop1["pkg.a.run_a"]["masked"] is False
    # run_b: tests reaching it all mock the call -> NO real test exercises this call site
    assert hop1["pkg.b.run_b"]["masked"] is True
    # run_c: nothing reaches it
    assert hop1["pkg.c.run_c"]["untested"] is True and hop1["pkg.c.run_c"]["masked"] is False
    assert r["summary"]["fully_masked_callers"] == 1 and r["summary"]["untested_callers"] == 1


def test_mocking_tests_are_flagged_and_grouped(nuance):
    r = br(nuance)
    by_test = {m["test"]: m for m in r["mocking_tests"]}
    lie = by_test["tests.test_nuance.test_patch_where_used"]
    assert lie["lying"] and lie["hides"][0]["caller"] == "pkg.a.run_a"
    assert not by_test["tests.test_nuance.test_patch_origin_misses_from_import_caller"]["lying"]  # mocks f, but not through a's name
    assert by_test["tests.test_api.test_handler"]["lying"]           # reaches run_b through handler
    # one entry per mock statement (not one per call site)
    assert len(r["mocking_tests"]) == len({(m["test"], m["mock_file"], m["mock_line"]) for m in r["mocking_tests"]})


def test_not_found_gives_suggestions(nuance):
    r = br(nuance, "compute")
    assert r["found"] is False and "pkg.util.compute" in r["suggestions"]
    assert "pkg.util.Svc.run" not in br(nuance, "pkg.util.compte")["suggestions"]
    assert "pkg.util.compute" in suggest_names(nuance.select_graph("canon_q"), "util.compute")


def test_hub_and_truncation(nuance):
    r = br(nuance, max_items=1)
    assert r["hub"] is not None and 1 <= r["hub"]["rank"] <= r["hub"]["of"]
    assert r["truncated_items"] > 0 and all(len(h["callers"]) <= 1 for h in r["hops"])
    assert br(nuance, max_hops=1)["summary"]["affected_functions"] == 3     # depth limit respected


# ------------------------------------------------------------------ independent check
def test_hops_equal_independent_bfs(tmp_path, db):
    """Random call graph. The graph answer must equal plain-Python reverse BFS on the same records."""
    rnd = random.Random(7)
    n = 60
    lines = []
    for i in range(n):
        callees = rnd.sample(range(i), k=min(i, rnd.randint(0, 3))) if i else []
        body = "\n".join(f"    f{j}()" for j in callees) or "    pass"
        lines.append(f"def f{i}():\n{body}\n")
    make(tmp_path, {"m.py": "\n".join(lines)})
    sync_workspace(tmp_path, "canon_rand", db=db)
    calls = ingest_workspace(tmp_path).resolved.calls
    rev: dict[str, set[str]] = {}
    for c in calls:
        rev.setdefault(c.callee, set()).add(c.caller)
    for target in ("m.f0", "m.f5", "m.f20"):
        want, seen, dq = {}, {target}, deque([(target, 0)])
        while dq:
            cur, d = dq.popleft()
            if d == 6:
                continue
            for p in rev.get(cur, ()):
                if p not in seen:
                    seen.add(p)
                    want[p] = d + 1
                    dq.append((p, d + 1))
        r = blast_radius("canon_rand", target, max_hops=6, db=db, max_items=500)
        got = {c["name"]: h["hop"] for h in r["hops"] for c in h["callers"]}
        assert got == want, target
        for h in r["hops"]:                                           # every witness path is real
            for c in h["callers"]:
                if h["hop"] > 1:
                    path = c["via"]
                    assert len(path) == h["hop"] + 1 and path[-1] == target
                    assert all(any(e.caller == a and e.callee == b for e in calls) for a, b in zip(path, path[1:]))


@pytest.mark.skipif(not os.environ.get("PREFLIGHT_TARGETS"), reason="set PREFLIGHT_TARGETS=~/graphhacks/targets")
def test_real_cookiecutter(db):
    ws = Path(os.environ["PREFLIGHT_TARGETS"]).expanduser() / "cookiecutter"
    sync_workspace(ws, "canon_cc", db=db)
    r = blast_radius("canon_cc", "cookiecutter.generate.generate_context", db=db)
    one = r["hops"][0]["callers"]
    assert [c["name"] for c in one] == ["cookiecutter.main.cookiecutter"]
    assert sorted(s["line"] for s in one[0]["call_sites"]) == [112, 128]          # matches the grep answer key
    assert r["summary"]["lying_tests"] == 6 and len(r["mocking_tests"]) == 6
