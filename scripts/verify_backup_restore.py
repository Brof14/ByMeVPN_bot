"""Offline, read-only SQLite restore rehearsal for verified snapshots.

Never points at a live write target, never overwrites the source, and does not
print rows or secrets. This validates a *database* snapshot; 3x-ui state and
real server configuration still require separate rehearsal.
"""
from __future__ import annotations

import argparse
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

REQUIRED_TABLES = ("users", "keys", "payments", "referrals")


def _uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve(strict=True)))}?mode=ro"


def _check(db: sqlite3.Connection) -> dict[str, int]:
    result = db.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError("SQLite integrity check failed")
    present = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if missing := set(REQUIRED_TABLES).difference(present):
        raise RuntimeError(f"Backup is missing required tables: {', '.join(sorted(missing))}")
    return {
        table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in REQUIRED_TABLES
    }


def rehearse_restore(snapshot_path: str) -> dict[str, int]:
    """Restore to a disposable file and compare safe row counts."""
    source = Path(snapshot_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("snapshot must be a regular file")
    with tempfile.TemporaryDirectory(prefix="bymevpn-restore-check-") as folder:
        recovered = Path(folder) / "rehearsed.sqlite"
        with sqlite3.connect(_uri(source), uri=True, timeout=30) as original:
            original.execute("PRAGMA query_only=ON")
            original_counts = _check(original)
            with sqlite3.connect(str(recovered)) as restored:
                original.backup(restored)
                restored_counts = _check(restored)
        if original_counts != restored_counts:
            raise RuntimeError("Restored database table counts differ from snapshot")
        # Verify the restored database can actually be opened from a new process/connection.
        with sqlite3.connect(_uri(recovered), uri=True) as reopened:
            if _check(reopened) != original_counts:
                raise RuntimeError("Reopened restored database differs")
        return restored_counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="offline SQLite backup file")
    args = parser.parse_args()
    result = rehearse_restore(args.snapshot)
    print("Offline SQLite restore rehearsal: PASS")
    print("Verified table counts:", ", ".join(f"{table}={count}" for table, count in result.items()))
    print("Real 3x-ui/API restoration and post-backup payments must be tested separately.")


if __name__ == "__main__":
    main()
