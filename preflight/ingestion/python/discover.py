"""Stage 0: find .py files and give each one its dotted module name."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {
    "__pycache__", "node_modules", "build", "dist", "site-packages", "docs",
    "venv", "env", "htmlcov",
    "vendor", "vendored", "_vendor", "third_party", "thirdparty",   # copied-in libraries
}
TEST_DIRS = {"test", "tests", "testing", "integration", "e2e"}
MAX_BYTES = 1_000_000


@dataclass(slots=True)
class SourceFile:
    path: Path
    rel: str            # relative to workspace root, "/" separators
    module: str         # dotted name, e.g. "cookiecutter.main"
    is_package: bool    # True for __init__.py
    is_test_file: bool
    is_conftest: bool
    sha256: str


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIRS or name.endswith(".egg-info")


def module_qname(root: Path, path: Path) -> tuple[str, bool] | None:
    """Dotted module name for a file, or None if the name is not importable.

    Rule: walk up while the folder has __init__.py. That chain is the package.
    A file outside any package is named by its path from the workspace root."""
    is_pkg = path.name == "__init__.py"
    chain: list[str] = []
    d = path.parent
    while (d == root or root in d.parents) and (d / "__init__.py").is_file():
        chain.append(d.name)
        d = d.parent
    chain.reverse()
    if chain:
        parts = chain + ([] if is_pkg else [path.stem])
    else:
        rel = path.relative_to(root).with_suffix("")
        parts = list(rel.parts)
        if is_pkg:
            parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts), is_pkg


def discover(root: Path) -> tuple[list[SourceFile], dict[str, int]]:
    """List source files. Second value counts what was skipped, and why."""
    root = root.resolve()
    skipped = {"not_importable": 0, "too_big": 0}
    found: list[SourceFile] = []

    def walk(d: Path) -> None:
        for entry in sorted(d.iterdir()):
            if entry.is_dir():
                if not _skip_dir(entry.name):
                    walk(entry)
            elif entry.suffix == ".py":
                if entry.stat().st_size > MAX_BYTES:
                    skipped["too_big"] += 1
                    continue
                named = module_qname(root, entry)
                if named is None:
                    skipped["not_importable"] += 1  # e.g. template folders like {{cookiecutter.x}}
                    continue
                module, is_pkg = named
                rel = entry.relative_to(root)
                stem = entry.stem
                is_test = (
                    any(p in TEST_DIRS for p in rel.parts[:-1])
                    or stem.startswith("test_") or stem.endswith("_test") or stem == "conftest"
                )
                found.append(SourceFile(
                    path=entry, rel=rel.as_posix(), module=module, is_package=is_pkg,
                    is_test_file=is_test, is_conftest=stem == "conftest",
                    sha256=hashlib.sha256(entry.read_bytes()).hexdigest(),
                ))

    walk(root)
    return found, skipped


CONFIG_FILES = ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "tasks.py",
                "requirements.txt", "dev-requirements.txt", "requirements-dev.txt", "setup.py")


def detect_relaxed(root: Path) -> bool:
    """True when the project uses the pytest-relaxed plugin (invoke, fabric).
    There, ANY public function or method in a test file is a test."""
    for name in CONFIG_FILES:
        f = root / name
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                continue
            if "pytest-relaxed" in text or "pytest_relaxed" in text:
                return True
    return False
