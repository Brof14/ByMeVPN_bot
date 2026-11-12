"""Customer-facing expiry and promotional notification checks."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from notifications import _send_urgent_notification, _send_warning_notification, _send_autorenew_upcoming


class TestNotificationMarketing(unittest.IsolatedAsyncioTestCase):
    async def test_expiry_does_not_distribute_blanket_thirty_percent_discount(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        item = {"user_id": 901, "expiry": int(time.time()) + 2 * 86400}
        with (
            patch("notifications._get_active_autorenew", new=AsyncMock(return_value=None)),
        ):
            self.assertTrue(await _send_urgent_notification(bot, item))
        text = bot.send_message.await_args.args[1]
        self.assertIn("До окончания подписки", text)
        self.assertNotIn("30% скидки", text)
        keyboard = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(keyboard[0][0].callback_data, "buy_vpn")
        self.assertEqual(keyboard[1][0].callback_data, "free_vpn")

    async def test_autorenew_reminder_can_open_cancellation_in_one_tap(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        item = {"user_id": 902, "key_id": 42, "expiry": int(time.time()) + 2 * 86400}
        sub = {
            "key_id": 42, "amount_rub": 89, "days": 30, "devices": 2,
            "payment_method_type": "bank_card",
        }
        await _send_autorenew_upcoming(bot, item, sub)
        keyboard = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(keyboard[0][0].callback_data, "autorenew_unbind_prompt:42")
        self.assertIn("Оплаченный доступ сохранится", bot.send_message.await_args.args[1])

    async def test_trial_message_is_delayed_until_last_day(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        item = {"user_id": 901, "expiry": int(time.time()) + 2 * 86400, "is_trial": True}
        with patch("notifications._get_active_autorenew", new=AsyncMock()) as auto:
            self.assertFalse(await _send_urgent_notification(bot, item))
        auto.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_opted_out_customer_gets_service_reminder_without_promo(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        item = {"user_id": 902, "expiry": int(time.time()) + 10 * 86400}
        with (
            patch("notifications._get_active_autorenew", new=AsyncMock(return_value=None)),
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=True)),
        ):
            await _send_warning_notification(bot, item)

        text = bot.send_message.await_args.args[1]
        self.assertIn("Подписка активна до", text)
        self.assertNotIn("30% скидки", text)
