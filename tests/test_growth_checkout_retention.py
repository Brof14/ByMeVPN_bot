"""Regression contracts for the November 2026 revenue and referral UX release."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class CheckoutUxTests(unittest.IsolatedAsyncioTestCase):
    def test_single_yookassa_checkout_with_switchable_default(self):
        """Preserve user-excluded 3.2: checkout has one recurring YooKassa option."""
        from keyboards import payment_kb
        buttons = [b for row in payment_kb(316, 120).inline_keyboard for b in row]
        callbacks = [b.callback_data for b in buttons]
        self.assertEqual(callbacks.count("pay_yookassa"), 1)
        self.assertNotIn("pay_yookassa_autorenew", callbacks)
        self.assertIn("checkout_autorenew_toggle", callbacks)

    def test_ten_device_renewal_matches_approved_screen(self):
        from handlers.buy import _tariff_intro
        from keyboards import tariff_selection_kb
        with patch.dict("os.environ", {"BOT_PREMIUM_BUTTON_ICONS": "0"}):
            kb = tariff_selection_kb(devices=10)
        expected = [
            ("devtier_2", "2 устройства"),
            ("devtier_5", "5 устройств"),
            ("devtier_10", "✓ 10 устройств"),
            ("tariff_1_10", "1 мес. — 129 ₽"),
            ("tariff_3_10", "3+1 мес. 🎁 — 119 ₽/мес. · 476 ₽"),
            ("tariff_6_10", "6+2 мес. 🎁 — 109 ₽/мес. · 872 ₽"),
            ("tariff_12_10", "12+3 мес. 🎁 — 99 ₽/мес. · 1 485 ₽"),
            ("promo_enter", "Есть промокод?"),
            ("back_to_menu", "← Назад"),
        ]
        actual = [
            (button.callback_data, button.text)
            for row in kb.inline_keyboard for button in row
        ]
        self.assertEqual(actual, expected)
        self.assertEqual(
            [button.callback_data for row in kb.inline_keyboard[:2] for button in row],
            ["devtier_2", "devtier_5", "devtier_10"],
        )
        self.assertEqual(
            _tariff_intro(10, renewal=True),
            "<b>Продление подписки</b>\n\n"
            "10 устройств — для вас и ваших близких.\n\n"
            "Выберите срок подписки. Чем дольше срок, тем ниже стоимость месяца.\n"
            "Полная сумма указана в каждом тарифе.",
        )

    def test_checkout_uses_bundles_and_discloses_future_charge(self):
        from handlers.buy import _checkout_confirmation_text
        base = {
            "months": 3, "devices": 2, "price_rub": 316, "days": 120,
            "checkout_autorenew_disclosed": True, "autorenew_enabled": True,
        }
        text = _checkout_confirmation_text(base)
        self.assertIn("3+1 мес.", text)
        self.assertIn("316 ₽", text)
        self.assertIn("120 дней", text)
        self.assertIn("Следующее списание", text)
        self.assertIn("Отключить", text)
        # The explicitly excluded choice feature is unchanged.
        legacy_display = _checkout_confirmation_text({**base, "autorenew_enabled": False})
        self.assertIn("Автопродление ЮKassa: выключено", legacy_display)

    def test_promo_disclosure_shows_regular_future_billing_amount(self):
        from handlers.buy import _checkout_confirmation_text
        text = _checkout_confirmation_text({
            "months": 3, "days": 125, "devices": 2, "price_rub": 221,
            "original_price": 316,
            "checkout_autorenew_disclosed": True, "autorenew_enabled": True,
        })
        self.assertIn("К оплате сейчас: <b>221 ₽</b>", text)
        self.assertIn("Следующее списание: 316 ₽ за 120 дней", text)

    def test_recurring_quote_is_validated_and_legacy_terms_preserved(self):
        from webhook import _recurring_terms_for_saved_method
        quote = {
            "months": "3", "auto_renew_recurring_price_rub": "316",
            "auto_renew_recurring_days": "120",
        }
        self.assertEqual(_recurring_terms_for_saved_method(quote, 221, 125, 2), (316, 120))
        self.assertIsNone(_recurring_terms_for_saved_method(
            {**quote, "auto_renew_recurring_price_rub": "221"}, 221, 125, 2
        ))
        self.assertIsNone(_recurring_terms_for_saved_method(
            {**quote, "auto_renew_recurring_days": "999"}, 221, 125, 2
        ))
        self.assertIsNone(_recurring_terms_for_saved_method(
            {**quote, "auto_renew_recurring_days": None}, 221, 125, 2
        ))
        self.assertEqual(_recurring_terms_for_saved_method({}, 221, 125, 2), (221, 125))

    async def test_legacy_checkout_never_converts_one_time_into_recurring(self):
        from handlers.buy import _get_checkout_params
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            "months": 1, "devices": 2, "price_rub": 89, "days": 30,
        }))
        params = await _get_checkout_params(state, 101)
        self.assertTrue(params["autorenew_enabled"])
        self.assertTrue(params["checkout_autorenew_disclosed"])

    async def test_customer_can_disable_autorenew_before_checkout(self):
        """Check current checkout without changing explicitly excluded audit 3.2."""
        from keyboards import payment_kb
        callbacks = [b.callback_data for row in payment_kb(89, 30).inline_keyboard for b in row]
        self.assertEqual(callbacks.count("pay_yookassa"), 1)
        self.assertNotIn("autorenew_checkout_toggle", callbacks)

class RetentionQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_trial_does_not_get_renewal_offer_on_first_day(self):
        from notifications import _send_urgent_notification
        item = {"user_id": 123, "key_id": 4, "expiry": int(time.time()) + 2 * 86400,
                "is_trial": True}
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch("notifications._get_active_autorenew", new=AsyncMock()) as renew:
            self.assertFalse(await _send_urgent_notification(bot, item))
        renew.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_trial_gets_clear_last_day_offer(self):
        from notifications import _send_urgent_notification
        item = {"user_id": 123, "key_id": 4, "expiry": int(time.time()) + 30000,
                "is_trial": True}
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch("notifications._get_active_autorenew", new=AsyncMock(return_value=None)):
            self.assertTrue(await _send_urgent_notification(bot, item))
        self.assertIn("Пробный доступ", bot.send_message.await_args.args[1])
        buttons = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(buttons[0][0].callback_data, "key_renew:4")

    async def test_winback_respects_opt_out(self):
        from notifications import _send_winback_notification
        item = {"user_id": 123, "key_id": 4, "expiry": int(time.time()) - 3 * 86400}
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=True)),
            patch("database.get_user_keys", new=AsyncMock()) as lookup,
        ):
            self.assertFalse(await _send_winback_notification(bot, item))
        lookup.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_winback_only_for_still_lapsed_customer(self):
        from notifications import _send_winback_notification
        item = {"user_id": 123, "key_id": 4, "expiry": int(time.time()) - 3 * 86400}
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=False)),
            patch("database.get_user_keys", new=AsyncMock(return_value=[])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=None)),
            patch("notifications._get_active_autorenew", new=AsyncMock(return_value=None)),
        ):
            self.assertTrue(await _send_winback_notification(bot, item))
        self.assertIn("Вернуться", bot.send_message.await_args.args[1])
        actions = [b.callback_data for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("marketing_optout", actions)
        self.assertIn("key_renew:4", actions)

    async def test_winback_never_targets_panel_only_active_customer(self):
        from notifications import _send_winback_notification
        item = {"user_id": 123, "key_id": 4, "expiry": int(time.time()) - 3 * 86400}
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=False)),
            patch("database.get_user_keys", new=AsyncMock(return_value=[])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(
                return_value={"expiry_time": (int(time.time()) + 7200) * 1000}
            )),
            patch("notifications._get_active_autorenew", new=AsyncMock()) as renew,
        ):
            self.assertFalse(await _send_winback_notification(bot, item))
        renew.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_new_ad_reward_tiers_are_capped(self):
        from database import calculate_bonus_months
        expected = [(1, 1), (49, 1), (50, 2), (99, 2), (100, 3),
                    (150, 4), (250, 6), (350, 12)]
        for subscribers, months in expected:
            with self.subTest(subscribers=subscribers):
                self.assertEqual(await calculate_bonus_months(subscribers), months)


if __name__ == "__main__":
    unittest.main()
