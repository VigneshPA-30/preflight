"""The command-line tools a tester can use on any Python project, with no agent."""
import json
import subprocess
import sys
from pathlib import Path

from test_ingest_python import NUANCE, make

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True, timeout=120)


def test_simulate_cli_compares_plans_and_names_the_silent_break(tmp_path):
    ws = make(tmp_path / "proj", NUANCE)
    r = run("-m", "preflight.simulate_cli", str(ws), "pkg.util.compute",
            "--plan", "adapter=add:z=", "--plan", "migrate=add:z", "--plan", "rename=rename:calc",
            "--plan", "alias=rename+alias:calc")
    assert r.returncode == 0, r.stderr[-400:]
    assert "Winner: adapter" in r.stdout
    assert "[masked  ] pkg.b.run_b" in r.stdout and "[untested] pkg.c.run_c" in r.stdout      # silent ones are listed
    assert "missing required argument(s): z" in r.stdout and "alias" in r.stdout
    assert "Edit checklist for 'adapter'" in r.stdout and "pkg/util.py" in r.stdout


def test_simulate_cli_relative_edits_and_full_list(tmp_path):
    ws = make(tmp_path / "proj", NUANCE)
    r = run("-m", "preflight.simulate_cli", str(ws), "pkg.util.compute",
            "--plan", "drop=remove:y", "--plan", "same=params:x,y")
    assert r.returncode == 0 and "same" in r.stdout and "safe" in r.stdout and "drop" in r.stdout
    assert "unknown keyword" in r.stdout or "passes 2 positional" in r.stdout        # removing y breaks the callers


def test_simulate_cli_helpful_errors(tmp_path):
    ws = make(tmp_path / "proj", NUANCE)
    r = run("-m", "preflight.simulate_cli", str(ws), "pkg.util.compte", "--plan", "a=add:z=")
    assert r.returncode == 1 and "function not found" in r.stdout and "pkg.util.compute" in r.stdout
    r = run("-m", "preflight.simulate_cli", str(ws), "pkg.util.compute", "--plan", "bad=explode:now")
    assert r.returncode == 2 and "cannot read the plan action" in r.stdout
    r = run("-m", "preflight.simulate_cli", str(ws), "pkg.util.compute", "--plan", "x=remove:nothing")
    assert r.returncode == 2 and "is not a parameter" in r.stdout
    r = run("-m", "preflight.simulate_cli", str(tmp_path / "nope"), "a.b", "--plan", "a=add:z=")
    assert r.returncode == 2 and "not a folder" in r.stdout


def test_validation_script_works_on_any_project(tmp_path):
    """The prediction-versus-reality experiment, on a tiny project with its own real tests."""
    ws = make(tmp_path / "proj", {
        "pkg/__init__.py": "",
        "pkg/util.py": "def compute(x, y=0):\n    return x + y\n",
        "pkg/a.py": "from pkg.util import compute\ndef run_a():\n    return compute(1)\n",
        "tests/__init__.py": "",
        "tests/test_a.py": "from pkg import a\ndef test_a():\n    assert a.run_a() == 1\n",
    })
    exp = [{"name": "make y required", "target": "pkg.util.compute",
            "mutation": {"kind": "change_params", "target": "pkg.util.compute", "new_params": ["x", "y"]},
            "edit": ["pkg/util.py", "def compute(x, y=0):", "def compute(x, y):"]}]
    f = tmp_path / "exp.json"
    f.write_text(json.dumps(exp))
    r = run(str(ROOT / "scripts" / "validate_prediction.py"), str(ws), sys.executable, str(f), cwd=tmp_path)
    assert r.returncode == 0, r.stderr[-500:]
    assert "predicted 1 failing | actually failed 1 | in both 1" in r.stdout
    assert "precision 1.0 | recall 1.0" in r.stdout
    assert (tmp_path / "validation_results_proj.json").exists()          # written next to you, not into the repo docs
