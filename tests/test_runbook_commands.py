"""
tests/test_runbook_commands.py

Every remediation command the monitor prints, and every command the runbooks tell you
to run, must be INVOKABLE: the target exists, has an entry point, can import the
project when run the documented way, and accepts the flags it is given.

Why: the health check told you to run "python data/prices_updater.py" to fix stale
prices. The file had no entry point, so it exited 0 having done nothing -- and without
PYTHONPATH it could not even import `data`. The scheduled_task hint pointed at a script
that registers a DIFFERENT task. Both looked like fixes and changed nothing: the same
false-confidence class as the hardcoded freshness checker and the %ERRORLEVEL% wrapper.

Nothing here executes a target's work: checks are static (AST), except
prices_updater's argument parsing, which runs with --dry-run and never writes.
"""

from __future__ import annotations

import ast
import re
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitoring import checks as ck  # noqa: E402

PROJECT_PACKAGES = {p.parent.name for p in ROOT.glob("*/__init__.py")}

# Code whose string literals carry remediation commands shown to a human.
HINT_SOURCES = [
    "monitoring/checks.py",
    "monitoring/alerting.py",
    "monitoring/health_check.py",
    "monitoring/break_test.py",
    "dashboard/app.py",
]
# Live runbooks. Retired/historical write-ups are excluded on purpose: they record
# what was run at the time, not what to run now.
DOC_SOURCES = [
    "README.md",
    "broker/RUNBOOK.md",
    "monitoring/README.md",
    "forward_test/README_2026Q4b.md",
]

_PY_CMD = re.compile(r"python(?:\.exe)? (-m [\w.]+|[\w./\\-]+\.py)([^`\"'\n|#)]*)")
_PS_FILE = re.compile(r"-File ([\w./\\-]+\.ps1)([^`\"'\n|#]*)")
_PS_XML = re.compile(r"Get-Content ([\w./\\-]+\.xml)")


# ── validation ──────────────────────────────────────────────────────────────────

def _has_entry_point(tree: ast.Module) -> bool:
    """An `if __name__ == "__main__"` block, or top-level code that does something.

    A module of only imports, defs, classes, assignments and path setup is a library:
    running it does nothing -- which is exactly the prices_updater bug.
    """
    for node in tree.body:
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test):
            return True
    inert = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
             ast.ClassDef, ast.Assign, ast.AnnAssign, ast.AugAssign)
    for node in tree.body:
        if isinstance(node, inert):
            continue
        src = ast.unparse(node)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue                                    # docstring
        if "sys.path" in src or "warnings." in src:
            continue                                    # setup, not work
        return True
    return False


def _first_project_import_line(tree: ast.Module) -> int | None:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.split(".")[0] in PROJECT_PACKAGES:
                return node.lineno
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] in PROJECT_PACKAGES for a in node.names):
                return node.lineno
    return None


def _root_on_path_before(tree: ast.Module, line: int) -> bool:
    return any("sys.path.insert" in ast.unparse(n) or "sys.path.append" in ast.unparse(n)
               for n in tree.body if n.lineno < line)


def _exported_names(mod: str) -> set[str] | None:
    """Top-level names a project module binds. None = the module does not exist.

    Read from source rather than imported: importing to ask `hasattr` would execute
    module-level code (agents.sentiment_agent reads settings and builds a prompt at
    import time), and a static check must not have side effects.
    """
    path = ROOT / (mod.replace(".", "/") + ".py")
    pkg = ROOT / mod.replace(".", "/")
    if not path.exists():
        if not (pkg / "__init__.py").exists():
            return None
        path = pkg / "__init__.py"
    names: set[str] = set()
    if pkg.is_dir():
        names.update(p.stem for p in pkg.glob("*.py"))       # submodules are names too
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


