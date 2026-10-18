"""MODULE 5: apply ONE candidate change to a CLONED graph and see what breaks.

The clone (a GRAPH.COPY universe) is changed with Cypher SET; the canonical graph is never touched.
Supported mutation kinds:
  change_params  new_params=["a", "b=", "*args", "*", "kw", "**opts"]   ("=" marks a default)
  rename         new_name="better_name"   (keep_alias=True keeps the old name working)

Every broken call site gets a STATUS, which is the point of this tool:
  caught    a test that really runs the call would fail (the agent's test loop will see it)
  masked    every test that reaches it mocks the call away -> tests stay GREEN, production breaks
  untested  no test reaches it at all                        -> nobody will notice
"""
from __future__ import annotations

import keyword
import re
from dataclasses import dataclass, field
from typing import Any

from preflight.queries import reach_map, suggest_names

IDENT = re.compile(r"^[A-Za-z_]\w*$")

# ---------------------------------------------------------------- queries
Q_NODE = ("MATCH (f:Function {qname:$q}) RETURN f.file, f.line, f.params, f.kwonly, f.required, "
          "f.has_varargs, f.has_varkw, f.module, f.is_method")

# A call site breaks when the call no longer fits the signature (Python's own rules).
# Validated against the real interpreter on 1000 random calls (tests/test_mutations.py).
Q_SIG_BREAKS = """
MATCH (c:Function)-[k:CALLS]->(f:Function {qname:$q})
WHERE k.dynamic_args = false
WITH c, k, f,
     k.n_pos > size(f.params) AND NOT f.has_varargs AS too_many,
     [kw IN k.kw_names WHERE NOT kw IN f.params AND NOT kw IN f.kwonly] AS unknown,
     [kw IN k.kw_names WHERE kw IN f.params[0..k.n_pos]] AS dup,
     [r IN f.required WHERE NOT r IN f.params[0..k.n_pos] AND NOT r IN k.kw_names] AS missing
WHERE too_many OR (size(unknown) > 0 AND NOT f.has_varkw) OR size(dup) > 0 OR size(missing) > 0
RETURN c.qname, c.file, k.line, k.ns, c.is_test, c.in_test_file, too_many,
       CASE WHEN f.has_varkw THEN [] ELSE unknown END, dup, missing, k.n_pos, c.module
ORDER BY c.file, k.line
"""
Q_ALL_SITES = """
MATCH (c:Function)-[k:CALLS]->(f:Function {qname:$q})
RETURN c.qname, c.file, k.line, k.ns, c.is_test, c.in_test_file, c.module
ORDER BY c.file, k.line
"""
Q_REACH = ("UNWIND $cs AS cq MATCH (c:Function {qname: cq}) "
           "OPTIONAL MATCH (t:Function {is_test:true})-[:CALLS|REFERENCES*1..4]->(c) RETURN cq, collect(DISTINCT t.qname)")
Q_IMPORTERS = ("MATCH (tm:Module)-[:IMPORTS*1..6]->(m:Module {qname:$m}) RETURN DISTINCT tm.qname")
Q_TEST_PATCH_NS = "MATCH (t:Function {is_test:true})-[m:MOCKS]->() RETURN DISTINCT t.qname, m.ns"
Q_ALL_TESTS = "MATCH (t:Function {is_test:true}) RETURN t.qname, t.module"
Q_MOCKS = ("MATCH (t:Function)-[m:MOCKS]->(f:Function {qname:$q}) "
           "RETURN t.qname, m.ns, m.file, m.line, m.via, m.scope, t.file")


# ---------------------------------------------------------------- signatures
@dataclass
class Sig:
    params: list[str] = field(default_factory=list)
    kwonly: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)
    has_varargs: bool = False
    has_varkw: bool = False
    error: str = ""

    def text(self) -> str:
        parts = [p + ("" if p in self.required else "=…") for p in self.params]
        if self.has_varargs:
            parts.append("*args")
        elif self.kwonly:
            parts.append("*")
        parts += [k + ("" if k in self.required else "=…") for k in self.kwonly]
        if self.has_varkw:
            parts.append("**kw")
        return "(" + ", ".join(parts) + ")"


