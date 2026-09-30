"""Regression tests for subscription delivery failure paths."""
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Keep imports isolated from the production database.
os.environ.setdefault("DB_FILE", "/tmp/test_subscription_delivery.db")

import subscription


class TestSubscriptionDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_new_admin_key_is_saved_after_3xui_creation(self):
        """A successful 3x-ui response must reach add_key without a local-import shadow."""
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()

        xui_result = {
            "subscription_url": "https://panel.example/sub/test-sub-id",
            "vless_links": [],
            "uuid": "test-client-uuid",
        }

        with (
            patch("subscription.get_user_keys", new=AsyncMock(return_value=[])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=None)),
            patch("subscription.create_xui_user", new=AsyncMock(return_value=xui_result)),
            patch("subscription.add_key", new=AsyncMock(return_value=42)) as add_key,
        ):
            result = await subscription.deliver_key(
                bot=bot,
                user_id=5283200584,
                chat_id=5283200584,
                config_name="Admin-Key",
                days=30,
                limit_ip=5,
                method="admin_grant",
            )

        self.assertTrue(result)
        add_key.assert_awaited_once_with(
            5283200584,
            "https://panel.example/sub/test-sub-id",
            "Admin-Key",
            "test-client-uuid",
            30,
            5,
        )
        bot.send_photo.assert_awaited_once()
        bot.send_message.assert_not_awaited()

    async def test_paid_renewal_reaches_payment_recording(self):
        """Renewal must not fail when another branch imports the same helper locally."""
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()
        existing_key = {
            "id": 7,
            "key": "https://panel.example/sub/existing-sub-id",
            "uuid": "existing-uuid",
            "expiry": int(time.time()) + 30 * 86400,
            "limit_ip": 2,
        }
        xui_update = {
            "subscription_url": "https://panel.example/sub/existing-sub-id",
            "expiry_time": (existing_key["expiry"] + 30 * 86400) * 1000,
        }

        with (
            patch("subscription.get_user_keys", new=AsyncMock(return_value=[existing_key])),
            patch("subscription.extend_key", new=AsyncMock(return_value=True)),
            patch("xui_client.update_xui_user", new=AsyncMock(return_value=xui_update)),
            patch(
                "xui_client.get_user_subscription_links",
                new=AsyncMock(return_value=["https://panel.example/sub/existing-sub-id"]),
            ),
            patch("subscription.record_payment_idempotent", new=AsyncMock()) as record_payment,
            patch("subscription.update_key_uuid", new=AsyncMock()),
            patch("subscription.log_analytics_event", new=AsyncMock()),
            patch("referral_system_new.process_payment_referral_bonus", new=AsyncMock(return_value=None)),
        ):
            result = await subscription.deliver_key(
                bot=bot,
                user_id=5283200584,
                chat_id=5283200584,
                config_name="Renewal",
                days=30,
                limit_ip=5,
                is_paid=True,
                amount=199,
                currency="RUB",
                method="yookassa",
                payload="test-payment-id",
            )

        self.assertTrue(result)
        record_payment.assert_awaited_once()
        bot.send_photo.assert_awaited_once()
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
