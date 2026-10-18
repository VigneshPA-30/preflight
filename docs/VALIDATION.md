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

The first run, before any fix, predicted 325 tests and 305 of them really failed (precision 0.94, recall 0.98).
Comparing with the real suite exposed flaws. Each fix was then **switched off on its own** and the experiment re-run, to measure what it did:

| Version | Predicted | In both | Precision | Recall |
|---|---|---|---|---|
| First run (no fixes) | 325 | 305 | 0.94 | 0.98 |
| All fixes except the path-cut fix | 327 | 307 | 0.94 | 0.99 |
| All fixes except the patch-import rule | 317 | 305 | 0.96 | 0.98 |
| All fixes except the conftest rule | 319 | 307 | 0.96 | 0.99 |
| **All fixes (final)** | **319** | **307** | **0.96** | **0.99** |

- **A mock in the middle of a call path cuts the path.** A test that mocks `cli.cookiecutter` never reaches the code behind it.
  This fix raised precision (experiment E4: 50 predicted tests became 42, with the same 31 correct).
- **`patch("pkg.mod.name")` must import `pkg.mod`.** A test whose patch target cannot be imported fails.
  This fix raised recall (experiment E2: 2 more real failures found).
- **A `conftest.py` that fails to import fails its whole folder.** This rule matches how pytest works, but it made **no measurable
  difference** in these five experiments. It stays because it is correct, not because the experiment proved it.

## Remaining errors (known limits)

- **Early exits cause false positives.** Tests such as `test_cli_help`, `test_cli_version` and `test_raise_on_invalid_mode` call the code but stop before the changed line. A static call graph cannot see this, so the tool over-predicts. The prediction is "tests that may fail".
- **Subprocess tests are invisible.** `test_should_invoke_main` runs `python -m cookiecutter` in a subprocess. It is the only miss in all five experiments.
- Five experiments on one project is a small sample. The numbers show the method works, not that it works everywhere.
- Changes are checked at call sites. Mock objects built with `autospec=True` enforce signatures and are not modelled.

## Run it yourself

```
python3 -m venv /tmp/cc_env && /tmp/cc_env/bin/pip install -e ~/targets/cookiecutter pytest pytest-mock freezegun
uv run python scripts/validate_prediction.py ~/targets/cookiecutter /tmp/cc_env/bin/python
```

## Check your own project

Give the script a Python that has your project's test dependencies, and a file with your experiments:

```
uv run python scripts/validate_prediction.py ~/work/myproject /path/to/myproject-venv/bin/python experiments.json
```

```json
[{"name": "make y required",
  "target": "mypkg.util.compute",
  "mutation": {"kind": "change_params", "target": "mypkg.util.compute", "new_params": ["x", "y"]},
  "edit": ["mypkg/util.py", "def compute(x, y=0):", "def compute(x, y):"]}]
```

- `mutation` is what the tool simulates. `edit` is the same change really made in a **copy** of your project: `[file, exact old text (it must appear once), new text]`.
- Your project is never changed. Results go to `validation_results_<project>.json` in the folder you run it from.
- Your project's tests must pass before the change, otherwise the comparison means nothing.
