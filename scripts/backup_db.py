#!/usr/bin/env python3
"""Create and verify a private, consistent SQLite backup using SQLite's backup API."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path


def main() -> int:
    source = Path(os.getenv("DB_FILE", "data/vpnbot.db")).expanduser()
    backup_dir = Path(os.getenv("BACKUP_DIR", "data/backups")).expanduser()
    keep = max(1, int(os.getenv("BACKUP_KEEP", "14")))
    if not source.is_file():
        raise SystemExit(f"Database does not exist: {source}")

    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    destination = backup_dir / f"vpnbot-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"

    # SQLite's backup API includes committed WAL content and produces a
    # transactionally consistent snapshot while the bot remains online.
    with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True, timeout=30) as src:
        with sqlite3.connect(destination, timeout=30) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"Backup integrity check failed: {result!r}")

    os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    backups = sorted(backup_dir.glob("vpnbot-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old_backup in backups[keep:]:
        old_backup.unlink()
    print(f"Verified backup created: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
