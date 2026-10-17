"""Python ingestion: names, calls, mock patterns, and the lying-mock query."""
import textwrap
from pathlib import Path

import pytest
from redislite.falkordb_client import FalkorDB

from preflight import schema as S
from preflight.ingestion.python.discover import module_qname
from preflight.ingestion.python.pipeline import ingest_workspace, sync_workspace


def make(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    return tmp_path


def ingest(tmp_path, files):
    return ingest_workspace(make(tmp_path, files)).resolved


def calls(r):
    return {(c.caller, c.callee, c.ns) for c in r.calls}


def mocks(r):
    return {(m.test, m.target, m.ns, m.scope) for m in r.mocks}


@pytest.fixture()
def db(tmp_path_factory):
    return FalkorDB(str(tmp_path_factory.mktemp("db") / "t.db"))


def lying(db, root, canon, target):
    sync_workspace(root, canon, db=db)
    rows = db.select_graph(canon).ro_query(S.Q_LYING_MOCKS, params={"q": target}).result_set
    return {(r[0], r[6]) for r in rows}   # (test, caller)


# ------------------------------------------------------------------ names
def test_module_names(tmp_path):
    make(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "", "src/lib/__init__.py": "",
                    "src/lib/b.py": "", "tests/test_x.py": "", "bad-name/x.py": ""})
    assert module_qname(tmp_path, tmp_path / "pkg/a.py") == ("pkg.a", False)
    assert module_qname(tmp_path, tmp_path / "pkg/__init__.py") == ("pkg", True)
    assert module_qname(tmp_path, tmp_path / "src/lib/b.py") == ("lib.b", False)   # src layout
    assert module_qname(tmp_path, tmp_path / "tests/test_x.py") == ("tests.test_x", False)
    assert module_qname(tmp_path, tmp_path / "bad-name/x.py") is None


def test_signatures(tmp_path):
    r = ingest(tmp_path, {"m.py": """
        class C:
            def meth(self, a, b=1, *args, k, j=2, **kw): ...
            @staticmethod
            def st(x, y): ...
        def f(a, /, b, c=3, *, d, e=5): ...
    """})
    f = {x.qname: x for x in r.functions}
    meth = f["m.C.meth"]
    assert (meth.params, meth.kwonly, meth.required) == (["a", "b"], ["k", "j"], ["a", "k"])
    assert meth.has_varargs and meth.has_varkw and meth.is_method           # self is dropped
    assert f["m.C.st"].params == ["x", "y"]                                 # staticmethod keeps all
    assert (f["m.f"].params, f["m.f"].required) == (["a", "b", "c"], ["a", "b", "d"])


# ------------------------------------------------------------------ calls
def test_call_resolution(tmp_path):
    r = ingest(tmp_path, {
        "pkg/__init__.py": "from .core import run\n",
        "pkg/core.py": """
            def run(x): ...
            class Base:
                def __init__(self, a): ...
                def helper(self): ...
            class Child(Base):
                def go(self):
                    self.helper()        # inherited method
                    super().helper()
            def make():
                c = Child(1)             # class call -> Base.__init__
                c.go()                   # variable typed by constructor
            def typed(c: Child):
                c.go()                   # variable typed by annotation
        """,
        "pkg/use.py": """
            import pkg
            from pkg import core as k
            from pkg.core import run as r2
            def a(): pkg.run(1)          # re-exported through __init__
            def b(): k.make()
            def c(): r2(1, z=2)
        """,
    })
    got = calls(r)
    assert ("pkg.core.Child.go", "pkg.core.Base.helper", "pkg.core.Base.helper") in got
    assert ("pkg.core.make", "pkg.core.Base.__init__", "pkg.core.Child") in got
    assert ("pkg.core.make", "pkg.core.Child.go", "pkg.core.Child.go") in got
    assert ("pkg.core.typed", "pkg.core.Child.go", "pkg.core.Child.go") in got
    assert ("pkg.use.a", "pkg.core.run", "pkg.run") in got                  # ns = name looked up in pkg
    assert ("pkg.use.b", "pkg.core.make", "pkg.core.make") in got
    assert ("pkg.use.c", "pkg.core.run", "pkg.use.r2") in got               # ns = caller's own binding
    c = next(x for x in r.calls if x.caller == "pkg.use.c")
    assert (c.n_pos, c.kw_names) == (1, ["z"])


