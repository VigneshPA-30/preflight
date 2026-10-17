"""MCP server: the thin layer Claude calls. Logic lives in core.py / simulate.py.

RULE: stdout belongs to the MCP protocol. Never print(). Log to stderr only.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, model_validator

logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("PREFLIGHT_LOG", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("preflight")

from preflight import core, simulate  # noqa: E402  (after logging setup)
from preflight.db import (  # noqa: E402
    UNIVERSE_PREFIX, canon_name, get_db, graph_exists, sweep_universes,
)

mcp = FastMCP(
    "preflight",
    instructions=(
        "Pre-flight checks for code changes. Use BEFORE editing files. "
        "1) sync_workspace_graph once per session and after edits. "
        "2) inspect_blast_radius before changing any function. "
        "3) simulate_refactor_decisions when choosing between 2-5 refactoring "
        "approaches; it tests each on a cloned graph and returns the safest "
        "approach plus a file/line checklist. Use fully qualified names, "
        "e.g. 'cookiecutter.main.generate_context'. Paths must be Linux paths."
    ),
)

_active_workspace: Path | None = None
_QNAME = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
MAX_ITEMS = 25  # keep tool output small: it costs the agent's context window

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


# ---------- input models (this is the "mutation list" contract) ----------
class Mutation(BaseModel):
    kind: Literal["change_params", "rename"] = Field(
        description="change_params: new parameter list. rename: new function name.")
    target: str = Field(description="Qualified function name, e.g. 'pkg.mod.func' or 'pkg.mod.Class.method'")
    new_params: list[str] | None = Field(
        None, description="For change_params: the COMPLETE new parameter list, in order, without self/cls. "
        "Append '=' to a name that has a default ('debug='). Use '*' before keyword-only parameters, "
        "'*args' and '**kwargs' for variadics. Example: ['path', 'mode=', '*', 'strict=']. [] means no parameters.")
    new_name: str | None = Field(None, description="For rename: the new function name (no module path).")
    keep_alias: bool = Field(False, description="For rename: keep the old name working as a deprecated alias.")

    @model_validator(mode="after")
    def _check(self) -> "Mutation":
        if not _QNAME.match(self.target):
            raise ValueError(f"target '{self.target}' is not a qualified Python name")
        need = {"change_params": "new_params", "rename": "new_name"}
        if getattr(self, need[self.kind]) is None:
            raise ValueError(f"kind '{self.kind}' needs '{need[self.kind]}'")
        return self


class Candidate(BaseModel):
    name: str = Field(description="Short label, e.g. 'safe_adapter' or 'full_migration'")
    description: str = ""
    mutations: list[Mutation] = Field(min_length=1)


# ---------- helpers ----------
def _fail(msg: str, hint: str = "") -> dict[str, Any]:
    return {"ok": False, "error": msg, **({"hint": hint} if hint else {})}


def _resolve_workspace(path: str | None) -> Path:
    global _active_workspace
    if not path:
        if _active_workspace is None:
            raise ValueError("no workspace yet; pass workspace_path or run sync_workspace_graph first")
        return _active_workspace
    if re.match(r"^[A-Za-z]:[\\/]", path):
        raise ValueError("Windows path given; use the Linux path, e.g. /home/<user>/... or /mnt/c/...")
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"not a directory: {p}")
    roots = [r for r in os.environ.get("PREFLIGHT_ALLOWED_ROOTS", "").split(":") if r]
    if roots and not any(p.is_relative_to(Path(r).resolve()) for r in roots):
        raise ValueError(f"{p} is outside PREFLIGHT_ALLOWED_ROOTS")
    return p


AUTOSYNC = os.environ.get("PREFLIGHT_AUTOSYNC", "1") != "0"


def _ensure_graph(ws: Path) -> tuple[str, dict[str, Any]]:
    """Make sure the graph exists and is fresh. A sync with no changes is cheap (it only re-hashes)."""
    global _active_workspace
    canon = canon_name(ws)
    sync: dict[str, Any] = {}
    if AUTOSYNC or not graph_exists(canon):
        r = core.sync_workspace(ws, canon)
        sync = {"status": r["status"], "files_changed": r["files_changed"]}
    _active_workspace = ws
    return canon, sync


def _check_name(name: str) -> None:
    if not _QNAME.match(name):
        raise ValueError(f"'{name}' is not a qualified Python name (example: pkg.mod.func)")


def _cap(items: list, n: int = MAX_ITEMS) -> tuple[list, int]:
    return items[:n], max(0, len(items) - n)


# ---------- tools ----------
@mcp.tool(annotations=WRITE)
def check_graph_engine() -> dict[str, Any]:
    """Health check. Creates a tiny graph, clones it with GRAPH.COPY, deletes both,
    and returns real timings. Use it to confirm the engine works."""
    db = get_db()
    run = uuid.uuid4().hex[:6]
    src_name, dst_name = f"{UNIVERSE_PREFIX}ping_{run}_a", f"{UNIVERSE_PREFIX}ping_{run}_b"
    src = clone = None
    try:
        src = db.select_graph(src_name)
        src.query("CREATE (:F {n:'a'})-[:CALLS]->(:F {n:'b'})")
        t = time.perf_counter()
        clone = src.copy(dst_name)
        copy_ms = (time.perf_counter() - t) * 1000
        clone.query("CREATE (:F {n:'only_in_clone'})")  # must not touch the source
        src_n = src.ro_query("MATCH (n:F) RETURN count(n)").result_set[0][0]
        clone_n = clone.ro_query("MATCH (n:F) RETURN count(n)").result_set[0][0]
        return {"ok": True, "copy_ms": round(copy_ms, 2),
                "source_nodes": src_n, "clone_nodes": clone_n,
                "isolated": src_n == 2 and clone_n == 3}
    except Exception as exc:  # noqa: BLE001
        log.exception("check_graph_engine failed")
        return _fail(f"{type(exc).__name__}: {exc}", "GRAPH.COPY or the embedded engine failed")
    finally:
        for g in (clone, src):
            if g is not None:
                try:
                    g.delete()
                except Exception:  # noqa: BLE001
                    log.exception("cleanup failed")


@mcp.tool(annotations=WRITE)
def sync_workspace_graph(workspace_path: str) -> dict[str, Any]:
    """Load or update the code graph for a workspace (only changed files are re-parsed).
    Call once per session and again after files change."""
    global _active_workspace
    try:
        ws = _resolve_workspace(workspace_path)
        t = time.perf_counter()
        stats = core.sync_workspace(ws, canon_name(ws))
        _active_workspace = ws
        return {"ok": True, "workspace": str(ws), **stats,
                "elapsed_ms": round((time.perf_counter() - t) * 1000, 2)}
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("sync failed")
        return _fail(f"{type(exc).__name__}: {exc}")


@mcp.tool(annotations=READ)
def inspect_blast_radius(function_name: str, max_hops: int = 5,
                         workspace_path: str | None = None) -> dict[str, Any]:
    """Show who is affected if a function changes: callers hop by hop, and tests that
    mock it (a mocked test can stay green while real callers break).
    function_name must be qualified, e.g. 'cookiecutter.main.generate_context'."""
    try:
        _check_name(function_name)
        max_hops = max(1, min(int(max_hops), 8))
        ws = _resolve_workspace(workspace_path)
        canon, sync = _ensure_graph(ws)
        res = core.blast_radius(canon, function_name, max_hops, max_items=MAX_ITEMS)
        return {"ok": True, **res, "synced": sync}
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("blast radius failed")
        return _fail(f"{type(exc).__name__}: {exc}")


@mcp.tool(annotations=WRITE)
def simulate_refactor_decisions(target_func: str, candidates: list[Candidate],
                                workspace_path: str | None = None) -> dict[str, Any]:
    """Test 1-5 rival refactoring plans BEFORE editing any file. Each plan runs on its
    own cloned graph (GRAPH.COPY); clones are deleted afterwards. Returns plans ranked
    safest-first, dead ends, broken callers with their call path, lying mocks, and an
    ordered file/line checklist for the winner."""
    try:
        _check_name(target_func)
        if not 1 <= len(candidates) <= 5:
            return _fail("send between 1 and 5 candidates")
        names = [c.name for c in candidates]
        if len(set(names)) != len(names):
            return _fail("candidate names must be unique")
        ws = _resolve_workspace(workspace_path)
        canon, sync = _ensure_graph(ws)
        run_id = uuid.uuid4().hex[:8]
        parallel = os.environ.get("PREFLIGHT_PARALLEL", "1") != "0"
        out = simulate.run_simulation(
            canon, target_func, [c.model_dump() for c in candidates], run_id, parallel
        )
        for r in out["ranked"]:  # keep the output small: it goes into the agent's context window
            is_winner = r["candidate"] == out["winner"]
            for key, n in (("broken_callers", 25 if is_winner else 5), ("lying_mocks", 15 if is_winner else 3)):
                if key in r:
                    r[key], cut = _cap(r[key], n)
                    if cut:
                        r[f"{key}_omitted"] = cut
            if "expected_failing_tests" in r:
                r["expected_failing_tests"], cut = _cap(r["expected_failing_tests"], 40 if is_winner else 0)
                if cut:
                    r["expected_failing_tests_omitted"] = cut
            if not is_winner and "files_to_edit" in r:
                r["files_to_edit_count"] = len(r.pop("files_to_edit"))
        out["checklist"], cut = _cap(out["checklist"], 40)
        if cut:
            out["checklist_files_omitted"] = cut
        return {"ok": True, "target": target_func, **out, "synced": sync}
    except ValueError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("simulation failed")
        return _fail(f"{type(exc).__name__}: {exc}")


def main() -> None:
    try:
        removed = sweep_universes()
        log.info("startup sweep removed %d leftover universe graph(s)", removed)
    except Exception:  # noqa: BLE001
        log.exception("startup sweep failed (continuing)")
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
