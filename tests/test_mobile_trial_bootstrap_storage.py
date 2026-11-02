"""Isolated SQLite checks for the additive mobile trial bootstrap storage."""
import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DB_FILE", "/tmp/bymevpn_trial_bootstrap_storage_import.db")

import aiosqlite
import database


class MobileTrialBootstrapStorageTests(unittest.TestCase):
    def test_schema_is_repeatable_and_bootstrap_expiry_cannot_be_extended(self):
        async def run():
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
                db = await aiosqlite.connect(tmp.name)
                try:
                    await db.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY)")
                    await db.execute("INSERT INTO users(user_id) VALUES(17)")
                    await database.ensure_mobile_trial_bootstrap_schema(db)
                    await database.ensure_mobile_trial_bootstrap_schema(db)
                    await db.commit()
                    fixed_expiry = int(time.time()) + 600
                    with patch.object(database, "get_db", new=AsyncMock(return_value=db)):
                        first = await database.create_mobile_trial_bootstrap(17, "digest-a", fixed_expiry)
                        second = await database.create_mobile_trial_bootstrap(17, "digest-a", fixed_expiry + 300)
                        expiry = await database.get_mobile_trial_bootstrap(17, "digest-a")
                        await database.delete_mobile_trial_bootstrap(17)
                        deleted = await database.get_mobile_trial_bootstrap(17, "digest-a")
                    self.assertTrue(first)
                    self.assertFalse(second)
                    self.assertEqual(expiry, fixed_expiry)
                    self.assertIsNone(deleted)
                    self.assertEqual((await (await db.execute("PRAGMA integrity_check")).fetchone())[0], "ok")
                    self.assertEqual((await (await db.execute("SELECT user_id FROM users")).fetchall()), [(17,)])
                finally:
                    await db.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
