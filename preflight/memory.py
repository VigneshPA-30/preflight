"""Decision memory (Track 02): what was decided about a function, by whom, and what happened.

One memory graph per workspace (mem_<project>_<hash>), kept SEPARATE from the code graph on purpose:
the code graph is deleted and rebuilt whenever a file changes, the memory must survive that.

Graph model
  (:Decision {id, ts, target, winner, why, status, owner, run})-[:ABOUT]->(:Func {qname})
  (:Decision)-[:CONSIDERED {rank}]->(:Plan {name, verdict, score, broken, silent, lying_tests, files, summary, changes})
  (:Plan)-[:SILENT_BREAK {status, reason, file, line}]->(:Func)      a break no test would notice
  (:Decision)-[:HAD_OUTCOME]->(:Outcome {status, notes, agent, ts})  what an agent reported afterwards
Status flow: proposed -> claimed -> applied | abandoned   (a claim stops a second agent from taking the same job)
"""
from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from preflight.db import canon_name, get_db, graph_exists

log = logging.getLogger("preflight.memory")

STATUSES = ("proposed", "claimed", "applied", "abandoned")
OPEN = ("proposed", "claimed")


def mem_name(workspace: Path) -> str:
    return "mem_" + canon_name(workspace)[len("canon_"):]


def _graph(db, workspace: Path):
    g = db.select_graph(mem_name(workspace))
    for q in ("CREATE INDEX FOR (d:Decision) ON (d.id)", "CREATE INDEX FOR (f:Func) ON (f.qname)"):
        try:
            g.query(q)
        except Exception as exc:  # noqa: BLE001
            if "already indexed" not in str(exc):
                raise
    return g


def _save(db) -> None:
    try:   # write to disk now, so the memory survives even if the database server is stopped later
        db.save()
    except Exception:  # noqa: BLE001
        pass


