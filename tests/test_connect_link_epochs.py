"""Persistent connection-page link epochs: storage, rotation and revocation."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import database
from app_links import build_connect_page_url, verify_permanent_connect_token


class PersistentConnectEpochStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "links.sqlite3")
        self.db_path_patch = patch.object(database, "DB_FILE", self.path)
        self.db_pool_patch = patch.object(database, "_db_pool", None)
        self.db_path_patch.start()
        self.db_pool_patch.start()
        self.db = await database.get_db()
        await self.db.executescript("""
            CREATE TABLE keys (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                key TEXT NOT NULL, uuid TEXT NOT NULL
            );
            CREATE TABLE connect_link_epochs (
                user_id INTEGER PRIMARY KEY,
                seed TEXT NOT NULL,
                legacy_revoked INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );
            INSERT INTO keys(id,user_id,key,uuid)
                VALUES(77,101,'https://vpn.example/sub/old','old-uuid');
        """)
        await self.db.commit()
        self.secret = patch.dict(os.environ, {"APP_HANDOFF_SECRET": "test-link-secret"})
        self.origin = patch("app_links.APP_CONNECT_PAGE_URL", "https://connect.example")
        self.secret.start()
        self.origin.start()

    async def asyncTearDown(self):
        self.origin.stop()
        self.secret.stop()
        await self.db.close()
        self.db_pool_patch.stop()
        self.db_path_patch.stop()
        self.temp.cleanup()

    async def test_one_seed_is_stable_and_rotated_atomically_with_credentials(self):
        original, concurrent = await asyncio.gather(
            database.get_or_create_connect_link_seed(101),
            database.get_or_create_connect_link_seed(101),
        )
        self.assertEqual(original, concurrent)
        self.assertEqual(len(original), 64)
        old = build_connect_page_url(
            "https://vpn.example/sub/old", user_id=101, key_id=77,
            link_seed=original,
        )
        self.assertNotIn("old", old)
        self.assertTrue(await database.update_user_key_credentials(
            101, "new-uuid", "https://vpn.example/sub/new",
        ))
        state = await database.get_connect_link_state(101)
        self.assertTrue(state["legacy_revoked"])
        self.assertNotEqual(original, state["seed"])
        self.assertFalse(verify_permanent_connect_token(
            101, old.split("s=")[-1], "https://vpn.example/sub/new",
            key_id=77, link_seed=state["seed"],
        ))
        cursor = await self.db.execute("SELECT key, uuid FROM keys WHERE id=77")
        self.assertEqual(await cursor.fetchone(), ("https://vpn.example/sub/new", "new-uuid"))

    async def test_failed_reissue_leaves_link_epoch_unchanged(self):
        original = await database.get_or_create_connect_link_seed(101)
        self.assertFalse(await database.update_user_key_credentials(
            999, "uuid", "https://vpn.example/sub/never",
        ))
        state = await database.get_connect_link_state(101)
        self.assertEqual(state["seed"], original)
        self.assertFalse(state["legacy_revoked"])


if __name__ == "__main__":
    unittest.main()
