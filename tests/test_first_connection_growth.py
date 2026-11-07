"""One-time organic-growth prompt after a verified, working VPN connection."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class FirstConnectionGrowthTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_connection_offers_optin_share_once(self):
        from notifications import _record_verified_first_connection
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("notifications.update_connection_reminder", new=AsyncMock()) as update,
            patch("database.log_analytics_event_once", new=AsyncMock(side_effect=[True, False])) as milestone,
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=False)),
        ):
            await _record_verified_first_connection(bot, {"user_id": 9, "key_id": 1}, "traffic")
            await _record_verified_first_connection(bot, {"user_id": 9, "key_id": 1}, "traffic")
        self.assertEqual(update.await_count, 2)
        self.assertEqual(milestone.await_count, 2)
        bot.send_message.assert_awaited_once()
        text = bot.send_message.await_args.args[1]
        self.assertIn("Подключение подтверждено", text)
        self.assertIn("+15 дней", text)
        actions = [b.callback_data for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("partner", actions)
        self.assertIn("marketing_optout", actions)

    async def test_opted_out_never_gets_growth_prompt(self):
        from notifications import _record_verified_first_connection
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("notifications.update_connection_reminder", new=AsyncMock()),
            patch("database.log_analytics_event_once", new=AsyncMock(return_value=True)),
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=True)),
        ):
            await _record_verified_first_connection(bot, {"user_id": 9, "key_id": 1}, "online")
        bot.send_message.assert_not_awaited()

    async def test_send_failure_does_not_unconfirm_vpn_connection(self):
        from notifications import _record_verified_first_connection
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("Telegram offline")))
        with (
            patch("notifications.update_connection_reminder", new=AsyncMock()) as update,
            patch("database.log_analytics_event_once", new=AsyncMock(return_value=True)),
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=False)),
        ):
            await _record_verified_first_connection(bot, {"user_id": 9, "key_id": 1}, "traffic")
        update.assert_awaited_once_with(1, "connected")


class AnalyticsOnceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import database
        self.db = database
        self.old_file = database.DB_FILE
        self.old_pool = database._db_pool
        self.path = "/tmp/bymevpn_one_time_growth_test.db"
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass
        database.DB_FILE = self.path
        database._db_pool = None
        await database.init_db()

    async def asyncTearDown(self):
        await self.db.close_db()
        self.db.DB_FILE = self.old_file
        self.db._db_pool = self.old_pool
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    async def test_first_connection_milestone_returns_true_only_once(self):
        await self.db.ensure_user(123)
        self.assertTrue(await self.db.log_analytics_event_once(123, "first_connection", "traffic"))
        self.assertFalse(await self.db.log_analytics_event_once(123, "first_connection", "online"))
        db = await self.db.get_db()
        cur = await db.execute(
            "SELECT COUNT(*) FROM analytics_events WHERE user_id=123 AND event_type='first_connection'"
        )
        self.assertEqual((await cur.fetchone())[0], 1)


if __name__ == "__main__":
    unittest.main()
