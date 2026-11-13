"""Isolated SQLite checks for active-subscription device bootstrap reservations."""
import asyncio
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite
import database


class MobileDeviceBootstrapStorageTests(unittest.TestCase):
    def test_schema_is_repeatable_live_retry_is_fixed_and_expired_same_device_can_retry(self):
        async def run():
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp:
                db = await aiosqlite.connect(tmp.name)
                try:
                    await db.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY)")
                    await db.execute("INSERT INTO users(user_id) VALUES(17)")
                    await database.ensure_mobile_device_bootstrap_schema(db)
                    await database.ensure_mobile_device_bootstrap_schema(db)
                    await db.commit()
                    subscription_expiry = int(time.time()) + 86400
                    fixed_expiry = int(time.time()) + 600
                    with (patch.object(database, "get_db", new=AsyncMock(return_value=db)),
                          patch.object(database, "DB_FILE", tmp.name)):
                        first = await database.reserve_mobile_device_bootstrap(
                            17, "device-a", subscription_expiry, fixed_expiry, 0, 2,
                        )
                        retry = await database.reserve_mobile_device_bootstrap(
                            17, "device-a", subscription_expiry, fixed_expiry + 300, 0, 2,
                        )
                        second_device = await database.reserve_mobile_device_bootstrap(
                            17, "device-b", subscription_expiry, fixed_expiry, 0, 2,
                        )
                        full = await database.reserve_mobile_device_bootstrap(
                            17, "device-c", subscription_expiry, fixed_expiry, 1, 2,
                        )
                        self.assertEqual(
                            await database.get_mobile_device_bootstrap(
                                17, "device-a", subscription_expiry,
                            ), fixed_expiry,
                        )
                        expired_now = fixed_expiry + 1
                        with patch.object(database.time, "time", return_value=expired_now):
                            renewed = await database.reserve_mobile_device_bootstrap(
                                17, "device-a", subscription_expiry, expired_now + 600, 0, 2,
                            )
                    self.assertEqual(first, fixed_expiry)
                    self.assertEqual(retry, fixed_expiry)
                    self.assertEqual(second_device, fixed_expiry)
                    self.assertIsNone(full)
                    self.assertEqual(renewed, expired_now + 600)
                    self.assertEqual(
                        (await (await db.execute("PRAGMA integrity_check")).fetchone())[0], "ok",
                    )
                finally:
                    await db.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
