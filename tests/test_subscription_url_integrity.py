"""A subscription URL must never overwrite its underlying Xray UUID."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import database


class SubscriptionUrlIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_rotation_preserves_uuid_and_owner(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = str(Path(root) / "keys.sqlite")
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "CREATE TABLE keys(id INTEGER PRIMARY KEY,user_id INTEGER,"
                    "uuid TEXT,key TEXT)"
                )
                await db.execute(
                    "INSERT INTO keys(id,user_id,uuid,key) VALUES(?,?,?,?)",
                    (12, 34, "00000000-0000-4000-8000-000000000000", "https://vpn.example/sub/old"),
                )
                await db.commit()
            with patch.object(database, "DB_FILE", db_path):
                self.assertFalse(await database.update_key_subscription_url(
                    12, 999, "https://vpn.example/sub/attacker"
                ))
                self.assertTrue(await database.update_key_subscription_url(
                    12, 34, "https://vpn.example/sub/new"
                ))
                with self.assertRaises(ValueError):
                    await database.update_key_subscription_url(
                        12, 34, "http://vpn.example/sub/insecure"
                    )
                with self.assertRaises(ValueError):
                    await database.update_key_subscription_url(
                        12, 34, "https://evil:password@vpn.example/sub/private"
                    )
            async with aiosqlite.connect(db_path) as db:
                row = await (await db.execute(
                    "SELECT uuid,key FROM keys WHERE id=12"
                )).fetchone()
            self.assertEqual(row, (
                "00000000-0000-4000-8000-000000000000",
                "https://vpn.example/sub/new",
            ))
