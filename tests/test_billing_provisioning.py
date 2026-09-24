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
    # Test 8: Pricing helper and single source of truth
    # ------------------------------------------------------------------------
    def test_08_pricing_and_device_limits(self):
        """
        Verify validate_device_limit and get_price_for_months match single source of truth.
        """
        self.assertEqual(validate_device_limit(1), 2)  # legacy 1 maps to base 2
        self.assertEqual(validate_device_limit(2), 2)
        self.assertEqual(validate_device_limit(5), 5)
        self.assertEqual(validate_device_limit(10), 10)
        self.assertEqual(validate_device_limit(99), 2)  # invalid maps to default 2

        # 2 devices base tier
        price_1m, days_1m = get_price_for_months(1, 2)
        self.assertEqual(price_1m, 89)
        self.assertEqual(days_1m, 30)

        # 5 devices optimal tier
        price_5d, days_5d = get_price_for_months(1, 5)
        self.assertEqual(price_5d, 129)

        # 10 devices family tier
        price_10d, days_10d = get_price_for_months(1, 10)
        self.assertEqual(price_10d, 179)


if __name__ == "__main__":
    unittest.main()
