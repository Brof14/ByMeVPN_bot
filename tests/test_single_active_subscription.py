"""Guard the one-active-subscription invariant without losing paid history."""
import tempfile
import time
import unittest
from pathlib import Path

import database


class SingleActiveSubscriptionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path, self.original_pool = database.DB_FILE, database._db_pool
        database.DB_FILE, database._db_pool = str(Path(self.tmp.name) / "single.sqlite"), None
        await database.init_db()
        await database.ensure_user(73001)

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE, database._db_pool = self.original_path, self.original_pool
        self.tmp.cleanup()

    async def test_second_active_key_rejected_without_deleting_first(self):
        import sqlite3
        first = await database.add_key(
            73001, "https://vpn.example/sub/one", "Original", "uuid-one",
            30, 2,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await database.add_key(
                73001, "https://vpn.example/sub/two", "Second", "uuid-two",
                30, 2,
            )
        db = await database.get_db()
        await db.rollback()
        keys = await database.get_user_keys(73001)
        self.assertEqual([key["id"] for key in keys], [first])
        self.assertEqual(len([k for k in keys if k["expiry"] > int(time.time())]), 1)

    async def test_history_retained_but_expired_key_cannot_be_reactivated_when_active(self):
        import sqlite3
        expired = await database.add_key(
            73001, "https://vpn.example/sub/old", "Old", "old-uuid",
            30, 2, expiry=int(time.time()) - 50,
        )
        active = await database.add_key(
            73001, "https://vpn.example/sub/current", "Current", "new-uuid",
            30, 5,
        )
        db = await database.get_db()
        with self.assertRaises(sqlite3.IntegrityError):
            await db.execute(
                "UPDATE keys SET expiry=? WHERE id=?",
                (int(time.time()) + 86400, expired),
            )
        await db.rollback()
        keys = await database.get_user_keys(73001)
        self.assertEqual(len(keys), 2)
        self.assertEqual(len([k for k in keys if k["expiry"] > int(time.time())]), 1)
        self.assertEqual(next(k["id"] for k in keys if k["expiry"] > int(time.time())), active)

    async def test_expired_keys_can_be_replaced_with_one_new_active_key(self):
        await database.add_key(
            73001, "https://vpn.example/sub/old", "Old", "old-uuid",
            30, 2, expiry=int(time.time()) - 50,
        )
        new_id = await database.add_key(
            73001, "https://vpn.example/sub/new", "New", "new-uuid",
            30, 2,
        )
        self.assertGreater(new_id, 0)
        keys = await database.get_user_keys(73001)
        self.assertEqual(len(keys), 2)
        self.assertEqual(len([k for k in keys if k["expiry"] > int(time.time())]), 1)


if __name__ == "__main__":
    unittest.main()
