"""SQLite-backed tests for retry-safe giveaway automation."""
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

TEST_DB_PATH = "/tmp/test_giveaway_automation.db"
os.environ["DB_FILE"] = TEST_DB_PATH

import database


class TestGiveawayAutomation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(TEST_DB_PATH + suffix)
            except FileNotFoundError:
                pass
        database.DB_FILE = TEST_DB_PATH
        database._db = None
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(TEST_DB_PATH + suffix)
            except FileNotFoundError:
                pass

    async def test_monthly_campaign_is_created_once_and_respects_opt_out(self):
        from giveaway_automation import run_giveaway_automation

        await database.ensure_user(501)
        await database.ensure_user(502)
        await database.set_promotional_opt_out(502)
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(username="ByMeVPNbot")),
        )
        with patch("giveaway_automation._notify_admins", new=AsyncMock()):
            await run_giveaway_automation(bot)
            await run_giveaway_automation(bot)

        giveaways = await database.get_all_giveaways()
        self.assertEqual(len(giveaways), 1)
        self.assertEqual(giveaways[0]["prize_days"], 30)
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertEqual(bot.send_message.await_args.args[0], 501)
        self.assertIn("Участвовать", bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text)

    async def test_due_draw_grants_winner_once_and_reports_results(self):
        from giveaway_automation import process_due_giveaways

        winner_id = 601
        await database.ensure_user(winner_id)
        giveaway_id = await database.create_giveaway(
            title="Тестовый розыгрыш", description="test", prize_days=30,
            winners_count=1, end_date=int(time.time()) + 60,
            automation_key="test-giveaway",
        )
        self.assertTrue(await database.join_giveaway(giveaway_id, winner_id))
        db = await database.get_db()
        await db.execute("UPDATE giveaways SET end_date=? WHERE id=?", (int(time.time()) - 1, giveaway_id))
        await db.commit()

        bot = SimpleNamespace()
        with (
            patch("giveaway_automation._notify_admins", new=AsyncMock()),
            patch("database.get_user_keys", new=AsyncMock(return_value=[])),
            patch("subscription.deliver_key", new=AsyncMock(return_value=True)) as deliver,
        ):
            await process_due_giveaways(bot)
            await process_due_giveaways(bot)

        self.assertEqual(deliver.await_count, 1)
        self.assertEqual(deliver.await_args.kwargs["method"], "giveaway_prize")
        self.assertEqual(deliver.await_args.kwargs["payload"], f"giveaway_{giveaway_id}_{winner_id}")
        self.assertEqual(deliver.await_args.kwargs["days"], 30)
        self.assertEqual(await database.get_giveaways_ready_to_report(), [])

    async def test_repeat_entry_and_closed_giveaway_do_not_add_duplicates(self):
        participant_id = 701
        await database.ensure_user(participant_id)
        giveaway_id = await database.create_giveaway(
            title="Вход", prize_days=30, winners_count=1, end_date=int(time.time()) + 60,
        )
        self.assertTrue(await database.join_giveaway(giveaway_id, participant_id))
        self.assertFalse(await database.join_giveaway(giveaway_id, participant_id))
        await database.set_giveaway_status(giveaway_id, "cancelled")
        self.assertFalse(await database.join_giveaway(giveaway_id, 702))

