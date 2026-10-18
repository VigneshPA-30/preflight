"""Compare rival changes to one function, from the terminal. No agent needed.

    uv run python -m preflight.simulate_cli ~/work/myproject pkg.mod.func \\
        --plan "optional=add:strict=" --plan "required=add:strict" --plan "rename=rename:better_name"

A plan is  NAME=ACTION  where ACTION is one of
    add:new_param          add a REQUIRED parameter          add:new_param=   add an OPTIONAL one (has a default)
    remove:param           remove a parameter
    params:a,b=,*,k        the COMPLETE new parameter list ('=' marks a default, '*' starts keyword-only ones)
    rename:new_name        rename the function              rename+alias:new_name   rename and keep the old name working
Parameters never include self/cls. Nothing in your project is changed.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import uuid
from pathlib import Path

from redislite.falkordb_client import FalkorDB

import preflight.db as D
from preflight.mutations import _node
from preflight.queries import suggest_names
from preflight.ingestion.python.pipeline import sync_workspace
from preflight.simulate import run_simulation


def _spec_from_node(node: dict) -> list[str]:
    """The function's current signature in the same notation as `params:`."""
    spec = [p if p in node["required"] else p + "=" for p in node["params"]]
    if node["has_varargs"]:
        spec.append("*args")
    elif node["kwonly"]:
        spec.append("*")
    spec += [k if k in node["required"] else k + "=" for k in node["kwonly"]]
    if node["has_varkw"]:
        spec.append("**kwargs")
    return spec


def build_mutation(target: str, action: str, node: dict) -> dict:
    kind, _, arg = action.partition(":")
    arg = arg.strip()
    if kind in {"rename", "rename+alias"} and arg:
        return {"kind": "rename", "target": target, "new_name": arg, "keep_alias": kind == "rename+alias"}
    spec = _spec_from_node(node)
    if kind == "params":
        return {"kind": "change_params", "target": target, "new_params": [p.strip() for p in arg.split(",") if p.strip()]}
    if kind == "add" and arg:
        first_special = next((i for i, p in enumerate(spec) if p.startswith("*")), len(spec))
        return {"kind": "change_params", "target": target, "new_params": spec[:first_special] + [arg] + spec[first_special:]}
    if kind == "remove" and arg:
        name = arg.rstrip("=")
        kept = [p for p in spec if p.rstrip("=") != name]
        if len(kept) == len(spec):
            raise ValueError(f"'{name}' is not a parameter of {target} (parameters: {', '.join(spec) or 'none'})")
        return {"kind": "change_params", "target": target, "new_params": kept}
    raise ValueError(f"cannot read the plan action '{action}'. Use add:, remove:, params:, rename: or rename+alias:")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("workspace", help="folder of the Python project")
    ap.add_argument("function", help="dotted name, e.g. mypackage.module.function or mypackage.module.Class.method")
    ap.add_argument("--plan", action="append", required=True, metavar="NAME=ACTION", help="repeat for 1 to 5 plans")
    ap.add_argument("--sites", type=int, default=6, help="broken call sites to list per plan (default 6)")
    a = ap.parse_args(argv)

    ws = Path(a.workspace).expanduser().resolve()
    if not ws.is_dir():
        print(f"not a folder: {ws}")
        return 2
    if not 1 <= len(a.plan) <= 5:
        print("give between 1 and 5 --plan options")
        return 2
    db = FalkorDB(str(Path(tempfile.mkdtemp()) / "sim.db"))
    D._db = db
    canon = D.canon_name(ws)
    rep = sync_workspace(ws, canon, db=db)
    node = _node(db.select_graph(canon), a.function)
    if node is None:
        print(f"function not found: {a.function}")
        print("did you mean: " + ", ".join(suggest_names(db.select_graph(canon), a.function)))
        return 1
    cands = []
    for p in a.plan:
        name, _, action = p.partition("=")
        if not name or not action:
            print(f"plan must look like NAME=ACTION, got: {p}")
            return 2
        try:
            cands.append({"name": name.strip(), "mutations": [build_mutation(a.function, action.strip(), node)]})
        except ValueError as exc:
            print(f"plan '{name}': {exc}")
            return 2

    out = run_simulation(canon, a.function, cands, uuid.uuid4().hex[:8], parallel=True)
    cur = ", ".join(_spec_from_node(node))
    print(f"\n{a.function}({cur})   {node['file']}:{node['line']}")
    print(f"project: {rep['files_total']} files, {rep['functions']} functions, {rep['tests']} tests | "
          f"{rep['calls_resolved']} calls resolved ({100 - rep['unresolved_call_pct']:.1f}% of project-internal calls)")
    print(f"\n{len(cands)} plan(s) tried on cloned graphs in {out['wall_ms']:.0f} ms, safest first:\n")
    for i, c in enumerate(out["comparison"], 1):
        print(f"  {i}. {c['candidate']:<18}{c['verdict']:<9}score {str(c['score']):<7}{c['summary']}")
    if out["winner"]:
        print(f"\nWinner: {out['winner']} ({out['why']})")
    for r in out["ranked"]:
        if r.get("verdict") in {"dead_end"} or r.get("error"):
            continue
        sites = r["broken_callers"]
        if not sites:
            continue
        print(f"\n--- {r['candidate']}: {len(sites)} call site(s) break (silent ones first) ---")
        for s in sites[: a.sites]:
            print(f"  [{s['status']:<8}] {s['name']}  {s['file']}:{s['line']}\n             {s['reason']}  "
                  f"({s['tests_reaching']} test(s) reach it, {s['tests_mocking']} mock it)")
        if len(sites) > a.sites:
            print(f"  ... and {len(sites) - a.sites} more (use --sites N)")
        print(f"  tests expected to fail: {r['counts']['tests_expected_to_fail']}")
    if out["winner"]:
        win = next(r for r in out["ranked"] if r["candidate"] == out["winner"])
        print(f"\nEdit checklist for '{out['winner']}':")
        for f in win["files_to_edit"][:10]:
            for e in f["edits"][:4]:
                print(f"  {f['file']}:{e['line']}  [{e['action']}]  {e['why']}")
    print("\ncaught = a real test will fail | masked = tests stay green because they mock the call | untested = no test reaches it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
