"""Module 5-6: universe lifecycle (real) + candidate evaluation (stub).

Lifecycle per candidate:  GRAPH.COPY -> evaluate -> delete (always).
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from preflight.db import UNIVERSE_PREFIX, get_db

log = logging.getLogger("preflight.simulate")

# GRAPH.COPY forks the database process. Two forks at once fail with
# "could not fork", so clone creation is serialized. Evaluation still runs in parallel.
_copy_lock = threading.Lock()
COPY_RETRIES = 5


def _copy_graph(canon: str, name: str):
    graph = get_db().select_graph(canon)
    with _copy_lock:
        for attempt in range(COPY_RETRIES):
            try:
                return graph.copy(name)
            except Exception as exc:  # noqa: BLE001
                if "could not fork" not in str(exc) or attempt == COPY_RETRIES - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))  # a background save may hold the fork


# YOUR scoring formula. Lower score = safer. Keep it simple and explainable.
W_BROKEN_CALLER = 3.0
W_LYING_MOCK = 2.0
W_FILE_TOUCHED = 0.25


def evaluate_candidate(universe, target: str, candidate: dict[str, Any]) -> dict[str, Any]:
    """MODULE 5 (the hard part). Apply the candidate's mutations to the
    CLONED graph `universe`, then check the rules.

    Return:
      {"verdict": "viable" | "dead_end" | "unknown",
       "broken_callers": [{"name","file","line","path":[...],"reason"}],
       "lying_mocks":   [{"test","file","line","mocks"}],
       "files_to_edit": [{"file","lines":[...],"why"}]}
    """
    return {
        "status": "stub",
        "verdict": "unknown",
        "broken_callers": [],
        "lying_mocks": [],
        "files_to_edit": [],
    }


def score(ev: dict[str, Any]) -> float:
    return (
        W_BROKEN_CALLER * len(ev.get("broken_callers", []))
        + W_LYING_MOCK * len(ev.get("lying_mocks", []))
        + W_FILE_TOUCHED * len(ev.get("files_to_edit", []))
    )


def _run_one(canon: str, run_id: str, index: int, target: str, cand: dict[str, Any]) -> dict[str, Any]:
    name = f"{UNIVERSE_PREFIX}{run_id}_{index}"
    out: dict[str, Any] = {"candidate": cand["name"], "universe": name}
    universe = None
    t_total = time.perf_counter()
    try:
        t = time.perf_counter()
        universe = _copy_graph(canon, name)  # GRAPH.COPY (serialized)
        out["copy_ms"] = round((time.perf_counter() - t) * 1000, 2)

        t = time.perf_counter()
        ev = evaluate_candidate(universe, target, cand)
        out["eval_ms"] = round((time.perf_counter() - t) * 1000, 2)

        out.update(ev)
        out["score"] = round(score(ev), 2)
    except Exception as exc:  # noqa: BLE001
        log.exception("universe %s failed", name)
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if universe is not None:
            try:
                universe.delete()
            except Exception:  # noqa: BLE001
                log.exception("could not delete %s (startup sweep will retry)", name)
    out["total_ms"] = round((time.perf_counter() - t_total) * 1000, 2)
    return out


def run_simulation(canon: str, target: str, candidates: list[dict[str, Any]],
                   run_id: str, parallel: bool = True) -> dict[str, Any]:
    """Fork one universe per candidate, evaluate, delete, then rank."""
    t0 = time.perf_counter()
    jobs = [(canon, run_id, i, target, c) for i, c in enumerate(candidates, 1)]
    if parallel and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=min(len(jobs), 5)) as pool:
            results = list(pool.map(lambda a: _run_one(*a), jobs))
    else:
        results = [_run_one(*a) for a in jobs]

    viable = [r for r in results if "error" not in r and r.get("verdict") != "dead_end"]
    dead = [r for r in results if r not in viable]
    viable.sort(key=lambda r: r["score"])
    ranked = viable + dead
    winner = viable[0] if viable else None
    return {
        "run_id": run_id,
        "ranked": ranked,
        "winner": winner["candidate"] if winner else None,
        "checklist": winner.get("files_to_edit", []) if winner else [],
        "wall_ms": round((time.perf_counter() - t0) * 1000, 2),
        "parallel": parallel,
    }
