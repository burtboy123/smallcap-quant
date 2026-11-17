"""
tests/test_backup.py — scripts/backup.py and the backup_fresh health check.

Temp directories only; the real database and backup folder are never touched.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitoring import checks as ck  # noqa: E402
from scripts import backup as bk     # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake repo: a DB with data, the three folders, and a .env with a secret."""
    root = tmp_path / "repo"
    (root / "broker" / "execution_log").mkdir(parents=True)
    (root / "broker" / "reconciliation").mkdir(parents=True)
    (root / "monitoring" / "state").mkdir(parents=True)
    (root / "broker" / "execution_log" / "run_1.json").write_text("{}")
    (root / "monitoring" / "state" / "alert_log.jsonl").write_text("{}\n")
    (root / ".env").write_text("ALERT_SMTP_PASSWORD=secret\n")
    db = root / "quant_system.db"
    with closing(sqlite3.connect(db)) as c, c:      # closing: Windows locks open files
        c.execute("CREATE TABLE prices (ticker TEXT, close REAL)")
        c.execute("INSERT INTO prices VALUES ('AMN', 35.5)")
    monkeypatch.setattr(bk, "ROOT", root)
    monkeypatch.setattr(bk, "db_path", lambda: db)
    return root


def test_backup_contains_a_readable_db_and_the_folders_but_not_env(repo, tmp_path):
    out = bk.make_backup(tmp_path / "dest")
    z = zipfile.ZipFile(out)
    names = set(z.namelist())
    assert {"quant_system.db", "broker/execution_log/run_1.json",
            "monitoring/state/alert_log.jsonl"} <= names
    assert not any(".env" in n for n in names)
    z.extract("quant_system.db", tmp_path / "x")
    with sqlite3.connect(tmp_path / "x" / "quant_system.db") as c:
        assert c.execute("SELECT close FROM prices").fetchone() == (35.5,)


def test_only_the_newest_backups_are_kept(repo, tmp_path):
    dest = tmp_path / "dest"
    for i in range(5):
        bk.make_backup(dest, keep=3, now=datetime(2026, 10, 1) + timedelta(days=7 * i))
    kept = [p.name for p in bk.list_backups(dest)]
    assert kept == ["quant_backup_2026-10-15_0000.zip", "quant_backup_2026-10-22_0000.zip",
                    "quant_backup_2026-10-29_0000.zip"]


def test_cli_reports_failure_with_exit_1(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "make_backup", lambda *a, **k: 1 / 0)
    assert bk.main(["--dest", str(tmp_path / "d")]) == 1


# ── backup_fresh ────────────────────────────────────────────────────────────────

