"""Safe pre-deploy SQLite backup, integrity check and migration rehearsal.

Example (on a stopped or quiescent deployment, before switching containers):
    python scripts/predeploy_db_guard.py --db data/vpnbot.db --backup-dir /secure/backups

Uses SQLite's online backup API, not unsafe copying of an open WAL database.
NEVER writes to the source database and NEVER prints secrets, UUIDs or keys.
After the backup, all migrations execute only on a disposable cloned database.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def _read_only_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve(strict=True)))}?mode=ro"


def _assert_integrity(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError("SQLite integrity check failed")


def _test_invariant(connection: sqlite3.Connection) -> None:
    required = {"trg_keys_single_active_insert", "trg_keys_single_active_extend"}
    found = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name IN (?,?)",
            tuple(sorted(required)),
        )
    }
    if found != required:
        raise RuntimeError("single-active-key triggers were not installed")
    user_id = -9223372036854775807
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO users(user_id) VALUES(?)", (user_id,))
        now = int(datetime.now(tz=timezone.utc).timestamp())
        values = (
            user_id, "https://example.invalid/smoke", "PREDEPLOY",
            "predeploy-test-uuid", 1, 2, now, now + 3600,
        )
        insert = (
            "INSERT INTO keys(user_id,key,remark,uuid,days,limit_ip,created,expiry) "
            "VALUES(?,?,?,?,?,?,?,?)"
        )
        connection.execute(insert, values)
        try:
            connection.execute(insert, values)
        except sqlite3.IntegrityError:
            pass
        else:
            raise RuntimeError("second active key was accepted")
    finally:
        connection.rollback()


def guard_database(database_path: str, backup_directory: str) -> dict:
    source = Path(database_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("source database is not a regular file")
    backups = Path(backup_directory).expanduser().resolve()
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not backups.is_dir():
        raise ValueError("backup directory is not a directory")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = backups / f"bymevpn-{timestamp}-{secrets.token_hex(6)}.sqlite"
    if output == source:
        raise ValueError("backup must not overwrite the source")
    # A new private file prevents leaking customer records through default umask.
    fd = os.open(str(output), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(_read_only_uri(source), uri=True, timeout=30) as db:
            db.execute("PRAGMA query_only=ON")
            _assert_integrity(db)
            with sqlite3.connect(str(output), timeout=30) as backup:
                db.backup(backup)
                _assert_integrity(backup)
        with tempfile.TemporaryDirectory(prefix="bymevpn-predeploy-") as folder:
            clone = Path(folder) / "migration-clone.sqlite"
            shutil.copy2(output, clone)
            env = os.environ.copy()
            env["DB_FILE"] = str(clone)
            # The sole process that invokes migrations receives the temporary
            # clone path. The operator's live DB is never opened for writing.
            script = (
                "import asyncio,database;"
                "asyncio.run(database.init_db());"
                "asyncio.run(database.close_db())"
            )
            run = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(Path(__file__).resolve().parent.parent),
                env=env, check=False, capture_output=True, text=True,
                timeout=120,
            )
            if run.returncode:
                # Do not print raw tracebacks which can include sensitive
                # deployment paths and environment details.
                raise RuntimeError(
                    "database migration rehearsal failed on the isolated clone"
                )
            with sqlite3.connect(str(clone), timeout=15) as migrated:
                _assert_integrity(migrated)
                _test_invariant(migrated)
        # Read-only count. Do not print Telegram user IDs, invoice IDs or keys.
        with sqlite3.connect(_read_only_uri(output), uri=True) as snapshot:
            conflicts = snapshot.execute(
                "SELECT COUNT(*) FROM (SELECT user_id FROM keys WHERE expiry>? "
                "GROUP BY user_id HAVING COUNT(*)>1)",
                (int(datetime.now(timezone.utc).timestamp()),),
            ).fetchone()[0]
        return {
            "backup_path": str(output), "backup_bytes": output.stat().st_size,
            "existing_active_duplicate_accounts": int(conflicts),
            "migration_rehearsal": "passed",
        }
    except BaseException:
        # Keep a complete verified backup for manual recovery if the clone
        # migration failed; remove only a clearly empty/partial file.
        if output.exists() and output.stat().st_size == 0:
            output.unlink()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="source SQLite database path")
    parser.add_argument("--backup-dir", required=True, help="private backup destination")
    args = parser.parse_args()
    result = guard_database(args.db, args.backup_dir)
    print(f"Backup: {result['backup_path']}")
    print(f"Size: {result['backup_bytes']} bytes")
    print("Integrity: OK; isolated migration rehearsal: PASS")
    print(f"Existing duplicate-active accounts: {result['existing_active_duplicate_accounts']}")
    if result["existing_active_duplicate_accounts"]:
        print(
            "ACTION: use scripts/audit_duplicate_subscriptions.py on the backup; "
            "reconcile paid access manually before enabling new checkouts."
        )


if __name__ == "__main__":
    main()
