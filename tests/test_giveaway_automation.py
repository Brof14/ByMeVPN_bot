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

    async def test_scheduler_recovers_new_signup_reward(self):
        from giveaway_automation import run_giveaway_automation
        await database.ensure_user(701)
        await database.ensure_user(702)
        await database.set_referrer(702, 701)
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(username="ByMeVPNbot")),
        )
        with (
            patch("subscription.deliver_key", new=AsyncMock(return_value=True)),
            patch("giveaway_automation._notify_admins", new=AsyncMock()),
        ):
            await run_giveaway_automation(bot)
        self.assertTrue(await database.has_referral_event(701, 702, "registration_bonus"))

    async def test_verified_ad_reward_is_retried_automatically_after_failure(self):
        from handlers.ad_program import process_pending_ad_rewards
        await database.ensure_user(801)
        app_id = await database.create_ad_application(
            user_id=801, channel_id=-100801, channel_username="channel801",
            subscriber_count=20, bonus_months=2, channel_ref="channel801", status="grant_pending",
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        delivery = AsyncMock(side_effect=[False, True])
        with patch("subscription.deliver_key", new=delivery):
            self.assertEqual(await process_pending_ad_rewards(bot), 0)
            self.assertEqual((await database.get_ad_application_by_id(app_id))["status"], "grant_pending")
            self.assertEqual(await process_pending_ad_rewards(bot), 1)
            self.assertEqual(await process_pending_ad_rewards(bot), 0)
        self.assertEqual((await database.get_ad_application_by_id(app_id))["status"], "approved")
        self.assertEqual([call.kwargs["payload"] for call in delivery.await_args_list], [f"ad_bonus_{app_id}"] * 2)

    async def test_scheduler_recovers_verified_ad_reward(self):
        from giveaway_automation import run_giveaway_automation
        await database.ensure_user(805)
        app_id = await database.create_ad_application(
            user_id=805, channel_id=-100805, channel_username="channel805",
            subscriber_count=20, bonus_months=2, channel_ref="channel805", status="grant_pending",
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(username="ByMeVPNbot")),
        )
        with (
            patch("subscription.deliver_key", new=AsyncMock(return_value=True)),
            patch("giveaway_automation._notify_admins", new=AsyncMock()),
        ):
            await run_giveaway_automation(bot)
        self.assertEqual((await database.get_ad_application_by_id(app_id))["status"], "approved")

    async def test_failed_verified_ad_reward_does_not_block_other_partners(self):
        from handlers.ad_program import process_pending_ad_rewards
        for user_id in (811, 812):
            await database.ensure_user(user_id)
            await database.create_ad_application(
                user_id=user_id, channel_id=-100000 - user_id, channel_username=f"channel{user_id}",
                subscriber_count=20, bonus_months=2, channel_ref=f"channel{user_id}", status="grant_pending",
            )
        bot = SimpleNamespace(send_message=AsyncMock())
        delivery = AsyncMock(side_effect=lambda **kw: kw["user_id"] == 812)
        with patch("subscription.deliver_key", new=delivery):
            self.assertEqual(await process_pending_ad_rewards(bot, limit=1), 0)
            self.assertEqual(await process_pending_ad_rewards(bot, limit=1), 1)
        self.assertEqual([call.kwargs["user_id"] for call in delivery.await_args_list], [811, 812])

    async def test_pending_ad_reward_migration_preserves_application(self):
        await database.ensure_user(821)
        app_id = await database.create_ad_application(
            user_id=821, channel_id=-100821, channel_username="channel821",
            subscriber_count=20, bonus_months=2, channel_ref="channel821", status="grant_pending",
        )
        db = await database.get_db()
        await db.execute("ALTER TABLE ad_applications DROP COLUMN grant_attempt_ms")
        await db.commit()
        await database.init_db()
        self.assertEqual([app["id"] for app in await database.get_pending_ad_reward_retries()], [app_id])

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
        self.assertEqual(giveaways[0]["winners_count"], 3)
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertEqual(bot.send_message.await_args.args[0], 501)
        self.assertIn("Участвовать", bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text)
        self.assertNotIn("YouTube", bot.send_message.await_args.args[1])
        self.assertNotIn("Instagram", bot.send_message.await_args.args[1])
        self.assertIn("пригласите хотя бы одного нового пользователя", bot.send_message.await_args.args[1])
        self.assertIn("В зачёт попадёт тот, кто впервые запустит бота по вашей ссылке", bot.send_message.await_args.args[1])
        self.assertIn("3 победителя получат по <b>+30 дней VPN</b>", bot.send_message.await_args.args[1])

    async def test_old_monthly_campaign_is_updated_without_resending_to_users(self):
        from giveaway_automation import _month_period, ensure_monthly_giveaway

        month, label, period_start, period_end = _month_period()
        await database.ensure_user(550)
        giveaway_id = await database.create_giveaway(
            title=f"Розыгрыш ByMeVPN · {label}", description="old rules",
            prize_days=30, winners_count=1, end_date=int(time.time()) + 86400,
            created_by=0, automation_key=f"auto-monthly-{month}",
        )
        await database.create_giveaway_notification_snapshot(giveaway_id)
        await database.set_giveaway_notification_status(giveaway_id, 550, "sent")

        with patch("giveaway_automation._notify_admins", new=AsyncMock()):
            updated = await ensure_monthly_giveaway(SimpleNamespace())

        self.assertEqual(updated["winners_count"], 3)
        self.assertEqual(updated["prize_days"], 30)
        self.assertEqual(updated["period_start"], period_start)
        self.assertEqual(updated["end_date"], period_end)
        self.assertEqual(await database.get_pending_giveaway_notifications(giveaway_id), [])

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

    async def test_monthly_contest_qualifies_on_one_referral_and_ranks_top_three(self):
        now = int(time.time())
        giveaway_id = await database.create_giveaway(
            title="Monthly contest", description="test", prize_days=30,
            winners_count=3, end_date=now + 60, period_start=now - 86400,
            automation_key="auto-monthly-test",
        )
        referral_counts = {801: 4, 802: 3, 803: 2, 804: 1, 805: 0}
        for user_id, count in referral_counts.items():
            await database.ensure_user(user_id)
            await database.join_giveaway(giveaway_id, user_id)
            for offset in range(count):
                referred_id = 9000 + user_id * 10 + offset
                await database.ensure_user(referred_id)
                await database.set_referrer(referred_id, user_id)
                db = await database.get_db()
                await db.execute("UPDATE referrals SET created=? WHERE referred_id=?", (now - 10, referred_id))
                await db.commit()

        # Historical inconsistent rows must not qualify a different inviter.
        db = await database.get_db()
        await db.execute(
            "INSERT INTO referrals(referrer_id, referred_id, created) VALUES(?,?,?)",
            (805, 9000 + 801 * 10, now - 10),
        )
        await db.commit()
        for user_id, count in referral_counts.items():
            status = await database.get_giveaway_entry_status(giveaway_id, user_id)
            self.assertEqual(status["qualified"], count > 0)
            self.assertEqual(status["referral_count"], count)

        db = await database.get_db()
        await db.execute("UPDATE giveaways SET end_date=? WHERE id=?", (now - 1, giveaway_id))
        await db.commit()
        winners = await database.pick_giveaway_winners(giveaway_id)
        self.assertEqual(set(winners), {801, 802, 803})
        winner_data = await database.get_unrewarded_giveaway_winners(retry_after_seconds=0)
        self.assertEqual({row["user_id"] for row in winner_data}, {801, 802, 803})