def parse_signature(spec: list[str]) -> Sig:
    s, seen, saw_default, after_star = Sig(), set(), False, False
    for i, raw in enumerate(spec):
        tok = raw.strip()
        if s.has_varkw:
            s.error = "'**' parameter must be last"
            return s
        if tok.startswith("**"):
            name, kind = tok[2:], "kw"
        elif tok == "*":
            after_star = True
            continue
        elif tok.startswith("*"):
            name, kind = tok[1:], "var"
        else:
            name, kind = tok.split("=")[0].strip(), "named"
        if not IDENT.match(name) or keyword.iskeyword(name):
            s.error = f"'{name}' is not a valid parameter name"
            return s
        if name in seen:
            s.error = f"duplicate parameter '{name}'"
            return s
        seen.add(name)
        if kind == "kw":
            s.has_varkw = True
        elif kind == "var":
            s.has_varargs, after_star = True, True
        else:
            has_default = "=" in tok
            if after_star:
                s.kwonly.append(name)
            else:
                if not has_default and saw_default:
                    s.error = f"non-default parameter '{name}' follows a default parameter"
                    return s
                saw_default = saw_default or has_default
                s.params.append(name)
            if not has_default:
                s.required.append(name)
    return s


# ---------------------------------------------------------------- helpers
def _node(g, q: str) -> dict | None:
    r = g.ro_query(Q_NODE, params={"q": q}).result_set
    if not r:
        return None
    f, line, params, kwonly, required, va, vk, module, is_method = r[0]
    return {"file": f, "line": line, "params": params, "kwonly": kwonly, "required": required,
            "has_varargs": va, "has_varkw": vk, "module": module, "is_method": is_method}


def _dead(m: dict, why: str, g=None) -> dict:
    out = {"dead_end": True, "dead_end_reason": why, "sites": [], "edits": [], "lying": [],
           "unverifiable": 0, "alias_debt": 0, "change": f"{m['kind']} {m['target']}"}
    if g is not None and "not found" in why:
        out["suggestions"] = suggest_names(g, m["target"], 3)
    return out


def import_failures(g, module: str) -> list[str]:
    """Tests that cannot even be imported when `module` fails to import.
    A conftest.py that fails to import takes down every test in its folder."""
    mods = {module} | {r[0] for r in g.ro_query(Q_IMPORTERS, params={"m": module}).result_set}
    conftest_pkgs = tuple(m.rsplit(".", 1)[0] + "." for m in mods if m.endswith(".conftest"))
    out = []
    for t, tmod in g.ro_query(Q_ALL_TESTS).result_set:
        if tmod in mods or (conftest_pkgs and tmod.startswith(conftest_pkgs)):
            out.append(t)
    for t, ns in g.ro_query(Q_TEST_PATCH_NS).result_set:     # patch("pkg.mod.x") must IMPORT pkg.mod first
        if ns and ns.rpartition(".")[0] in mods and t not in out:
            out.append(t)
    return sorted(out)


