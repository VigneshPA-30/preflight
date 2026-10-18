"""Decision memory: saved per workspace, recalled by function or neighbours, shared between sessions and agents."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from redislite.falkordb_client import FalkorDB

import preflight.db as D
from preflight import memory as M
from preflight.ingestion.python.pipeline import sync_workspace
from preflight.simulate import run_simulation

from test_ingest_python import NUANCE, make

T = "pkg.util.compute"


def plan(name, new_params):
    return {"name": name, "mutations": [{"kind": "change_params", "target": T, "new_params": new_params}]}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    db = FalkorDB(str(tmp_path / "m.db"))
    monkeypatch.setattr(D, "_db", db)
    ws = make(tmp_path / "ws", NUANCE)
    canon = D.canon_name(ws)
    sync_workspace(ws, canon, db=db)
    return db, ws, canon


def simulate(world, run="r1"):
    _, _, canon = world
    return run_simulation(canon, T, [plan("adapter", ["x", "y", "z="]), plan("migrate", ["x", "y", "z"])], run, parallel=False)


def test_record_and_recall_same_function_and_neighbours(world):
    db, ws, _ = world
    did = M.record_simulation(ws, T, simulate(world), db=db)
    same = M.recall(ws, T, db=db)
    assert [d["id"] for d in same] == [did] and same[0]["relation"] == "same function"
    assert same[0]["winner"] == "adapter" and [p["name"] for p in same[0]["plans"]] == ["adapter", "migrate"]
    near = M.recall(ws, "pkg.a.run_a", db=db)              # run_a calls compute: a neighbour
    assert [d["id"] for d in near] == [did] and near[0]["relation"].startswith("nearby")
    assert M.recall(ws, "pkg.api.handler", db=db) == []    # not within two calls of compute
    silent = db.select_graph(M.mem_name(ws)).ro_query(
        "MATCH (p:Plan {name:'migrate'})-[s:SILENT_BREAK]->(f) RETURN f.qname, s.status ORDER BY f.qname").result_set
    assert silent == [["pkg.b.run_b", "masked"], ["pkg.c.run_c", "untested"]]    # the silent breaks are remembered


def test_claim_conflict_handoff_and_outcome(world):
    db, ws, _ = world
    did = M.record_simulation(ws, T, simulate(world), db=db)
    assert M.update(ws, did, "claimed", agent="agent-a", db=db)["owner"] == "agent-a"
    refused = M.update(ws, did, "claimed", agent="agent-b", db=db)
    assert refused["ok"] is False and "already claimed by agent-a" in refused["error"]
    assert [d["id"] for d in M.recall(ws, status="open", db=db)] == [did]
    assert M.update(ws, did, "proposed", agent="agent-a", db=db)["owner"] is None          # handed back
    assert M.update(ws, did, "claimed", agent="agent-b", db=db)["owner"] == "agent-b"      # now b can take it
    M.update(ws, did, "applied", notes="added z with a default; tests green", agent="agent-b", db=db)
    assert M.recall(ws, status="open", db=db) == []
    d = M.recall(ws, T, db=db)[0]
    assert d["status"] == "applied" and d["outcomes"][0]["notes"] == "added z with a default; tests green"
    assert M.update(ws, "nope", "applied", db=db)["ok"] is False
    assert M.update(ws, did, "finished", db=db)["ok"] is False


def test_two_agents_claiming_at_the_same_moment_only_one_wins(world):
    from concurrent.futures import ThreadPoolExecutor
    db, ws, _ = world
    did = M.record_simulation(ws, T, simulate(world), db=db)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: M.update(ws, did, "claimed", agent=f"agent-{i}", db=db), range(8)))
    winners = [r["owner"] for r in results if r["ok"]]
    assert len(winners) == 1
    assert M.recall(ws, T, db=db)[0]["owner"] == winners[0]
    assert M.update(ws, did, "claimed", db=db)["ok"] is False          # a claim must carry a name


def test_memory_survives_a_code_change_and_resync(world):
    db, ws, canon = world
    did = M.record_simulation(ws, T, simulate(world), db=db)
    (ws / "pkg/util.py").write_text("def compute(x, y, w=0):\n    return x\n")        # the code graph is rebuilt...
    assert sync_workspace(ws, canon, db=db)["status"] == "loaded"
    assert [d["id"] for d in M.recall(ws, T, db=db)] == [did]                           # ...the memory is not


def test_each_workspace_has_its_own_memory(world, tmp_path):
    db, ws, _ = world
    other = make(tmp_path / "other", NUANCE)
    M.record_simulation(ws, T, simulate(world), db=db)
    assert M.mem_name(ws) != M.mem_name(other)
    assert M.recall(other, T, db=db) == []


def test_server_tools_show_earlier_decisions_and_trace(world, tmp_path, monkeypatch):
    from preflight import server
    db, ws, _ = world
    monkeypatch.setenv("PREFLIGHT_EVENTS", str(tmp_path / "ev.jsonl"))
    cands = [server.Candidate(name="adapter", mutations=[server.Mutation(kind="change_params", target=T, new_params=["x", "y", "z="])])]
    first = server.simulate_refactor_decisions(T, cands, workspace_path=str(ws))
    assert first["memory"]["earlier_decisions"] == []
    second = server.simulate_refactor_decisions(T, cands, workspace_path=str(ws))
    assert [d["id"] for d in second["memory"]["earlier_decisions"]] == [first["memory"]["decision_id"]]
    rec = server.recall_decisions(function_name="pkg.a.run_a", workspace_path=str(ws))
    assert rec["ok"] and rec["count"] == 2
    up = server.update_decision(first["memory"]["decision_id"], "claimed", agent="me", workspace_path=str(ws))
    assert up["ok"] and server.recall_decisions(status="open", workspace_path=str(ws))["count"] == 2
    kinds = {json.loads(l)["kind"] for l in (tmp_path / "ev.jsonl").read_text().splitlines()}
    assert {"memory_saved", "memory_recalled", "memory_updated"} <= kinds


def test_memory_is_shared_between_separate_agent_sessions(tmp_path):
    """Three separate MCP server processes (three agent sessions) on one database: the later ones see the earlier work."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    ws = make(tmp_path / "ws", NUANCE)
    db_path = str(tmp_path / "shared.db")
    root = str(Path(__file__).resolve().parents[1])

    async def session(calls):
        env = {**os.environ, "PREFLIGHT_DB": db_path, "PREFLIGHT_EVENTS": str(tmp_path / "e.jsonl")}
        p = StdioServerParameters(command=sys.executable, args=["-m", "preflight.server"], env=env, cwd=root)
        out = []
        async with stdio_client(p) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for name, args in calls:
                    res = await s.call_tool(name, {**args, "workspace_path": str(ws)})
                    out.append(res.structuredContent)
        return out

    cand = [{"name": "adapter", "mutations": [{"kind": "change_params", "target": T, "new_params": ["x", "y", "z="]}]}]
    try:
        (sim,) = asyncio.run(session([("simulate_refactor_decisions", {"target_func": T, "candidates": cand})]))      # agent A
        did = sim["memory"]["decision_id"]
        seen, claimed = asyncio.run(session([("recall_decisions", {"function_name": T}),                              # agent B
                                             ("update_decision", {"decision_id": did, "status": "claimed", "agent": "agent-b"})]))
        assert [d["id"] for d in seen["decisions"]] == [did] and claimed["ok"]
        (late,) = asyncio.run(session([("update_decision", {"decision_id": did, "status": "claimed", "agent": "agent-c"})]))  # agent C
        assert late["ok"] is False and "agent-b" in late["error"]
    finally:
        try:
            FalkorDB(db_path).execute_command("SHUTDOWN", "NOSAVE")
        except Exception:  # noqa: BLE001
            pass
