#!/usr/bin/env python3
"""Rehearse SQLite recovery in a temporary directory without modifying source.

Backups are ephemeral and removed before exit. This is NOT encrypted offsite
backup: production backup encryption and remote storage must be verified
independently.
"""
import argparse
import sqlite3
import tempfile
from pathlib import Path


def rehearse_sqlite_restore(path: str) -> dict[str, int]:
    source_path = Path(path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    with tempfile.TemporaryDirectory(prefix="bymevpn-restore-") as temp:
        source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(str(Path(temp) / "restored.sqlite"))
        try:
            source.backup(target)
            original_tables = set(row[0] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ))
            restored_tables = set(row[0] for row in target.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ))
            if original_tables != restored_tables:
                raise RuntimeError("SQLite table inventory differs after restore")
            checks = [row[0] for row in target.execute("PRAGMA integrity_check").fetchall()]
            if checks != ["ok"]:
                raise RuntimeError("Restored backup failed integrity_check")
            # Compare row counts for the critical business tables without
            # exporting or logging customer identifiers or secret keys.
            results = {}
            for table in ("users", "keys", "payments", "auto_renew_subscriptions"):
                if table not in original_tables:
                    continue
                original = source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                restored = target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if original != restored:
                    raise RuntimeError(f"Row counts differ for {table}")
                results[table] = int(restored)
            return results
        finally:
            source.close()
            target.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", help="SQLite database path")
    args = parser.parse_args()
    rows = rehearse_sqlite_restore(args.db)
    print("Temporary restore verified; row counts:", rows)


if __name__ == "__main__":
    main()
