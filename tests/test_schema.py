"""Module 1 checks: does the schema hold, and does it support the core idea?"""
import pytest
from redislite.falkordb_client import FalkorDB

from preflight import schema as S


@pytest.fixture()
def graph(tmp_path):
    db = FalkorDB(str(tmp_path / "t.db"))
    return S.reset_graph(db, "canon_test")


def load_sample(g):
    mods = [S.ModuleRec("pkg.mod", "pkg/mod.py", "h1"), S.ModuleRec("tests.test_mod", "tests/test_mod.py", "h2")]
    fns = [
        S.FunctionRec("pkg.mod.f", "f", "pkg.mod", "pkg/mod.py", 1, 3, params=["x", "y"], required=["x"]),
        S.FunctionRec("pkg.mod.main", "main", "pkg.mod", "pkg/mod.py", 5, 9),
        S.FunctionRec("tests.test_mod.test_main", "test_main", "tests.test_mod", "tests/test_mod.py", 1, 6, is_test=True),
        S.FunctionRec("pkg.mod.Box.get", "get", "pkg.mod", "pkg/mod.py", 12, 14, is_method=True, class_name="Box"),
    ]
    S.load_batch(g, S.LOAD_MODULES, mods)
    S.load_batch(g, S.LOAD_FUNCTIONS, fns)
    S.load_batch(g, S.LOAD_DEFINED_IN, fns)
    S.load_batch(g, S.LOAD_IMPORTS, [S.ImportRec("tests.test_mod", "pkg.mod")])
    S.load_batch(g, S.LOAD_CALLS, [
        S.CallRec("pkg.mod.main", "pkg.mod.f", 7, n_pos=2, ns="pkg.mod.f"),
        S.CallRec("tests.test_mod.test_main", "pkg.mod.main", 3),
        S.CallRec("pkg.mod.main", "pkg.mod.Box.get", 8, kw_names=["key"]),
    ])
    S.load_batch(g, S.LOAD_MOCKS, [S.MockRec("tests.test_mod.test_main", "pkg.mod.f", 2, via="patch", ns="pkg.mod.f", scope="body", file="tests/test_mod.py")])


def count(g, cypher):
    return g.ro_query(cypher).result_set[0][0]


def test_ensure_schema_is_idempotent(graph):
    S.ensure_schema(graph)
    S.ensure_schema(graph)
    idx = {(r[0], prop) for r in graph.list_indices().result_set for prop in r[1]}
    assert idx == set(S.INDEXES)


def test_loaders_create_expected_counts(graph):
    load_sample(graph)
    assert count(graph, "MATCH (n:Function) RETURN count(n)") == 4
    assert count(graph, "MATCH (n:Module) RETURN count(n)") == 2
    assert count(graph, "MATCH ()-[r:DEFINED_IN]->() RETURN count(r)") == 4
    assert count(graph, "MATCH ()-[r:CALLS]->() RETURN count(r)") == 3
    assert count(graph, "MATCH ()-[r:MOCKS]->() RETURN count(r)") == 1


def test_properties_round_trip(graph):
    load_sample(graph)
    row = graph.ro_query(
        "MATCH (f:Function {qname:'pkg.mod.f'}) RETURN f.params, f.required, f.returns, f.is_test, f.line"
    ).result_set[0]
    assert row == [["x", "y"], ["x"], "", False, 1]
    edge = graph.ro_query("MATCH ()-[c:CALLS]->(:Function {qname:'pkg.mod.Box.get'}) RETURN c.kw_names, c.line").result_set[0]
    assert edge == [["key"], 8]


def test_missing_endpoint_rows_are_skipped_not_fatal(graph):
    load_sample(graph)
    S.load_batch(graph, S.LOAD_CALLS, [S.CallRec("pkg.mod.main", "external.lib.fn", 9)])
    assert count(graph, "MATCH ()-[r:CALLS]->() RETURN count(r)") == 3


def test_blast_radius_is_expressible(graph):
    load_sample(graph)
    rows = graph.ro_query(
        "MATCH p=(c:Function)-[:CALLS*1..5]->(f:Function {qname:$q}) "
        "RETURN c.qname, length(p) ORDER BY length(p), c.qname",
        params={"q": "pkg.mod.f"},
    ).result_set
    assert rows == [["pkg.mod.main", 1], ["tests.test_mod.test_main", 2]]


def test_lying_mock_is_expressible(graph):
    """The core idea, using the shared query: the test mocks f AND reaches a caller of f."""
    load_sample(graph)
    rows = graph.ro_query(S.Q_LYING_MOCKS, params={"q": "pkg.mod.f"}).result_set
    assert [(r[0], r[6]) for r in rows] == [("tests.test_mod.test_main", "pkg.mod.main")]


def test_different_namespace_is_not_lying(graph):
    """patch of a DIFFERENT name than the caller used must not be flagged."""
    load_sample(graph)
    graph.query("MATCH (:Function {is_test:true})-[m:MOCKS]->() SET m.ns = 'other.module.f'")
    assert graph.ro_query(S.Q_LYING_MOCKS, params={"q": "pkg.mod.f"}).result_set == []
