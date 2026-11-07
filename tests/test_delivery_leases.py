"""SQLite-backed, cross-process subscription delivery lease regressions."""
import tempfile
import unittest
from pathlib import Path

import aiosqlite

import database


class DeliveryLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_path, self.old_pool = database.DB_FILE, database._db_pool
        self.tmp = tempfile.TemporaryDirectory()
        database.DB_FILE, database._db_pool = str(Path(self.tmp.name) / "leases.db"), None
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE, database._db_pool = self.old_path, self.old_pool
        self.tmp.cleanup()

    async def test_only_one_token_can_hold_lease_for_user(self):
        self.assertTrue(await database.try_acquire_delivery_lease(123, "first"))
        self.assertFalse(await database.try_acquire_delivery_lease(123, "second"))
        self.assertFalse(await database.renew_delivery_lease(123, "second"))
        self.assertTrue(await database.renew_delivery_lease(123, "first"))
        await database.release_delivery_lease(123, "second")
        self.assertFalse(await database.try_acquire_delivery_lease(123, "second"))
        await database.release_delivery_lease(123, "first")
        self.assertTrue(await database.try_acquire_delivery_lease(123, "second"))

    async def test_stale_holder_cannot_release_reclaimed_lease(self):
        self.assertTrue(await database.try_acquire_delivery_lease(123, "old"))
        db = await database.get_db()
        await db.execute("UPDATE delivery_leases SET expires_at=0 WHERE user_id=123")
        await db.commit()
        self.assertTrue(await database.try_acquire_delivery_lease(123, "new"))
        self.assertFalse(await database.renew_delivery_lease(123, "old"))
        await database.release_delivery_lease(123, "old")
        self.assertFalse(await database.try_acquire_delivery_lease(123, "third"))
        self.assertTrue(await database.renew_delivery_lease(123, "new"))

    async def test_another_sqlite_connection_cannot_acquire_same_lease(self):
        self.assertTrue(await database.try_acquire_delivery_lease(123, "first"))
        # Independent connections model two bot workers on the same VPS.
        async with aiosqlite.connect(database.DB_FILE) as worker_two:
            cursor = await worker_two.execute(
                """INSERT INTO delivery_leases(user_id,token,expires_at)
                   VALUES(123,'second',9999999999)
                   ON CONFLICT(user_id) DO UPDATE SET
                       token=excluded.token,expires_at=excluded.expires_at
                   WHERE delivery_leases.expires_at<=0"""
            )
            await worker_two.commit()
            self.assertEqual(cursor.rowcount, 0)
        self.assertTrue(await database.renew_delivery_lease(123, "first"))

    async def test_different_users_are_not_blocked(self):
        self.assertTrue(await database.try_acquire_delivery_lease(123, "one"))
        self.assertTrue(await database.try_acquire_delivery_lease(456, "two"))

    async def test_rejects_invalid_lease_inputs(self):
        with self.assertRaises(ValueError):
            await database.try_acquire_delivery_lease(0, "owner")
        with self.assertRaises(ValueError):
            await database.try_acquire_delivery_lease(123, "", 180)
        with self.assertRaises(ValueError):
            await database.renew_delivery_lease(123, "owner", 20)


if __name__ == "__main__":
    unittest.main()
