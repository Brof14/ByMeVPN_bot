"""
Production hardening regression tests (2026-10 hardening round).

Closes the historical error classes with permanent guards:
  - unbounded days input must never reach expiry arithmetic (99999999 case);
  - valid day grants must keep working;
  - Telegram notification failure must NOT downgrade provisioning success
    ("chat not found" → notification_failed, not subscription_creation_failed);
  - no legacy Marzban references anywhere in the codebase;
  - fallback router fires ONLY with no active FSM state (never eats
    admin/ad-program/auth inputs) and is registered last.

Run:  python3 tests/test_production_hardening.py
"""
import os
import sys
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

TEST_DB_PATH = "/tmp/test_hardening_vpnbot.db"
os.environ["DB_FILE"] = TEST_DB_PATH

import database  # noqa: E402
import constants  # noqa: E402
from constants import normalize_days, MAX_GRANT_DAYS  # noqa: E402


def _clean_db():
    for ext in ["", "-wal", "-shm"]:
        if os.path.exists(TEST_DB_PATH + ext):
            os.remove(TEST_DB_PATH + ext)


class TestDaysValidation(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _clean_db()
        database.DB_FILE = TEST_DB_PATH
        database._db_pool = None
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        _clean_db()

    async def _seed_key(self, user_id: int = 9001) -> int:
        await database.ensure_user(user_id)
        return await database.add_key(user_id, "https://sub/test", "remark", "uuid-x", 30, 2)

    async def test_normalize_days_bounds(self):
        self.assertIsNone(normalize_days(99999999))
        self.assertIsNone(normalize_days(0))
        self.assertIsNone(normalize_days(-5))
        self.assertIsNone(normalize_days("abc"))
        self.assertIsNone(normalize_days(None))
        self.assertIsNone(normalize_days(MAX_GRANT_DAYS + 1))
        self.assertEqual(normalize_days(30), 30)
        self.assertEqual(normalize_days(MAX_GRANT_DAYS), MAX_GRANT_DAYS)

    async def test_extend_key_rejects_huge(self):
        key_id = await self._seed_key()
        row_before = await database.get_key_by_id(key_id)
        ok = await database.extend_key(key_id, 99999999)
        self.assertFalse(ok)
        row_after = await database.get_key_by_id(key_id)
        self.assertEqual(row_before["expiry"], row_after["expiry"])

    async def test_set_key_days_rejects_huge(self):
        key_id = await self._seed_key()
        row_before = await database.get_key_by_id(key_id)
        ok = await database.set_key_days(key_id, 99999999)
        self.assertFalse(ok)
        row_after = await database.get_key_by_id(key_id)
        self.assertEqual(row_before["expiry"], row_after["expiry"])

    async def test_add_manual_days_rejects_huge(self):
        await self._seed_key(user_id=9002)
        ok = await database.add_manual_days(9002, 99999999, admin_id=1)
        self.assertFalse(ok)

    async def test_valid_extension_still_works(self):
        key_id = await self._seed_key()
        row_before = await database.get_key_by_id(key_id)
        ok = await database.extend_key(key_id, 30)
        self.assertTrue(ok)
        row_after = await database.get_key_by_id(key_id)
        self.assertAlmostEqual(row_after["expiry"] - row_before["expiry"], 30 * 86400, delta=5)


class TestNotificationSeparation(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _clean_db()
        database.DB_FILE = TEST_DB_PATH
        database._db_pool = None
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        _clean_db()

    async def test_chat_not_found_does_not_fail_provisioning(self):
        """'chat not found' on delivery → provisioning SUCCESS + notification_failed."""
        import subscription
        from aiogram.exceptions import TelegramBadRequest

        user_id = 9101
        await database.ensure_user(user_id)

        bot = MagicMock()
        bot.send_photo = AsyncMock(
            side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: chat not found")
        )

        with patch.object(subscription, "get_user_keys", new=AsyncMock(return_value=[])), \
             patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=None)), \
             patch.object(
                 subscription, "create_xui_user",
                 new=AsyncMock(return_value={
                     "subscription_url": "https://sub/test-link", "uuid": "uuid-1", "vless_links": [],
                 }),
             ):
            success = await subscription.deliver_key(
                bot=bot, user_id=user_id, chat_id=user_id,
                config_name="ByMeVPN_test", days=30, limit_ip=2,
                is_paid=False, amount=0, currency="RUB",
                method="test", payload="", extend_existing=True,
            )

        self.assertTrue(success, "provisioning must succeed even if Telegram delivery failed")
        keys = await database.get_user_keys(user_id)
        self.assertEqual(len(keys), 1, "key must be persisted")

        db = await database.get_db()
        cur = await db.execute(
            "SELECT COUNT(*) FROM key_errors WHERE user_id=? AND error_type='notification_failed'", (user_id,)
        )
        self.assertEqual((await cur.fetchone())[0], 1, "notification failure must be classified, not swallowed")
        cur = await db.execute(
            "SELECT COUNT(*) FROM key_errors WHERE user_id=? AND error_type='subscription_creation_failed'", (user_id,)
        )
        self.assertEqual((await cur.fetchone())[0], 0, "must NOT be classified as provisioning failure")

    async def test_successful_delivery_has_no_error_rows(self):
        import subscription

        user_id = 9102
        await database.ensure_user(user_id)
        bot = MagicMock()
        bot.send_photo = AsyncMock()

        with patch.object(subscription, "get_user_keys", new=AsyncMock(return_value=[])), \
             patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=None)), \
             patch.object(
                 subscription, "create_xui_user",
                 new=AsyncMock(return_value={
                     "subscription_url": "https://sub/test-link", "uuid": "uuid-2", "vless_links": [],
                 }),
             ):
            success = await subscription.deliver_key(
                bot=bot, user_id=user_id, chat_id=user_id,
                config_name="ByMeVPN_test", days=30, limit_ip=2,
                is_paid=False, amount=0, currency="RUB",
                method="test", payload="", extend_existing=True,
            )
        self.assertTrue(success)
        db = await database.get_db()
        cur = await db.execute("SELECT COUNT(*) FROM key_errors WHERE user_id=?", (user_id,))
        self.assertEqual((await cur.fetchone())[0], 0)


