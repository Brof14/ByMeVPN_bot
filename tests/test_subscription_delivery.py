"""Regression tests for subscription delivery failure paths."""
import os
import sys
import time
import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Keep imports isolated from the production database.
os.environ.setdefault("DB_FILE", "/tmp/test_subscription_delivery.db")

import subscription


class TestSubscriptionDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_retried_signup_reward_does_not_shorten_newer_subscription(self):
        expiry = int(time.time()) + 90 * 86400
        key = {"id": 7, "key": "https://example.test/sub", "expiry": expiry, "limit_ip": 2}
        with (
            patch("subscription.get_user_keys", new=AsyncMock(return_value=[key])),
            patch("subscription.database.get_or_create_provisioning_intent", new=AsyncMock(return_value=expiry - 30 * 86400)),
            patch("subscription.set_key_expiry", new=AsyncMock()) as set_expiry,
        ):
            result = await subscription.deliver_key(
                bot=MagicMock(), user_id=123, chat_id=123, config_name="ByMeVPN_123",
                days=15, is_paid=True, amount=0, method="ref_signup_bonus", payload="ref_signup_123_456",
            )
        self.assertFalse(result)
        set_expiry.assert_not_awaited()

    async def test_retried_referral_reward_does_not_shorten_panel_only_subscription(self):
        expiry = int(time.time()) + 90 * 86400
        panel_client = {"expiry_time": expiry * 1000, "subscription_url": "https://example.test/sub", "uuid": "uuid"}
        with (
            patch("subscription.get_user_keys", new=AsyncMock(return_value=[])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=panel_client)),
            patch("subscription.database.get_or_create_provisioning_intent", new=AsyncMock(return_value=expiry - 30 * 86400)),
            patch("xui_client.update_xui_user", new=AsyncMock()) as update_panel,
            patch("subscription.add_key", new=AsyncMock()) as add_key,
        ):
            result = await subscription.deliver_key(
                bot=MagicMock(), user_id=123, chat_id=123, config_name="ByMeVPN_123",
                days=30, is_paid=True, amount=0, method="ref_bonus", payload="ref_bonus_123_456",
            )
        self.assertFalse(result)
        update_panel.assert_not_awaited()
        add_key.assert_not_awaited()

    async def test_retried_referral_reward_does_not_shorten_newer_subscription(self):
        existing_expiry = int(time.time()) + 90 * 86400
        key = {"id": 7, "key": "https://example.test/sub", "expiry": existing_expiry, "limit_ip": 2}
        with (
            patch("subscription.get_user_keys", new=AsyncMock(return_value=[key])),
            patch("subscription.database.get_or_create_provisioning_intent", new=AsyncMock(return_value=existing_expiry - 30 * 86400)),
            patch("subscription.set_key_expiry", new=AsyncMock()) as set_expiry,
            patch("xui_client.update_xui_user", new=AsyncMock()) as update_panel,
            patch("xui_client.get_user_subscription_links", new=AsyncMock(return_value=[])),
            patch("subscription._send_delivery_message", new=AsyncMock(return_value=True)),
        ):
            result = await subscription.deliver_key(
                bot=MagicMock(), user_id=123, chat_id=123, config_name="ByMeVPN_123",
                days=30, is_paid=True, amount=0, method="ref_bonus", payload="ref_bonus_123_456",
            )
        self.assertFalse(result)
        set_expiry.assert_not_awaited()
        update_panel.assert_not_awaited()

    async def test_existing_panel_client_is_not_imported_when_update_fails(self):
        """Never claim delivery or persist a key from a stale panel snapshot."""
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()
        panel_client = {
            "subscription_url": "https://panel.example/sub/existing-sub-id",
            "uuid": "existing-uuid",
        }

        with (
            patch("subscription.get_user_keys", new=AsyncMock(return_value=[])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=panel_client)),
            patch("xui_client.update_xui_user", new=AsyncMock(return_value=None)),
            patch("subscription.database.get_or_create_provisioning_intent", new=AsyncMock(return_value=int(time.time()) + 30 * 86400)),
            patch("subscription.add_key", new=AsyncMock()) as add_key,
            patch("subscription.log_key_error", new=AsyncMock()) as log_error,
        ):
            result = await subscription.deliver_key(
                bot=bot,
                user_id=5283200584,
                chat_id=5283200584,
                config_name="Recovered-Key",
                days=30,
                limit_ip=5,
                is_paid=True,
                amount=199,
                method="yookassa",
                payload="payment-recovery-test",
            )

        self.assertFalse(result)
        add_key.assert_not_awaited()
        log_error.assert_awaited_once()
        bot.send_photo.assert_not_awaited()

    async def test_imported_key_uses_authoritative_xui_expiry(self):
        """SQLite must mirror the extended expiry returned by 3x-ui."""
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()
        panel_client = {
            "subscription_url": "https://panel.example/sub/existing-sub-id",
            "uuid": "existing-uuid",
        }
        expected_expiry = int(time.time()) + 120 * 86400
        xui_result = {
            "subscription_url": "https://panel.example/sub/existing-sub-id",
            "uuid": "existing-uuid",
            "expiry_time": expected_expiry * 1000,
        }

        with (
            patch("subscription.get_user_keys", new=AsyncMock(return_value=[])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=panel_client)),
            patch("xui_client.update_xui_user", new=AsyncMock(return_value=xui_result)),
            patch("subscription.add_key", new=AsyncMock(return_value=43)) as add_key,
            patch("subscription.record_payment_idempotent", new=AsyncMock()),
            patch("subscription.log_analytics_event", new=AsyncMock()),
        ):
            result = await subscription.deliver_key(
                bot=bot,
                user_id=5283200584,
                chat_id=5283200584,
                config_name="Recovered-Key",
                days=30,
                limit_ip=5,
                is_paid=False,
                method="admin_grant",
            )

        self.assertTrue(result)
        self.assertEqual(add_key.await_args.kwargs["expiry"], expected_expiry)

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
            patch("subscription.create_xui_user", new=AsyncMock(return_value=xui_result)) as create_user,
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
        self.assertEqual(create_user.await_args.kwargs["limit_ip"], 0)
        self.assertEqual(create_user.await_args.kwargs["limit_hwid"], 5)
        add_key.assert_awaited_once_with(
            5283200584,
            "https://panel.example/sub/test-sub-id",
            "Admin-Key",
            "test-client-uuid",
            30,
            5,
            expiry=ANY,
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
            patch("subscription.set_key_expiry", new=AsyncMock(return_value=True)) as set_expiry,
            patch("subscription.database.get_or_create_provisioning_intent", new=AsyncMock(return_value=xui_update["expiry_time"] // 1000)),
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
        set_expiry.assert_awaited_once_with(7, xui_update["expiry_time"] // 1000, limit_ip=5)
        record_payment.assert_awaited_once()
        bot.send_photo.assert_awaited_once()
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
