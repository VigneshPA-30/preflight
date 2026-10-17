"""Stage 1: read ONE file with `ast` and collect raw facts.

No cross-file knowledge here. Names stay as written ("utils.foo"); stage 2
(resolve.py) turns them into graph qnames.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

from preflight.schema import FunctionRec

from .discover import SourceFile
from .mocks import SETUP_NAMES, RawMock, classify, dotted, is_mock_call


@dataclass(slots=True)
class ClassFacts:
    qname: str
    name: str
    module: str
    line: int
    bases: list[str]
    methods: dict[str, str] = field(default_factory=dict)  # method name -> function qname
    use_fixtures: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RawCall:
    caller: str
    expr: str            # "foo", "mod.foo", "self.run", "super().run"
    line: int
    n_pos: int
    kw_names: list[str]
    dynamic_args: bool


@dataclass(slots=True)
class FuncFacts:
    rec: FunctionRec
    cls: str                                   # class qname, or ""
    var_types: dict[str, str] = field(default_factory=dict)   # local name -> dotted type expr
    fixture_name: str = ""                     # set when this is a pytest fixture
    autouse: bool = False
    use_fixtures: list[str] = field(default_factory=list)     # @pytest.mark.usefixtures(...)

    @property
    def request_names(self) -> list[str]:
        return list(self.rec.params) + list(self.rec.kwonly)


@dataclass(slots=True)
class FileFacts:
    src: SourceFile
    aliases: dict[str, str] = field(default_factory=dict)      # local name -> absolute dotted target
    star_imports: list[str] = field(default_factory=list)
    import_targets: list[str] = field(default_factory=list)    # for IMPORTS edges
    defs: dict[str, str] = field(default_factory=dict)         # top-level name -> qname
    classes: list[ClassFacts] = field(default_factory=list)
    functions: list[FuncFacts] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    mocks: list[RawMock] = field(default_factory=list)         # owned by a function
    class_mocks: dict[str, list[RawMock]] = field(default_factory=dict)  # owned by a class
    dynamic_calls: int = 0                                     # callee is not a plain dotted name
    module_level_calls: int = 0
    error: str = ""


# ---------------------------------------------------------------- imports
def _abs_from(pkg: str, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module or ""
    parts = pkg.split(".") if pkg else []
    up = node.level - 1
    if up > len(parts):
        return None
    base = parts[: len(parts) - up]
    if node.module:
        base += node.module.split(".")
    return ".".join(base)


def _collect_imports(tree: ast.AST, facts: FileFacts) -> None:
    src = facts.src
    pkg = src.module if src.is_package else src.module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    facts.aliases[a.asname] = a.name
                else:
                    root = a.name.split(".")[0]
                    facts.aliases[root] = root
                facts.import_targets.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            base = _abs_from(pkg, node)
            if base is None:
                continue
            facts.import_targets.append(base)
            for a in node.names:
                if a.name == "*":
                    facts.star_imports.append(base)
                    continue
                full = f"{base}.{a.name}" if base else a.name
                facts.aliases[a.asname or a.name] = full
                facts.import_targets.append(full)


# ---------------------------------------------------------------- signature
def _signature(args: ast.arguments, drop_first: bool) -> tuple[list[str], list[str], list[str]]:
    pos_all = [a.arg for a in args.posonlyargs + args.args]
    n_def = len(args.defaults)
    required_pos = set(pos_all[: len(pos_all) - n_def])
    pos = pos_all[1:] if (drop_first and pos_all) else pos_all
    kwonly = [a.arg for a in args.kwonlyargs]
    required = [p for p in pos if p in required_pos]
    required += [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is None]
    return pos, kwonly, required


def _annotation_text(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        txt = node.value.strip()
        return txt if txt.replace(".", "").replace("_", "").isalnum() else None
    if isinstance(node, ast.Subscript):  # Optional[Foo] / Foo | None handled loosely
        inner = node.slice
        if dotted(node.value) in {"Optional", "typing.Optional"}:
            return _annotation_text(inner)
        return None
    return dotted(node)


def _const_strs(call: ast.Call) -> list[str]:
    return [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


# ---------------------------------------------------------------- extractor
class _Extractor:
    def __init__(self, src: SourceFile, tree: ast.Module, facts: FileFacts, relaxed: bool) -> None:
        self.src, self.tree, self.facts, self.relaxed = src, tree, facts, relaxed

    def run(self) -> None:
        self._stmts(self.tree.body, None, self.src.module)

    # statements at module / class level
    def _stmts(self, stmts: list[ast.stmt], cls: ClassFacts | None, prefix: str) -> None:
        for st in stmts:
            if isinstance(st, ast.ClassDef):
                self._class(st, cls, prefix)
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(st, cls, prefix)
            else:
                if cls is None:
                    self.facts.module_level_calls += sum(isinstance(n, ast.Call) for n in ast.walk(st))
                for attr in ("body", "orelse", "finalbody"):
                    inner = getattr(st, attr, None)
                    if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                        self._stmts(inner, cls, prefix)
                for h in getattr(st, "handlers", []):
                    self._stmts(h.body, cls, prefix)

    def _class(self, node: ast.ClassDef, outer: ClassFacts | None, prefix: str) -> None:
        qname = f"{prefix}.{node.name}"
        cf = ClassFacts(qname=qname, name=node.name, module=self.src.module, line=node.lineno,
                        bases=[b for b in (dotted(x) for x in node.bases) if b])
        self.facts.classes.append(cf)
        if outer is None and prefix == self.src.module:
            self.facts.defs.setdefault(node.name, qname)
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                name = dotted(dec.func) or ""
                if name.endswith("usefixtures"):
                    cf.use_fixtures += _const_strs(dec)
                for m in classify(dec, qname, "class_decorator"):
                    m.file = self.src.rel
                    self.facts.class_mocks.setdefault(qname, []).append(m)
        self._stmts(node.body, cf, qname)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef,
                  cls: ClassFacts | None, prefix: str) -> None:
        src, facts = self.src, self.facts
        qname = f"{prefix}.{node.name}"
        deco_names, fixture_name, autouse, use_fx = [], "", False, []
        for d in node.decorator_list:
            target = d.func if isinstance(d, ast.Call) else d
            n = dotted(target) or ""
            deco_names.append(n)
            if n.split(".")[-1] == "fixture":
                fixture_name = node.name
                if isinstance(d, ast.Call):
                    for kw in d.keywords:
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                            fixture_name = str(kw.value.value)
                        if kw.arg == "autouse" and isinstance(kw.value, ast.Constant):
                            autouse = bool(kw.value.value)
            if n.endswith("usefixtures") and isinstance(d, ast.Call):
                use_fx += _const_strs(d)
        is_static = any(n.split(".")[-1] == "staticmethod" for n in deco_names)
        pos, kwonly, required = _signature(node.args, drop_first=cls is not None and not is_static)
        if self.relaxed:   # pytest-relaxed: every public function/method in a test file is a test
            is_test = (
                src.is_test_file and not src.is_conftest and not fixture_name
                and not node.name.startswith("_")
                and not any(p.startswith("_") for p in src.rel.split("/"))
                and (cls is None or not cls.name.startswith("_"))
            )
        else:
            is_test = (
                src.is_test_file and node.name.startswith("test") and not fixture_name
                and (cls is None or cls.name.startswith("Test")
                     or any(b.split(".")[-1] == "TestCase" for b in cls.bases))
            )
        rec = FunctionRec(
            qname=qname, name=node.name, module=src.module, file=src.rel,
            line=node.lineno, end_line=node.end_lineno or node.lineno,
            params=pos, kwonly=kwonly, required=required,
            has_varargs=node.args.vararg is not None, has_varkw=node.args.kwarg is not None,
            returns=(ast.unparse(node.returns) if node.returns is not None else ""),
            is_async=isinstance(node, ast.AsyncFunctionDef),
            is_method=cls is not None, is_test=is_test,
            class_name=cls.name if cls else "",
        )
        ff = FuncFacts(rec=rec, cls=cls.qname if cls else "", fixture_name=fixture_name,
                       autouse=autouse, use_fixtures=use_fx)
        facts.functions.append(ff)
        if cls is None and prefix == src.module:
            facts.defs.setdefault(node.name, qname)
        if cls is not None:
            cls.methods.setdefault(node.name, qname)

        own_mocks: list[RawMock] = []
        for d in node.decorator_list:
            if isinstance(d, ast.Call):
                own_mocks += classify(d, qname, "decorator")
        self._scan_body(node, ff, own_mocks)

        for m in own_mocks:
            m.file = src.rel
        if cls is not None and node.name in SETUP_NAMES:
            for m in own_mocks:
                m.scope, m.owner = "class_setup", cls.qname
                facts.class_mocks.setdefault(cls.qname, []).append(m)
        else:
            facts.mocks += own_mocks

    def _scan_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef, ff: FuncFacts,
                   own_mocks: list[RawMock]) -> None:
        facts, qname = self.facts, ff.rec.qname
        all_args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        for a in all_args:
            t = _annotation_text(a.annotation)
            if t:
                ff.var_types[a.arg] = t
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call):
                    found = classify(sub, qname, "body")
                    if found:
                        own_mocks += found
                        continue
                    if is_mock_call(sub):
                        continue
                    expr = dotted(sub.func)
                    if expr is None:
                        facts.dynamic_calls += 1
                        continue
                    facts.calls.append(RawCall(
                        caller=qname, expr=expr, line=sub.lineno,
                        n_pos=sum(not isinstance(a, ast.Starred) for a in sub.args),
                        kw_names=[k.arg for k in sub.keywords if k.arg],
                        dynamic_args=any(isinstance(a, ast.Starred) for a in sub.args)
                        or any(k.arg is None for k in sub.keywords),
                    ))
                elif isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                        and isinstance(sub.targets[0], ast.Name) and isinstance(sub.value, ast.Call):
                    t = dotted(sub.value.func)
                    if t:
                        ff.var_types[sub.targets[0].id] = t
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    t = _annotation_text(sub.annotation)
                    if t:
                        ff.var_types[sub.target.id] = t
                elif isinstance(sub, (ast.With, ast.AsyncWith)):
                    for item in sub.items:
                        if isinstance(item.optional_vars, ast.Name) and isinstance(item.context_expr, ast.Call):
                            t = dotted(item.context_expr.func)
                            if t:
                                ff.var_types[item.optional_vars.id] = t


def extract_file(src: SourceFile, relaxed: bool = False) -> FileFacts:
    facts = FileFacts(src=src)
    try:
        text = src.path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text, filename=src.rel)
    except (SyntaxError, ValueError, RecursionError) as exc:
        facts.error = f"{type(exc).__name__}: {exc}"
        return facts
    _collect_imports(tree, facts)
    _Extractor(src, tree, facts, relaxed).run()
    return facts
