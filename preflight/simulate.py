"""MODULES 5-6: universe lifecycle, candidate evaluation, scoring and ranking.

Lifecycle per candidate:  GRAPH.COPY -> evaluate on the clone -> delete the clone (always).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from preflight.db import UNIVERSE_PREFIX, get_db, sweep_universes
from preflight.events import emit
from preflight.mutations import evaluate_mutation

log = logging.getLogger("preflight.simulate")

# ---- YOUR SCORING FORMULA (lower = safer). Keep it simple enough to explain in one breath:
# a break that tests will catch is cheap; a break that nobody will notice is expensive.
W_CAUGHT = 1.0          # call site breaks, but a real test will fail and show it
W_SILENT = 5.0          # call site breaks and tests stay green (masked by a mock) or never run (untested)
W_LYING_TEST = 0.5      # a test that stays green falsely: false confidence
W_FILE = 0.25           # every extra file to edit
W_ALIAS = 0.5           # a kept deprecated alias is technical debt
W_UNVERIFIABLE = 0.5    # call sites using *args/**kwargs cannot be checked

def keep_universes() -> bool:
    """Demo switch: leave the clones in the database so the FalkorDB Browser can show them."""
    return os.environ.get("PREFLIGHT_KEEP_UNIVERSES") == "1"


# GRAPH.COPY forks the database process. Two forks at once fail with "could not fork",
# so clone creation is serialized. Evaluation still runs in parallel.
_copy_lock = threading.Lock()
COPY_RETRIES = 5


def _copy_graph(canon: str, name: str):
    """Clone the canonical graph. Returns (clone, milliseconds spent copying, milliseconds waiting for the lock)."""
    graph = get_db().select_graph(canon)
    waited = time.perf_counter()
    with _copy_lock:
        waited_ms = (time.perf_counter() - waited) * 1000
        for attempt in range(COPY_RETRIES):
            try:
                t = time.perf_counter()
                clone = graph.copy(name)
                return clone, round((time.perf_counter() - t) * 1000, 2), round(waited_ms, 2)
            except Exception as exc:  # noqa: BLE001
                if "could not fork" not in str(exc) or attempt == COPY_RETRIES - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))


# ---------------------------------------------------------------- evaluation
def _group_files(edits: list[dict], definition_files: list[str]) -> list[dict]:
    by_file: dict[str, list[dict]] = {}
    for e in edits:
        by_file.setdefault(e["file"], []).append({"line": e["line"], "action": e["action"], "why": e["why"]})
    order = definition_files + sorted(f for f in by_file if f not in definition_files)
    return [{"file": f, "edits": sorted(by_file[f], key=lambda e: (e["line"], e["action"]))}
            for f in order if f in by_file]


def _clean(site: dict) -> dict:
    keep = ("name", "file", "line", "reason", "fix", "status", "tests_reaching", "tests_mocking", "mutation", "note")
    return {k: site[k] for k in keep if k in site}


def evaluate_candidate(universe, target: str, candidate: dict[str, Any]) -> dict[str, Any]:
    """Apply every mutation of the candidate to the CLONED graph and report what breaks."""
    results = [evaluate_mutation(universe, m) for m in candidate["mutations"]]
    dead = [r for r in results if r["dead_end"]]
    sites = [s for r in results for s in r["sites"]]
    # silent breaks first: they are the ones nobody would notice (and what an agent's capped output must keep)
    sites.sort(key=lambda s: ({"masked": 0, "untested": 1, "caught": 2}[s["status"]], s["file"], s["line"]))
    caught = sum(s["status"] == "caught" for s in sites)
    masked = sum(s["status"] == "masked" for s in sites)
    untested = sum(s["status"] == "untested" for s in sites)
    lying = [l for r in results for l in r["lying"]]
    lying_tests = len({l["test"] for l in lying})
    edits = [e for r in results for e in r["edits"]]
    failing = sorted({t for s_ in sites for t in s_.get("_failing", [])})
    def_files = [e["file"] for e in edits if e["action"] == "edit_definition"]
    files = _group_files(edits, def_files)
    counts = {"broken": len(sites), "caught": caught, "masked": masked, "untested": untested,
              "silent": masked + untested, "lying_tests": lying_tests,
              "unverifiable": sum(r["unverifiable"] for r in results),
              "alias_debt": sum(r["alias_debt"] for r in results),
              "files": len(files), "edits": len(edits), "tests_expected_to_fail": len(failing)}
    if dead:
        verdict, summary = "dead_end", "dead end: " + "; ".join(r["dead_end_reason"] for r in dead)
    elif not sites:
        verdict, summary = "safe", "no call site breaks"
    else:
        verdict = "risky" if counts["silent"] else "viable"
        summary = (f"{len(sites)} call site(s) break: {caught} caught by real tests, {masked} masked by mocks, "
                   f"{untested} untested; {lying_tests} test(s) would stay green falsely; edit {len(files)} file(s)")
    if counts["unverifiable"] and not dead:
        summary += f"; {counts['unverifiable']} call site(s) use *args/**kwargs and need a manual check"
    return {
        "verdict": verdict, "summary": summary, "counts": counts,
        "changes": [r["change"] for r in results],
        "dead_end_reason": "; ".join(r["dead_end_reason"] for r in dead),
        "suggestions": [s for r in dead for s in r.get("suggestions", [])],
        "expected_failing_tests": failing,
        "broken_callers": [_clean(s) for s in sites],
        "lying_mocks": lying,
        "files_to_edit": files,
    }


def score(ev: dict[str, Any]) -> float:
    c = ev["counts"]
    return round(W_CAUGHT * c["caught"] + W_SILENT * c["silent"] + W_LYING_TEST * c["lying_tests"]
                 + W_FILE * c["files"] + W_ALIAS * c["alias_debt"] + W_UNVERIFIABLE * c["unverifiable"], 2)


def annotate_clone(g, cand: dict[str, Any], ev: dict[str, Any]) -> None:
    """Demo mode only: label the clone so the FalkorDB Browser (which colors nodes by label) tells the story.

    Changed        the function this plan modifies
    CaughtBreak    a caller that breaks, and a real test would fail
    MaskedBreak    a caller that breaks while every test that reaches it mocks the call: tests stay green
    UntestedBreak  a caller that breaks and no test reaches
    LyingTest      a test that mocks a broken call and stays green
    ExpectedToFail a test the tool expects to fail
    Broken calls also get k.broken = true, k.status and k.reason on the CALLS edge."""
    try:
        g.query("UNWIND $t AS q MATCH (f:Function {qname:q}) SET f:Changed",
                params={"t": [m["target"] for m in cand["mutations"]]})
        for status, label in (("caught", "CaughtBreak"), ("masked", "MaskedBreak"), ("untested", "UntestedBreak")):
            names = sorted({s["name"] for s in ev["broken_callers"] if s["status"] == status})
            if names:
                g.query(f"UNWIND $n AS q MATCH (c:Function {{qname:q}}) SET c:{label}", params={"n": names})
        edges = [{"c": s["name"], "line": s["line"], "t": s["mutation"], "s": s["status"], "r": s["reason"]}
                 for s in ev["broken_callers"]]
        if edges:
            g.query("UNWIND $e AS r MATCH (c:Function {qname:r.c})-[k:CALLS {line:r.line}]->(f:Function {qname:r.t}) "
                    "SET k.broken = true, k.status = r.s, k.reason = r.r", params={"e": edges})
        for label, names in (("LyingTest", sorted({l["test"] for l in ev["lying_mocks"]})),
                             ("ExpectedToFail", ev["expected_failing_tests"])):
            if names:
                g.query(f"UNWIND $n AS q MATCH (t:Function {{qname:q}}) SET t:{label}", params={"n": names})
    except Exception:  # noqa: BLE001
        log.exception("could not label the clone (demo mode only)")


CLONE_QUERIES = [
    ("What did this plan change?", "MATCH (f:Changed) RETURN f"),
    ("What breaks? (picture)", "MATCH (c)-[k:CALLS]->(f:Changed) WHERE k.broken = true RETURN c,k,f LIMIT 100"),
    ("Silent breaks and the mocks that hide them",
     "MATCH (c)-[k:CALLS]->(f:Changed) WHERE k.status IN ['masked','untested'] "
     "OPTIONAL MATCH (t:LyingTest)-[m:MOCKS]->(f) RETURN c,k,f,t,m LIMIT 100"),
    ("How a failing test reaches a broken call",
     "MATCH p=(t:ExpectedToFail)-[:CALLS|REFERENCES*1..3]->(c)-[:CALLS {broken:true}]->(f:Changed) RETURN p LIMIT 25"),
]


# ---------------------------------------------------------------- orchestration
def _run_one(canon: str, run_id: str, index: int, target: str, cand: dict[str, Any]) -> dict[str, Any]:
    slug = re.sub(r"\W+", "_", cand["name"]).strip("_")[:24]
    name = f"{UNIVERSE_PREFIX}{run_id}_{index}_{slug}"
    out: dict[str, Any] = {"candidate": cand["name"], "description": cand.get("description", ""), "universe": name}
    universe = None
    t_total = time.perf_counter()
    try:
        universe, out["copy_ms"], out["copy_wait_ms"] = _copy_graph(canon, name)  # GRAPH.COPY (serialized)
        emit("clone_created", run=run_id, candidate=cand["name"], graph=name, copy_ms=out["copy_ms"],
             wait_ms=out["copy_wait_ms"])
        t = time.perf_counter()
        ev = evaluate_candidate(universe, target, cand)
        out["eval_ms"] = round((time.perf_counter() - t) * 1000, 2)
        out.update(ev)
        out["score"] = score(ev)
        kept = keep_universes()
        if kept:
            annotate_clone(universe, cand, ev)
        emit("candidate_evaluated", run=run_id, candidate=cand["name"], graph=name, eval_ms=out["eval_ms"],
             verdict=ev["verdict"], score=out["score"], counts=ev["counts"], summary=ev["summary"],
             changes=ev["changes"], dead_end_reason=ev["dead_end_reason"],
             broken=[{k: b.get(k) for k in ("name", "file", "line", "status", "reason", "tests_reaching", "tests_mocking")}
                     for b in ev["broken_callers"][:12]],
             broken_total=len(ev["broken_callers"]),
             lying=[{"test": l["test"], "hides": len(l["hides"]), "mock_file": l["mock_file"], "mock_line": l["mock_line"]}
                    for l in ev["lying_mocks"][:6]],
             queries=[{"title": t, "cypher": c} for t, c in CLONE_QUERIES] if kept else [])
    except Exception as exc:  # noqa: BLE001
        log.exception("universe %s failed", name)
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if universe is not None:
            if keep_universes():
                emit("clone_kept", run=run_id, candidate=cand["name"], graph=name)
            else:
                try:
                    universe.delete()
                    emit("clone_deleted", run=run_id, candidate=cand["name"], graph=name)
                except Exception:  # noqa: BLE001
                    log.exception("could not delete %s (startup sweep will retry)", name)
    out["total_ms"] = round((time.perf_counter() - t_total) * 1000, 2)
    return out


def run_simulation(canon: str, target: str, candidates: list[dict[str, Any]],
                   run_id: str, parallel: bool = True) -> dict[str, Any]:
    """Fork one universe per candidate, evaluate, delete, then rank (dead ends last)."""
    t0 = time.perf_counter()
    if keep_universes():
        sweep_universes()          # remove the clones of the previous run, keep this run's
    emit("simulation_start", run=run_id, target=target, graph=canon, candidates=[c["name"] for c in candidates])
    jobs = [(canon, run_id, i, target, c) for i, c in enumerate(candidates, 1)]
    if parallel and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=min(len(jobs), 5)) as pool:
            results = list(pool.map(lambda a: _run_one(*a), jobs))
    else:
        results = [_run_one(*a) for a in jobs]

    ok = [r for r in results if "error" not in r and r.get("verdict") != "dead_end"]
    dead = [r for r in results if r not in ok]
    ok.sort(key=lambda r: (r["score"], r["counts"]["edits"]))
    ranked = ok + dead
    winner = ok[0] if ok else None
    why = ""
    if winner:
        why = f"lowest risk score ({winner['score']})"
        if len(ok) > 1:
            why += f" vs {ok[1]['score']} for '{ok[1]['candidate']}'"
        if winner["verdict"] == "safe":
            why += "; nothing breaks"
    comparison = [{"candidate": r["candidate"], "verdict": r.get("verdict", "error"), "score": r.get("score"),
                   "broken": r.get("counts", {}).get("broken"), "caught": r.get("counts", {}).get("caught"),
                   "silent": r.get("counts", {}).get("silent"), "lying_tests": r.get("counts", {}).get("lying_tests"),
                   "files": r.get("counts", {}).get("files"), "summary": r.get("summary", r.get("error", ""))}
                  for r in ranked]
    return {
        "run_id": run_id, "winner": winner["candidate"] if winner else None, "why": why,
        "comparison": comparison, "ranked": ranked,
        "checklist": winner.get("files_to_edit", []) if winner else [],
        "scoring": {"caught": W_CAUGHT, "silent": W_SILENT, "lying_test": W_LYING_TEST, "file": W_FILE,
                    "alias": W_ALIAS, "unverifiable": W_UNVERIFIABLE},
        "wall_ms": round((time.perf_counter() - t0) * 1000, 2), "parallel": parallel,
    }
