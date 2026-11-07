"""Real database regression tests for truthful onboarding and growth."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class VerifiedOnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import database
        self.db = database
        self.original_path = database.DB_FILE
        self.original_pool = database._db_pool
        self.tmp = tempfile.TemporaryDirectory()
        database.DB_FILE = str(Path(self.tmp.name) / "growth.db")
        database._db_pool = None
        await database.init_db()
        await database.ensure_user(123)
        self.key_id = await database.add_key(123, "sub", "test", "uuid", 30, 2)
        await database.schedule_connection_reminder(self.key_id, 0, delay_seconds=0)

    async def asyncTearDown(self):
        await self.db.close_db()
        self.db.DB_FILE = self.original_path
        self.db._db_pool = self.original_pool
        self.tmp.cleanup()

    async def _verify(self):
        from notifications import _record_verified_first_connection
        bot = SimpleNamespace(send_message=AsyncMock())
        await _record_verified_first_connection(
            bot, {"user_id": 123, "key_id": self.key_id}, "traffic"
        )
        return bot

    async def test_verified_connection_records_one_milestone_without_sales_message(self):
        bot = await self._verify()
        await self._verify()
        cur = await (await self.db.get_db()).execute(
            "SELECT COUNT(*) FROM analytics_events WHERE user_id=123 AND event_type='first_connection'"
        )
        self.assertEqual((await cur.fetchone())[0], 1)
        self.assertEqual(await self.db.get_due_connection_reminders(), [])
        bot.send_message.assert_not_awaited()
        self.assertEqual(await self.db.get_due_referral_nudges(), [])

    async def test_referral_due_after_day_only_and_rechecked_for_opt_out(self):
        from notifications import _referral_nudge_pass
        await self._verify()
        now = int(time.time())
        self.assertEqual(await self.db.get_due_referral_nudges(now=now), [])
        db = await self.db.get_db()
        await db.execute(
            "UPDATE analytics_events SET timestamp=? WHERE user_id=123 AND event_type='first_connection'",
            (now - 25 * 3600,),
        )
        await db.commit()
        self.assertEqual(await self.db.get_due_referral_nudges(now=now), [123])
        await self.db.set_promotional_opt_out(123, True)
        self.assertEqual(await self.db.get_due_referral_nudges(now=now), [])
        bot = SimpleNamespace(send_message=AsyncMock())
        await _referral_nudge_pass(bot)
        bot.send_message.assert_not_awaited()
        await self.db.set_promotional_opt_out(123, False)
        await _referral_nudge_pass(bot)
        bot.send_message.assert_awaited_once()
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        actions = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("partner", actions)
        self.assertIn("marketing_optout", actions)
        await _referral_nudge_pass(bot)
        self.assertEqual(bot.send_message.await_count, 1)

    async def test_failed_telegram_send_is_not_repeated(self):
        from notifications import _referral_nudge_pass
        await self._verify()
        db = await self.db.get_db()
        await db.execute(
            "UPDATE analytics_events SET timestamp=? WHERE user_id=123 AND event_type='first_connection'",
            (int(time.time()) - 25 * 3600,),
        )
        await db.commit()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("timeout")))
        await _referral_nudge_pass(bot)
        await _referral_nudge_pass(bot)
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertEqual(await self.db.get_due_referral_nudges(), [])

    async def test_no_referral_to_expired_access(self):
        await self._verify()
        db = await self.db.get_db()
        await db.execute(
            "UPDATE analytics_events SET timestamp=? WHERE user_id=123 AND event_type='first_connection'",
            (int(time.time()) - 25 * 3600,),
        )
        await db.execute("UPDATE keys SET expiry=0 WHERE id=?", (self.key_id,))
        await db.commit()
        self.assertEqual(await self.db.get_due_referral_nudges(), [])

    async def test_repeated_start_is_not_an_acquisition(self):
        old = int(time.time()) - 40 * 86400
        await self.db.ensure_user(321)
        db = await self.db.get_db()
        await db.execute("UPDATE users SET created=? WHERE user_id=321", (old,))
        await db.commit()
        await self.db.log_analytics_event(321, "bot_started", source="returning")
        await self.db.log_analytics_event(123, "bot_started", source="direct")
        result = await self.db.get_conversion_funnel(30)
        self.assertEqual(result["totals"]["started"], 1)
        self.assertEqual(result["by_source"]["direct"]["started"], 1)
        self.assertNotIn("returning", result["by_source"])


if __name__ == "__main__":
    unittest.main()