def test_unresolved_is_counted_not_guessed(tmp_path):
    r = ingest(tmp_path, {"m.py": """
        import os
        def f(cb):
            os.path.join("a")   # library
            cb()                # parameter
            print("x")          # builtin
    """})
    assert not r.calls
    assert r.stats["calls_external"] == 1 and r.stats["calls_dynamic"] == 2


# ------------------------------------------------------------------ mock forms
LIB = {"pkg/__init__.py": "", "pkg/util.py": "def compute(x, y): ...\ndef other(): ...\n"
       "class Svc:\n    def run(self): ...\n"}


def test_mock_forms(tmp_path):
    files = dict(LIB)
    files["tests/test_forms.py"] = """
        from unittest import mock
        from unittest.mock import patch
        import pytest
        from pkg import util

        @patch("pkg.util.compute")
        def test_decorator(m): ...

        def test_with():
            with mock.patch("pkg.util.compute"): ...

        def test_mocker(mocker):
            mocker.patch("pkg.util.compute")

        def test_object_form(mocker):
            mocker.patch.object(util, "compute")

        def test_class_attr(monkeypatch):
            monkeypatch.setattr(util.Svc, "run", lambda self: 1)

        def test_monkeypatch_str(monkeypatch):
            monkeypatch.setattr("pkg.util.compute", lambda *a: 1)

        def test_multiple():
            with patch.multiple("pkg.util", compute=1, other=2): ...

        def test_not_a_mock(client):
            client.patch("/api/items")          # HTTP verb, must be ignored

        def test_library(mocker):
            mocker.patch("os.path.exists")      # library: no edge
    """
    r = ingest(tmp_path, files)
    got = mocks(r)
    T = "tests.test_forms."
    assert (T + "test_decorator", "pkg.util.compute", "pkg.util.compute", "decorator") in got
    assert (T + "test_with", "pkg.util.compute", "pkg.util.compute", "body") in got
    assert (T + "test_mocker", "pkg.util.compute", "pkg.util.compute", "body") in got
    assert (T + "test_object_form", "pkg.util.compute", "pkg.util.compute", "body") in got
    assert (T + "test_class_attr", "pkg.util.Svc.run", "pkg.util.Svc.run", "body") in got
    assert (T + "test_monkeypatch_str", "pkg.util.compute", "pkg.util.compute", "body") in got
    assert (T + "test_multiple", "pkg.util.compute", "pkg.util.compute", "body") in got
    assert (T + "test_multiple", "pkg.util.other", "pkg.util.other", "body") in got
    assert not [m for m in r.mocks if m.test.endswith(("test_not_a_mock", "test_library"))]
    assert r.stats["mocks_external"] == 1


def test_class_level_and_fixture_mocks(tmp_path):
    files = dict(LIB)
    files["tests/conftest.py"] = """
        import pytest
        @pytest.fixture
        def fake_compute(mocker):
            return mocker.patch("pkg.util.compute")
        @pytest.fixture(autouse=True)
        def quiet(mocker):
            mocker.patch("pkg.util.other")
        @pytest.fixture
        def outer(fake_compute): ...
    """
    files["tests/test_scopes.py"] = """
        import unittest
        from unittest import mock
        import pytest

        def test_uses_fixture(fake_compute): ...
        def test_nested_fixture(outer): ...
        def test_marker_only(): ...
        @pytest.mark.usefixtures("fake_compute")
        def test_usefixtures(): ...

        @mock.patch("pkg.util.compute")
        class TestDeco:
            def test_a(self): ...

        class TestSetup(unittest.TestCase):
            def setUp(self):
                self.p = mock.patch("pkg.util.compute")
                self.p.start()
            def test_b(self): ...
    """
    r = ingest(tmp_path, files)
    got = mocks(r)
    T, C = "tests.test_scopes.", "pkg.util.compute"
    assert (T + "test_uses_fixture", C, C, "fixture:fake_compute") in got
    assert (T + "test_nested_fixture", C, C, "fixture:fake_compute") in got     # fixture asks for fixture
    assert (T + "test_usefixtures", C, C, "fixture:fake_compute") in got
    assert (T + "test_marker_only", C, C, "fixture:fake_compute") not in got
    assert (T + "test_marker_only", "pkg.util.other", "pkg.util.other", "autouse:quiet") in got  # autouse
    assert (T + "TestDeco.test_a", C, C, "class_decorator") in got
    assert (T + "TestSetup.test_b", C, C, "class_setup") in got


