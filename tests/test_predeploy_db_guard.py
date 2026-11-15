"""Production backups and migrations must not alter source billing records."""
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

import database
from scripts.predeploy_db_guard import guard_database


class PredeployDatabaseGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path, self.old_pool = database.DB_FILE, database._db_pool
        database.DB_FILE, database._db_pool = str(Path(self.temp.name) / "live.sqlite"), None
        await database.init_db()
        await database.ensure_user(80008)
        self.expiry = int(time.time()) + 14 * 86400
        await database.add_key(
            80008, "https://vpn.example.invalid/sub/private", "Private",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 14, 2, expiry=self.expiry,
        )

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE, database._db_pool = self.old_path, self.old_pool
        self.temp.cleanup()

    async def test_rehearsal_backups_database_and_never_mutates_original(self):
        before = await database.get_user_keys(80008)
        backup_dir = str(Path(self.temp.name) / "backups")
        result = guard_database(database.DB_FILE, backup_dir)
        self.assertEqual(result["migration_rehearsal"], "passed")
        self.assertEqual(result["existing_active_duplicate_accounts"], 0)
        backup = Path(result["backup_path"])
        self.assertTrue(backup.exists())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        after = await database.get_user_keys(80008)
        self.assertEqual(before, after)
        self.assertIn("private", before[0]["key"])
        self.assertNotIn("private", str(result))

    async def test_guard_rejects_missing_db_file(self):
        with self.assertRaises(FileNotFoundError):
            guard_database(str(Path(self.temp.name) / "missing.sqlite"), self.temp.name)


if __name__ == "__main__":
    unittest.main()
