"""
Comprehensive Regression Test Suite for ByMeVPN Billing, Idempotency & Provisioning.
Run with: python3 tests/test_billing_provisioning.py
Uses an isolated SQLite test database and mock 3x-ui / Bot interfaces.
"""
import os
import sys
import time
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in python path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Point DB_FILE to an isolated test DB before importing database
TEST_DB_PATH = "/tmp/test_billing_vpnbot.db"
os.environ["DB_FILE"] = TEST_DB_PATH

import database
import constants
from constants import (
    VALID_DEVICE_LIMITS, DEFAULT_DEVICE_LIMIT, DEVICE_CONFIG,
    get_price_for_months, get_monthly_display, validate_device_limit,
)


class TestBillingProvisioning(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Clean up old test db
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        for ext in ["-wal", "-shm"]:
            if os.path.exists(TEST_DB_PATH + ext):
                os.remove(TEST_DB_PATH + ext)

        database.DB_FILE = TEST_DB_PATH
        database._db = None
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        for ext in ["-wal", "-shm"]:
            if os.path.exists(TEST_DB_PATH + ext):
                os.remove(TEST_DB_PATH + ext)

    # ------------------------------------------------------------------------
    # Test 1: Duplicate Webhook x 10 test (Idempotency)
    # ------------------------------------------------------------------------
    async def test_01_duplicate_webhooks_idempotency(self):
        """
        Simulate 10 webhooks arriving for payment A.
        Must result in exactly:
        - 1 payment in DB
        - 1 subscription extension
        - 0 duplicate days
        """
        user_id = 999001
        await database.ensure_user(user_id)
        payment_id = "yk_test_pay_abc123"

        # Record payment concurrently 10 times
        async def send_webhook():
            return await database.record_payment_idempotent(
                user_id=user_id,
                amount=89,
                currency="RUB",
                method="yookassa",
                days=30,
                payload=payment_id,
                status="success",
                tariff="30 дней (2 устр.)",
                devices=2,
                provider="yookassa",
                provider_payment_id=payment_id,
            )

        results = await asyncio.gather(*[send_webhook() for _ in range(10)])

        # Exactly 1 must have is_new = True, 9 must have is_new = False
        new_count = sum(1 for is_new, _ in results if is_new)
        dup_count = sum(1 for is_new, _ in results if not is_new)

        self.assertEqual(new_count, 1, "Expected exactly 1 new payment recorded")
        self.assertEqual(dup_count, 9, "Expected exactly 9 duplicate detections")

        # Verify DB records
        payments = await database.get_user_payments(user_id)
        self.assertEqual(len(payments), 1, "DB should contain exactly 1 payment record")
        self.assertEqual(payments[0]["amount"], 89)

    # ------------------------------------------------------------------------
    # Test 2: Two real legitimate payments
    # ------------------------------------------------------------------------
    async def test_02_two_real_payments(self):
        """
        User performs payment A (+30 days) and payment B (+30 days).
        Result:
        - 2 payment records
        - key expiry = now + 60 days
        - single key (1 client)
        """
        user_id = 999002
        await database.ensure_user(user_id)

        # Create base key with 30 days
        key_id = await database.add_key(user_id, "https://sub.url/test2", "ByMeVPN_test", "uuid-test-02", 30, 2)
        initial_key = await database.get_key_by_id(key_id)
        initial_expiry = initial_key["expiry"]

        # Payment A (+30 days)
        is_new_a, pay_a = await database.record_payment_idempotent(
            user_id, 89, "RUB", "yookassa", 30, "pay_A", "success", "1 мес", 2, "yookassa", "pay_A"
        )
        await database.extend_key(key_id, 30)

        # Payment B (+30 days)
        is_new_b, pay_b = await database.record_payment_idempotent(
            user_id, 89, "RUB", "stars", 30, "pay_B", "success", "1 мес", 2, "stars", "pay_B"
        )
        await database.extend_key(key_id, 30)

        self.assertTrue(is_new_a)
        self.assertTrue(is_new_b)

        # Total payments in DB = 2
        user_payments = await database.get_user_payments(user_id)
        self.assertEqual(len(user_payments), 2)

        # Verify key expiry = initial + 60 days
        updated_key = await database.get_key_by_id(key_id)
        expected_expiry = initial_expiry + 60 * 86400
        self.assertEqual(updated_key["expiry"], expected_expiry, "Key expiry must be extended by 60 days total")

    # ------------------------------------------------------------------------
    # Test 3: Concurrent payments race condition test
    # ------------------------------------------------------------------------
    async def test_03_concurrent_payments_atomic_expiry(self):
        """
        Simulate payment A and payment B arriving at the exact same moment.
        Both call extend_key(key_id, 30) simultaneously.
        Must result in +60 days total, neither payment lost.
        """
        user_id = 999003
        await database.ensure_user(user_id)

        now = int(time.time())
        key_id = await database.add_key(user_id, "https://sub.url/test3", "test3", "uuid-test-03", 30, 2)
        key_before = await database.get_key_by_id(key_id)
        start_expiry = key_before["expiry"]

        # Run 2 extensions concurrently
        async def do_extend(days):
            return await database.extend_key(key_id, days)

        res = await asyncio.gather(do_extend(30), do_extend(30))
        self.assertTrue(all(res))

        key_after = await database.get_key_by_id(key_id)
        self.assertEqual(key_after["expiry"], start_expiry + 60 * 86400, "Concurrent updates must atomically add to +60 days")

    # ------------------------------------------------------------------------
    # Test 4: Renewal and device tier upgrade (2 -> 5 -> 10 devices)
    # ------------------------------------------------------------------------
    async def test_04_device_tier_upgrade(self):
        """
        User starts with 2 devices (base tier).
        Renews with 5 devices tier.
        Then renews with 10 devices tier.
        Device limit and expiry must update correctly.
        """
        user_id = 999004
        await database.ensure_user(user_id)

        # Start with 2 devices
        key_id = await database.add_key(user_id, "https://sub.url/test4", "test4", "uuid-test-04", 30, 2)
        key = await database.get_key_by_id(key_id)
        self.assertEqual(key["limit_ip"], 2)

        # Upgrade to 5 devices with 30 days extension
        await database.extend_key(key_id, 30, limit_ip=5)
        key = await database.get_key_by_id(key_id)
        self.assertEqual(key["limit_ip"], 5, "Device limit must update to 5")

        # Upgrade to 10 devices with 30 days extension
        await database.extend_key(key_id, 30, limit_ip=10)
        key = await database.get_key_by_id(key_id)
        self.assertEqual(key["limit_ip"], 10, "Device limit must update to 10")
        self.assertEqual(key["uuid"], "uuid-test-04", "UUID must remain unchanged")

    # ------------------------------------------------------------------------
    # Test 5: Zero-delete client UUID preservation
    # ------------------------------------------------------------------------
    async def test_05_zero_delete_uuid_preservation(self):
        """
        Verify create_xui_user checks existing client and preserves UUID.
        """
        from xui_client import create_xui_user, update_xui_user

        user_id = 999005
        existing_uuid = "existing-uuid-fixed-12345"

        # Mock py3xui API
        mock_api = MagicMock()
        mock_client = MagicMock()
        mock_client.uuid = existing_uuid
        mock_client.sub_id = "existingsubid123"
        mock_client.limit_ip = 2
        mock_client.expiry_time = int(time.time() * 1000)

        with patch("xui_client._get_api", new_callable=AsyncMock) as mock_get_api, \
             patch("xui_client._find_client_uuid", new_callable=AsyncMock) as mock_find_uuid, \
             patch("xui_client._find_client_sub_id", new_callable=AsyncMock) as mock_find_sub_id, \
             patch("xui_client._api_call_with_retry", new_callable=AsyncMock) as mock_call:

            mock_get_api.return_value = mock_api
            mock_find_uuid.return_value = existing_uuid
            mock_find_sub_id.return_value = "existingsubid123"
            mock_call.return_value = mock_client

            # Calling create_xui_user for existing client
            res = await create_xui_user(user_id, days=30, limit_ip=5)

            self.assertIsNotNone(res)
            self.assertEqual(res.get("uuid"), existing_uuid, "Existing client UUID must be strictly preserved!")
            # Ensure delete was NEVER called
            mock_api.client.delete.assert_not_called()

    # ------------------------------------------------------------------------
    # Test 6: Atomic trial claiming test
    # ------------------------------------------------------------------------
    async def test_06_atomic_trial_claim(self):
        """
        Simulate 10 concurrent requests to claim free trial for same user.
        Must result in exactly 1 success, 9 rejections.
        """
        user_id = 999006
        await database.ensure_user(user_id)

        # Verify trial is initially available
        self.assertFalse(await database.has_trial_used(user_id))

        # 10 concurrent attempts
        claims = await asyncio.gather(*[database.try_claim_trial(user_id) for _ in range(10)])

        success_count = sum(1 for c in claims if c is True)
        fail_count = sum(1 for c in claims if c is False)

        self.assertEqual(success_count, 1, "Exactly 1 claim attempt must succeed")
        self.assertEqual(fail_count, 9, "9 concurrent claim attempts must be rejected")
        self.assertTrue(await database.has_trial_used(user_id), "Trial must now be marked used")

    # ------------------------------------------------------------------------
    # Test 7: Promo code non-burning validation
    # ------------------------------------------------------------------------
    async def test_07_promo_code_lifecycle(self):
        """
        Promo code validation does NOT mark it as used.
        Only use_promo_code upon successful payment marks it used.
        """
        user_id = 999007
        code = "TEST20"
        await database.create_promo_code(code, promo_type="percent", discount_value=20, max_uses=5, valid_days=30)

        # 1. Validation check
        valid = await database.validate_promo_code(code)
        self.assertIsNotNone(valid)
        self.assertEqual(valid["discount_value"], 20)

        # User has NOT used it yet
        self.assertFalse(await database.has_user_used_promo(code, user_id))

        # 2. Simulate entering promo multiple times without buying
        for _ in range(3):
            self.assertFalse(await database.has_user_used_promo(code, user_id))

        # 3. Simulate payment completion -> burns promo
        used = await database.use_promo_code(code, user_id)
        self.assertTrue(used, "Promo code should be successfully consumed on purchase")

        # User has now used it
        self.assertTrue(await database.has_user_used_promo(code, user_id))

        # 4. Attempting to use again must fail
        used_again = await database.use_promo_code(code, user_id)
        self.assertFalse(used_again, "Cannot reuse single-use promo code")

    # ------------------------------------------------------------------------
    # Test 8: Pricing helper and single source of truth (All 12 combinations)
    # ------------------------------------------------------------------------
    def test_08_pricing_and_device_limits(self):
        """
        Verify validate_device_limit, get_monthly_display, and get_price_for_months
        across all 12 combinations of (devices x months).
        """
        self.assertEqual(validate_device_limit(1), 2)  # legacy 1 maps to base 2
        self.assertEqual(validate_device_limit(2), 2)
        self.assertEqual(validate_device_limit(5), 5)
        self.assertEqual(validate_device_limit(10), 10)
        self.assertEqual(validate_device_limit(99), 2)  # invalid maps to default 2

        # Expected table:
        # monthly_prices:
        #             1 мес   3 мес   6 мес   12 мес
        # 2 devices      89      79      69       59
        # 5 devices     109      99      89       79
        # 10 devices    129     119     109       99
        #
        # total_prices:
        #             1 мес   3 мес   6 мес   12 мес
        # 2 devices      89     237     414      708
        # 5 devices     109     297     534      948
        # 10 devices    129     357     654     1188

        expected_matrix = {
            2: {
                1:  {"monthly": 89,  "total": 89,   "days": 30},
                3:  {"monthly": 79,  "total": 237,  "days": 120},
                6:  {"monthly": 69,  "total": 414,  "days": 240},
                12: {"monthly": 59,  "total": 708,  "days": 450},
            },
            5: {
                1:  {"monthly": 109, "total": 109,  "days": 30},
                3:  {"monthly": 99,  "total": 297,  "days": 120},
                6:  {"monthly": 89,  "total": 534,  "days": 240},
                12: {"monthly": 79,  "total": 948,  "days": 450},
            },
            10: {
                1:  {"monthly": 129, "total": 129,  "days": 30},
                3:  {"monthly": 119, "total": 357,  "days": 120},
                6:  {"monthly": 109, "total": 654,  "days": 240},
                12: {"monthly": 99,  "total": 1188, "days": 450},
            },
        }

        for dev, months_dict in expected_matrix.items():
            for months, exp in months_dict.items():
                monthly = get_monthly_display(months, dev)
                total, days = get_price_for_months(months, dev)

                self.assertEqual(
                    monthly, exp["monthly"],
                    f"Mismatch in monthly display price for dev={dev}, months={months}: expected {exp['monthly']}, got {monthly}"
                )
                self.assertEqual(
                    total, exp["total"],
                    f"Mismatch in total price for dev={dev}, months={months}: expected {exp['total']}, got {total}"
                )
                self.assertEqual(
                    days, exp["days"],
                    f"Mismatch in days for dev={dev}, months={months}: expected {exp['days']}, got {days}"
                )

    # ------------------------------------------------------------------------
    # Test 9: Global Error Handler & HTML escaping & message splitting
    # ------------------------------------------------------------------------
    async def test_09_global_error_handler(self):
        """
        Verify global error handler extracts exception details,
        safely HTML-escapes special characters (<test> & "quotes"),
        and splits long tracebacks without losing the end cause.
        """
        from main import format_error_messages, error_handler
        from aiogram.types import ErrorEvent

        # 1. Test formatting with special HTML characters
        test_exc = ValueError("Test <error> & \"quotes\" failure")
        try:
            raise test_exc
        except ValueError as caught_exc:
            messages = format_error_messages(caught_exc, update=None)

        self.assertGreaterEqual(len(messages), 1)
        first_msg = messages[0]

        # Verify HTML escaping
        self.assertIn("&lt;error&gt;", first_msg)
        self.assertIn("&amp;", first_msg)
        self.assertIn("&quot;quotes&quot;", first_msg)
        self.assertIn("ValueError", first_msg)
        self.assertIn("🚨 <b>Bot Error</b>", first_msg)

        # 2. Test error_handler with mock Bot
        mock_bot = AsyncMock()
        mock_event = MagicMock(spec=ErrorEvent)
        mock_event.exception = test_exc
        mock_event.update = None

        with patch("main.ADMIN_ID", 123456789):
            await error_handler(mock_event, mock_bot)
            mock_bot.send_message.assert_called()
            call_args = mock_bot.send_message.call_args[0]
            admin_id = call_args[0]
            text_sent = call_args[1]
            self.assertEqual(admin_id, 123456789)
            self.assertIn("&lt;error&gt;", text_sent)
            self.assertIn("ValueError", text_sent)

        # 3. Test long traceback message splitting
        long_exc = RuntimeError("Long traceback test " + "X" * 4500)
        try:
            raise long_exc
        except RuntimeError as caught_long:
            split_messages = format_error_messages(caught_long, update=None)

        self.assertGreater(len(split_messages), 1, "Deep traceback should be split across multiple messages")
        # Check that header is in first message
        self.assertIn("🚨 <b>Bot Error</b>", split_messages[0])
        # Check that traceback chunks contain the full exception cause and end
        combined_tb = "".join(split_messages[1:])
        self.assertIn("RuntimeError", combined_tb)
        self.assertIn("Long traceback test", combined_tb)
        self.assertTrue(split_messages[-1].endswith("</code></pre>"))
        # Check that all messages respect Telegram length limits
        for msg in split_messages:
            self.assertLessEqual(len(msg), 4000, f"Message length {len(msg)} exceeds Telegram limit")

    # ------------------------------------------------------------------------
    # Test 10: Guide navigation and content verification
    # ------------------------------------------------------------------------
    def test_10_guide_navigation_and_content(self):
        """
        Verify Linux distro selection keyboard, back buttons,
        and platform guide texts (iOS HAPP + V2Box, Linux v2rayN distros).
        """
        from keyboards import (
            connection_guide_kb, linux_distro_kb,
            linux_guide_back_kb, guide_back_kb,
        )
        from handlers.guide import _GUIDES, _LINUX_GUIDES

        # 1. Connection guide keyboard has Linux button
        conn_kb = connection_guide_kb()
        conn_cbs = [btn.callback_data for row in conn_kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("guide_linux", conn_cbs)
        self.assertIn("guide_ios", conn_cbs)
        self.assertIn("back_to_menu", conn_cbs)

        # 2. Linux distro keyboard has 4 distros and back to connection_guide
        linux_kb = linux_distro_kb()
        linux_cbs = [btn.callback_data for row in linux_kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("guide_linux_ubuntu", linux_cbs)
        self.assertIn("guide_linux_arch", linux_cbs)
        self.assertIn("guide_linux_fedora", linux_cbs)
        self.assertIn("guide_linux_other", linux_cbs)
        self.assertIn("connection_guide", linux_cbs, "Back button from Linux distro menu must go to connection_guide")

        # 3. Linux distro back keyboard must go back to guide_linux
        distro_back = linux_guide_back_kb()
        distro_back_cbs = [btn.callback_data for row in distro_back.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("guide_linux", distro_back_cbs, "Back button from specific distro must go to guide_linux")

        # 4. Guide back keyboard must go back to connection_guide
        gen_back = guide_back_kb()
        gen_back_cbs = [btn.callback_data for row in gen_back.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn("connection_guide", gen_back_cbs, "Back button from general guide must go to connection_guide")

        # 5. iOS guide content: INCY (current client app)
        ios_text = _GUIDES["ios"]
        self.assertIn("INCY", ios_text)
        self.assertIn("https://apps.apple.com/ru/app/incy/id6756943388", ios_text)
        self.assertNotIn("Streisand", ios_text, "Streisand must be removed from iOS guide")
        self.assertNotIn("HAPP Proxy", ios_text, "iOS guide must not recommend HAPP (INCY is the current app)")

        # 6. Linux Arch guide: v2rayN (simplified guide, no nftables deep-dive)
        arch_text = _LINUX_GUIDES["arch"]
        self.assertIn("Arch / EndeavourOS / Manjaro", arch_text)
        self.assertIn("v2rayN-linux-64.zip", arch_text)
        self.assertIn("v2rayN", arch_text)

        # 7. Linux Ubuntu guide: v2rayN
        ubuntu_text = _LINUX_GUIDES["ubuntu"]
        self.assertIn("Ubuntu", ubuntu_text)
        self.assertIn("v2rayN", ubuntu_text)

        # 8. Linux Fedora guide: v2rayN
        fedora_text = _LINUX_GUIDES["fedora"]
        self.assertIn("Fedora", fedora_text)
        self.assertIn("v2rayN", fedora_text)

        # 9. Other Linux guide: v2rayN
        other_text = _LINUX_GUIDES["other"]
        self.assertIn("Другой Linux", other_text)
        self.assertIn("v2rayN", other_text)

    # ------------------------------------------------------------------------
    # Test 11: Device switching edits caption and preserves photo (no deletion)
    # ------------------------------------------------------------------------
    def test_11_device_switching_preserves_photo_and_edits_caption(self):
        """
        Verify that send_or_edit and send_with_photo on a photo message edit caption
        in-place rather than deleting the message or sending a new text message.
        """
        from utils import send_or_edit, send_with_photo
        from unittest.mock import AsyncMock, MagicMock
        from aiogram.types import CallbackQuery, Message, PhotoSize

        async def run_test():
            bot = MagicMock()
            bot.edit_message_caption = AsyncMock()
            bot.edit_message_text = AsyncMock()
            bot.delete_message = AsyncMock()
            bot.send_message = AsyncMock()
            bot.send_photo = AsyncMock()

            # Create mock message with a photo
            msg = MagicMock(spec=Message)
            msg.chat = MagicMock(id=12345)
            msg.message_id = 999
            msg.photo = [MagicMock(spec=PhotoSize)]  # Has photo!

            callback = MagicMock(spec=CallbackQuery)
            callback.message = msg

            # 1. Test send_or_edit on a photo message
            await send_or_edit(bot, callback, "Updated caption 1")
            bot.edit_message_caption.assert_called_once_with(
                chat_id=12345,
                message_id=999,
                caption="Updated caption 1",
                parse_mode="HTML",
                reply_markup=None,
            )
            bot.delete_message.assert_not_called()
            bot.send_message.assert_not_called()

            # 2. Test send_with_photo on a photo message
            bot.edit_message_caption.reset_mock()
            bot.delete_message.reset_mock()
            await send_with_photo(bot, callback, "Updated caption 2")
            bot.edit_message_caption.assert_called_once_with(
                chat_id=12345,
                message_id=999,
                caption="Updated caption 2",
                parse_mode="HTML",
                reply_markup=None,
            )
            bot.delete_message.assert_not_called()
            bot.send_photo.assert_not_called()

        asyncio.run(run_test())

    # ------------------------------------------------------------------------
    # Test 12: Auto-renew database lifecycle, status toggling, and expiry worker
    # ------------------------------------------------------------------------
    def test_12_autorenew_database_lifecycle_and_recurrent_charge(self):
        """
        Verify auto_renew_subscriptions creation, status toggling,
        failure backoff, and get_expiring_auto_renew_subscriptions logic.
        """
        from database import (
            init_db, ensure_user, add_key,
            save_auto_renew_subscription, get_auto_renew_subscription,
            set_auto_renew_status, set_auto_renew_status_by_key,
            update_auto_renew_charge_success, update_auto_renew_charge_failure,
            get_expiring_auto_renew_subscriptions,
        )
        import time

        async def run_test():
            await init_db()
            user_id = 888123
            await ensure_user(user_id)

            now = int(time.time())
            # Add key expiring in the past (expired 1 hour ago)
            key_id = await add_key(user_id, "vless://test", "test_key", "uuid-123", 30, 2)
            # Manually set key expiry to now - 3600 (expired 1h ago)
            from database import get_db
            db = await get_db()
            await db.execute("UPDATE keys SET expiry = ? WHERE id = ?", (now - 3600, key_id))
            await db.commit()

            # 1. Save auto-renew subscription
            sub_id = await save_auto_renew_subscription(
                user_id=user_id,
                key_id=key_id,
                payment_method_id="pm_test_card_123",
                payment_method_title="*4444",
                payment_method_type="bank_card",
                months=1,
                days=30,
                devices=2,
                amount_rub=89,
            )
            self.assertGreater(sub_id, 0)

            # 2. Get auto-renew subscription
            sub = await get_auto_renew_subscription(user_id, key_id)
            self.assertIsNotNone(sub)
            self.assertEqual(sub["status"], "active")
            self.assertEqual(sub["payment_method_id"], "pm_test_card_123")
            self.assertEqual(sub["amount_rub"], 89)

            # 3. Check expiring subscriptions
            expiring = await get_expiring_auto_renew_subscriptions(now=now)
            exp_sub_ids = [s["sub_id"] for s in expiring]
            self.assertIn(sub_id, exp_sub_ids, "Expired key with active auto-renew must be returned")

            # 4. Toggle status to cancelled
            await set_auto_renew_status_by_key(user_id, key_id, "cancelled")
            sub_cancelled = await get_auto_renew_subscription(user_id, key_id)
            self.assertEqual(sub_cancelled["status"], "cancelled")

            # Must NOT be returned when cancelled
            expiring = await get_expiring_auto_renew_subscriptions(now=now)
            self.assertNotIn(sub_id, [s["sub_id"] for s in expiring])

            # Re-activate
            await set_auto_renew_status(sub_id, "active")

            # 5. Simulate charge success
            await update_auto_renew_charge_success(sub_id, new_days=30, new_amount=89)
            # Also extend key into the future
            await db.execute("UPDATE keys SET expiry = ? WHERE id = ?", (now + 30 * 86400, key_id))
            await db.commit()

            # Since key is now in the future, it must NOT be returned
            expiring = await get_expiring_auto_renew_subscriptions(now=now)
            self.assertNotIn(sub_id, [s["sub_id"] for s in expiring])

            # 6. Test failure increments and disabling
            # Set key back to expired
            await db.execute("UPDATE keys SET expiry = ? WHERE id = ?", (now - 100, key_id))
            await db.execute("UPDATE auto_renew_subscriptions SET last_charge_at = NULL, next_retry_at = NULL WHERE id = ?", (sub_id,))
            await db.commit()

            # 1st fail
            is_dis = await update_auto_renew_charge_failure(sub_id, "funds error", max_fails=3)
            self.assertFalse(is_dis)
            # 2nd fail
            is_dis = await update_auto_renew_charge_failure(sub_id, "funds error", max_fails=3)
            self.assertFalse(is_dis)
            # 3rd fail -> disabled
            is_dis = await update_auto_renew_charge_failure(sub_id, "funds error", max_fails=3)
            self.assertTrue(is_dis)

            sub_failed = await get_auto_renew_subscription(user_id, key_id)
            self.assertEqual(sub_failed["status"], "failed")
            self.assertEqual(sub_failed["fail_count"], 3)

        asyncio.run(run_test())

    # ------------------------------------------------------------------------
    # Test 13: Auto-renew cancellation preserves paid access (spec #8)
    # ------------------------------------------------------------------------
    def test_13_cancel_autorenew_preserves_paid_access(self):
        """
        Verify that cancelling auto-renew (card management screen):
        1. Marks auto_renew_subscriptions cancelled.
        2. Does NOT touch the active key expiry — user keeps paid access.
        3. Does NOT disable the 3x-ui client.
        4. Subscription is no longer eligible for recurring charges.
        5. re-enabling restores the active status with the saved card.
        """
        from database import (
            init_db, ensure_user, add_key, get_key_by_id,
            save_auto_renew_subscription, get_auto_renew_subscription,
            cancel_auto_renew, resume_auto_renew,
            get_expiring_auto_renew_subscriptions,
        )
        import time

        async def run_test():
            await init_db()
            user_id = 777321
            await ensure_user(user_id)

            # User has an active key with 30 days left
            key_id = await add_key(user_id, "vless://test_unbind", "Active Key", "uuid-unbind-123", 30, 2)
            sub_id = await save_auto_renew_subscription(
                user_id=user_id,
                key_id=key_id,
                payment_method_id="pm_saved_card_999",
                payment_method_title="*9999",
                payment_method_type="bank_card",
                months=1,
                days=30,
                devices=2,
                amount_rub=89,
            )

            expiry_before = (await get_key_by_id(key_id))["expiry"]
            self.assertGreater(expiry_before, int(time.time()))

            # Cancel auto-renew — access MUST be preserved
            res = await cancel_auto_renew(user_id, key_id)
            self.assertTrue(res["cancelled"])
            self.assertEqual(res["expires_at"], expiry_before)

            # Key untouched
            key_after = await get_key_by_id(key_id)
            self.assertEqual(key_after["expiry"], expiry_before)

            # Card data preserved (for re-enable), status cancelled
            sub_after = await get_auto_renew_subscription(user_id, key_id)
            self.assertEqual(sub_after["status"], "cancelled")
            self.assertEqual(sub_after["payment_method_id"], "pm_saved_card_999")

            # No longer eligible for recurring charges
            expiring = await get_expiring_auto_renew_subscriptions()
            self.assertNotIn(sub_id, [s["sub_id"] for s in expiring])

            # Re-enable restores active status
            reenabled = await resume_auto_renew(user_id, key_id)
            self.assertTrue(reenabled)
            sub_re = await get_auto_renew_subscription(user_id, key_id)
            self.assertEqual(sub_re["status"], "active")
            self.assertEqual(sub_re["payment_method_id"], "pm_saved_card_999")

        asyncio.run(run_test())


    # ------------------------------------------------------------------------
    # Test 14: Intro trial (1 ₽) webhook flow → trial_used, ARS 89₽/30d, no referral
    # ------------------------------------------------------------------------
    def test_14_intro_trial_webhook_flow(self):
        """
        A succeeded 1 ₽ trial payment must:
        1. Deliver a 3-day key (days from metadata).
        2. Set users.trial_used and clear the pending trial checkout.
        3. Save auto-renew renewal terms 89 ₽ / 30 days (NOT 1 ₽ / 3 days).
        4. NOT trigger referral rewards (trial is not a paid conversion).
        5. Mark the payment processed and idempotently recorded.
        """
        import webhook
        from database import (
            init_db, ensure_user, has_trial_used, get_auto_renew_subscription,
            is_yookassa_processed, get_yookassa_trial_pending,
        )

        async def run_test():
            await init_db()
            user_id = 887001
            await ensure_user(user_id)

            trial_payment = {
                "id": "yk_trial_payment_1",
                "status": "succeeded",
                "amount": {"value": "1.00", "currency": "RUB"},
                "payment_method": {"saved": True, "id": "pm_trial_1", "title": "*4242", "type": "bank_card"},
                "metadata": {"user_id": str(user_id), "days": "3", "devices": "2", "months": "1", "trial": "1"},
            }

            with patch.object(webhook, "_fetch_yookassa_payment", new_callable=AsyncMock, return_value=trial_payment), \
                 patch.object(webhook, "deliver_key", new_callable=AsyncMock, return_value=True) as mock_deliver, \
                 patch.object(webhook, "add_referral_earning", new_callable=AsyncMock, return_value=True) as mock_referral:
                bot = MagicMock()
                bot.send_message = AsyncMock()
                await webhook._process_payment(bot, "yk_trial_payment_1")

                mock_deliver.assert_awaited_once()
                kwargs = mock_deliver.await_args.kwargs
                self.assertEqual(kwargs["days"], 3)
                self.assertEqual(kwargs["amount"], 1)
                self.assertTrue(kwargs["skip_referral_bonus"])

                # Referral reward must be skipped for the 1 ₽ trial
                mock_referral.assert_not_awaited()

            self.assertTrue(await has_trial_used(user_id))
            self.assertIsNone(await get_yookassa_trial_pending(user_id))
            self.assertTrue(await is_yookassa_processed("yk_trial_payment_1"))

            ars = await get_auto_renew_subscription(user_id)
            self.assertIsNotNone(ars)
            self.assertEqual(ars["amount_rub"], constants.RECURRING_MONTHLY_PRICE)  # 89, not 1
            self.assertEqual(ars["days"], constants.RECURRING_DAYS)                # 30, not 3
            self.assertEqual(ars["payment_method_id"], "pm_trial_1")
            self.assertEqual(ars["status"], "active")

        asyncio.run(run_test())

    # ------------------------------------------------------------------------
    # Test 15: Duplicate webhook for the same payment is a no-op
    # ------------------------------------------------------------------------
    def test_15_duplicate_webhook_no_double_delivery(self):
        import webhook
        from database import init_db, ensure_user, get_user_keys

        async def run_test():
            await init_db()
            user_id = 887002
            await ensure_user(user_id)

            payment = {
                "id": "yk_dup_payment_1",
                "status": "succeeded",
                "amount": {"value": "89.00", "currency": "RUB"},
                "payment_method": {"saved": False},
                "metadata": {"user_id": str(user_id), "days": "30", "devices": "2", "months": "1"},
            }

            with patch.object(webhook, "_fetch_yookassa_payment", new_callable=AsyncMock, return_value=payment), \
                 patch.object(webhook, "deliver_key", new_callable=AsyncMock, return_value=True) as mock_deliver:
                bot = MagicMock()
                bot.send_message = AsyncMock()
                await webhook._process_payment(bot, "yk_dup_payment_1")
                await webhook._process_payment(bot, "yk_dup_payment_1")
                await webhook._process_payment(bot, "yk_dup_payment_1")

                # Deliver exactly once despite three webhook invocations
                mock_deliver.assert_awaited_once()

            keys = await get_user_keys(user_id)
            self.assertEqual(len(keys), 0)  # deliver_key was mocked; nothing extra created

        asyncio.run(run_test())

    # ------------------------------------------------------------------------
    # Test 16: Referral earning is idempotent and credits balance once
    # ------------------------------------------------------------------------
    def test_16_referral_earning_idempotent(self):
        from database import (
            init_db, ensure_user, set_referrer, add_referral_earning,
            get_referral_balance, get_referral_stats_detailed,
        )

        async def run_test():
            await init_db()
            referrer, referred = 887003, 887004
            await ensure_user(referrer)
            await ensure_user(referred)
            await set_referrer(referred, referrer)

            first = await add_referral_earning(referrer, referred, 50, payment_id=1)
            self.assertTrue(first)
            second = await add_referral_earning(referrer, referred, 50, payment_id=1)
            self.assertFalse(second)

            balance = await get_referral_balance(referrer)
            self.assertEqual(balance["balance"], 50)
            self.assertEqual(balance["total_earned"], 50)

            # Detailed stats query must not crash (previously: no r.source column)
            stats = await get_referral_stats_detailed()
            self.assertEqual(len(stats), 1)
            self.assertEqual(stats[0]["bonus_amount"], 50)

        asyncio.run(run_test())

    # ------------------------------------------------------------------------
    # Test 17: Autorenew worker uses a deterministic business idempotence key
    # ------------------------------------------------------------------------
    def test_17_autorenew_charge_idempotence_key_is_deterministic(self):
        """
        The same billing period must always produce the same YooKassa
        Idempotence-Key (worker restart must not double-charge).
        """
        import autorenew
        from database import (
            init_db, ensure_user, add_key, save_auto_renew_subscription,
            get_expiring_auto_renew_subscriptions,
        )

        async def run_test():
            await init_db()
            user_id = 887005
            await ensure_user(user_id)
            # Key already expired (inside 7-day grace window)
            key_id = await add_key(user_id, "vless://ars", "ARS Key", "uuid-ars", 2, 2)
            import time as _t
            past_expiry = int(_t.time()) - 3600
            db = await database.get_db()
            await db.execute("UPDATE keys SET expiry = ? WHERE id = ?", (past_expiry, key_id))
            await db.commit()
            await save_auto_renew_subscription(
                user_id=user_id, key_id=key_id,
                payment_method_id="pm_ars_1", payment_method_title="*1111",
                payment_method_type="bank_card", months=1, days=30, devices=2, amount_rub=89,
            )

            expiring = await get_expiring_auto_renew_subscriptions()
            matching = [s for s in expiring if s["sub_id"]]
            self.assertTrue(matching)
            sub = matching[0]

            expected_key = f"autorenew_{sub['sub_id']}_{sub['key_id'] or 0}_{sub['expiry']}_{sub['fail_count']}"

            captured = {}

            class FakeResp:
                status_code = 200
                def raise_for_status(self): pass
                def json(self):
                    return {"id": "yk_recur_1", "status": "succeeded"}

            class FakeClient:
                def __init__(self, *a, **k): pass
                async def __aenter__(self): return self
                async def __aexit__(self, *a): return False
                async def post(self, url, json=None, headers=None):
                    captured["headers"] = headers
                    return FakeResp()

            with patch("autorenew.charge_yookassa_recurrent", new_callable=AsyncMock) as mock_charge:
                mock_charge.return_value = {"id": "yk_recur_1", "status": "canceled", "cancellation_details": {"reason": "insufficient_funds"}}
                bot = MagicMock()
                bot.send_message = AsyncMock()
                await autorenew.process_auto_renewals(bot)
                mock_charge.assert_awaited_once()
                # The key passed to the charge must match the deterministic formula
                self.assertEqual(mock_charge.await_args.kwargs.get("idempotence_key"), expected_key)

            # Direct API-level check: same key passed through to HTTP headers
            from payments import charge_yookassa_recurrent
            with patch("payments.httpx.AsyncClient", FakeClient):
                await charge_yookassa_recurrent(
                    amount_rub=89, description="test", user_id=1, days=30,
                    devices=2, months=1, key_id=5, payment_method_id="pm_x",
                    idempotence_key=expected_key,
                )
                self.assertEqual(captured["headers"]["Idempotence-Key"], expected_key)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
