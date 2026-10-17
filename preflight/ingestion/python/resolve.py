"""Stage 2: turn raw names into graph records, using ALL files.

The two hard jobs:
  1. resolve_call:  "utils.foo" in module X  ->  callee qname + namespace key `ns`
  2. resolve_mock:  patch("a.b.f")           ->  target qname   + namespace key `ns`
A call is "mocked" only when both carry the same `ns` (see schema.py).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from posixpath import dirname

from preflight.schema import CallRec, FunctionRec, ImportRec, MockRec, ModuleRec, RefRec

from .extract import ClassFacts, FileFacts, FuncFacts, RawCall
from .mocks import RawMock


@dataclass
class Resolved:
    modules: list[ModuleRec] = field(default_factory=list)
    functions: list[FunctionRec] = field(default_factory=list)
    imports: list[ImportRec] = field(default_factory=list)
    calls: list[CallRec] = field(default_factory=list)
    mocks: list[MockRec] = field(default_factory=list)
    refs: list[RefRec] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)


class World:
    def __init__(self, files: list[FileFacts]) -> None:
        self.stats: Counter = Counter()
        self.files: dict[str, FileFacts] = {}
        for f in files:
            if f.error:
                self.stats["parse_errors"] += 1
                continue
            if f.src.module in self.files:
                self.stats["module_name_collisions"] += 1
                continue
            self.files[f.src.module] = f
        self.modules = set(self.files)
        self.roots = {m.split(".")[0] for m in self.modules}
        self.functions: dict[str, FuncFacts] = {}
        self.classes: dict[str, ClassFacts] = {}
        self.file_of_func: dict[str, FileFacts] = {}
        for f in self.files.values():
            for c in f.classes:
                self.classes.setdefault(c.qname, c)
            for fn in f.functions:
                if fn.rec.qname not in self.functions:
                    self.functions[fn.rec.qname] = fn
                    self.file_of_func[fn.rec.qname] = f
                else:
                    self.stats["duplicate_function_names"] += 1
        self._cache: dict[str, tuple[str, str] | None] = {}

    # ------------------------------------------------------------ name lookup
    def lookup(self, q: str) -> tuple[str, str] | None:
        """What does dotted name `q` refer to? ('module'|'class'|'function', canonical qname)."""
        if q not in self._cache:
            self._cache[q] = self._lookup(q, 0)
        return self._cache[q]

    def _lookup(self, q: str, depth: int) -> tuple[str, str] | None:
        if depth > 8 or not q:
            return None
        if q in self.functions:
            return "function", q
        if q in self.classes:
            return "class", q
        head, _, last = q.rpartition(".")
        if head:
            h = self._lookup(head, depth + 1)
            if h and h[0] == "module":
                mod = self.files[h[1]]
                if last in mod.aliases and last not in mod.defs:  # re-export: pkg/__init__ imports it
                    target = mod.aliases[last]
                    if target != q:
                        r = self._lookup(target, depth + 1)
                        if r:
                            return r
                elif last not in mod.defs:
                    for sm in mod.star_imports:      # `from .x import *` re-exports everything
                        r = self._lookup(f"{sm}.{last}", depth + 1)
                        if r:
                            return r
            elif h and h[0] == "class":
                m = self.find_method(h[1], last)
                return ("function", m) if m else None
        if q in self.modules:
            return "module", q
        return None

    def bind(self, f: FileFacts, name: str) -> str | None:
        """Qualified target of a bare name in file f: own definition, import, or star import."""
        q = f.defs.get(name) or f.aliases.get(name)
        if q:
            return q
        for sm in f.star_imports:
            if self.lookup(f"{sm}.{name}"):
                return f"{sm}.{name}"
        return None

    def resolve_expr(self, expr: str, module: str) -> tuple[str, str] | None:
        """Resolve a dotted expression as written inside `module`."""
        f = self.files.get(module)
        if f is None:
            return None
        parts = expr.split(".")
        base = self.bind(f, parts[0])
        if base is None:
            return None
        return self.lookup(".".join([base] + parts[1:]))

    def find_method(self, class_q: str, name: str, _seen: set[str] | None = None) -> str | None:
        seen = _seen if _seen is not None else set()
        if class_q in seen or len(seen) > 12:
            return None
        seen.add(class_q)
        c = self.classes.get(class_q)
        if c is None:
            return None
        if name in c.methods:
            return c.methods[name]
        for base in c.bases:
            r = self.resolve_expr(base, c.module)
            if r and r[0] == "class":
                m = self.find_method(r[1], name, seen)
                if m:
                    return m
        outer = class_q.rpartition(".")[0]
        if outer in self.classes and self.files[c.module].src.is_test_file:
            return self.find_method(outer, name, seen)   # pytest-relaxed nested classes
        return None

    def has_external_base(self, class_q: str, _seen: set[str] | None = None) -> bool:
        seen = _seen if _seen is not None else set()
        c = self.classes.get(class_q)
        if c is None or class_q in seen:
            return False
        seen.add(class_q)
        for base in c.bases:
            if base == "object":
                continue
            r = self.resolve_expr(base, c.module)
            if not (r and r[0] == "class") or self.has_external_base(r[1], seen):
                return True
        return False

    def _alias_is_external(self, file: FileFacts, name: str) -> bool:
        t = file.aliases.get(name)
        return t is not None and t.split(".")[0] not in self.roots

    # ------------------------------------------------------------ calls
    def _holder(self, prefix: list[str], ff: FuncFacts, file: FileFacts):
        """Resolve the part before the final '.' of a call. Returns (kind, qname) where kind is
        module | class | super | external | dynamic | unresolved."""
        first = prefix[0]
        module = file.src.module
        if first in {"self", "cls"} and ff.cls:
            return ("class", ff.cls) if len(prefix) == 1 else ("dynamic", "")
        if first == "super()" and ff.cls:
            return ("super", ff.cls) if len(prefix) == 1 else ("dynamic", "")
        if first in ff.var_types and len(prefix) == 1:
            t = ff.var_types[first]
            r = self.resolve_expr(t, module)
            if r and r[0] == "class":
                return "class", r[1]
            tfirst = t.split(".")[0]
            return ("external", "") if self._alias_is_external(file, tfirst) else ("dynamic", "")
        if first in ff.rec.params or first in ff.rec.kwonly:
            return "dynamic", ""
        if any("()" in p for p in prefix):
            return "dynamic", ""
        base = self.bind(file, first)
        if base is None:
            return "dynamic", ""          # builtin or local variable
        r = self.lookup(".".join([base] + prefix[1:]))
        if r and r[0] in {"module", "class"}:
            return r
        if r and r[0] == "function":
            return "dynamic", ""
        return ("external", "") if base.split(".")[0] not in self.roots else ("unresolved", "")

    def resolve_call(self, rc: RawCall, ff: FuncFacts, file: FileFacts) -> tuple[str, str, str]:
        """-> (status, callee_qname, ns). status: resolved|external|unresolved|dynamic|noinit"""
        module = file.src.module
        if "()" in rc.expr and not rc.expr.startswith("super()"):      # Foo(args).method(...)
            recv, _, rest = rc.expr.partition("().")
            if not rest or "." in rest or "()" in rest or "()" in recv:
                return "dynamic", "", ""
            r = self.resolve_expr(recv, module)
            if r and r[0] == "class":
                m = self.find_method(r[1], rest)
                if m:
                    return "resolved", m, m
                return ("external" if self.has_external_base(r[1]) else "unresolved"), "", ""
            first = recv.split(".")[0]
            return ("external" if self._alias_is_external(file, first) else "dynamic"), "", ""
        parts = rc.expr.split(".")
        if len(parts) == 1:
            n = parts[0]
            if n in ff.rec.params or n in ff.rec.kwonly:
                return "dynamic", "", ""
            q = self.bind(file, n)
            if q is None:
                return "dynamic", "", ""
            res = self.lookup(q)
            ns = f"{module}.{n}"
            return self._finish(res, ns, external=q.split(".")[0] not in self.roots)

        kind, hq = self._holder(parts[:-1], ff, file)
        last = parts[-1]
        if kind in {"dynamic", "external", "unresolved"}:
            return kind, "", ""
        if kind == "module":
            res = self.lookup(f"{hq}.{last}")
            ns = f"{hq}.{last}"
            ext = (not res) and self._alias_is_external(self.files[hq], last)
            return self._finish(res, ns, external=ext)
        if kind == "super":
            c = self.classes[hq]
            for base in c.bases:
                r = self.resolve_expr(base, c.module)
                if r and r[0] == "class":
                    m = self.find_method(r[1], last)
                    if m:
                        return "resolved", m, m
            return ("external" if self.has_external_base(hq) else "unresolved"), "", ""
        # kind == "class"
        m = self.find_method(hq, last)
        if m:
            return "resolved", m, m
        return ("external" if self.has_external_base(hq) else "unresolved"), "", ""

    def _finish(self, res, ns: str, external: bool) -> tuple[str, str, str]:
        if res is None:
            return ("external" if external else "unresolved"), "", ""
        kind, q = res
        if kind == "function":
            return "resolved", q, ns
        if kind == "class":
            init = self.find_method(q, "__init__")
            return ("resolved", init, ns) if init else ("noinit", "", "")
        return "unresolved", "", ""

    # ------------------------------------------------------------ mocks
    def _mock_miss_status(self, head: str) -> str:
        """patch("pkg.mod.subprocess.run"): `subprocess` is a library imported into pkg.mod."""
        if head.split(".")[0] not in self.roots:
            return "external"
        parts = head.split(".")
        for i in range(len(parts) - 1, 0, -1):
            mod = ".".join(parts[:i])
            if mod in self.files:
                return "external" if self._alias_is_external(self.files[mod], parts[i]) else "unresolved"
        return "unresolved"

    def resolve_mock(self, rm: RawMock, owner: FuncFacts | None, file: FileFacts) -> tuple[str, str, str]:
        """-> (status, target_qname, ns). status: resolved|external|unresolved"""
        module = file.src.module
        if rm.path:
            head, _, attr = rm.path.rpartition(".")
            holder = self.lookup(head)
            if holder is None:
                return self._mock_miss_status(head), "", ""
        else:
            attr = rm.attr
            holder = None
            first = rm.obj_expr.split(".")[0]
            if owner is not None and first in {"self", "cls"} and owner.cls and rm.obj_expr == first:
                holder = ("class", owner.cls)
            elif owner is not None and rm.obj_expr in owner.var_types:
                holder = self.resolve_expr(owner.var_types[rm.obj_expr], module)
            else:
                holder = self.resolve_expr(rm.obj_expr, module)
            if holder is None:
                ext = first in file.aliases and self._alias_is_external(file, first)
                return ("external" if ext else "unresolved"), "", ""
        kind, hq = holder
        if kind == "module":
            res, ns = self.lookup(f"{hq}.{attr}"), f"{hq}.{attr}"
            if res is None:  # e.g. patch("pkg.mod.which") where `which` was imported from a library
                return ("external" if self._alias_is_external(self.files[hq], attr) else "unresolved"), "", ""
        elif kind == "class":
            m = self.find_method(hq, attr)
            res, ns = (("function", m) if m else None), (m or "")
            if res is None:  # method inherited from a library base class
                return ("external" if self.has_external_base(hq) else "unresolved"), "", ""
        else:
            return "unresolved", "", ""
        if res is None:
            return "unresolved", "", ""
        if res[0] == "class":
            init = self.find_method(res[1], "__init__")
            return ("resolved", init, ns) if init else ("unresolved", "", "")
        if res[0] != "function":
            return "unresolved", "", ""
        return "resolved", res[1], ns


# ====================================================================== driver
def resolve_all(files: list[FileFacts]) -> Resolved:
    world = World(files)
    out = Resolved(stats=world.stats)
    st = out.stats

    # ---- modules, functions
    for mod, f in world.files.items():
        out.modules.append(ModuleRec(qname=mod, file=f.src.rel, sha256=f.src.sha256))
    out.functions = [ff.rec for ff in world.functions.values()]
    st["files"], st["functions"] = len(world.files), len(out.functions)
    st["tests"] = sum(ff.rec.is_test for ff in world.functions.values())
    st["classes"] = len(world.classes)

    # ---- imports
    seen_imp: set[tuple[str, str]] = set()
    for mod, f in world.files.items():
        for t in f.import_targets:
            m = t
            while m and m not in world.modules:
                m = m.rpartition(".")[0]
            if m and m != mod and (mod, m) not in seen_imp:
                seen_imp.add((mod, m))
                out.imports.append(ImportRec(src=mod, dst=m))

    # ---- calls
    for mod, f in world.files.items():
        st["calls_dynamic_expr"] += f.dynamic_calls
        for rc in f.calls:
            ff = world.functions.get(rc.caller)
            if ff is None:
                continue
            status, callee, ns = world.resolve_call(rc, ff, f)
            st[f"calls_{status}"] += 1
            if status == "resolved":
                out.calls.append(CallRec(caller=rc.caller, callee=callee, line=rc.line, n_pos=rc.n_pos,
                                         kw_names=rc.kw_names, dynamic_args=rc.dynamic_args, ns=ns))
    resolved, unresolved = st["calls_resolved"], st["calls_unresolved"]
    st["internal_resolution_pct"] = round(100 * resolved / max(1, resolved + unresolved), 1)

    # ---- references: functions used as values in test files (callbacks, click runners)
    have: set[tuple[str, str]] = set()
    for f in world.files.values():
        for rr in f.refs:
            ff = world.functions.get(rr.caller)
            if ff is None:
                continue
            status, callee, _ns = world.resolve_call(rr, ff, f)
            if status == "resolved" and callee != rr.caller and (rr.caller, callee) not in have:
                have.add((rr.caller, callee))
                out.refs.append(RefRec(src=rr.caller, dst=callee, line=rr.line))

    # ---- mocks (also adds test -> fixture links)
    _resolve_mocks(world, out)
    st["refs"] = len(out.refs)
    return out


def _resolve_mocks(world: World, out: Resolved) -> None:
    st = out.stats
    # index every raw mock by its owner (function qname or class qname)
    by_owner: dict[str, list[tuple[RawMock, FileFacts]]] = {}
    for f in world.files.values():
        for m in f.mocks:
            by_owner.setdefault(m.owner, []).append((m, f))
        for cq, lst in f.class_mocks.items():
            for m in lst:
                by_owner.setdefault(cq, []).append((m, f))
    st["mocks_found"] = sum(len(v) for v in by_owner.values())

    # fixtures: (module, class, name) and conftest by directory
    fixtures: dict[tuple, FuncFacts] = {}
    conftest: dict[tuple[str, str], FuncFacts] = {}
    for ff in world.functions.values():
        if not ff.fixture_name:
            continue
        f = world.file_of_func[ff.rec.qname]
        fixtures[(f.src.module, ff.cls, ff.fixture_name)] = ff
        if f.src.is_conftest:
            conftest[(dirname(f.src.rel), ff.fixture_name)] = ff

    def find_fixture(name: str, f: FileFacts, cls: str) -> FuncFacts | None:
        hit = fixtures.get((f.src.module, cls, name)) or fixtures.get((f.src.module, "", name))
        if hit:
            return hit
        alias = f.aliases.get(name)                       # fixture imported into this module
        if alias:
            r = world.lookup(alias)
            if r and r[0] == "function" and world.functions[r[1]].fixture_name:
                return world.functions[r[1]]
        d = dirname(f.src.rel)
        while True:
            if (d, name) in conftest:
                return conftest[(d, name)]
            if not d:
                return None
            d = dirname(d)

    # a test (or fixture) that requests a fixture by name reaches it
    have = {(r.src, r.dst) for r in out.refs}
    for tq, ff in world.functions.items():
        if not ff.rec.in_test_file:
            continue
        f = world.file_of_func[tq]
        for name in ff.request_names + ff.use_fixtures:
            dep = find_fixture(name, f, ff.cls)
            if dep and dep.rec.qname != tq and (tq, dep.rec.qname) not in have:
                have.add((tq, dep.rec.qname))
                out.refs.append(RefRec(src=tq, dst=dep.rec.qname, line=ff.rec.line))

    def autouse_fixtures(f: FileFacts, cls: str) -> list[FuncFacts]:
        found = [fx for (mod, c, _n), fx in fixtures.items()
                 if mod == f.src.module and fx.autouse and c in {"", cls}]
        d = dirname(f.src.rel)
        while True:
            found += [fx for (cd, _n), fx in conftest.items() if cd == d and fx.autouse]
            if not d:
                break
            d = dirname(d)
        return found

    def fixture_closure(fx: FuncFacts, seen: set[str]) -> list[FuncFacts]:
        """fx plus every fixture it asks for (directly or through other fixtures)."""
        if fx.rec.qname in seen:
            return []
        seen.add(fx.rec.qname)
        f = world.file_of_func[fx.rec.qname]
        chain = [fx]
        for name in fx.request_names + fx.use_fixtures:
            dep = find_fixture(name, f, fx.cls)
            if dep:
                chain += fixture_closure(dep, seen)
        return chain

    resolved_cache: dict[tuple, tuple[str, str, str]] = {}

    def resolve_owned(rm: RawMock, owner_q: str, f: FileFacts) -> tuple[str, str, str]:
        key = (rm.owner, rm.line, rm.via, rm.path, rm.obj_expr, rm.attr)
        if key not in resolved_cache:
            owner_ff = world.functions.get(owner_q) or world.functions.get(rm.owner)
            resolved_cache[key] = world.resolve_mock(rm, owner_ff, f)
        return resolved_cache[key]

    emitted: set[tuple] = set()
    counted: set[tuple] = set()

    def emit(test_q: str, rm: RawMock, f: FileFacts, scope: str, derived: bool) -> None:
        status, target, ns = resolve_owned(rm, rm.owner, f)
        ck = (rm.owner, rm.line, rm.file, rm.via, rm.path, rm.obj_expr, rm.attr)
        if ck not in counted:
            counted.add(ck)
            st[f"mocks_{status}"] += 1
        if status != "resolved":
            return
        key = (test_q, target, ns, rm.line, rm.file)
        if key in emitted:
            return
        emitted.add(key)
        st["mock_edges_derived" if derived else "mock_edges_direct"] += 1
        out.mocks.append(MockRec(test=test_q, target=target, line=rm.line, via=rm.via,
                                 ns=ns, scope=scope, file=rm.file))

    for tq, test in world.functions.items():
        if not test.rec.is_test:
            continue
        f = world.file_of_func[tq]
        # 1. patches written on the test itself (decorators, with-blocks, calls)
        for rm, rf in by_owner.get(tq, []):
            emit(tq, rm, rf, rm.scope, derived=False)
        # 2. patches on the test's class (class decorator, setUp)
        if test.cls:
            for rm, rf in by_owner.get(test.cls, []):
                emit(tq, rm, rf, rm.scope, derived=True)
        # 3. patches made by fixtures the test asks for, plus autouse fixtures
        wanted: list[tuple[FuncFacts, str]] = []
        cls_use = world.classes[test.cls].use_fixtures if test.cls in world.classes else []
        for name in test.request_names + test.use_fixtures + cls_use:
            fx = find_fixture(name, f, test.cls)
            if fx:
                wanted.append((fx, "fixture"))
        wanted += [(fx, "autouse") for fx in autouse_fixtures(f, test.cls)]
        seen: set[str] = set()
        for fx, how in wanted:
            for dep in fixture_closure(fx, seen):
                for rm, rf in by_owner.get(dep.rec.qname, []):
                    emit(tq, rm, rf, f"{how}:{dep.fixture_name}", derived=True)

    # patches whose owner is neither a test, a fixture, nor a class: count only
    for owner_q, lst in by_owner.items():
        ff = world.functions.get(owner_q)
        if ff is not None and not ff.rec.is_test and not ff.fixture_name:
            st["mocks_in_helpers"] += len(lst)