# ------------------------------------------------------------------ the point of it all
NUANCE = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def compute(x, y):\n    return x + y\n",
    "pkg/a.py": "from pkg.util import compute\ndef run_a():\n    return compute(1, 2)\n",
    "pkg/b.py": "from pkg import util\ndef run_b():\n    return util.compute(1, 2)\n",
    "pkg/c.py": "from pkg.util import compute\ndef run_c():\n    return compute(3, 4)\n",
    "tests/test_nuance.py": """
        from unittest import mock
        from pkg import a, b, c

        @mock.patch("pkg.a.compute")          # patches a's OWN name: hits run_a
        def test_patch_where_used(m):
            a.run_a()

        def test_patch_origin_hits_module_attr_caller():
            with mock.patch("pkg.util.compute"):   # run_b looks it up on pkg.util: hit
                b.run_b()

        def test_patch_origin_misses_from_import_caller():
            with mock.patch("pkg.util.compute"):   # run_a bound its own name: NOT hit
                a.run_a()

        def test_mock_but_never_reaches_caller():
            with mock.patch("pkg.c.compute"):
                a.run_a()                          # run_c is never called
    """,
}


def test_lying_mock_precision(tmp_path, db):
    got = lying(db, make(tmp_path, NUANCE), "canon_nuance", "pkg.util.compute")
    T = "tests.test_nuance."
    assert (T + "test_patch_where_used", "pkg.a.run_a") in got
    assert (T + "test_patch_origin_hits_module_attr_caller", "pkg.b.run_b") in got
    # the two cases a naive "test mocks f" detector would wrongly flag:
    assert (T + "test_patch_origin_misses_from_import_caller", "pkg.a.run_a") not in got
    assert not [x for x in got if x[0].endswith("test_mock_but_never_reaches_caller")]
    assert len(got) == 2


def test_indirect_reach_through_helper(tmp_path, db):
    files = dict(NUANCE)
    files["pkg/api.py"] = "from pkg import b\ndef handler():\n    return b.run_b()\n"
    files["tests/test_api.py"] = """
        from unittest import mock
        from pkg import api
        def test_handler():
            with mock.patch("pkg.util.compute"):
                api.handler()          # test -> handler -> run_b -> util.compute (mocked)
    """
    got = lying(db, make(tmp_path, files), "canon_indirect", "pkg.util.compute")
    assert ("tests.test_api.test_handler", "pkg.b.run_b") in got


# ------------------------------------------------------------------ sync behaviour
def test_sync_skips_when_unchanged_then_reloads(tmp_path, db):
    root = make(tmp_path, NUANCE)
    first = sync_workspace(root, "canon_sync", db=db)
    again = sync_workspace(root, "canon_sync", db=db)
    assert first["status"] == "loaded" and first["files_changed"] == first["files_total"]
    assert again["status"] == "unchanged" and again["files_changed"] == 0
    (root / "pkg/util.py").write_text("def compute(x, y, z):\n    return x\n")
    third = sync_workspace(root, "canon_sync", db=db)
    assert third["status"] == "loaded" and third["files_changed"] == 1
    n = db.select_graph("canon_sync").ro_query(
        "MATCH (f:Function {qname:'pkg.util.compute'}) RETURN f.params").result_set
    assert n == [[["x", "y", "z"]]]


