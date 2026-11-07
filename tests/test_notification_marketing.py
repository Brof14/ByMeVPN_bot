"""Customer-facing expiry and promotional notification checks."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from notifications import _send_urgent_notification, _send_warning_notification


class TestNotificationMarketing(unittest.IsolatedAsyncioTestCase):
    async def test_renewal_offer_skips_used_future_and_tariff_bound_codes(self):
        from notifications import get_or_create_renewal_promo
        now = int(time.time())
        base = {"is_active": True, "promo_type": "percent", "discount_value": 30,
                "uses_count": 0, "max_uses": 10, "start_date": now - 10,
                "expires_at": now + 86400, "tariff_binding": None}
        codes = [
            {**base, "code": "FUTURE", "start_date": now + 3600},
            {**base, "code": "BOUND", "tariff_binding": 12},
            {**base, "code": "USED"},
            {**base, "code": "AVAILABLE"},
        ]
        with (
            patch("database.get_all_promo_codes", new=AsyncMock(return_value=codes)),
            patch("database.has_user_used_promo", new=AsyncMock(side_effect=lambda code, user: code == "USED")),
            patch("notifications.create_promo_code", new=AsyncMock()) as create,
        ):
            result = await get_or_create_renewal_promo(901)
        self.assertEqual(result, "AVAILABLE")
        create.assert_not_awaited()

    async def test_expiry_offer_states_exact_promo_deadline_and_clear_actions(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        item = {"user_id": 901, "expiry": int(time.time()) + 2 * 86400}
        with (
            patch("notifications._get_active_autorenew", new=AsyncMock(return_value=None)),
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=False)),
            patch("notifications.get_or_create_renewal_promo", new=AsyncMock(return_value="TEST30")),
            patch("notifications.validate_promo_code", new=AsyncMock(return_value={"expires_at": int(time.time()) + 86400})),
        ):
            await _send_urgent_notification(bot, item)

        text = bot.send_message.await_args.args[1]
        self.assertIn("До окончания подписки", text)
        self.assertIn("30% скидки", text)
        self.assertIn("до ", text)
        self.assertNotIn("🚨", text)
        keyboard = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(keyboard[0][0].callback_data, "buy_vpn")
        self.assertEqual(keyboard[0][0].style, "primary")
        self.assertEqual(keyboard[1][0].callback_data, "free_vpn")

    async def test_opted_out_customer_gets_service_reminder_without_promo(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        item = {"user_id": 902, "expiry": int(time.time()) + 10 * 86400}
        with (
            patch("notifications._get_active_autorenew", new=AsyncMock(return_value=None)),
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=True)),
            patch("notifications.get_or_create_renewal_promo", new=AsyncMock()) as create_promo,
            patch("notifications.validate_promo_code", new=AsyncMock()) as validate,
        ):
            await _send_warning_notification(bot, item)

        text = bot.send_message.await_args.args[1]
        self.assertIn("Подписка активна до", text)
        self.assertNotIn("30% скидки", text)
        create_promo.assert_not_awaited()
        validate.assert_not_awaited()