def _statuses(g, target: str, sites: list[dict], target_module: str) -> list[dict]:
    """Give every broken site a status (caught | masked | untested) and collect the lying tests."""
    callers = sorted({s["name"] for s in sites})
    reach = reach_map(g, callers)
    mocks_by_ns: dict[str, list[dict]] = {}
    for t, ns, mfile, mline, via, scope, tfile in g.ro_query(Q_MOCKS, params={"q": target}).result_set:
        mocks_by_ns.setdefault(ns, []).append({"test": t, "file": mfile, "line": mline, "via": via,
                                               "scope": scope, "test_file": tfile})
    lying: dict[tuple, dict] = {}
    for s in sites:
        reaching = reach[s["name"]] | ({s["name"]} if s["is_test"] else set())
        mockers = [m for m in mocks_by_ns.get(s["ns"], []) if m["test"] in reaching]
        masking = {m["test"] for m in mockers}
        real = reaching - masking
        s["_failing"] = sorted(real)                      # tests that run this call for real
        s["tests_reaching"], s["tests_mocking"] = len(reaching), len(masking)
        s["status"] = "untested" if not reaching else ("caught" if real else "masked")
        if s.pop("import_binding", False):
            s["status"] = "caught"          # a renamed `from x import f` fails at import time: always loud,
            s["note"] = "import fails at import time (loud)"
            s["_failing"] = import_failures(g, s["module"])   # every test whose module imports it fails to load
            continue                        # so a mock cannot hide it: these tests do NOT stay green
        for m in mockers:
            e = lying.setdefault((m["test"], m["file"], m["line"]),
                                 {"test": m["test"], "mock_file": m["file"], "mock_line": m["line"],
                                  "via": m["via"], "scope": m["scope"], "hides": []})
            e["hides"].append({"caller": s["name"], "file": s["file"], "line": s["line"]})
    return list(lying.values())


def _edit_entries(sites: list[dict], definition: dict | None) -> list[dict]:
    edits = []
    if definition:
        edits.append(definition)
    for s in sites:
        edits.append({"file": s["file"], "line": s["line"],
                      "action": "fix_test_call_site" if s["in_test_file"] else "fix_call_site",
                      "why": f"{s['name']}: {s['reason']}. {s['fix']}"})
    for s in sites:
        if s["status"] in {"masked", "untested"} and not s["in_test_file"]:
            edits.append({"file": s["file"], "line": s["line"], "action": "add_real_test",
                          "why": f"{s['name']} is {s['status']}: add a test that calls it WITHOUT mocking "
                                 f"{s['callee_short']}"})
    return edits


# ---------------------------------------------------------------- change_params
def _change_params(g, m: dict) -> dict:
    q = m["target"]
    node = _node(g, q)
    if node is None:
        return _dead(m, f"target not found: {q}", g)
    if m.get("new_params") is None:
        return _dead(m, "change_params needs new_params")
    sig = parse_signature(m["new_params"])
    if sig.error:
        return _dead(m, f"invalid new signature: {sig.error}")
    old = Sig(node["params"], node["kwonly"], node["required"], node["has_varargs"], node["has_varkw"])
    before = {(r[0], r[2]) for r in g.ro_query(Q_SIG_BREAKS, params={"q": q}).result_set}
    g.query("MATCH (f:Function {qname:$q}) SET f.params=$p, f.kwonly=$k, f.required=$r, "
            "f.has_varargs=$va, f.has_varkw=$vk, f.mutated=true, f.old_params=$op, f.old_required=$orq",
            params={"q": q, "p": sig.params, "k": sig.kwonly, "r": sig.required,
                    "va": sig.has_varargs, "vk": sig.has_varkw, "op": node["params"], "orq": node["required"]})
    rows = [r for r in g.ro_query(Q_SIG_BREAKS, params={"q": q}).result_set if (r[0], r[2]) not in before]
    short = q.rsplit(".", 1)[-1]
    sites = []
    for c, cfile, line, ns, is_test, in_tf, too_many, unknown, dup, missing, n_pos, _mod in rows:
        why, fix = [], []
        if missing:
            why.append(f"missing required argument(s): {', '.join(missing)}")
            fix.append(f"pass {', '.join(missing)}")
        if too_many:
            why.append(f"passes {n_pos} positional argument(s) but the new signature takes {len(sig.params)}")
            fix.append("drop or convert the extra positional arguments")
        if unknown:
            why.append(f"unknown keyword(s): {', '.join(unknown)}")
            fix.append(f"remove or rename {', '.join(unknown)}")
        if dup:
            why.append(f"keyword(s) {', '.join(dup)} already given positionally")
            fix.append("pass each argument only once")
        sites.append({"name": c, "file": cfile, "line": line, "ns": ns, "is_test": is_test,
                      "in_test_file": in_tf, "reason": "; ".join(why), "fix": "; ".join(fix).capitalize() + ".",
                      "callee_short": short, "kind": "signature", "mutation": q})
    lying = _statuses(g, q, sites, node["module"]) if sites else []
    unver = g.ro_query("MATCH ()-[k:CALLS {dynamic_args:true}]->(f:Function {qname:$q}) RETURN count(k)",
                       params={"q": q}).result_set[0][0]
    definition = {"file": node["file"], "line": node["line"], "action": "edit_definition",
                  "why": f"change signature of {short}: {old.text()} -> {sig.text()}"}
    return {"dead_end": False, "sites": sites, "lying": lying, "unverifiable": unver, "alias_debt": 0,
            "edits": _edit_entries(sites, definition),
            "change": f"{short}{old.text()} -> {short}{sig.text()}"}


