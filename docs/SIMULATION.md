# Simulation: how rival refactors are tested

`simulate_refactor_decisions` takes 1 to 5 candidate changes. Each one runs in its own **cloned graph**.

1. `GRAPH.COPY canon -> uni_<run>_<n>` (FalkorDB clones the whole code graph).
2. The mutation is applied to the clone with Cypher `SET`. The real graph is never written to.
3. Cypher finds every call site that no longer fits.
4. Each broken call site gets a status. The clone is deleted.
5. Plans are ranked. The winner gets an ordered edit checklist.

`GRAPH.COPY` forks the database process, so clones are created one at a time. Evaluation of the clones runs in parallel.

## Supported mutations

| Kind | Input | Meaning |
|---|---|---|
| `change_params` | `new_params: ["a", "b=", "*", "k"]` | New parameter list. `=` marks a default. `*` starts keyword-only parameters. |
| `rename` | `new_name`, optional `keep_alias` | New function name. `keep_alias` keeps the old name working. |

## The signature rule (Cypher)

A call site breaks when the call no longer fits the new signature. This query was checked against the real Python
interpreter on 1000 random calls (zero disagreements), see `tests/test_mutations.py`.

```cypher
MATCH (c:Function)-[k:CALLS]->(f:Function {qname:$q})
WHERE k.dynamic_args = false
WITH c, k, f,
     k.n_pos > size(f.params) AND NOT f.has_varargs AS too_many,
     [kw IN k.kw_names WHERE NOT kw IN f.params AND NOT kw IN f.kwonly] AS unknown,
     [kw IN k.kw_names WHERE kw IN f.params[0..k.n_pos]] AS dup,
     [r IN f.required WHERE NOT r IN f.params[0..k.n_pos] AND NOT r IN k.kw_names] AS missing
WHERE too_many OR (size(unknown) > 0 AND NOT f.has_varkw) OR size(dup) > 0 OR size(missing) > 0
RETURN c.qname, k.line, too_many, unknown, dup, missing
```

Only **new** breakages count: the same query runs before and after the change, and the difference is reported.

## Status of every broken call site

| Status | Meaning | Who notices |
|---|---|---|
| `caught` | A test that really runs the call would fail. | The agent's test loop. |
| `masked` | Every test that reaches the call mocks it away. Tests stay green. | **Nobody.** |
| `untested` | No test reaches the caller by a call, a value reference, or a fixture. | **Nobody.** |

For a renamed function imported with `from x import f`, the import fails when the module loads. That is always loud, so it is `caught`.

## Scoring (lower is safer)

`score = 1.0 * caught + 5.0 * silent + 0.5 * lying_tests + 0.25 * files_to_edit + 0.5 * alias_debt + 0.5 * unverifiable`

- `silent` = `masked` + `untested`.
- `lying_tests` = tests that mock a broken call and stay green.
- `unverifiable` = call sites that use `*args` or `**kwargs`, which cannot be checked.

Verdicts: `safe` (nothing breaks), `viable` (breaks, but all are caught), `risky` (some break silently), `dead_end` (invalid change, for example a name collision or a bad parameter list). Dead ends are ranked last.

## Limits

- **`untested` is an upper bound.** A test can reach code in ways static analysis cannot see: dunder methods called by Python
  (`__eq__`, `__getitem__`), properties, dynamic dispatch, plugins. Treat `untested` as "no test path found", and check by hand.
  The scan on cookiecutter, fabric and invoke found and fixed two false `untested` cases: functions passed as values
  (`runner.invoke(main)`) and `Class(args).method()` chains.
- Only `change_params` and `rename` are simulated. Changes to return values need information about how callers use them, which is not collected.
- A call through an object of unknown type is not resolved, so it is not checked.
- The check follows calls up to 4 hops from a test to a caller.
- Call sites that pass `*args` or `**kwargs` are counted as `unverifiable`, not as broken.