@pytest.fixture
def dest(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    import scripts.backup as real
    monkeypatch.setattr(real, "backup_dir", lambda: d)
    monkeypatch.setattr(real, "db_path", lambda: None)     # disk placement tested below
    return d


def test_backup_on_the_same_disk_as_the_db_warns_without_failing(dest, tmp_path,
                                                                 monkeypatch):
    import scripts.backup as real
    db = tmp_path / "quant_system.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(real, "db_path", lambda: db)          # same tmp volume
    _zip(dest, 1)
    r = ck.check_backup()
    assert r.status == ck.WARN and not r.failed and "same disk" in r.summary
    assert r.metrics["same_disk"] is True


@pytest.fixture
def onedrive_dest(tmp_path, monkeypatch):
    """BACKUP_DIR inside a OneDrive folder, on the same volume as the DB."""
    import scripts.backup as real
    d = tmp_path / "OneDrive" / "quant_backups"
    d.mkdir(parents=True)
    db = tmp_path / "quant_system.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(real, "backup_dir", lambda: d)
    monkeypatch.setattr(real, "db_path", lambda: db)
    _zip(d, 1)
    return d


def test_onedrive_backup_passes_only_while_onedrive_runs(onedrive_dest, monkeypatch):
    monkeypatch.setattr(ck, "_onedrive_running", lambda: True)
    assert ck.check_backup().status == ck.PASS


def test_onedrive_backup_with_the_client_closed_warns(onedrive_dest, monkeypatch):
    """2026-11-17: BACKUP_DIR in OneDrive, client not running, and the check said PASS."""
    monkeypatch.setattr(ck, "_onedrive_running", lambda: False)
    r = ck.check_backup()
    assert r.status == ck.WARN and not r.failed and "not running" in r.summary


def test_onedrive_sync_unknown_off_windows_is_not_a_warning(onedrive_dest, monkeypatch):
    monkeypatch.setattr(ck, "_onedrive_running", lambda: None)
    assert ck.check_backup().status == ck.PASS


def test_onedrive_running_reads_tasklist_both_ways():
    class _Out:
        def __init__(self, s):
            self.stdout = s
    none = "INFO: No tasks are running which match the specified criteria.\n"
    some = "\nOneDrive.exe                 12345 Console                    1     95,312 K\n"
    assert ck._onedrive_running(True, lambda *a, **k: _Out(none)) is False
    assert ck._onedrive_running(True, lambda *a, **k: _Out(some)) is True
    assert ck._onedrive_running(False, lambda *a, **k: _Out(some)) is None

    def broken(*a, **k):
        raise OSError("no tasklist")
    assert ck._onedrive_running(True, broken) is None


def test_same_device_is_a_plain_device_comparison(tmp_path):
    db = tmp_path / "quant_system.db"
    db.write_bytes(b"x")
    assert ck._same_device(tmp_path, db) is True
    assert ck._same_device(tmp_path, None) is False


def test_a_verified_backup_removes_superseded_partials(repo, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    old = out / "quant_backup_2026-10-06_1440.zip.partial"
    old.write_bytes(b"half a zip")
    bk.make_backup(out)
    assert not old.exists()
    assert len(bk.list_backups(out)) == 1


def test_weekly_email_lists_warnings_as_to_dos():
    from monitoring import weekly_report as wr
    rs = [ck.CheckResult("backup_fresh", ck.WARN, "backup verified but on the same disk",
                         ck.INFO),
          ck.CheckResult("prices_db_fresh", ck.PASS, "ok", ck.INFO)]
    body = "\n".join(wr._health(rs))
    assert "to do (not urgent) backup_fresh" in body


def _zip(dest, age_days):
    dest.mkdir(exist_ok=True)
    when = datetime.now() - timedelta(days=age_days)      # named like real backups
    p = dest / f"quant_backup_{when:%Y-%m-%d_%H%M}.zip"
    with zipfile.ZipFile(p, "w") as z:                    # a real (tiny) zip: the
        z.writestr("monitoring/state/x.json", "{}")       # check now opens it
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))


def test_no_backup_ever_fails_as_a_warning(dest):
    r = ck.check_backup()
    assert (r.status, r.severity) == (ck.FAIL, ck.WARNING)
    assert r.runbook == "python scripts/backup.py"


def test_stale_backup_fails_and_recent_one_passes(dest):
    _zip(dest, ck.MAX_BACKUP_AGE_DAYS + 2)
    assert ck.check_backup().status == ck.FAIL
    _zip(dest, 1)
    assert ck.check_backup().status == ck.PASS


def test_backup_check_never_blocks_a_rebalance(dest):
    assert ck.check_backup().severity != ck.CRITICAL


# ── restore verification ────────────────────────────────────────────────────────

def test_backup_is_verified_and_carries_a_manifest(repo, tmp_path):
    p = bk.make_backup(tmp_path / "out")
    v = bk.verify_backup(p)
    assert v["ok"] and v["manifest"], v["problems"]
    assert v["tables"] == {"prices": 1}                  # restored DB, real row count
    with zipfile.ZipFile(p) as z:
        man = __import__("json").loads(z.read(bk.MANIFEST))
    assert man["db"] == "quant_system.db" and man["tables"] == {"prices": 1}
    assert "broker/execution_log/run_1.json" in man["files"]


def test_pytest_scratch_is_never_backed_up(repo, tmp_path):
    """2026-11-17: 542 of 575 files in a backup were pytest temp files."""
    junk = repo / "monitoring" / "state" / "pytest_tmp" / "test_x0"
    junk.mkdir(parents=True)
    (junk / "t.db").write_bytes(b"x")
    with zipfile.ZipFile(bk.make_backup(tmp_path / "out")) as z:
        names = z.namelist()
    assert "monitoring/state/alert_log.jsonl" in names
    assert not [n for n in names if "pytest_tmp" in n]


def _rewrite(p, drop=None, replace=None):
    """Copy a zip, dropping one member or replacing its bytes."""
    items = {}
    with zipfile.ZipFile(p) as z:
        for n in z.namelist():
            items[n] = z.read(n)
    if drop:
        items.pop(drop)
    if replace:
        items.update(replace)
    with zipfile.ZipFile(p, "w") as z:
        for n, b in items.items():
            z.writestr(n, b)


