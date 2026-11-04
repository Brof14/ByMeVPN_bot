"""SQLite tests for shared, two-phase trial claims."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import database


class TrialClaimReservationTests(unittest.TestCase):
    def test_mobile_and_bot_share_one_atomic_reservation_and_commit_only_after_confirmation(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                db_path = Path(temp) / "trial.sqlite3"
                db = await aiosqlite.connect(db_path)
                db_file_patch = patch.object(database, "DB_FILE", str(db_path))
                db_file_patch.start()
                try:
                    await db.execute(
                        "CREATE TABLE users(user_id INTEGER PRIMARY KEY, trial_used INTEGER NOT NULL DEFAULT 0)"
                    )
                    await db.commit()
                    await database.ensure_trial_claim_reservation_schema(db)
                    await database.ensure_trial_claim_reservation_schema(db)
                    with patch.object(database, "get_db", new=AsyncMock(return_value=db)):
                        claims = await asyncio.gather(
                            database.reserve_trial_claim(42),
                            database.reserve_trial_claim(42),
                        )
                        claim = next((value for value in claims if value), None)
                        self.assertIsNotNone(claim)
                        self.assertEqual(sum(value is not None for value in claims), 1)
                        self.assertFalse(await database.has_trial_used(42))
                        self.assertTrue(await database.has_trial_claim_reservation(42))
                        self.assertFalse(await database.complete_trial_claim(42, "wrong-claim"))
                        self.assertFalse(await database.has_trial_used(42))
                        self.assertTrue(await database.complete_trial_claim(42, claim))
                        self.assertTrue(await database.has_trial_used(42))
                        self.assertFalse(await database.has_trial_claim_reservation(42))
                        self.assertIsNone(await database.reserve_trial_claim(42))
                    integrity = await (await db.execute("PRAGMA integrity_check")).fetchone()
                    self.assertEqual(integrity[0], "ok")
                finally:
                    await db.close()
                    db_file_patch.stop()

        asyncio.run(run())

    def test_only_explicit_release_makes_an_unspent_reservation_retryable(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                db_path = Path(temp) / "trial.sqlite3"
                db = await aiosqlite.connect(db_path)
                db_file_patch = patch.object(database, "DB_FILE", str(db_path))
                db_file_patch.start()
                try:
                    await db.execute(
                        "CREATE TABLE users(user_id INTEGER PRIMARY KEY, trial_used INTEGER NOT NULL DEFAULT 0)"
                    )
                    await db.commit()
                    await database.ensure_trial_claim_reservation_schema(db)
                    with patch.object(database, "get_db", new=AsyncMock(return_value=db)):
                        first = await database.reserve_trial_claim(77)
                        self.assertIsNotNone(first)
                        self.assertIsNone(await database.reserve_trial_claim(77))
                        self.assertFalse(await database.has_trial_used(77))
                        self.assertTrue(await database.release_trial_claim(77, first))
                        second = await database.reserve_trial_claim(77)
                        self.assertIsNotNone(second)
                        self.assertFalse(await database.has_trial_used(77))
                finally:
                    await db.close()
                    db_file_patch.stop()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