def _when(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def record_simulation(workspace: Path, target: str, out: dict[str, Any], db=None) -> str:
    """Save one simulation as a decision. Returns its id."""
    db = db or get_db()
    g = _graph(db, workspace)
    did = uuid.uuid4().hex[:8]
    g.query("MERGE (t:Func {qname:$t}) CREATE (d:Decision {id:$id, ts:$ts, target:$t, winner:$w, why:$why, "
            "status:'proposed', owner:'', run:$run})-[:ABOUT]->(t)",
            params={"t": target, "id": did, "ts": int(time.time()), "w": out.get("winner") or "", "why": out.get("why") or "",
                    "run": out.get("run_id", "")})
    for rank, r in enumerate(out.get("ranked", []), 1):
        c = r.get("counts", {})
        g.query("MATCH (d:Decision {id:$id}) CREATE (d)-[:CONSIDERED {rank:$rank}]->(p:Plan {name:$n, verdict:$v, "
                "score:$s, broken:$b, silent:$si, lying_tests:$l, files:$f, summary:$sum, changes:$ch})",
                params={"id": did, "rank": rank, "n": r["candidate"], "v": r.get("verdict", "error"),
                        "s": r.get("score") or 0, "b": c.get("broken", 0), "si": c.get("silent", 0),
                        "l": c.get("lying_tests", 0), "f": c.get("files", 0),
                        "sum": r.get("summary", r.get("error", "")), "ch": "; ".join(r.get("changes", []))})
        silent = [{"name": b["name"], "status": b["status"], "reason": b["reason"], "file": b["file"], "line": b["line"]}
                  for b in r.get("broken_callers", []) if b["status"] in {"masked", "untested"}][:10]
        if silent:
            g.query("MATCH (d:Decision {id:$id})-[:CONSIDERED {rank:$rank}]->(p:Plan) UNWIND $rows AS s "
                    "MERGE (f:Func {qname:s.name}) CREATE (p)-[:SILENT_BREAK {status:s.status, reason:s.reason, "
                    "file:s.file, line:s.line}]->(f)", params={"id": did, "rank": rank, "rows": silent})
    _save(db)
    return did


def _neighbors(db, canon: str, function: str, limit: int = 300) -> list[str]:
    """Functions within two calls of `function` (callers or callees), from the code graph."""
    if not graph_exists(canon):
        return []
    rows = db.select_graph(canon).ro_query(
        "MATCH (f:Function {qname:$q})-[:CALLS*1..2]-(n:Function) RETURN DISTINCT n.qname LIMIT $n",
        params={"q": function, "n": limit}).result_set
    return [r[0] for r in rows]


def recall(workspace: Path, function: str | None = None, status: str | None = None, limit: int = 8,
           db=None) -> list[dict[str, Any]]:
    """Earlier decisions: about `function` or functions near it in the call graph; or the latest ones.
    status="open" means proposed or claimed (work that is not finished)."""
    db = db or get_db()
    if mem_name(workspace) not in [n.decode() if isinstance(n, bytes) else n for n in db.list_graphs()]:
        return []
    g = db.select_graph(mem_name(workspace))
    if function:
        names = [function] + _neighbors(db, canon_name(workspace), function)
        rows = g.ro_query("MATCH (d:Decision)-[:ABOUT]->(t:Func) WHERE t.qname IN $n "
                          "RETURN d.id, d.ts, d.target, d.winner, d.why, d.status, d.owner ORDER BY d.ts DESC",
                          params={"n": names}).result_set
    else:
        rows = g.ro_query("MATCH (d:Decision) RETURN d.id, d.ts, d.target, d.winner, d.why, d.status, d.owner "
                          "ORDER BY d.ts DESC").result_set
    if status == "open":
        rows = [r for r in rows if r[5] in OPEN]
    elif status:
        rows = [r for r in rows if r[5] == status]
    out = []
    for did, ts, target, winner, why, st, owner in rows[:max(1, min(int(limit), 25))]:
        plans = g.ro_query("MATCH (d:Decision {id:$id})-[c:CONSIDERED]->(p:Plan) "
                           "RETURN p.name, p.verdict, p.score, p.silent ORDER BY c.rank", params={"id": did}).result_set
        outcomes = g.ro_query("MATCH (d:Decision {id:$id})-[:HAD_OUTCOME]->(o:Outcome) "
                              "RETURN o.status, o.notes, o.agent, o.ts ORDER BY o.ts DESC LIMIT 3",
                              params={"id": did}).result_set
        out.append({
            "id": did, "when": _when(ts), "target": target, "winner": winner, "why": why, "status": st,
            "owner": owner or None,
            "relation": "same function" if function and target == function else ("nearby (calls or is called by it)" if function else None),
            "plans": [{"name": n, "verdict": v, "score": s, "silent_breaks": si} for n, v, s, si in plans],
            "outcomes": [{"status": s, "notes": n, "agent": a, "when": _when(t)} for s, n, a, t in outcomes],
        })
    return out


def update(workspace: Path, decision_id: str, status: str, notes: str | None = None, agent: str | None = None,
           db=None) -> dict[str, Any]:
    """Claim a decision, mark it applied or abandoned, or hand it back. Returns {"ok": ..., ...}."""
    db = db or get_db()
    if status not in STATUSES:
        return {"ok": False, "error": f"status must be one of {', '.join(STATUSES)}"}
    g = _graph(db, workspace)
    row = g.ro_query("MATCH (d:Decision {id:$id}) RETURN d.status, d.owner, d.target", params={"id": decision_id}).result_set
    if not row:
        return {"ok": False, "error": f"unknown decision id: {decision_id}", "hint": "use recall_decisions to list ids"}
    cur_status, owner, target = row[0]
    if status == "claimed":
        if not agent:
            return {"ok": False, "error": "give your name in `agent` when you claim a decision"}
        # ONE query decides: it only changes the decision if nobody else holds it, so two agents claiming at the
        # same moment cannot both win (FalkorDB runs write queries on one graph one after the other).
        won = g.query("MATCH (d:Decision {id:$id}) WHERE d.status <> 'claimed' OR d.owner = '' OR d.owner = $a "
                      "SET d.status = 'claimed', d.owner = $a, d.updated = $ts RETURN count(d)",
                      params={"id": decision_id, "a": agent, "ts": int(time.time())}).result_set[0][0]
        if not won:
            holder = g.ro_query("MATCH (d:Decision {id:$id}) RETURN d.owner", params={"id": decision_id}).result_set[0][0]
            return {"ok": False, "error": f"already claimed by {holder}",
                    "hint": "pick another decision, or ask that agent to hand it back (status 'proposed')"}
        if notes:
            g.query("MATCH (d:Decision {id:$id}) CREATE (d)-[:HAD_OUTCOME]->(:Outcome {status:'claimed', notes:$n, agent:$a, ts:$ts})",
                    params={"id": decision_id, "n": notes, "a": agent, "ts": int(time.time())})
        _save(db)
        return {"ok": True, "id": decision_id, "target": target, "status": "claimed", "owner": agent}
    new_owner = "" if status == "proposed" else (agent or owner or "")
    g.query("MATCH (d:Decision {id:$id}) SET d.status=$s, d.owner=$o, d.updated=$ts", params={
        "id": decision_id, "s": status, "o": new_owner, "ts": int(time.time())})
    if notes or status in {"applied", "abandoned"}:
        g.query("MATCH (d:Decision {id:$id}) CREATE (d)-[:HAD_OUTCOME]->(:Outcome {status:$s, notes:$n, agent:$a, ts:$ts})",
                params={"id": decision_id, "s": status, "n": notes or "", "a": agent or "", "ts": int(time.time())})
    _save(db)
    return {"ok": True, "id": decision_id, "target": target, "status": status, "owner": new_owner or None}