# ---------------------------------------------------------------- rename
def _rename(g, m: dict) -> dict:
    q = m["target"]
    node = _node(g, q)
    if node is None:
        return _dead(m, f"target not found: {q}", g)
    new = (m.get("new_name") or "").strip()
    old_name = q.rsplit(".", 1)[-1]
    if not IDENT.match(new) or keyword.iskeyword(new):
        return _dead(m, f"'{new}' is not a valid name")
    if new == old_name:
        return _dead(m, "new name equals the old name")
    new_q = q.rpartition(".")[0] + "." + new
    if g.ro_query("MATCH (x:Function {qname:$n}) RETURN count(x)", params={"n": new_q}).result_set[0][0]:
        return _dead(m, f"name collision: {new_q} already exists")
    definition = {"file": node["file"], "line": node["line"], "action": "edit_definition",
                  "why": f"rename {old_name} -> {new}"}
    if m.get("keep_alias"):
        definition["why"] += f" and keep `{old_name} = {new}` as a deprecated alias"
        return {"dead_end": False, "sites": [], "lying": [], "unverifiable": 0, "alias_debt": 1,
                "edits": [definition], "change": f"rename {old_name} -> {new} (alias kept)"}
    g.query("MATCH (f:Function {qname:$q}) SET f.renamed_to=$n, f.old_name=$o, f.mutated=true", params={"q": q, "n": new, "o": old_name})
    sites = []
    for c, cfile, line, ns, is_test, in_tf, cmod in g.ro_query(Q_ALL_SITES, params={"q": q}).result_set:
        sites.append({"name": c, "file": cfile, "line": line, "ns": ns, "is_test": is_test,
                      "in_test_file": in_tf, "reason": f"still calls the old name '{old_name}'",
                      "fix": f"Call '{new}' instead.", "callee_short": old_name, "kind": "renamed", "mutation": q,
                      "import_binding": ns.rpartition(".")[0] == cmod and cmod != node["module"], "module": cmod})
    lying = _statuses(g, q, sites, node["module"]) if sites else []
    edits = _edit_entries(sites, definition)
    imported = sorted({s["file"] for s in sites if s.get("note")})
    for f in imported:
        edits.append({"file": f, "line": 0, "action": "update_import",
                      "why": f"change the import of {old_name} to {new}"})
    seen = set()
    for t, ns, mfile, mline, via, scope, tfile in g.ro_query(Q_MOCKS, params={"q": q}).result_set:
        if (mfile, mline) not in seen:
            seen.add((mfile, mline))
            edits.append({"file": mfile, "line": mline, "action": "update_mock_target",
                          "why": f"patch target '{old_name}' no longer exists ({via}); update it to '{new}'"})
    return {"dead_end": False, "sites": sites, "lying": lying, "unverifiable": 0, "alias_debt": 0,
            "edits": edits, "change": f"rename {old_name} -> {new}"}


def evaluate_mutation(g, m: dict) -> dict:
    if m["kind"] == "change_params":
        return _change_params(g, m)
    if m["kind"] == "rename":
        return _rename(g, m)
    return _dead(m, f"mutation kind '{m['kind']}' is not supported yet (use change_params or rename)")
