"""MODULE 1: the graph schema, as code.

This file is the CONTRACT between three parts of the project:
  - ingest (module 2) builds the record types below and loads them,
  - queries (modules 3-4) read the labels, relations and properties,
  - simulation (module 5) changes those same properties on a cloned graph.
Change a name here and every part sees it. Never hard-code a label elsewhere.

Conventions (ingest MUST follow these):
  * qname  = full dotted name, e.g. "cookiecutter.main.generate_context";
             methods are "pkg.mod.Class.method". It is the unique key.
  * file   = path relative to the workspace root, with "/" separators.
  * Methods: `params` EXCLUDES the leading self/cls, because a call like
             obj.method(a) does not pass self. Set is_method=True.
  * Empty text is "" (never None). Empty lists are [].
  * A test is a Function with is_test=True (name starts with "test").
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

log = logging.getLogger("preflight.schema")


# ---------- names (the only place labels and relations are spelled) ----------
class Label:
    MODULE = "Module"
    FUNCTION = "Function"


class Rel:
    DEFINED_IN = "DEFINED_IN"  # (Function)-[:DEFINED_IN]->(Module)
    IMPORTS = "IMPORTS"        # (Module)-[:IMPORTS]->(Module)
    CALLS = "CALLS"            # (Function)-[:CALLS]->(Function), one per call site
    MOCKS = "MOCKS"            # (Function test)-[:MOCKS]->(Function)
    REFERENCES = "REFERENCES"  # a test-file function uses another function as a VALUE or fixture (not a call)


# ---------- the namespace rule (this is what makes lying-mock detection precise) ----------
# patch("a.b.f") replaces the NAME `f` inside module a.b. It does not replace the
# original function everywhere. So every call and every patch carries a namespace
# key `ns`, and a call is "mocked" only when call.ns == patch.ns:
#   call  f()        in module X          -> ns "X.f"      (X's own binding)
#   call  mod.f()    where mod is a.b     -> ns "a.b.f"    (looked up at call time)
#   call  self.m()   resolved to Cls.m    -> ns "pkg.Cls.m" (the method's qname)
#   patch("a.b.f")                        -> ns "a.b.f"
# A caller that did `from a.b import f` has ns "X.f", so patch("a.b.f") does NOT hit it.


# ---------- record types: what ingest produces ----------
@dataclass(slots=True)
class ModuleRec:
    qname: str            # "cookiecutter.main"
    file: str             # "cookiecutter/main.py"
    sha256: str = ""      # file hash; lets a no-change sync skip all work

    def to_props(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FunctionRec:
    qname: str
    name: str
    module: str                                   # qname of the Module it lives in
    file: str
    line: int
    end_line: int
    params: list[str] = field(default_factory=list)    # positional-capable names, in order
    kwonly: list[str] = field(default_factory=list)    # keyword-only names
    required: list[str] = field(default_factory=list)  # names (params+kwonly) with NO default
    has_varargs: bool = False                     # has *args
    has_varkw: bool = False                       # has **kwargs
    returns: str = ""                             # return annotation as text
    is_async: bool = False
    is_method: bool = False
    is_test: bool = False
    class_name: str = ""
    in_test_file: bool = False                    # lives in a test file (tests, helpers, fixtures)

    def to_props(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ImportRec:
    src: str   # module qname that imports
    dst: str   # module qname being imported

    def to_props(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CallRec:
    caller: str                                   # Function qname
    callee: str                                   # Function qname
    line: int
    n_pos: int = 0                                # count of positional arguments at the call
    kw_names: list[str] = field(default_factory=list)  # keyword names used at the call
    dynamic_args: bool = False                    # call uses *x or **x: arity cannot be checked
    ns: str = ""                                  # NAMESPACE KEY the call looked the name up in (see below)

    def to_props(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RefRec:
    """Not a call: `runner.invoke(main)` passes `main` as a value, a test requests a fixture by name.
    Tests reach code through these links, so reach queries follow CALLS and REFERENCES."""
    src: str
    dst: str
    line: int = 0

    def to_props(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MockRec:
    test: str      # Function qname of the test
    target: str    # Function qname being mocked
    line: int      # line of the patch statement
    via: str = ""  # "patch", "patch.object", "mocker.patch", "monkeypatch.setattr", ...
    ns: str = ""   # NAMESPACE KEY that is patched (see below)
    scope: str = ""  # where the patch lives: decorator | body | class_decorator | class_setup
                     #                        | fixture:<name> | autouse:<name>
    file: str = ""   # file that holds the patch statement

    def to_props(self) -> dict[str, Any]:
        return asdict(self)


# ---------- indexes ----------
# (label, property). Create BEFORE loading data, or the loader's MATCH is slow.
INDEXES: list[tuple[str, str]] = [
    (Label.FUNCTION, "qname"),
    (Label.FUNCTION, "file"),
    (Label.MODULE, "qname"),
]


def ensure_schema(graph) -> None:
    """Create the indexes. Safe to call many times."""
    for label, prop in INDEXES:
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception as exc:  # noqa: BLE001
            if "already indexed" not in str(exc):
                raise


# ---------- batched loaders (parameterised: never build Cypher from data) ----------
LOAD_MODULES = (
    "UNWIND $rows AS r "
    f"CREATE (:{Label.MODULE} {{qname: r.qname, file: r.file, sha256: r.sha256}})"
)
LOAD_FUNCTIONS = f"UNWIND $rows AS r CREATE (f:{Label.FUNCTION}) SET f = r"
LOAD_DEFINED_IN = (  # rows: FunctionRec props (needs qname + module)
    "UNWIND $rows AS r "
    f"MATCH (f:{Label.FUNCTION} {{qname: r.qname}}), (m:{Label.MODULE} {{qname: r.module}}) "
    f"CREATE (f)-[:{Rel.DEFINED_IN}]->(m)"
)
LOAD_IMPORTS = (
    "UNWIND $rows AS r "
    f"MATCH (a:{Label.MODULE} {{qname: r.src}}), (b:{Label.MODULE} {{qname: r.dst}}) "
    f"CREATE (a)-[:{Rel.IMPORTS}]->(b)"
)
LOAD_CALLS = (
    "UNWIND $rows AS r "
    f"MATCH (a:{Label.FUNCTION} {{qname: r.caller}}), (b:{Label.FUNCTION} {{qname: r.callee}}) "
    f"CREATE (a)-[:{Rel.CALLS} {{line: r.line, n_pos: r.n_pos, kw_names: r.kw_names, "
    "dynamic_args: r.dynamic_args, ns: r.ns}]->(b)"
)
LOAD_REFS = (
    "UNWIND $rows AS r "
    f"MATCH (a:{Label.FUNCTION} {{qname: r.src}}), (b:{Label.FUNCTION} {{qname: r.dst}}) "
    f"CREATE (a)-[:{Rel.REFERENCES} {{line: r.line}}]->(b)"
)
LOAD_MOCKS = (
    "UNWIND $rows AS r "
    f"MATCH (t:{Label.FUNCTION} {{qname: r.test}}), (f:{Label.FUNCTION} {{qname: r.target}}) "
    f"CREATE (t)-[:{Rel.MOCKS} {{line: r.line, via: r.via, ns: r.ns, scope: r.scope, file: r.file}}]->(f)"
)


# ---------- the core query ----------
# A LYING MOCK: test t patches name `ns` for function f, AND t reaches (or is) a
# caller c whose call to f goes through that same name. The call is replaced by a
# mock in that test, so the test stays green even if f's contract changes.
Q_LYING_MOCKS = (
    f"MATCH (t:{Label.FUNCTION} {{is_test: true}})-[m:{Rel.MOCKS}]->(f:{Label.FUNCTION} {{qname: $q}}) "
    f"MATCH (c:{Label.FUNCTION})-[k:{Rel.CALLS}]->(f) "
    f"WHERE k.ns = m.ns AND (c.qname = t.qname OR (t)-[:{Rel.CALLS}|{Rel.REFERENCES}*1..4]->(c)) "
    "RETURN DISTINCT t.qname AS test, t.file AS test_file, m.line AS mock_line, m.file AS mock_file, "
    "m.via AS via, m.scope AS scope, c.qname AS caller, c.file AS caller_file, k.line AS call_line "
    "ORDER BY test, caller"
)


def load_batch(graph, query: str, records: Iterable[Any], batch_size: int = 5000) -> int:
    """Run one loader query over records in batches. Returns rows sent.
    Rows whose endpoints do not exist are skipped by Cypher (MATCH finds nothing)."""
    rows = [r.to_props() for r in records]
    for i in range(0, len(rows), batch_size):
        graph.query(query, params={"rows": rows[i:i + batch_size]})
    return len(rows)


def reset_graph(db, name: str):
    """Delete the graph if it exists, make a fresh one with indexes, and return it.
    This is the 'full re-ingest' path: simple and safe for small repos."""
    if name in [n.decode() if isinstance(n, bytes) else n for n in db.list_graphs()]:
        db.select_graph(name).delete()
    graph = db.select_graph(name)
    # A graph key only exists after the first write; the index query creates it.
    ensure_schema(graph)
    return graph
