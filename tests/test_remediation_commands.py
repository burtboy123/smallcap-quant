"""
tests/test_remediation_commands.py — every command the monitoring tells you to run
must actually be runnable.

WHY THIS EXISTS

`check_prices_db` printed `RUN: python data/prices_updater.py`. That module had no
`__main__` block and no argparse, and running a script inside `data/` puts `data/` on
`sys.path` rather than the repo root — so the command either died on
`ModuleNotFoundError: No module named 'data'` or, with PYTHONPATH set, imported
cleanly and exited having done nothing.

Separately, `check_scheduled_task` printed
`powershell -File monitoring/register_tasks.ps1`, which exits 2 with a refusal because
registering an S4U task needs elevation and the `-Elevate` flag was missing.

Both are the same class of bug as the freshness checker hardcoded to the wrong period
and the wrapper that masked `%ERRORLEVEL%`: **the remediation looks like it worked when
nothing happened.** An alert whose fix command is a no-op is worse than no alert,
because it converts a real problem into a false belief that the problem is handled.

WHAT THIS TEST CAN AND CANNOT DO

It statically extracts every remediation string from `monitoring/checks.py` and asserts
each is *invokable*: the script exists and has an entry point, the `-m` module resolves
and has one, the referenced file exists, the cmdlet is a real cmdlet. It deliberately
does NOT execute them — several fetch from the network, register scheduled tasks, or
take minutes. Invokability is the property that regressed; correctness of effect is
covered by actually using them.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import re
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

CHECKS_PY = ROOT / "monitoring" / "checks.py"

# PowerShell cmdlets that may legitimately appear in a hint. Listed explicitly rather
# than shelling out to Get-Command, so the test does not depend on a PowerShell host.
KNOWN_CMDLETS = {"Get-ScheduledTaskInfo", "Enable-ScheduledTask", "Disable-ScheduledTask",
                 "Start-ScheduledTask", "Get-ScheduledTask", "Register-ScheduledTask",
                 "Unregister-ScheduledTask", "Get-Content"}

# cmd.exe builtins that are not files on PATH.
CMD_BUILTINS = {"type", "dir", "echo", "more"}


def _literal(node) -> str | None:
    """Text of a str / f-string / implicit-or-explicit concatenation node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                # Substitute a placeholder so the command still parses. The value of a
                # runtime f-string field cannot be known statically.
                parts.append("PLACEHOLDER")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def extract_remediation_commands() -> list[tuple[int, str]]:
    """Every `runbook=...` kwarg and `rb = ...` assignment in monitoring/checks.py."""
    tree = ast.parse(io.open(CHECKS_PY, encoding="utf-8").read())
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "runbook":
                    text = _literal(kw.value)
                    if text:
                        out.append((node.lineno, text))
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                # `rb = ...` in a check, or a module-level hint constant such as
                # SNAPSHOT_TASK_REGISTER / PRICES_TASK_REGISTER.
                if isinstance(tgt, ast.Name) and (tgt.id == "rb"
                                                  or tgt.id.endswith("_REGISTER")):
                    text = _literal(node.value)
                    if text:
                        out.append((node.lineno, text))
    # de-duplicate, keeping the first line each command appears on
    seen: dict[str, int] = {}
    for line, text in out:
        seen.setdefault(text, line)
    return sorted((line, text) for text, line in seen.items())


def generated_remediation_commands() -> list[tuple[int, str]]:
    """Hints built at runtime, which the static extractor cannot see.

    prices_db_fresh sizes its --lookback-days to the actual gap, so its command comes
    from prices_refresh_command(). Exercise the default and a wide gap.
    """
    from datetime import date
    from monitoring import checks as ck
    return [(0, ck.prices_refresh_command()),
            (0, ck.prices_refresh_command(date(2026, 7, 1), date(2026, 9, 30)))]


COMMANDS = extract_remediation_commands() + generated_remediation_commands()


def test_commands_were_actually_found():
    """Guard the guard: a broken extractor would make every test below vacuously pass."""
    assert len(COMMANDS) >= 8, (
        f"only found {len(COMMANDS)} remediation commands in {CHECKS_PY.name} — the "
        f"AST extractor is probably broken, which would make this whole file a no-op")
    joined = " ".join(t for _l, t in COMMANDS)
    assert "monitoring.health_check" in joined
    assert "prices_updater" in joined