def test_a_missing_file_fails_verification(repo, tmp_path):
    p = bk.make_backup(tmp_path / "out")
    _rewrite(p, drop="broker/execution_log/run_1.json")
    v = bk.verify_backup(p)
    assert not v["ok"] and "missing" in v["problems"][0]


def test_a_database_with_lost_rows_fails_verification(repo, tmp_path):
    p = bk.make_backup(tmp_path / "out")
    db = tmp_path / "short.db"
    with closing(sqlite3.connect(db)) as c, c:      # closing: Windows locks open files
        c.execute("CREATE TABLE prices (ticker TEXT, close REAL)")   # 0 rows, not 1
    _rewrite(p, replace={"quant_system.db": db.read_bytes()})
    v = bk.verify_backup(p)
    assert not v["ok"] and "row counts differ" in v["problems"][0]


def test_a_garbage_database_fails_verification(repo, tmp_path):
    p = bk.make_backup(tmp_path / "out")
    _rewrite(p, replace={"quant_system.db": b"not a database" * 100})
    assert not bk.verify_backup(p)["ok"]


def test_a_corrupted_zip_fails_verification(repo, tmp_path):
    p = bk.make_backup(tmp_path / "out")
    raw = bytearray(p.read_bytes())
    mid = len(raw) // 2
    raw[mid:mid + 64] = b"\x00" * 64                   # flip bytes inside the data
    p.write_bytes(bytes(raw))
    assert not bk.verify_backup(p)["ok"]


def test_a_backup_that_does_not_verify_is_never_given_its_name(repo, tmp_path, monkeypatch):
    out = tmp_path / "out"
    old = bk.make_backup(out, now=datetime(2026, 9, 1))
    monkeypatch.setattr(bk, "verify_backup",
                        lambda p: {"ok": False, "problems": ["simulated"]})
    with pytest.raises(RuntimeError, match="did not verify"):
        bk.make_backup(out, keep=1)
    assert bk.list_backups(out) == [old]                 # old one kept, not rotated out
    assert list(out.glob("*.partial"))                   # left for inspection


def test_health_check_fails_on_a_backup_that_does_not_restore(repo, tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(bk, "backup_dir", lambda: out)
    monkeypatch.setattr(ck, "_same_device", lambda a, b: False)   # placement: own tests
    p = bk.make_backup(out)
    assert ck.check_backup().status == ck.PASS
    _rewrite(p, replace={"quant_system.db": b"junk" * 50})
    r = ck.check_backup()
    assert r.status == ck.FAIL and "does not restore" in r.summary
    assert r.severity == ck.WARNING                      # never blocks a rebalance


def test_cli_verify(repo, tmp_path, capsys):
    out = tmp_path / "out"
    bk.make_backup(out)
    assert bk.main(["--dest", str(out), "--verify"]) == 0
    assert "verified:" in capsys.readouterr().out
    assert bk.main(["--dest", str(tmp_path / "empty"), "--verify"]) == 1


def test_a_missing_database_fails_the_backup(repo, tmp_path):
    (bk.ROOT / "quant_system.db").unlink()
    with pytest.raises(FileNotFoundError, match="database not found"):
        bk.make_backup(tmp_path / "out")
    assert bk.list_backups(tmp_path / "out") == []


def test_every_database_connection_is_closed(repo, tmp_path, monkeypatch):
    """On Windows an open SQLite handle locks the file, so the temp copy could not be
    deleted and every backup failed with WinError 32 (2026-10-06). Linux does not lock,
    so this asserts the cause directly: every connection opened is closed again.
    (`with sqlite3.connect(...)` only commits -- it does NOT close.)"""
    real = sqlite3.connect
    opened = []

    class Tracked(sqlite3.Connection):        # a real Connection: .backup() accepts it
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.was_closed = False
            opened.append(self)

        def close(self):
            self.was_closed = True
            super().close()

    monkeypatch.setattr(bk.sqlite3, "connect",
                        lambda *a, **k: real(*a, factory=Tracked, **k))
    p = bk.make_backup(tmp_path / "out")
    bk.verify_backup(p)
    assert opened and all(c.was_closed for c in opened), \
        f"{sum(not c.was_closed for c in opened)} of {len(opened)} connections left open"