def test_syntax_error_does_not_stop_ingest(tmp_path):
    r = ingest(tmp_path, {"ok.py": "def f(): ...\n", "broken.py": "def (:\n"})
    assert r.stats["parse_errors"] == 1 and [f.qname for f in r.functions] == ["ok.f"]


# ------------------------------------------------------------------ other test styles / imports
def test_pytest_relaxed_tests_are_detected(tmp_path):
    files = dict(LIB)
    files["pytest.ini"] = "[pytest]\npython_files = *\n"
    files["pyproject.toml"] = '[tool.x]\ndev = ["pytest-relaxed>=2"]\n'
    files["tests/runners.py"] = """
        from unittest.mock import patch

        class Runner_:
            def _helper(self): ...
            class run:
                @patch("pkg.util.compute")
                def does_a_thing(self, m):
                    self._helper()          # outer-class helper, visible from nested class
            def top_level_case(self): ...
        def _private(): ...
    """
    r = ingest(tmp_path, files)
    tests = {f.qname for f in r.functions if f.is_test}
    assert tests == {"tests.runners.Runner_.run.does_a_thing", "tests.runners.Runner_.top_level_case"}
    assert ("tests.runners.Runner_.run.does_a_thing", "tests.runners.Runner_._helper") in {(c.caller, c.callee) for c in r.calls}
    assert r.stats["relaxed_tests"] == 1


def test_star_import_and_vendor_skip(tmp_path):
    r = ingest(tmp_path, {
        "pkg/__init__.py": "from .impl import *\n",
        "pkg/impl.py": "def build(): ...\nclass Engine:\n    def __init__(self): ...\n",
        "pkg/use.py": "from pkg import *\ndef go():\n    build()\n    Engine()\n",
        "pkg/vendor/yaml.py": "def load(): ...\n",
    })
    got = {(c.caller, c.callee) for c in r.calls}
    assert ("pkg.use.go", "pkg.impl.build") in got
    assert ("pkg.use.go", "pkg.impl.Engine.__init__") in got
    assert not [f for f in r.functions if "vendor" in f.qname]


def test_tests_reach_code_through_values_and_fixtures(tmp_path):
    """click's CliRunner: `runner.invoke(main)` passes main as a value; 62 cookiecutter tests do this."""
    r = ingest(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/cli.py": "from pkg import core\ndef main():\n    return core.work()\n",
        "pkg/core.py": "def work(): ...\n",
        "tests/conftest.py": """
            import pytest
            from click.testing import CliRunner
            from pkg.cli import main
            @pytest.fixture
            def cli_runner():
                def run(*args):
                    return CliRunner().invoke(main, args)       # main used as a value, not called
                return run
        """,
        "tests/test_cli.py": """
            def test_it(cli_runner):
                cli_runner("--x")
        """,
    })
    refs = {(x.src, x.dst) for x in r.refs}
    assert ("tests.conftest.cli_runner", "pkg.cli.main") in refs      # fixture -> function used as a value
    assert ("tests.test_cli.test_it", "tests.conftest.cli_runner") in refs   # test -> fixture it requests


def test_constructor_receiver_calls(tmp_path):
    """Tests write  Call(x).make_context()  all the time."""
    r = ingest(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/task.py": "class Call:\n    def __init__(self, t): ...\n    def make_context(self): ...\n",
        "tests/test_task.py": "from pkg.task import Call\nfrom pkg import task\n"
                              "def test_a():\n    Call(1).make_context()\n"
                              "def test_b():\n    task.Call(2).make_context()\n"
                              "def test_c():\n    Call(3).missing().deeper()\n",
    })
    got = {(c.caller, c.callee) for c in r.calls}
    for t in ("test_a", "test_b"):
        assert (f"tests.test_task.{t}", "pkg.task.Call.make_context") in got
        assert (f"tests.test_task.{t}", "pkg.task.Call.__init__") in got
