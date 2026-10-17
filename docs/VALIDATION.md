# Validation: does the tool predict what really breaks?

`scripts/validate_prediction.py` runs this experiment on a real project (cookiecutter, 379 tests, commit pinned in `TARGETS.md`):

1. The tool **predicts** which tests will fail for a change (`expected_failing_tests`).
2. The change is **really made** in a throwaway copy of the code (the definition only, callers untouched).
3. The **real test suite** runs (pytest, nothing mocked by us).
4. Prediction and reality are compared.

| Experiment | Predicted | Actually failed | In both | Precision | Recall |
|---|---|---|---|---|---|
| E1 make `context`, `output_dir` required (`generate_files`) | 9 | 9 | 9 | 1.00 | 1.00 |
| E2 rename `generate_files` | 94 | 94 | 93 | 0.99 | 0.99 |
| E3 add required parameter to `read_user_variable` | 2 | 2 | 2 | 1.00 | 1.00 |
| E4 add required parameter to `replay.dump` | 42 | 32 | 31 | 0.74 | 0.97 |
| E5 rename `make_sure_path_exists` | 172 | 173 | 172 | 1.00 | 0.99 |
| **Total** | **319** | **310** | **307** | **0.96** | **0.99** |

**Recall (0.99)** means the tool almost never misses a test that fails. **Precision (0.96)** means a few predicted tests do not fail.

## What the experiment found and fixed

The first run had precision 0.62 on E4 and missed whole groups of tests. Comparing with the real suite showed three flaws, all fixed:

- A test that **mocks a call in the middle of a path** never reaches the code behind it. The tool now follows only unmocked paths.
- A `conftest.py` that fails to import takes down **every test in its folder**.
- `mocker.patch("pkg.mod.name")` must **import** `pkg.mod`; a test whose patch target cannot be imported fails.

## Remaining errors (known limits)

- **Early exits cause false positives.** Tests such as `test_cli_help`, `test_cli_version` and `test_raise_on_invalid_mode` call the code but stop before the changed line. A static call graph cannot see this, so the tool over-predicts. The prediction is "tests that may fail".
- **Subprocess tests are invisible.** `test_should_invoke_main` runs `python -m cookiecutter` in a subprocess. It is the only miss in all five experiments.
- Five experiments on one project is a small sample. The numbers show the method works, not that it works everywhere.
- Changes are checked at call sites. Mock objects built with `autospec=True` enforce signatures and are not modelled.

## Run it yourself

```
python3 -m venv /tmp/cc_env && /tmp/cc_env/bin/pip install -e ~/graphhacks/targets/cookiecutter pytest pytest-mock freezegun
uv run python scripts/validate_prediction.py ~/graphhacks/targets/cookiecutter /tmp/cc_env/bin/python
```