class TestStaticGuarantees(unittest.TestCase):

    def _repo_py_sources(self):
        for root, _dirs, files in os.walk(PROJECT_ROOT):
            if "__pycache__" in root or ".git" in root or root.endswith("tests"):
                continue
            for f in files:
                if f.endswith(".py"):
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        yield os.path.join(root, f), fh.read()

    def test_no_marzban_references(self):
        """create_marzban_user NameError must never be able to return."""
        for path, src in self._repo_py_sources():
            self.assertNotIn("marzban", src.lower(), f"legacy marzban reference in {path}")

    def test_fallback_only_fires_without_active_state(self):
        """Fallback must never consume messages while any FSM flow is active."""
        path = os.path.join(PROJECT_ROOT, "handlers", "fallback.py")
        src = open(path, encoding="utf-8").read()
        self.assertIn("StateFilter(None)", src, "fallback handlers must require StateFilter(None)")
        self.assertNotIn("waiting_for_config_name", src, "old per-flow exclusion must be gone")

    def test_fallback_router_registered_last(self):
        import re
        path = os.path.join(PROJECT_ROOT, "main.py")
        src = open(path, encoding="utf-8").read()
        m = re.search(r"routers\s*=\s*\[(.*?)\]", src, re.S)
        self.assertIsNotNone(m, "routers list not found in main.py")
        routers_block = m.group(1)
        self.assertLess(
            routers_block.index("ad_program_router"), routers_block.index("fallback_router"),
            "fallback_router must come after ad_program_router in the routers list",
        )

    def test_max_grant_days_single_source(self):
        self.assertEqual(constants.MAX_GRANT_DAYS, 3650)
        self.assertIsNotNone(constants.normalize_days(3650))


if __name__ == "__main__":
    unittest.main(verbosity=2)
