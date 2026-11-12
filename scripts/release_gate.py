#!/usr/bin/env python3
"""Read-only production release preflight.

Checks local configuration and SQLite restore consistency. This is NOT proof
of a real VPN tunnel or settled live payment; those require separate recorded
checks performed on actual devices and a sandbox payment provider.
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from encrypted_backup import require_backup_passphrase
from release_info import release_identity
from scripts.verify_sqlite_restore import rehearse_sqlite_restore


def verify_release_prerequisites(db_path: str, expected_sha: str) -> list[str]:
    errors = []
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("expected_sha must be an exact 40-character commit SHA")
    recorded = release_identity()["sha"]
    if recorded != expected_sha:
        errors.append("BYMEVPN_RELEASE_SHA does not match expected immutable commit")
    try:
        require_backup_passphrase()
    except RuntimeError:
        errors.append("Encrypted Telegram backup passphrase is not configured")
    try:
        rehearse_sqlite_restore(db_path)
    except Exception as exc:
        errors.append(f"SQLite restore rehearsal failed: {type(exc).__name__}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="SQLite database file")
    parser.add_argument("--expected-sha", required=True, help="Deployed full commit SHA")
    args = parser.parse_args()
    errors = verify_release_prerequisites(args.db, args.expected_sha)
    if errors:
        for item in errors:
            print("BLOCKER:", item)
        print("NOT READY: live VPN and payment E2E acceptance still mandatory.")
        return 1
    print("Static preflight passed. Live connection/payment/rollback tests remain required.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
