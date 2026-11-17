"""
scripts/backup.py — weekly backup of everything that exists ONLY on this machine.

Git holds the code and the forward-test snapshots. These do not reach GitHub, by design
(too big, or gitignored), so a dead disk would lose them:

  quant_system.db            prices, fundamentals, factor scores, sentiment cache
  broker/execution_log/      every execution run (the dashboard's Execution page)
  broker/reconciliation/     fills vs intended, positions after each run
  monitoring/state/          alert log, dedup state, health history, weekly reports

The database is copied with SQLite's online backup API, so the copy is consistent even
if something is writing to it. Everything goes into one dated zip in BACKUP_DIR (from
.env; default <repo>/backups, which is gitignored). Point BACKUP_DIR at a synced folder
(OneDrive / Google Drive) to get it off the machine. The newest --keep zips are kept.

.env is deliberately NOT included: it holds the SMTP App Password and API keys.

    python scripts/backup.py                  # back up now
    python scripts/backup.py --keep 12        # keep the newest 12
    python scripts/backup.py --verify         # prove the newest backup restores
    python scripts/backup.py --verify PATH    # ... or a specific zip

A backup that cannot be restored is the silent failure in a new place, so every zip is
VERIFIED before it gets its real name: each member's CRC is checked, the database is
restored to a temp folder and passes SQLite's quick_check, and every table's row count
and every file must match the manifest.json written into the zip. The weekly health
check re-verifies the newest zip (a synced folder can corrupt a file after the fact).

Exit 0 = backup written and verified (or --verify passed); 1 = failed, reason printed.
The weekly health check's backup_fresh check warns when the newest backup is more than
8 days old or does not verify.
"""

from __future__ import annotations

import argparse
import json
from contextlib import closing
import os
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FOLDERS = ["broker/execution_log", "broker/reconciliation", "monitoring/state"]
PREFIX = "quant_backup_"
MANIFEST = "manifest.json"


def backup_dir() -> Path:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    d = os.getenv("BACKUP_DIR", "").strip()
    return Path(d).expanduser() if d else ROOT / "backups"


def db_path() -> Path | None:
    from config.settings import DATABASE_URL
    if not DATABASE_URL.startswith("sqlite:///"):
        return None
    p = Path(DATABASE_URL[len("sqlite:///"):])
    return p if p.is_absolute() else ROOT / p


def list_backups(dest: Path | None = None) -> list[Path]:
    dest = dest or backup_dir()
    return sorted(dest.glob(f"{PREFIX}*.zip")) if dest.exists() else []


def _table_counts(db: Path) -> dict[str, int]:
    # closing(), not `with connect()`: the latter only COMMITS. An open handle on
    # Windows locks the file, so the temp copy could not be deleted (WinError 32).
    with closing(sqlite3.connect(db)) as c:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def verify_backup(zip_path: Path) -> dict:
    """Prove a backup zip restores. Returns {'ok', 'problems', 'files', 'tables',
    'manifest'}; never raises for a bad zip (the problem is reported instead)."""
    problems: list[str] = []
    out = {"ok": False, "problems": problems, "files": 0, "tables": {},
           "manifest": False}
    try:
        with zipfile.ZipFile(zip_path) as z:
            bad = z.testzip()                              # CRC of every member
            if bad:
                problems.append(f"corrupt member: {bad}")
            names = set(z.namelist())
            out["files"] = len(names - {MANIFEST})
            man = json.loads(z.read(MANIFEST)) if MANIFEST in names else None
            out["manifest"] = man is not None
            if man:
                missing = sorted(set(man.get("files", [])) - names)
                if missing:
                    problems.append(f"{len(missing)} file(s) missing, e.g. {missing[0]}")
            db_name = (man or {}).get("db") or next(
                (n for n in names if n.endswith(".db")), None)
            if db_name and db_name in names:
                with tempfile.TemporaryDirectory() as td:
                    restored = Path(z.extract(db_name, td))
                    with closing(sqlite3.connect(restored)) as c:
                        qc = c.execute("PRAGMA quick_check").fetchone()[0]
                    if qc != "ok":
                        problems.append(f"database fails quick_check: {qc}")
                    else:
                        out["tables"] = _table_counts(restored)
                        want = (man or {}).get("tables") or {}
                        diff = [t for t, n in want.items() if out["tables"].get(t) != n]
                        if diff:
                            problems.append(
                                f"row counts differ from the source for {len(diff)} "
                                f"table(s), e.g. {diff[0]}: {out['tables'].get(diff[0])} "
                                f"vs {want[diff[0]]}")
            elif man and man.get("db"):
                problems.append(f"database {man['db']} missing from the zip")
    except Exception as e:                                      # noqa: BLE001
        # Anything at all (BadZipFile, zlib.error on damaged data, sqlite errors):
        # a verifier that crashes is no better than none. Report it as the problem.
        problems.append(f"{type(e).__name__}: {e}")
    out["ok"] = not problems
    return out


