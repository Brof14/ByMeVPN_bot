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

        # 5. iOS guide content: HAPP first, V2Box second
        ios_text = _GUIDES["ios"]
        self.assertIn("Вариант 1: HAPP Proxy", ios_text)
        self.assertIn("Вариант 2: V2Box", ios_text)
        self.assertNotIn("Streisand", ios_text, "Streisand must be removed from iOS guide")
        self.assertIn("https://apps.apple.com/us/app/v2box-v2ray-client/id6446814690", ios_text)

        # 6. Linux Arch guide: nftables + v2rayN
        arch_text = _LINUX_GUIDES["arch"]
        self.assertIn("Arch / EndeavourOS / Manjaro", arch_text)
        self.assertIn("sudo pacman -S nftables", arch_text)
        self.assertIn("v2rayN-linux-64.zip", arch_text)
        self.assertIn("v2rayN", arch_text)

        # 7. Linux Ubuntu guide: .deb package
        ubuntu_text = _LINUX_GUIDES["ubuntu"]
        self.assertIn("Ubuntu / Debian / Linux Mint", ubuntu_text)
        self.assertIn("v2rayN-linux-64.deb", ubuntu_text)
        self.assertIn("sudo apt install", ubuntu_text)

        # 8. Linux Fedora guide: .rpm package
        fedora_text = _LINUX_GUIDES["fedora"]
        self.assertIn("Fedora / RHEL", fedora_text)
        self.assertIn("v2rayN-linux-rhel-64.rpm", fedora_text)
        self.assertIn("sudo dnf install", fedora_text)

        # 9. Other Linux guide: .zip package
        other_text = _LINUX_GUIDES["other"]
        self.assertIn("Другой Linux", other_text)
        self.assertIn("v2rayN-linux-64.zip", other_text)


if __name__ == "__main__":
    unittest.main()
