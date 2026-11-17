---
name: verify-before-commit
description: Pre-commit verification for quant_system. Use before committing or opening a PR for any change to code, scheduled-task wrappers, monitoring, the broker path or forward-test tooling, and whenever claiming a fix "works". Catches the silent-failure class this repo keeps hitting (no-op commands, exit code 0 on failure, hardcoded periods, tests that cannot fail).
---

# Verify before commit

Every serious bug in this repo looked like it worked. Run this before any commit that
changes behavior. Report each step's outcome honestly, including what you could not
check.

## 1. Run what CI runs

```bash
python -m pytest tests/ broker/test_killswitch_trips.py broker/test_book_guard.py -q
```

All must pass. Never skip, xfail or delete a test to get there, and never add to
`monitoring/known_test_failures.txt`.

## 2. Mutation check: would the test catch the bug?

For each test you added or changed, and for every fix:

1. Break the code under test on purpose: revert the fix, flip a comparison, return
   early, make the job exit 0.
2. Run the test. **It must fail.** If it still passes, it proves nothing: rewrite it.
3. Restore the code (`git diff` must show only your intended change afterwards).

Required for changes to: `broker/` (kill-switches, execution, sizing), `monitoring/`,
`forward_test/` (snapshot, evaluate), any `.bat`/`.ps1`. Optional for docs-only.

## 3. Silent-failure questions

Answer each for the change, with evidence, not belief:

- **Does a failure exit non-zero?** What does the job print and return when its input
  is empty, the network is down, or the file is missing? "0 rows" must not be exit 0.
- **Does every command you print or document actually run, as written, and do the
  thing claimed?** Including `RUN:` hints and README lines. A remediation that runs but
  does nothing is a bug.
- **Is anything hardcoded that should be read?** Period names
  (`config.settings.ACTIVE_FT_PERIOD`), paths, dates, account ids.
- **Does it handle both directions?** A rebalance must sell as well as buy; a trim is
  not an exit; a gate must block as well as pass.
- **Does the docstring or comment still describe the code?** Stale docs are how the
  next session gets misled.
- **Is the claimed result supported?** Did the test assert the number, or only that no
  exception was raised?

## 4. Run the real thing where you can

Tests use fakes. Where it is safe, also run the actual entry point (dry-run, read-only,
or against a temp dir) and read its output. Never run anything that could place an
order outside `--dry-run`/`--read-only`, and never against port 4001.

## 5. Re-read the diff adversarially

`git diff` once more, asking: what would make CI or the user's Windows machine reject
this? (Windows paths, `.bat` ERRORLEVEL handling, encoding: wrappers set
`PYTHONIOENCODING=utf-8`, Python 3.12 there vs CI.)

## 6. Report

In the commit message or the reply, state what was verified and how, and say plainly
what was not (e.g. "not run against IB Gateway", "yfinance not reachable from the
sandbox; covered by a monkeypatched test only").
