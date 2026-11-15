"""Read-only ledger triage for pre-migration duplicate active VPN subscriptions.

Run on a *copy* of the production database before any manual consolidation:
    python scripts/audit_duplicate_subscriptions.py --db /path/to/backup.sqlite

This script NEVER deletes keys, changes expiry, suspends payments or queries
the VPN panel. A payment receipt is not tied reliably to a particular key;
operators must inspect YooKassa/Stars/Crypto provider records separately.
"""
import argparse
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote


def collect_conflicts(db: sqlite3.Connection, now: int) -> list[dict]:
    """Return only users with >1 still-active key; no UUIDs or subscription URLs."""
    rows = db.execute(
        "SELECT user_id,id,expiry,limit_ip FROM keys "
        "WHERE expiry>? ORDER BY user_id,expiry DESC,id DESC",
        (int(now),),
    ).fetchall()
    by_user = defaultdict(list)
    for user_id, key_id, expiry, limit in rows:
        by_user[int(user_id)].append({
            "key_id": int(key_id), "expiry": int(expiry),
            "devices": int(limit),
        })
    result = []
    for user_id, keys in sorted(by_user.items()):
        if len(keys) < 2:
            continue
        ledger = db.execute(
            "SELECT currency,COUNT(*),COALESCE(SUM(amount),0) FROM payments "
            "WHERE user_id=? AND status='success' GROUP BY currency",
            (user_id,),
        ).fetchall()
        recurring = db.execute(
            "SELECT COUNT(*) FROM auto_renew_subscriptions "
            "WHERE user_id=? AND status='active' AND "
            "TRIM(COALESCE(payment_method_id,''))!=''",
            (user_id,),
        ).fetchone()
        result.append({
            "user_id": user_id,
            "keys": keys,
            "settled_totals_by_currency": {
                str(currency): {"payments": int(count), "amount": int(amount)}
                for currency, count, amount in ledger
            },
            "active_recurring_methods": int(recurring[0] or 0),
        })
    return result


def audit_database(path: str, now: int | None = None) -> list[dict]:
    target = Path(path).expanduser().resolve(strict=True)
    # URI mode=ro and PRAGMA query_only prohibit accidental database changes.
    url = f"file:{quote(str(target))}?mode=ro"
    with sqlite3.connect(url, uri=True, timeout=5) as db:
        db.execute("PRAGMA query_only=ON")
        return collect_conflicts(db, int(time.time()) if now is None else int(now))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to a SQLite backup")
    args = parser.parse_args()
    conflicts = audit_database(args.db)
    print(f"Accounts with multiple ACTIVE key rows: {len(conflicts)}")
    for entry in conflicts:
        print(
            f"User {entry['user_id']}: {len(entry['keys'])} active key rows; "
            f"active recurring methods={entry['active_recurring_methods']}"
        )
        for key in entry["keys"]:
            print(f"  local key_id={key['key_id']} expiry={key['expiry']} devices={key['devices']}")
        for currency, amounts in entry["settled_totals_by_currency"].items():
            print(
                f"  settled {currency}: payments={amounts['payments']} "
                f"amount={amounts['amount']}"
            )
        print("  ACTION: manual invoice/panel reconciliation only; do not truncate paid access.")
    print("READ-ONLY REPORT COMPLETE. No key or payment records changed.")


if __name__ == "__main__":
    main()
