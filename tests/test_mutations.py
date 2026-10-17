"""Module 5: simulate candidate changes on cloned graphs."""
import random
import textwrap

import pytest
from redislite.falkordb_client import FalkorDB

import preflight.db as D
from preflight.ingestion.python.pipeline import sync_workspace
from preflight.mutations import Q_SIG_BREAKS, parse_signature
from preflight.simulate import run_simulation

from test_ingest_python import NUANCE, make

T = "pkg.util.compute"


# ------------------------------------------------------------------ signature parser
def test_parse_signature():
    s = parse_signature(["a", "b=", "*args", "k", "j=", "**kw"])
    assert (s.params, s.kwonly, s.required) == (["a", "b"], ["k", "j"], ["a", "k"])
    assert s.has_varargs and s.has_varkw and not s.error
    s = parse_signature(["a", "*", "k"])
    assert (s.params, s.kwonly, s.required, s.has_varargs) == (["a"], ["k"], ["a", "k"], False)
    assert parse_signature([]).params == []
    assert "follows a default" in parse_signature(["a=", "b"]).error
    assert "duplicate" in parse_signature(["a", "a"]).error
    assert "not a valid" in parse_signature(["class"]).error
    assert "must be last" in parse_signature(["**kw", "a"]).error


# ------------------------------------------------------------------ the rule vs the real interpreter
def test_signature_rule_matches_python_interpreter(tmp_path):
    """1000 random calls: the graph query must flag a call exactly when Python raises TypeError."""
    rnd = random.Random(1)
    g = FalkorDB(str(tmp_path / "t.db")).select_graph("g")
    q = Q_SIG_BREAKS.replace("$q", "'t.f'")
    checked = 0
    for _ in range(40):
        n = rnd.randint(0, 4)
        nd = rnd.randint(0, n)
        params = [f"p{i}" for i in range(n)]
        defaults = set(params[n - nd:]) if nd else set()
        kwonly = [f"k{i}" for i in range(rnd.randint(0, 2))]
        kw_def = {k for k in kwonly if rnd.random() < .5}
        va, vk = rnd.random() < .3, rnd.random() < .3
        required = [p for p in params if p not in defaults] + [k for k in kwonly if k not in kw_def]
        sig = ", ".join([p + ("=1" if p in defaults else "") for p in params]
                        + (["*a"] if va else (["*"] if kwonly else []))
                        + [k + ("=1" if k in kw_def else "") for k in kwonly] + (["**kw"] if vk else []))
        ns = {}
        exec(f"def f({sig}): pass", ns)
        g.query("MATCH (n) DETACH DELETE n")
        g.query("CREATE (:Function {qname:'t.f', params:$p, kwonly:$k, required:$r, has_varargs:$va, has_varkw:$vk})",
                params={"p": params, "k": kwonly, "r": required, "va": va, "vk": vk})
        calls, truth = [], {}
        for j in range(25):
            npos = rnd.randint(0, 6)
            pool = params + kwonly + ["zzz"]
            kws = rnd.sample(pool, rnd.randint(0, min(3, len(pool))))
            try:
                ns["f"](*range(npos), **{k: 1 for k in kws})
                truth[f"c{j}"] = False
            except TypeError:
                truth[f"c{j}"] = True
            calls.append({"n": f"c{j}", "np": npos, "kw": kws})
        g.query("UNWIND $rows AS r MATCH (f:Function {qname:'t.f'}) CREATE (c:Function {qname:r.n, file:'x', "
                "is_test:false, in_test_file:false, module:'m'})-[:CALLS {line:1, ns:'x', n_pos:r.np, kw_names:r.kw, "
                "dynamic_args:false}]->(f)", params={"rows": calls})
        flagged = {r[0] for r in g.ro_query(q).result_set}
        for name, broke in truth.items():
            assert (name in flagged) == broke, (sig, next(c for c in calls if c["n"] == name))
            checked += 1
    assert checked == 1000


# ------------------------------------------------------------------ candidates on a real (small) project
@pytest.fixture()
def world(tmp_path, monkeypatch):
    db = FalkorDB(str(tmp_path / "t.db"))
    monkeypatch.setattr(D, "_db", db)
    ws = tmp_path / "ws"
    files = dict(NUANCE)
    files["pkg/util.py"] += "def other():\n    return 0\n"        # gives the rename-collision test something to hit
    files["pkg/api.py"] = "from pkg import b\ndef handler():\n    return b.run_b()\n"
    make(ws, files)
    canon = D.canon_name(ws)
    sync_workspace(ws, canon, db=db)
    return db, ws, canon


def sim(world, *cands):
    _, _, canon = world
    return run_simulation(canon, T, [c for c in cands], "t1", parallel=False)


def cp(name, new_params, target=T):
    return {"name": name, "mutations": [{"kind": "change_params", "target": target, "new_params": new_params}]}


def by_name(out):
    return {r["candidate"]: r for r in out["ranked"]}


def test_statuses_caught_masked_untested(world):
    out = sim(world, cp("migrate", ["x", "y", "z"]))
    r = by_name(out)["migrate"]
    st = {s["name"]: s["status"] for s in r["broken_callers"]}
    assert st["pkg.a.run_a"] == "caught"     # a test reaches it and does NOT mock this call
    assert st["pkg.b.run_b"] == "masked"     # every test that reaches it mocks it away
    assert st["pkg.c.run_c"] == "untested"   # nothing reaches it
    assert r["verdict"] == "risky" and r["counts"]["silent"] == 2 and r["counts"]["caught"] == 1
    liars = {l["test"] for l in r["lying_mocks"]}
    assert "tests.test_nuance.test_patch_origin_hits_module_attr_caller" in liars
    assert "tests.test_nuance.test_patch_origin_misses_from_import_caller" not in liars   # namespace rule
    reasons = {s["name"]: s["reason"] for s in r["broken_callers"]}
    assert "missing required argument(s): z" in reasons["pkg.a.run_a"]
    assert any(a["action"] == "add_real_test" for f in r["files_to_edit"] for a in f["edits"])
    assert r["files_to_edit"][0]["file"] == "pkg/util.py"                     # definition comes first


