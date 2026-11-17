"""
tests/test_requirements.py — requirements.txt lists exactly what the code imports.

It once listed torch, transformers, langchain and six more packages nothing imported
(gigabytes on a fresh install), while five packages the code did import were missing.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL = {"agents", "backtesting", "broker", "config", "dashboard", "data", "database",
         "factors", "forward_test", "models", "monitoring", "research", "scripts", "tests"}
# import name -> pip name, where they differ
PIP = {"sklearn": "scikit-learn", "bs4": "beautifulsoup4", "dotenv": "python-dotenv",
       "finnhub": "finnhub-python"}
# listed but never imported, each for a stated reason
NOT_IMPORTED = {"matplotlib": "pandas Styler.background_gradient on the dashboard",
                "tzdata": "zoneinfo time zone data on Windows (forward_test/snapshot.py)"}


def _listed() -> set[str]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return {re.split(r"[=<>\s#]", ln.strip(), maxsplit=1)[0].lower()
            for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}


def _imported() -> set[str]:
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True,
                           text=True, check=True).stdout.split()
    out = set()
    for f in files:
        for n in ast.walk(ast.parse((ROOT / f).read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                names = [n.module]
            else:
                continue
            for m in names:
                top = m.split(".")[0]
                if top not in sys.stdlib_module_names and top not in LOCAL:
                    out.add(PIP.get(top, top).lower())
    return out


def test_every_imported_package_is_listed():
    missing = _imported() - _listed()
    assert not missing, f"imported but not in requirements.txt: {sorted(missing)}"


def test_every_listed_package_is_imported_or_has_a_reason():
    unused = _listed() - _imported() - set(NOT_IMPORTED)
    assert not unused, f"in requirements.txt but never imported: {sorted(unused)}"


def _pins(name: str) -> dict[str, str]:
    out = {}
    for ln in (ROOT / name).read_text(encoding="utf-8").splitlines():
        ln = ln.split("#", 1)[0].strip()
        if ln:
            pkg, _, ver = ln.partition("==")
            out[pkg.strip().lower()] = ver.strip()
    return out


def test_ci_installs_exactly_the_machine_versions():
    """CI must test the versions the trading machine runs: every CI package pinned, to
    the same version as requirements.txt."""
    ci, machine = _pins("requirements-ci.txt"), _pins("requirements.txt")
    unpinned = sorted(p for p, v in ci.items() if not v)
    assert not unpinned, f"unpinned in requirements-ci.txt: {unpinned}"
    differ = {p: (v, machine.get(p)) for p, v in ci.items() if machine.get(p) != v}
    assert not differ, f"requirements-ci.txt vs requirements.txt: {differ}"
