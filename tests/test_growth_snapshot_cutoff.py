import asyncio
import importlib.util
import sqlite3
import sys
import types
import unittest
from pathlib import Path


class AsyncCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    async def fetchone(self):
        return self.cursor.fetchone()


class AsyncDB:
    def __init__(self, db):
        self.db = db

    async def execute(self, sql, params):
        return AsyncCursor(self.db.execute(sql, params))


class ReferralSnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_excludes_future_referrals_and_events(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE referrals(referrer_id INTEGER, referred_id INTEGER, created INTEGER);
            CREATE TABLE referral_events(referrer_id INTEGER, referred_id INTEGER,
                event_type TEXT, days_awarded INTEGER, created INTEGER);
            INSERT INTO referrals VALUES (1, 2, 900), (1, 3, 1001);
            INSERT INTO referral_events VALUES (1, 2, 'registration_bonus', 15, 900),
                (1, 2, 'payment_bonus', 30, 1001);
        """)
        module = types.ModuleType("database")
        async def get_db():
            return AsyncDB(db)
        module.get_db = get_db
        previous = sys.modules.get("database")
        sys.modules["database"] = module
        try:
            spec = importlib.util.spec_from_file_location("growth_metrics_tested", Path(__file__).resolve().parents[1] / "growth_metrics.py")
            target = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(target)
        finally:
            if previous is None:
                del sys.modules["database"]
            else:
                sys.modules["database"] = previous
        report = await target.referral_growth_snapshot(days=1, now=1000)
        self.assertEqual((report["invited"], report["paying"], report["bonus_days"]), (1, 0, 15))


if __name__ == "__main__":
    unittest.main()
