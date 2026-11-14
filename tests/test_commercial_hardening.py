"""Regression gates for monetization-safe navigation and free reward delivery."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from keyboards import (
    authorized_user_menu, key_detail_kb, key_settings_kb,
    main_menu_active, main_menu_existing, main_menu_expired,
    main_menu_new_user, main_menu_panel_active, main_menu_with_keys,
)


def callbacks(keyboard):
    return [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]


class CommercialNavigationTests(unittest.TestCase):
    def test_every_home_state_has_colored_free_days(self):
        states = (
            main_menu_new_user(), main_menu_existing(trial_used=True),
            main_menu_with_keys(), main_menu_active(42),
            main_menu_panel_active(), main_menu_expired(key_ids=[42]),
            authorized_user_menu(),
        )
        for keyboard in states:
            with self.subTest(rows=len(keyboard.inline_keyboard)):
                buttons = [b for row in keyboard.inline_keyboard for b in row]
                self.assertIn("free_vpn", callbacks(keyboard))
                self.assertTrue(all(b.style in ("primary", "success", "danger") for b in buttons))
                free = next(b for b in buttons if b.callback_data == "free_vpn")
                self.assertEqual(free.style, "primary")

    def test_devices_live_inside_subscription_and_actions_are_grouped(self):
        home = main_menu_active(42)
        self.assertNotIn("key_devices:42", callbacks(home))
        self.assertEqual(
            [b.callback_data for b in home.inline_keyboard[1]],
            ["my_keys"],
        )
        details = key_detail_kb(42, subscription_url="https://example.test/sub/key")
        self.assertEqual(
            [b.callback_data for b in details.inline_keyboard[1]],
            ["key_renew:42", "key_devices:42"],
        )
        self.assertIn("key_settings:42", callbacks(details))
        self.assertNotIn("key_diagnostics:42", callbacks(key_settings_kb(42)))
        expired = callbacks(key_detail_kb(42, is_active=False))
        self.assertEqual(expired[0], "key_renew:42")
        self.assertNotIn("key_instructions:42", expired)

    def test_tariff_bills_entire_duration_transparently(self):
        from keyboards import tariff_selection_kb
        menu = tariff_selection_kb(devices=2)
        labels = {b.callback_data: b.text for row in menu.inline_keyboard for b in row}
        for months, label, total in (
            (1, "1 мес.", 89), (3, "3+1 мес.", 316),
            (6, "6+2 мес.", 552), (12, "12+3 мес.", 885)
        ):
            text = labels[f"tariff_{months}_2"]
            self.assertIn(label, text)
            self.assertIn(f"{total:,}".replace(",", " ") + " ₽", text)
            self.assertEqual("🎁" in text, months > 1)

    def test_connection_html_has_no_stale_apk_hardcoding(self):
        root = Path(__file__).resolve().parents[1]
        chooser = (root / "templates" / "connect.html").read_text("utf-8")
        installer = (root / "static" / "app" / "index.html").read_text("utf-8")
        self.assertNotIn("ByMeVPN-v1.0.14.apk", chooser)
        self.assertNotIn("ByMeVPN-v1.0.14.apk", installer)
        self.assertIn("в разработке", installer)
        self.assertIn("https://t.me/ByMeVPNSupportBot", installer)


class FreeRewardAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_file = database.DB_FILE
        self.original_pool = database._db_pool
        database.DB_FILE = str(Path(self.temp.name) / "rewards.sqlite")
        database._db_pool = None
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.original_file
        database._db_pool = self.original_pool
        self.temp.cleanup()

    async def test_channel_claim_is_serialized_and_permanently_one_time(self):
        self.assertIsNone(await database.get_channel_subscription_bonus_status(1001))
        self.assertTrue(await database.reserve_channel_subscription_bonus(1001))
        self.assertFalse(await database.reserve_channel_subscription_bonus(1001))
        self.assertEqual(await database.get_channel_subscription_bonus_status(1001), "reserved")
        self.assertTrue(await database.complete_channel_subscription_bonus(1001))
        self.assertEqual(await database.get_channel_subscription_bonus_status(1001), "awarded")
        self.assertFalse(await database.reserve_channel_subscription_bonus(1001))
        self.assertFalse(await database.complete_channel_subscription_bonus(1001))

    async def test_free_promo_freezes_selection_and_reserves_capacity(self):
        created = await database.create_promo_code(
            "FREESAFE", promo_type="percent", discount_value=100, max_uses=1,
            valid_days=30,
        )
        self.assertTrue(created)
        self.assertTrue(await database.reserve_free_promo_order(1001, "freesafe", 1, 2, 30))
        self.assertTrue(await database.reserve_free_promo_order(1001, "FREESAFE", 1, 2, 30))
        self.assertFalse(await database.reserve_free_promo_order(1001, "FREESAFE", 1, 10, 30))
        self.assertFalse(await database.reserve_free_promo_order(1002, "FREESAFE", 1, 2, 30))
        self.assertTrue(await database.complete_free_promo_order(1001, "freesafe"))
        self.assertFalse(await database.reserve_free_promo_order(1001, "FREESAFE", 1, 2, 30))

    async def test_failed_channel_ledger_finalize_never_claims_success(self):
        from handlers.start import cb_channel_bonus_check
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member"))
        )
        callback = SimpleNamespace(from_user=SimpleNamespace(id=1001))
        with (
            patch("handlers.start.safe_answer", new=AsyncMock()) as answer,
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[])),
            patch("database.reserve_channel_subscription_bonus", new=AsyncMock(return_value=True)),
            patch("database.complete_channel_subscription_bonus", new=AsyncMock(return_value=False)),
            patch("subscription.deliver_key", new=AsyncMock(return_value=True)),
            patch("handlers.start.cb_free_vpn", new=AsyncMock()) as refresh,
        ):
            await cb_channel_bonus_check(callback, bot)
        refresh.assert_awaited_once_with(callback, bot)
        self.assertIn("проверяется", answer.await_args.args[1])
        self.assertNotIn("Готово", answer.await_args.args[1])

    async def test_earned_channel_reward_is_not_offered_twice(self):
        from handlers.start import cb_free_vpn
        callback = SimpleNamespace(from_user=SimpleNamespace(id=1001))
        with (
            patch("handlers.start.safe_answer", new=AsyncMock()),
            patch("database.log_analytics_event", new=AsyncMock()),
            patch("database.get_channel_subscription_bonus_status", new=AsyncMock(return_value="awarded")),
            patch("handlers.start.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_free_vpn(callback, object())
        actions = callbacks(send.await_args.args[3])
        self.assertNotIn("channel_bonus_check", actions)
        self.assertIn("partner", actions)
        self.assertIn("ad_program", actions)
        self.assertIn("my_keys", actions)

    async def test_panel_recovery_is_read_only_and_duplicate_safe(self):
        from panel_recovery import restore_panel_only_subscription
        user_id = 2101
        expiry = int(time.time()) + 86400 * 20
        panel = {
            "email": str(user_id),
            "enable": True,
            "uuid": "11111111-1111-4111-8111-111111111111",
            "subscription_url": "https://vpn.example.test/subscription/example-id",
            "expiry_time": expiry * 1000,
            "limit_hwid": 5,
        }
        first = await restore_panel_only_subscription(user_id, panel)
        self.assertIsInstance(first, int)
        again = await restore_panel_only_subscription(user_id, panel)
        self.assertEqual(first, again)
        keys = await database.get_user_keys(user_id)
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["expiry"], expiry)
        self.assertEqual(keys[0]["limit_ip"], 5)
        self.assertEqual(keys[0]["uuid"], panel["uuid"])

    async def test_panel_recovery_rejects_wrong_user_expiry_and_limit(self):
        from panel_recovery import restore_panel_only_subscription
        user_id = 2102
        base = {
            "email": str(user_id), "enable": True,
            "uuid": "11111111-1111-4111-8111-111111111111",
            "subscription_url": "https://vpn.example.test/subscription/example-id",
            "expiry_time": (int(time.time()) + 86400) * 1000,
            "limit_hwid": 2,
        }
        for override in (
            {"email": "2103"},
            {"expiry_time": (int(time.time()) - 10) * 1000},
            {"limit_hwid": 0},
            {"subscription_url": "http://vpn.example.test/subscription/key"},
            {"enable": False},
            {"uuid": "not-a-uuid"},
        ):
            with self.subTest(case=override):
                self.assertIsNone(await restore_panel_only_subscription(user_id, {**base, **override}))
        self.assertEqual(await database.get_user_keys(user_id), [])

    async def test_panel_only_subscription_is_visible_without_local_key(self):
        from handlers.keys import cb_my_keys
        now = int(time.time())
        callback = SimpleNamespace(from_user=SimpleNamespace(id=1001))
        with (
            patch("handlers.keys.safe_answer", new=AsyncMock()),
            patch("handlers.keys.get_user_keys", new=AsyncMock(return_value=[])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                "enable": True, "expiry_time": (now + 86400) * 1000,
                "limit_hwid": 5,
            })),
            patch("handlers.keys.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_my_keys(callback, object())
        self.assertIn("Подписка активна на сервере", send.await_args.args[2])
        self.assertTrue(any(
            button.url and "ByMeVPNSupportBot" in button.url
            for row in send.await_args.args[3].inline_keyboard for button in row
        ))


class AutomatedGrowthTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_time_offer_promotes_ads_and_referrals_with_optout(self):
        from notifications import _referral_nudge_pass
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("database.get_due_referral_nudges", new=AsyncMock(return_value=[1001])),
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=False)),
            patch("database.log_analytics_event_once", new=AsyncMock(return_value=True)) as only_once,
            patch("database.log_analytics_event", new=AsyncMock()),
        ):
            await _referral_nudge_pass(bot)
        only_once.assert_awaited_once_with(1001, "referral_nudge_attempted")
        buttons = callbacks(bot.send_message.await_args.kwargs["reply_markup"])
        self.assertIn("partner", buttons)
        self.assertIn("free_vpn", buttons)
        self.assertIn("marketing_optout", buttons)
        self.assertTrue(all(
            button.style in ("primary", "success", "danger")
            for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ))

    async def test_opted_out_user_never_receives_growth_offer(self):
        from notifications import _referral_nudge_pass
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("database.get_due_referral_nudges", new=AsyncMock(return_value=[1001])),
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=True)),
            patch("database.log_analytics_event_once", new=AsyncMock()) as only_once,
        ):
            await _referral_nudge_pass(bot)
        bot.send_message.assert_not_awaited()
        only_once.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