def test_additive_change_is_safe_and_wins(world):
    out = sim(world, cp("migrate", ["x", "y", "z"]), cp("adapter", ["x", "y", "z="]), cp("drop", ["x"]))
    assert out["winner"] == "adapter" and by_name(out)["adapter"]["verdict"] == "safe"
    assert [c["candidate"] for c in out["comparison"]][0] == "adapter"
    assert by_name(out)["drop"]["counts"]["broken"] == 3                      # y can no longer be passed
    assert out["checklist"][0]["file"] == "pkg/util.py"


def test_rename(world):
    out = sim(world, {"name": "rn", "mutations": [{"kind": "rename", "target": T, "new_name": "calc"}]},
              {"name": "alias", "mutations": [{"kind": "rename", "target": T, "new_name": "calc", "keep_alias": True}]})
    rn, al = by_name(out)["rn"], by_name(out)["alias"]
    st = {s["name"]: s["status"] for s in rn["broken_callers"]}
    assert st["pkg.a.run_a"] == "caught" and st["pkg.c.run_c"] == "caught"    # `from x import f` fails at import: loud
    assert st["pkg.b.run_b"] == "masked"                                       # util.compute() hidden behind a mock
    liars = {l["test"] for l in rn["lying_mocks"]}
    assert "tests.test_nuance.test_patch_origin_hits_module_attr_caller" in liars       # really stays green
    assert "tests.test_nuance.test_patch_where_used" not in liars                       # its module fails to import: loud
    actions = {(f["file"], e["action"]) for f in rn["files_to_edit"] for e in f["edits"]}
    assert ("pkg/a.py", "update_import") in actions and ("pkg/c.py", "update_import") in actions
    assert any(a == "update_mock_target" for _, a in actions)
    assert al["verdict"] == "safe" and al["counts"]["alias_debt"] == 1
    assert out["winner"] == "alias"


def test_dead_ends(world):
    out = sim(world,
              {"name": "clash", "mutations": [{"kind": "rename", "target": T, "new_name": "other"}]},
              cp("bad_sig", ["a=", "b"]),
              cp("ghost", ["x"], target="pkg.util.compte"),
              cp("fine", ["x", "y", "z="]))
    r = by_name(out)
    assert r["clash"]["verdict"] == "dead_end" and "collision" in r["clash"]["dead_end_reason"]
    assert r["bad_sig"]["verdict"] == "dead_end" and "follows a default" in r["bad_sig"]["dead_end_reason"]
    assert r["ghost"]["verdict"] == "dead_end" and "pkg.util.compute" in r["ghost"]["suggestions"]
    assert out["winner"] == "fine" and [c["verdict"] for c in out["comparison"]][-3:] == ["dead_end"] * 3


def test_canonical_graph_is_never_changed(world):
    db, _, canon = world
    g = db.select_graph(canon)
    snap = lambda: g.ro_query("MATCH (f:Function {qname:$q}) RETURN f.params, f.required, f.mutated, f.renamed_to",
                              params={"q": T}).result_set
    before = snap()
    sim(world, cp("migrate", ["x", "y", "z"]),
        {"name": "rn", "mutations": [{"kind": "rename", "target": T, "new_name": "calc"}]})
    assert snap() == before == [[["x", "y"], ["x", "y"], None, None]]
    assert not [n for n in D.list_graph_names() if n.startswith("uni_")]       # every clone was deleted


def test_two_mutations_in_one_candidate(world):
    out = sim(world, {"name": "both", "mutations": [
        {"kind": "change_params", "target": T, "new_params": ["x", "y", "z="]},          # safe on its own
        {"kind": "change_params", "target": "pkg.b.run_b", "new_params": ["req"]}]})     # breaks run_b's callers
    r = by_name(out)["both"]
    assert {s["mutation"] for s in r["broken_callers"]} == {"pkg.b.run_b"}
    assert len(r["changes"]) == 2 and r["counts"]["broken"] >= 1


# ------------------------------------------------------------------ through the MCP layer
def test_server_tool_auto_syncs_and_caps(world):
    from preflight import server
    db, ws, _ = world
    D._db = db
    server._active_workspace = None
    cands = [server.Candidate(name="adapter", mutations=[server.Mutation(kind="change_params", target=T,
                                                                          new_params=["x", "y", "z="])]),
             server.Candidate(name="migrate", mutations=[server.Mutation(kind="change_params", target=T,
                                                                          new_params=["x", "y", "z"])])]
    out = server.simulate_refactor_decisions(T, cands, workspace_path=str(ws))   # no sync call before this
    assert out["ok"] and out["winner"] == "adapter" and out["synced"]["status"] in {"unchanged", "loaded"}
    assert "files_to_edit" not in by_name(out)["migrate"]                        # non-winners are compacted
    with pytest.raises(ValueError):
        server.Mutation(kind="rename", target=T)                                 # rename needs new_name
    assert server.simulate_refactor_decisions(T, [], workspace_path=str(ws))["ok"] is False
