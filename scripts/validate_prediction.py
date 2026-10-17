"""Does the tool predict which tests really fail?

For each experiment:  predict (graph simulation)  ->  really edit a COPY of the repo  ->  run the real test suite
->  compare. Nothing in your checkout is changed.

    uv run python scripts/validate_prediction.py ~/graphhacks/targets/cookiecutter /path/to/python-with-test-deps
The second argument is a Python that has the target's test dependencies installed (see docs/VALIDATION.md).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # run from anywhere

from redislite.falkordb_client import FalkorDB  # noqa: E402

import preflight.db as D  # noqa: E402
from preflight.ingestion.python.pipeline import sync_workspace  # noqa: E402
from preflight.simulate import run_simulation  # noqa: E402

G = "cookiecutter.generate.generate_files"
EXPERIMENTS = [
    {"name": "E1 make context and output_dir required", "target": G,
     "mutation": {"kind": "change_params", "target": G, "new_params": [
         "repo_dir", "context", "output_dir", "overwrite_if_exists=", "skip_if_file_exists=",
         "accept_hooks=", "keep_project_on_failure="]},
     "edit": ("cookiecutter/generate.py",
              "    context: dict[str, Any] | None = None,\n    output_dir: Path | str = '.',\n",
              "    context: dict[str, Any] | None,\n    output_dir: Path | str,\n")},
    {"name": "E2 rename generate_files (definition only)", "target": G,
     "mutation": {"kind": "rename", "target": G, "new_name": "render_project"},
     "edit": ("cookiecutter/generate.py", "def generate_files(", "def render_project(")},
    {"name": "E3 add required param to read_user_variable", "target": "cookiecutter.prompt.read_user_variable",
     "mutation": {"kind": "change_params", "target": "cookiecutter.prompt.read_user_variable",
                  "new_params": ["var_name", "default_value", "extra", "prompts=", "prefix="]},
     "edit": ("cookiecutter/prompt.py",
              "def read_user_variable(var_name: str, default_value, prompts=None, prefix: str = \"\"):",
              "def read_user_variable(var_name: str, default_value, extra, prompts=None, prefix: str = \"\"):")},
    {"name": "E4 add required param to replay.dump", "target": "cookiecutter.replay.dump",
     "mutation": {"kind": "change_params", "target": "cookiecutter.replay.dump",
                  "new_params": ["replay_dir", "template_name", "context", "version"]},
     "edit": ("cookiecutter/replay.py",
              "def dump(replay_dir: Path | str, template_name: str, context: dict[str, Any]) -> None:",
              "def dump(replay_dir: Path | str, template_name: str, context: dict[str, Any], version: str) -> None:")},
    {"name": "E5 rename make_sure_path_exists (definition only)", "target": "cookiecutter.utils.make_sure_path_exists",
     "mutation": {"kind": "rename", "target": "cookiecutter.utils.make_sure_path_exists", "new_name": "ensure_dir"},
     "edit": ("cookiecutter/utils.py", "def make_sure_path_exists(", "def ensure_dir(")},
]


def to_qname(nodeid: str) -> str:
    path, *rest = nodeid.split("::")
    mod = path[:-3].replace("/", ".") if path.endswith(".py") else path
    return ".".join([mod] + [re.sub(r"\[.*$", "", r) for r in rest])


def run_real(copy: Path, python: str) -> tuple[set[str], set[str], str]:
    """Run the real test suite. Returns (failing test qnames, files that failed to collect, summary line)."""
    env = {**os.environ, "PYTHONPATH": str(copy)}
    p = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "--tb=no",
                        "-rfE", "--continue-on-collection-errors"], cwd=copy, env=env, capture_output=True, text=True)
    failing, files = set(), set()
    for line in p.stdout.splitlines():
        m = re.match(r"^(FAILED|ERROR) (\S+)", line)
        if m:
            (failing if "::" in m.group(2) else files).add(to_qname(m.group(2)) if "::" in m.group(2) else m.group(2))
    return failing, files, (p.stdout.strip().splitlines() or [""])[-1]


def main() -> None:
    root, python = Path(sys.argv[1]).expanduser().resolve(), sys.argv[2]
    db = FalkorDB(str(Path(tempfile.mkdtemp()) / "v.db"))
    D._db = db
    canon = D.canon_name(root)
    sync_workspace(root, canon, db=db)
    g = db.select_graph(canon)
    tests_in_file: dict[str, set[str]] = {}
    for q, f in g.ro_query("MATCH (t:Function {is_test:true}) RETURN t.qname, t.file").result_set:
        tests_in_file.setdefault(f, set()).add(q)
    all_tests = {q for s in tests_in_file.values() for q in s}

    results = []
    for ex in EXPERIMENTS:
        sim = run_simulation(canon, ex["target"], [{"name": "x", "mutations": [ex["mutation"]]}], "v", parallel=False)
        pred = set(sim["ranked"][0]["expected_failing_tests"])
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / "repo"
            shutil.copytree(root, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "*.egg-info"))
            rel, old, new = ex["edit"]
            text = (copy / rel).read_text()
            assert text.count(old) == 1, f"edit target not unique in {rel}"
            (copy / rel).write_text(text.replace(old, new))
            failing, bad_files, summary = run_real(copy, python)
        actual = {t for t in failing if t in all_tests}
        for f in bad_files:                                  # a file that cannot be collected: all its tests fail
            actual |= tests_in_file.get(f, set())
        tp = pred & actual
        r = {"experiment": ex["name"], "predicted": len(pred), "actual": len(actual), "both": len(tp),
             "precision": round(len(tp) / len(pred), 3) if pred else None,
             "recall": round(len(tp) / len(actual), 3) if actual else None,
             "false_positives": sorted(pred - actual)[:6], "missed": sorted(actual - pred)[:6],
             "pytest": summary, "score": sim["ranked"][0]["score"]}
        results.append(r)
        print(f"\n{ex['name']}\n  pytest: {summary}\n  predicted {r['predicted']} failing | actually failed {r['actual']} | "
              f"in both {r['both']} | precision {r['precision']} | recall {r['recall']}")
        for k in ("false_positives", "missed"):
            for t in r[k][:3]:
                print(f"    {k}: {t}")
    Path("docs").mkdir(exist_ok=True)
    Path("docs/validation_results.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
