"""Live view: the event log, the traced tools, and the viewer's HTTP/SSE endpoints."""
import http.client
import json
import socket
import threading
import time

import pytest
from redislite.falkordb_client import FalkorDB

import preflight.db as D
from preflight import events, viewer


@pytest.fixture()
def ev_file(tmp_path, monkeypatch):
    p = tmp_path / "events.jsonl"
    monkeypatch.setenv("PREFLIGHT_EVENTS", str(p))
    events.CURRENT["call"] = None
    return p


def read(p):
    return [json.loads(l) for l in p.read_text().splitlines()]


def test_emit_attaches_current_call_and_rotates(ev_file):
    events.emit("a", x=1)
    events.CURRENT["call"] = "c1"
    events.emit("b")
    events.CURRENT["call"] = None
    rows = read(ev_file)
    assert rows[0]["kind"] == "a" and "call" not in rows[0] and rows[1]["call"] == "c1"
    for i in range(50):
        events.emit("fill", i=i, pad="x" * 200)
    events.rotate(max_bytes=1000, keep_lines=5)
    assert len(read(ev_file)) == 5


def test_emit_never_raises(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_EVENTS", "/proc/definitely/not/writable/events.jsonl")
    events.emit("x")          # must not raise


def test_traced_tool_events_and_schema(ev_file, tmp_path, monkeypatch):
    from preflight import server
    monkeypatch.setattr(D, "_db", FalkorDB(str(tmp_path / "t.db")))
    res = server.check_graph_engine()
    assert res["ok"]
    kinds = [r["kind"] for r in read(ev_file)]
    assert kinds == ["tool_start", "tool_end"]
    end = read(ev_file)[-1]
    assert end["tool"] == "check_graph_engine" and end["ok"] is True and end["summary"]["isolated"] is True
    assert read(ev_file)[0]["call"] == end["call"]
    # tracing must not change what the agent sees: the tool schema still comes from the real signature
    import asyncio
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    props = tools["simulate_refactor_decisions"].inputSchema["properties"]
    assert {"target_func", "candidates", "workspace_path"} <= set(props)


def test_failed_tool_is_reported(ev_file, tmp_path, monkeypatch):
    from preflight import server
    out = server.sync_workspace_graph(str(tmp_path / "does-not-exist"))
    assert out["ok"] is False
    end = read(ev_file)[-1]
    assert end["kind"] == "tool_end" and end["ok"] is False and "not a directory" in end["error"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server_url(ev_file):
    srv = viewer.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def sse_lines(resp, want, timeout=5):
    got, t0 = [], time.time()
    while len(got) < want and time.time() - t0 < timeout:
        line = resp.fp.readline().decode().strip()
        if line.startswith("data: "):
            got.append(json.loads(line[6:]))
    return got


def test_stream_replays_then_follows(server_url, ev_file):
    events.emit("old", n=1)
    conn = http.client.HTTPConnection("127.0.0.1", server_url, timeout=5)
    conn.request("GET", "/stream?replay=10")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
    assert sse_lines(resp, 1)[0]["kind"] == "old"                 # backlog
    events.emit("live", n=2)
    assert sse_lines(resp, 1)[0]["kind"] == "live"                # new line, delivered while connected
    conn.close()


def test_page_and_info_and_graphs_without_port(server_url, monkeypatch):
    monkeypatch.delenv("PREFLIGHT_PORT", raising=False)
    c = http.client.HTTPConnection("127.0.0.1", server_url, timeout=5)
    c.request("GET", "/")
    body = c.getresponse().read().decode()
    assert "Preflight live" in body and "EventSource" in body
    c.request("GET", "/api/graphs")
    assert json.loads(c.getresponse().read())["connected"] is False
    c.request("GET", "/nope")
    assert c.getresponse().status == 404


def test_graph_listing_over_tcp_port(server_url, tmp_path, monkeypatch):
    port = free_port()
    db = FalkorDB(str(tmp_path / "p.db"), serverconfig={"port": str(port), "bind": "127.0.0.1"})
    db.select_graph("canon_demo").query("CREATE (:F)-[:CALLS]->(:F)")
    db.select_graph("uni_ab_1_plan").query("CREATE (:F)")
    monkeypatch.setenv("PREFLIGHT_PORT", str(port))
    viewer._client = None
    c = http.client.HTTPConnection("127.0.0.1", server_url, timeout=10)
    c.request("GET", "/api/graphs")
    r = json.loads(c.getresponse().read())
    assert r["connected"] is True
    by = {g["name"]: g for g in r["graphs"]}
    assert by["canon_demo"]["kind"] == "code graph" and by["canon_demo"]["edges"] == 1
    assert by["uni_ab_1_plan"]["kind"] == "clone"
    viewer._client = None


def test_keep_universes_leaves_clones_for_the_browser(ev_file, tmp_path, monkeypatch):
    from preflight.ingestion.python.pipeline import sync_workspace
    from preflight.simulate import run_simulation
    from test_ingest_python import NUANCE, make
    db = FalkorDB(str(tmp_path / "k.db"))
    monkeypatch.setattr(D, "_db", db)
    ws = make(tmp_path / "ws", NUANCE)
    canon = D.canon_name(ws)
    sync_workspace(ws, canon, db=db)
    cand = {"name": "adapter plan", "mutations": [{"kind": "change_params", "target": "pkg.util.compute",
                                                    "new_params": ["x", "y", "z="]}]}
    monkeypatch.setenv("PREFLIGHT_KEEP_UNIVERSES", "1")
    run_simulation(canon, "pkg.util.compute", [cand], "run1", parallel=False)
    clones = [n for n in D.list_graph_names() if n.startswith("uni_")]
    assert clones == ["uni_run1_1_adapter_plan"]
    changed = db.select_graph(clones[0]).ro_query("MATCH (f:Function {qname:'pkg.util.compute'}) RETURN f.params, f.mutated").result_set
    assert changed == [[["x", "y", "z"], True]]                   # the clone shows the change; ...
    assert db.select_graph(canon).ro_query("MATCH (f:Function {qname:'pkg.util.compute'}) RETURN f.params").result_set == [[["x", "y"]]]
    kinds = [r["kind"] for r in read(ev_file)]
    assert "clone_kept" in kinds and "clone_deleted" not in kinds
    run_simulation(canon, "pkg.util.compute", [cand], "run2", parallel=False)   # next run clears the old clones
    assert [n for n in D.list_graph_names() if n.startswith("uni_")] == ["uni_run2_1_adapter_plan"]


def test_orphaned_server_gets_its_port(tmp_path):
    """Claude Desktop kills the MCP server; the database server it started keeps running without a port."""
    import os
    import signal
    import subprocess
    import sys
    path, port = str(tmp_path / "o.db"), free_port()
    owner = subprocess.Popen([sys.executable, "-c",
        f"import time\nfrom redislite.falkordb_client import FalkorDB\n"
        f"db = FalkorDB({path!r}); db.select_graph('g').query('CREATE (:F {{n:1}})')\nprint('ready', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True)
    owner.stdout.readline()
    os.kill(owner.pid, signal.SIGKILL)               # what Claude Desktop does
    time.sleep(1)
    db = None
    try:
        db = D.open_database(path, str(port))
        assert D._listening(port)                    # the port is open although the old server had none
        assert db.list_graphs() == ["g"]             # and the data survived the restart
    finally:
        if db is not None:
            try:
                db.execute_command("SHUTDOWN", "NOSAVE")
            except Exception:  # noqa: BLE001
                pass


def test_kept_clone_is_labelled_so_the_browser_tells_the_story(ev_file, tmp_path, monkeypatch):
    from preflight.ingestion.python.pipeline import sync_workspace
    from preflight.simulate import run_simulation
    from test_ingest_python import NUANCE, make
    db = FalkorDB(str(tmp_path / "l.db"))
    monkeypatch.setattr(D, "_db", db)
    ws = make(tmp_path / "ws", NUANCE)
    canon = D.canon_name(ws)
    sync_workspace(ws, canon, db=db)
    cand = {"name": "migrate", "mutations": [{"kind": "change_params", "target": "pkg.util.compute",
                                               "new_params": ["x", "y", "z"]}]}      # z required: every caller breaks
    monkeypatch.setenv("PREFLIGHT_KEEP_UNIVERSES", "1")
    out = run_simulation(canon, "pkg.util.compute", [cand], "lab", parallel=False)
    # silent breaks are listed first
    assert [b["status"] for b in out["ranked"][0]["broken_callers"]] == ["masked", "untested", "caught"]
    g = db.select_graph("uni_lab_1_migrate")
    count = lambda label: g.ro_query(f"MATCH (n:{label}) RETURN count(n)").result_set[0][0]
    assert (count("Changed"), count("CaughtBreak"), count("MaskedBreak"), count("UntestedBreak")) == (1, 1, 1, 1)
    assert count("LyingTest") >= 1 and count("ExpectedToFail") >= 1
    edge = g.ro_query("MATCH (c:MaskedBreak)-[k:CALLS]->(f:Changed) RETURN c.qname, k.broken, k.status, k.reason").result_set
    assert edge == [["pkg.b.run_b", True, "masked", "missing required argument(s): z"]]
    f = g.ro_query("MATCH (f:Changed) RETURN f.old_params, f.params, f.old_required, f.required").result_set
    assert f == [[["x", "y"], ["x", "y", "z"], ["x", "y"], ["x", "y", "z"]]]            # old and new signature side by side
    assert db.select_graph(canon).ro_query("MATCH (n:Changed) RETURN count(n)").result_set == [[0]]   # real graph untouched
    ev = [r for r in read(ev_file) if r["kind"] == "candidate_evaluated"][0]
    assert ev["broken"][0]["status"] == "masked" and len(ev["queries"]) == 4
