"""Protect recurring charges from duplicate debits after provider timeouts."""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DB_FILE", "/tmp/test_autorenew_uncertain.db")

import autorenew


class TestUncertainAutoRenew(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_provider_response_reuses_same_business_key(self):
        subscription = {
            "sub_id": 77,
            "user_id": 12345,
            "key_id": 9,
            "expiry": 1_800_000_000,
            "fail_count": 2,
            "days": 30,
            "devices": 2,
            "months": 1,
            "amount_rub": 89,
            "payment_method_id": "pm_saved",
            "payment_method_title": "*1234",
        }
        bot = MagicMock()
        with (
            patch("autorenew.get_expiring_auto_renew_subscriptions", new=AsyncMock(return_value=[subscription])),
            patch("autorenew.charge_yookassa_recurrent", new=AsyncMock(return_value=None)) as charge,
            patch("autorenew.defer_auto_renew_charge_retry", new=AsyncMock()) as defer,
            patch("autorenew.update_auto_renew_charge_failure", new=AsyncMock()) as mark_failed,
        ):
            await autorenew.process_auto_renewals(bot)

        charge.assert_awaited_once()
        self.assertEqual(charge.await_args.kwargs["idempotence_key"], "autorenew_77_9_1800000000")
        defer.assert_awaited_once_with(77, "provider response was not confirmed")
        mark_failed.assert_not_awaited()

    async def test_successful_charge_is_fulfilled_before_success_marker(self):
        subscription = {
            "sub_id": 78,
            "user_id": 12346,
            "key_id": 10,
            "expiry": 1_800_000_001,
            "fail_count": 0,
            "days": 30,
            "devices": 2,
            "months": 1,
            "amount_rub": 89,
            "payment_method_id": "pm_saved",
            "payment_method_title": "*4321",
        }
        bot = MagicMock()
        with (
            patch("autorenew.get_expiring_auto_renew_subscriptions", new=AsyncMock(return_value=[subscription])),
            patch("autorenew.charge_yookassa_recurrent", new=AsyncMock(return_value={"id": "yk-1", "status": "succeeded"})),
            patch("webhook._process_payment", new=AsyncMock()) as fulfill,
        ):
            await autorenew.process_auto_renewals(bot)

        fulfill.assert_awaited_once_with(bot, "yk-1")


if __name__ == "__main__":
    unittest.main()
