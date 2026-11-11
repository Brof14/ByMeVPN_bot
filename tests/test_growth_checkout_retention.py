"""Regression contracts for the November 2026 revenue and referral UX release."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class CheckoutUxTests(unittest.IsolatedAsyncioTestCase):
    def test_single_yookassa_checkout_with_switchable_default(self):
        from keyboards import payment_kb
        first = payment_kb(316, 120)
        actions = [b for row in first.inline_keyboard for b in row]
        self.assertEqual(sum(b.callback_data == "pay_yookassa" for b in actions), 1)
        self.assertFalse(any(b.callback_data == "pay_yookassa_autorenew" for b in actions))
        control = next(b for b in actions if b.callback_data == "autorenew_checkout_toggle")
        self.assertIn("включено", control.text)
        disabled = payment_kb(316, 120, autorenew_enabled=False)
        self.assertTrue(any(
            b.callback_data == "autorenew_checkout_toggle" and "выключено" in b.text
            for row in disabled.inline_keyboard for b in row
        ))

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
        self.assertIn("повторное списание", text)
        self.assertIn("Отключить", text)
        one_time = _checkout_confirmation_text({**base, "autorenew_enabled": False})
        self.assertIn("разовой", one_time)
        self.assertNotIn("соглашаетесь на повторное", one_time)

    async def test_legacy_checkout_never_converts_one_time_into_recurring(self):
        from handlers.buy import _get_checkout_params
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            "months": 1, "devices": 2, "price_rub": 89, "days": 30,
        }))
        params = await _get_checkout_params(state, 101)
        self.assertFalse(params["autorenew_enabled"])
        self.assertFalse(params["checkout_autorenew_disclosed"])

    async def test_customer_can_disable_autorenew_before_checkout(self):
        from handlers.buy import cb_autorenew_checkout_toggle
        callback = SimpleNamespace(from_user=SimpleNamespace(id=101))
        state = SimpleNamespace(update_data=AsyncMock())
        params = {"price_rub": 89, "days": 30, "months": 1, "devices": 2,
                  "checkout_autorenew_disclosed": True, "autorenew_enabled": True}
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy._get_checkout_params", new=AsyncMock(return_value=params)),
            patch("handlers.buy._show_checkout", new=AsyncMock()) as show,
        ):
            await cb_autorenew_checkout_toggle(callback, object(), state)
        state.update_data.assert_awaited_once_with(autorenew_enabled=False)
        show.assert_awaited_once()


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

    async def test_new_ad_reward_tiers_are_capped(self):
        from database import calculate_bonus_months
        expected = [(1, 1), (49, 1), (50, 2), (99, 2), (100, 3),
                    (150, 4), (250, 6), (350, 12)]
        for subscribers, months in expected:
            with self.subTest(subscribers=subscribers):
                self.assertEqual(await calculate_bonus_months(subscribers), months)


if __name__ == "__main__":
    unittest.main()