MAIN_GUARD = re.compile(r"if\s+__name__\s*==\s*[\"']__main__[\"']")

# First-party packages. A script that imports one of these absolutely needs the repo
# root on sys.path when run as `python path/to/script.py`, because sys.path[0] is then
# the script's OWN directory.
FIRST_PARTY = ("data", "forward_test", "monitoring", "broker", "config", "factors",
               "backtesting", "agents", "scripts", "database")
FIRST_PARTY_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(" + "|".join(FIRST_PARTY) + r")\b", re.M)


def _check_python_script(path_str: str, cmd: str) -> None:
    """A `python <file.py>` target must exist AND actually do something when run."""
    p = ROOT / path_str
    assert p.exists(), f"{cmd!r} -> script does not exist: {path_str}"
    src = io.open(p, encoding="utf-8").read()
    assert MAIN_GUARD.search(src), (
        f"{cmd!r} -> {path_str} has NO `if __name__ == \"__main__\"` block, so running "
        f"it does nothing. This is the data/prices_updater.py bug: an alert whose fix "
        f"command is a silent no-op.")
    # The second half of that bug. Only scripts that actually import a first-party
    # package are affected: running `python data/prices_updater.py` put data/ on
    # sys.path[0], so `from data.prices import ...` raised ModuleNotFoundError before
    # main() could run. A script importing only stdlib and third-party packages (e.g.
    # forward_test/snapshot.py) needs no such fix, so this is not asserted blindly.
    if ("/" in path_str or "\\" in path_str) and FIRST_PARTY_IMPORT.search(src):
        assert "sys.path" in src, (
            f"{cmd!r} -> {path_str} imports a first-party package but never touches "
            f"sys.path. Run directly, sys.path[0] is its OWN directory, so that import "
            f"raises ModuleNotFoundError before main() is reached.")


def _check_python_module(mod: str, cmd: str) -> None:
    """A `python -m <module>` target must resolve AND be runnable.

    Runnable means one of two things, and both are legitimate: a module with a
    `__main__` guard, or a PACKAGE with a `__main__.py` submodule (how `python -m
    pytest` works — pytest/__init__.py has no guard at all).
    """
    try:
        spec = importlib.util.find_spec(mod)
    except Exception as e:                                            # noqa: BLE001
        pytest.fail(f"{cmd!r} -> `python -m {mod}` does not resolve: "
                    f"{type(e).__name__}: {e}")
    assert spec is not None and spec.origin, (
        f"{cmd!r} -> `python -m {mod}` does not resolve to a module")

    if spec.submodule_search_locations is not None:      # it is a package
        try:
            dunder = importlib.util.find_spec(f"{mod}.__main__")
        except Exception:                                             # noqa: BLE001
            dunder = None
        if dunder is not None:
            return
    src = io.open(spec.origin, encoding="utf-8").read()
    assert MAIN_GUARD.search(src), (
        f"{cmd!r} -> `python -m {mod}` does nothing: no __main__ guard, and no "
        f"{mod}.__main__ submodule either")


