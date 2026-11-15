"""Read-only duplicate subscription reconciliation cannot mutate paid data."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from scripts.audit_duplicate_subscriptions import audit_database, collect_conflicts


class DuplicateSubscriptionTriageTests(unittest.TestCase):
    def test_reports_only_live_conflicts_with_currency_separated(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript("""
                CREATE TABLE keys (
                    id INTEGER PRIMARY KEY, user_id INTEGER, expiry INTEGER, limit_ip INTEGER
                );
                CREATE TABLE payments (
                    id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER,
                    currency TEXT, status TEXT
                );
                CREATE TABLE auto_renew_subscriptions (
                    user_id INTEGER, status TEXT, payment_method_id TEXT
                );
            """)
            now = int(time.time())
            db.executemany(
                "INSERT INTO keys(id,user_id,expiry,limit_ip) VALUES(?,?,?,?)",
                [(1, 12, now + 3600, 2),
                 (2, 12, now + 7200, 5),
                 (3, 12, now - 100, 2),
                 (4, 13, now + 86400, 2)],
            )
            db.executemany(
                "INSERT INTO payments(id,user_id,amount,currency,status) VALUES(?,?,?,?,?)",
                [(1, 12, 89, "RUB", "success"),
                 (2, 12, 100, "XTR", "success"),
                 (3, 12, 999, "RUB", "failed")],
            )
            db.execute(
                "INSERT INTO auto_renew_subscriptions VALUES(12,'active','saved-card')"
            )
            before = list(db.iterdump())
            conflicts = collect_conflicts(db, now)
            after = list(db.iterdump())
            self.assertEqual(before, after)
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["user_id"], 12)
            self.assertEqual([key["key_id"] for key in conflicts[0]["keys"]], [2, 1])
            self.assertEqual(conflicts[0]["settled_totals_by_currency"], {
                "RUB": {"payments": 1, "amount": 89},
                "XTR": {"payments": 1, "amount": 100},
            })
            self.assertEqual(conflicts[0]["active_recurring_methods"], 1)
            self.assertNotIn("key", str(conflicts[0]["settled_totals_by_currency"]))

    def test_file_audit_uses_read_only_connection(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "backup.sqlite"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE keys (
                        id INTEGER PRIMARY KEY, user_id INTEGER, expiry INTEGER,
                        limit_ip INTEGER
                    );
                    CREATE TABLE payments (
                        user_id INTEGER, currency TEXT, amount INTEGER,
                        status TEXT
                    );
                    CREATE TABLE auto_renew_subscriptions (
                        user_id INTEGER, status TEXT, payment_method_id TEXT
                    );
                """)
            self.assertEqual(audit_database(str(path)), [])


if __name__ == "__main__":
    unittest.main()
