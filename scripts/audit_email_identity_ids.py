#!/usr/bin/env python3
"""Read-only report of legacy hashed email identity migration candidates.

This does not claim to distinguish a Telegram identity from an email identity
when both already share an integer user_id; such collisions require review
against authoritative registration provenance before any data migration.
"""
import argparse
import hashlib
import sqlite3
from pathlib import Path


def inspect_identity_db(path: str) -> dict[str, int]:
    # Open in read-only mode and make no writes to production data.
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "app_users" not in tables:
            raise ValueError("app_users table is missing")
        google_ids = set()
        if "google_accounts" in tables:
            google_ids = {int(row[0]) for row in db.execute(
                "SELECT user_id FROM google_accounts"
            )}
        total = legacy = unproven = 0
        for user_id, email in db.execute("SELECT user_id,email FROM app_users"):
            total += 1
            if int(user_id) in google_ids:
                continue
            expected_legacy_id = int(hashlib.md5(email.lower().encode()).hexdigest()[:8], 16)
            if int(user_id) == expected_legacy_id:
                legacy += 1
            else:
                unproven += 1
        return {
            "app_identities": total,
            "legacy_email_hash_candidates": legacy,
            "unclassified_non_google_identities": unproven,
            "google_identities": len(google_ids),
        }
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("db", help="Existing SQLite database path, read-only")
    args = parser.parse_args()
    result = inspect_identity_db(args.db)
    for key, value in result.items():
        print(f"{key}: {value}")
    print("No users, keys, emails or subscriptions were modified.")


if __name__ == "__main__":
    main()