def make_backup(dest: Path, keep: int = 8, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{PREFIX}{now:%Y-%m-%d_%H%M}.zip"
    tmp_zip = out.with_suffix(".zip.partial")
    manifest = {"created": now.isoformat(timespec="seconds"), "db": None,
                "tables": {}, "files": []}
    with tempfile.TemporaryDirectory() as td, \
            zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
        db = db_path()
        if db and not db.exists():
            # The database is the main thing this protects. A backup that quietly
            # leaves it out would still read "written" -- fail instead.
            raise FileNotFoundError(f"database not found at {db} (DATABASE_URL)")
        if db:
            snap = Path(td) / db.name
            src, dst = sqlite3.connect(db), sqlite3.connect(snap)
            try:
                src.backup(dst)                 # consistent online copy
            finally:
                dst.close()
                src.close()
            z.write(snap, db.name)
            manifest.update(db=db.name, tables=_table_counts(snap))
            manifest["files"].append(db.name)
        for rel in FOLDERS:
            folder = ROOT / rel
            if folder.exists():
                for f in folder.rglob("*"):
                    # pytest_tmp: test scratch the health check once left here (542
                    # junk files per backup, 2026-11-17); never data
                    if (f.is_file() and not f.name.endswith(".partial")
                            and "pytest_tmp" not in f.parts):
                        arc = f.relative_to(ROOT).as_posix()
                        z.write(f, arc)
                        manifest["files"].append(arc)
        z.writestr(MANIFEST, json.dumps(manifest, indent=1))
    # Only a backup that PROVABLY restores gets the real name. A failure leaves the
    # .partial for inspection, raises, and the old backups are not rotated away.
    v = verify_backup(tmp_zip)
    if not v["ok"]:
        raise RuntimeError("backup did not verify: " + "; ".join(v["problems"])
                           + f" (left as {tmp_zip.name})")
    tmp_zip.replace(out)
    for old in list_backups(dest)[:-keep] if keep > 0 else []:
        old.unlink()
    # A failed run's .partial is kept for inspection until a verified backup supersedes
    # it; two from 2026-10-06 (600 MB) were never removed and never rotated.
    for stale in dest.glob(f"{PREFIX}*.zip.partial"):
        stale.unlink()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Back up the machine-only data.")
    ap.add_argument("--keep", type=int, default=8, help="newest backups to keep (8)")
    ap.add_argument("--dest", default=None, help="override BACKUP_DIR")
    ap.add_argument("--verify", nargs="?", const="latest", default=None,
                    metavar="ZIP", help="verify the newest backup (or ZIP) restores")
    args = ap.parse_args(argv)
    dest = Path(args.dest) if args.dest else backup_dir()
    if args.verify:
        zips = list_backups(dest)
        target = Path(args.verify) if args.verify != "latest" else (
            zips[-1] if zips else None)
        if target is None or not target.exists():
            print(f"VERIFY FAILED: no backup found ({args.verify}) in {dest}")
            return 1
        v = verify_backup(target)
        rows = sum(v["tables"].values())
        if not v["ok"]:
            print(f"VERIFY FAILED: {target.name}: " + "; ".join(v["problems"]))
            return 1
        print(f"verified: {target.name} restores -- {v['files']} files, "
              f"{len(v['tables'])} tables, {rows:,} rows"
              + ("" if v["manifest"] else " (no manifest: made before 2026-10-06; "
                 "CRC and database integrity checked only)"))
        return 0
    try:
        p = make_backup(dest, keep=args.keep)
    except Exception as e:                                          # noqa: BLE001
        print(f"BACKUP FAILED: {type(e).__name__}: {e}")
        return 1
    print(f"backup written and verified: {p}  ({p.stat().st_size / 1e6:,.1f} MB); "
          f"{len(list_backups(dest))} kept in {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