@pytest.mark.parametrize("line,cmd", COMMANDS, ids=[f"L{l}" for l, _c in COMMANDS])
def test_remediation_command_is_invokable(line: int, cmd: str):
    """Each remediation command must be something that can actually be run."""
    # posix=False keeps Windows backslashes intact.
    try:
        parts = shlex.split(cmd, posix=False)
    except ValueError as e:
        pytest.fail(f"checks.py:{line} -> command is not parseable: {cmd!r} ({e})")
    assert parts, f"checks.py:{line} -> empty command"
    exe = parts[0].strip('"')

    if exe == "python":
        assert len(parts) > 1, f"checks.py:{line} -> bare `python`: {cmd!r}"
        if parts[1] == "-m":
            _check_python_module(parts[2].strip('"'), cmd)
        elif parts[1] == "-c":
            pass                       # inline snippet; nothing to resolve statically
        else:
            _check_python_script(parts[1].strip('"').replace("\\", "/"), cmd)

    elif exe == "powershell":
        joined = " ".join(parts)
        if "-File" in parts:
            target = parts[parts.index("-File") + 1].strip('"').replace("\\", "/")
            p = ROOT / target
            assert p.exists(), f"{cmd!r} -> -File target missing: {target}"
            # register_tasks.ps1 refuses unelevated (S4U needs admin) and exits 2, so a
            # hint that tells you to register WITHOUT -Elevate does not work.
            if "register_tasks.ps1" in target:
                assert "-Elevate" in parts or "-WhatIf" in parts \
                    or "-Unregister" in parts, (
                    f"{cmd!r} -> register_tasks.ps1 REFUSES unelevated and exits 2. "
                    f"A registration hint must pass -Elevate.")
        elif "-Command" in parts:
            cmdlets = re.findall(r"\b([A-Z][a-zA-Z]+-[A-Z][a-zA-Z]+)\b", joined)
            assert cmdlets, f"{cmd!r} -> -Command names no recognisable cmdlet"
            for c in cmdlets:
                assert c in KNOWN_CMDLETS, (
                    f"{cmd!r} -> unknown cmdlet {c!r}; add it to KNOWN_CMDLETS if it "
                    f"is genuinely valid")
        else:
            pytest.fail(f"{cmd!r} -> powershell invocation with neither -File "
                        f"nor -Command")

    elif exe in CMD_BUILTINS:
        pass                           # `type <log>` etc. — a shell builtin, not a file

    else:
        pytest.fail(f"checks.py:{line} -> unrecognised command form: {cmd!r}. Add a "
                    f"rule here so it is verified rather than silently trusted.")


def test_prices_updater_hint_is_the_working_invocation():
    """Regression lock on the specific bug that motivated this file.

    The hint is `python -m data.prices_updater --lookback-days N` (N sized to the gap;
    -m resolves from the repo root regardless of sys.path).
    """
    from monitoring import checks as ck

    hints = [t for _l, t in COMMANDS if "prices_updater" in t]
    assert hints, "the prices_db_fresh remediation hint disappeared"
    for h in hints:
        assert h.startswith("python -m data.prices_updater --lookback-days "), h
    # And the module really does expose a CLI with the flag the hint passes.
    src = io.open(ROOT / "data" / "prices_updater.py", encoding="utf-8").read()
    assert "--lookback-days" in src, (
        "the hint passes --lookback-days but the CLI does not define it")
    assert re.search(r"if\s+__name__\s*==\s*[\"']__main__[\"']", src)
    assert ck.check_prices_db().runbook.startswith("python -m data.prices_updater")


def test_every_documented_python_m_target_resolves():
    """The same standard for commands in monitoring/README.md and broker/RUNBOOK.md.

    Docs rot the same way code does, and these two files are what a human follows at
    the moment something is broken.
    """
    failures = []
    for doc in (ROOT / "monitoring" / "README.md", ROOT / "broker" / "RUNBOOK.md"):
        text = io.open(doc, encoding="utf-8").read()
        for mod in set(re.findall(r"python -m ([A-Za-z_][\w.]*)", text)):
            if mod == "pytest":
                continue
            try:
                spec = importlib.util.find_spec(mod)
            except Exception:                                         # noqa: BLE001
                spec = None
            if spec is None or not spec.origin:
                failures.append(f"{doc.name}: `python -m {mod}` does not resolve")
                continue
            src = io.open(spec.origin, encoding="utf-8").read()
            if not re.search(r"if\s+__name__\s*==\s*[\"']__main__[\"']", src):
                failures.append(f"{doc.name}: `python -m {mod}` has no __main__ guard")
    assert not failures, "documented commands that do not work:\n  " + \
                         "\n  ".join(failures)


def test_every_documented_python_script_has_an_entry_point():
    """`python path/to/script.py` in the docs must also actually do something."""
    failures = []
    for doc in (ROOT / "monitoring" / "README.md", ROOT / "broker" / "RUNBOOK.md"):
        text = io.open(doc, encoding="utf-8").read()
        for rel in set(re.findall(r"python ([A-Za-z_][\w/\\.-]*\.py)", text)):
            p = ROOT / rel.replace("\\", "/")
            if not p.exists():
                failures.append(f"{doc.name}: {rel} does not exist")
                continue
            src = io.open(p, encoding="utf-8").read()
            if not re.search(r"if\s+__name__\s*==\s*[\"']__main__[\"']", src):
                failures.append(f"{doc.name}: {rel} has no __main__ guard — running it "
                                f"does nothing")
    assert not failures, "documented commands that do not work:\n  " + \
                         "\n  ".join(failures)