def _guarded_import_lines(tree: ast.Module) -> set[int]:
    """Imports inside a try/except -- a deliberate optional dependency, not a defect."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    out.add(sub.lineno)
    return out


def _broken_project_imports(tree: ast.Module, path: Path) -> list[str]:
    """`from <project module> import name` where that module defines no such name.

    Walks the WHOLE tree, not only the top level. live_screener's --sentiment flag
    imported `analyze_batch_sentiment` from agents.sentiment_agent, a name that module
    has never exported, from inside `if use_sentiment:` -- so the script imported fine,
    the flag parsed fine, and it died only for whoever actually passed the flag. Same
    false-confidence class as a hint that runs and does nothing: the failure is real
    from the day it is documented, and nothing reports it until it is used.
    """
    guarded = _guarded_import_lines(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 0 or not node.module:
            continue
        if node.lineno in guarded or node.module.split(".")[0] not in PROJECT_PACKAGES:
            continue
        exported = _exported_names(node.module)
        where = f"{path.relative_to(ROOT)}:{node.lineno}"
        if exported is None:
            out.append(f"{where} imports from {node.module}, which does not exist")
            continue
        for alias in node.names:
            if alias.name != "*" and alias.name not in exported:
                out.append(f"{where} imports {alias.name} from {node.module}, "
                           f"which does not define it")
    return out


def problems_with(cmd: str) -> list[str]:
    """Why `cmd` would not work as printed. Empty list = invokable."""
    out: list[str] = []
    for target, rest in _PY_CMD.findall(cmd):
        if target in ("-m pytest", "-m venv", "-m pip"):          # not ours
            continue
        by_module = target.startswith("-m ")
        if by_module:
            mod = target[3:]
            path = ROOT / (mod.replace(".", "/") + ".py")
            if not path.exists():
                path = ROOT / mod.replace(".", "/") / "__main__.py"
        else:
            path = ROOT / target.replace("\\", "/")
        if not path.exists():
            out.append(f"{cmd!r}: {target} does not exist")
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        if not _has_entry_point(tree):
            out.append(f"{cmd!r}: {path.relative_to(ROOT)} has no entry point -- "
                       f"running it does nothing")
        if not by_module:
            line = _first_project_import_line(tree)
            if line and not _root_on_path_before(tree, line):
                out.append(f"{cmd!r}: run as a path, sys.path[0] is "
                           f"{path.parent.relative_to(ROOT)}/ so the project import on "
                           f"line {line} raises ModuleNotFoundError")
        for msg in _broken_project_imports(tree, path):
            out.append(f"{cmd!r}: {msg}")
        for flag in re.findall(r"(?<![\w-])(--[\w-]+)", rest):
            if f'"{flag}"' not in src and f"'{flag}'" not in src:
                out.append(f"{cmd!r}: {path.relative_to(ROOT)} does not accept {flag}")
    for script, rest in _PS_FILE.findall(cmd):
        path = ROOT / script.replace("\\", "/")
        if not path.exists():
            out.append(f"{cmd!r}: {script} does not exist")
            continue
        src = path.read_text(encoding="utf-8-sig")
        for flag in re.findall(r"(?<![\w-])-([A-Z]\w+)", rest):
            if f"${flag}" not in src:
                out.append(f"{cmd!r}: {script} has no -{flag} parameter")
    for xml in _PS_XML.findall(cmd):
        if not (ROOT / xml.replace("\\", "/")).exists():
            out.append(f"{cmd!r}: {xml} does not exist")
    return out


def _commands_in(rel: str) -> list[str]:
    text = (ROOT / rel).read_text(encoding="utf-8")
    return [m.group(0) for rx in (_PY_CMD, _PS_FILE) for m in rx.finditer(text)]


# ── the validator catches the bugs it exists for ────────────────────────────────

class TestValidatorCatchesKnownBugs:
    def test_library_module_with_no_entry_point(self):
        # data.prices is a pure library, as prices_updater used to be.
        assert any("no entry point" in p for p in problems_with("python -m data.prices"))

    def test_missing_module(self):
        assert problems_with("python -m data.pipeline")

    def test_the_windows_venv_form_is_checked_too(self):
        # NEXT_STEPS.md gives commands as venv\Scripts\python.exe -m ...; a pattern
        # that only knew "python -m" let those through unchecked.
        assert problems_with(r"venv\Scripts\python.exe -m data.pipeline")
        assert not problems_with(r"venv\Scripts\python.exe -m research.oos_picking")

    def test_path_run_without_root_on_sys_path(self, tmp_path, monkeypatch):
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "x.py").write_text(
            "from data.storage import get_session\nif __name__ == '__main__':\n    pass\n")
        monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
        assert any("ModuleNotFoundError" in p for p in problems_with("python data/x.py"))

    def test_unknown_flag(self):
        assert any("--no-such-flag" in p for p in
                   problems_with("python -m data.prices_updater --no-such-flag"))

    def test_import_of_a_name_the_module_does_not_export(self, tmp_path, monkeypatch):
        """The live_screener --sentiment bug, reproduced: a real module, a dead name."""
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "__init__.py").write_text("")
        (tmp_path / "agents" / "s.py").write_text("def analyse_portfolio(t):\n    ...\n")
        (tmp_path / "go.py").write_text(
            "if __name__ == '__main__':\n"
            "    from agents.s import analyze_batch, analyse_portfolio\n")
        monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
        monkeypatch.setattr(sys.modules[__name__], "PROJECT_PACKAGES", {"agents"})
        bad = problems_with("python go.py")
        assert any("analyze_batch" in p and "does not define it" in p for p in bad)
        assert not any("analyse_portfolio" in p for p in bad)   # the good one is quiet

    def test_import_inside_a_try_is_not_a_defect(self, tmp_path, monkeypatch):
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "__init__.py").write_text("")
        (tmp_path / "agents" / "s.py").write_text("x = 1\n")
        (tmp_path / "go.py").write_text(
            "try:\n    from agents.s import optional_extra\n"
            "except ImportError:\n    optional_extra = None\n"
            "if __name__ == '__main__':\n    pass\n")
        monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
        monkeypatch.setattr(sys.modules[__name__], "PROJECT_PACKAGES", {"agents"})
        assert problems_with("python go.py") == []

    def test_the_extractor_actually_finds_commands(self):
        """Guard the guard: a regex that silently stops matching passes everything."""
        found = {rel: _commands_in(rel) for rel in HINT_SOURCES + DOC_SOURCES}
        total = sum(len(v) for v in found.values())
        assert total >= 25, f"only {total} commands extracted: {found}"

    def test_wrong_register_script_is_not_the_snapshot_task(self):
        # The old hint: runnable, but it registers QuantHealthCheck.
        src = (ROOT / "monitoring/register_tasks.ps1").read_text(encoding="utf-8-sig")
        assert ck.TASK_NAME not in re.findall(r'\$TaskName = "(\w+)"', src)


# ── the monitor's hints ─────────────────────────────────────────────────────────

class TestHealthCheckHints:
    @pytest.mark.parametrize("rel", HINT_SOURCES)
    def test_every_hint_literal_is_invokable(self, rel):
        cmds = _commands_in(rel)
        assert cmds or rel == "monitoring/health_check.py"
        bad = [p for c in cmds for p in problems_with(c)]
        assert not bad, "\n".join(bad)

    def test_stale_prices_hint_end_to_end(self, tmp_path, monkeypatch):
        """Build a stale DB, run the real check, validate the command it prints."""
        import sqlite3
        import config.settings as settings
        db = tmp_path / "p.db"
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE prices (ticker TEXT, date TEXT)")
            c.execute("INSERT INTO prices VALUES ('AMN', '2026-09-18')")
        monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{db}")

        r = ck.check_prices_db(today=date(2026, 9, 30))
        assert r.status == ck.FAIL and r.name == "prices_db_fresh"
        assert problems_with(r.runbook) == []
        assert r.runbook.startswith("python -m data.prices_updater")

    def test_prices_hint_lookback_closes_the_gap(self):
        latest, today = date(2026, 7, 1), date(2026, 9, 30)
        days = int(ck.prices_refresh_command(latest, today).split()[-1])
        assert days > (today - latest).days
        assert int(ck.prices_refresh_command().split()[-1]) == \
            ck.PRICES_DEFAULT_LOOKBACK_DAYS

    def test_prices_hint_arguments_parse(self, monkeypatch):
        """The flags in the hint are accepted by the real parser (dry run: no fetch)."""
        from data import prices_updater as pu
        monkeypatch.setattr(pu, "_latest_recorded_date", lambda: None)
        monkeypatch.setattr(pu, "update_recent_prices",
                            lambda *a, **k: pytest.fail("dry run fetched"))
        argv = ck.prices_refresh_command(date(2026, 9, 18),
                                         date(2026, 9, 30)).split()[3:]
        assert pu.main(argv + ["--tickers", "AMN", "--dry-run"]) == 0

    def test_unregistered_snapshot_task_hint_registers_that_task(self, monkeypatch):
        monkeypatch.setattr(ck, "_task_info", lambda name: {})
        r = ck.check_scheduled_task()
        assert r.status == ck.FAIL
        assert f"-TaskName {ck.TASK_NAME}" in r.runbook
        assert "register_tasks.ps1" not in r.runbook
        assert problems_with(r.runbook) == []


# ── the runbooks ────────────────────────────────────────────────────────────────

def test_monthly_run_steps_are_invokable():
    """scripts/monthly_run.py starts each step as `python -m <module>` in a subprocess;
    no README line names them, so a removed or renamed step module would only fail on
    the day of the run."""
    tree = ast.parse((ROOT / "scripts" / "monthly_run.py").read_text(encoding="utf-8"))
    mods = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
            and isinstance(n.value, str) and re.fullmatch(r"(scripts|agents)\.\w+", n.value)]
    assert len(mods) >= 3, mods
    for m in mods:
        assert not problems_with(f"python -m {m}"), (m, problems_with(f"python -m {m}"))


@pytest.mark.parametrize("rel", DOC_SOURCES)
def test_runbook_commands_are_invokable(rel):
    bad = [p for c in _commands_in(rel) for p in problems_with(c)]
    assert not bad, "\n".join(bad)
