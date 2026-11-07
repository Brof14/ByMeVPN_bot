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
    async def test_unexpected_status_or_missing_payment_id_keeps_same_attempt(self):
        subscription = {
            "sub_id": 77, "user_id": 12345, "key_id": 9, "expiry": 1_800_000_000,
            "fail_count": 2, "days": 30, "devices": 2, "months": 1,
            "amount_rub": 89, "payment_method_id": "pm_saved",
        }
        for response in ({"id": "yk-1", "status": "waiting_for_capture"}, {"status": "succeeded"}, {"status": "pending"}, "malformed"):
            with (
                self.subTest(response=response),
                patch("autorenew.get_expiring_auto_renew_subscriptions", new=AsyncMock(return_value=[subscription])),
                patch("autorenew.charge_yookassa_recurrent", new=AsyncMock(return_value=response)) as charge,
                patch("autorenew.defer_auto_renew_charge_retry", new=AsyncMock()) as defer,
                patch("autorenew.update_auto_renew_charge_failure", new=AsyncMock()) as mark_failed,
                patch("webhook._process_payment", new=AsyncMock()) as fulfill,
                patch("database.add_yookassa_pending", new=AsyncMock()) as persist,
            ):
                await autorenew.process_auto_renewals(MagicMock())
            self.assertEqual(charge.await_args.kwargs["idempotence_key"], "autorenew_77_9_1800000000_2")
            defer.assert_awaited_once()
            mark_failed.assert_not_awaited()
            fulfill.assert_not_awaited()
            persist.assert_not_awaited()

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
        self.assertEqual(charge.await_args.kwargs["idempotence_key"], "autorenew_77_9_1800000000_2")
        defer.assert_awaited_once_with(77, "provider response was not confirmed")
        mark_failed.assert_not_awaited()

    async def test_canceled_payment_with_null_details_is_handled(self):
        subscription = {
            "sub_id": 79, "user_id": 12347, "key_id": 11, "expiry": 1_800_000_002,
            "fail_count": 0, "days": 30, "devices": 2, "months": 1,
            "amount_rub": 89, "payment_method_id": "pm_saved",
        }
        with (
            patch("autorenew.get_expiring_auto_renew_subscriptions", new=AsyncMock(return_value=[subscription])),
            patch("autorenew.charge_yookassa_recurrent", new=AsyncMock(return_value={
                "id": "yk-canceled", "status": "canceled", "cancellation_details": None,
            })),
            patch("autorenew.update_auto_renew_charge_failure", new=AsyncMock(return_value=False)) as failed,
            patch("autorenew._notify_autorenew_failed", new=AsyncMock()) as notify,
        ):
            await autorenew.process_auto_renewals(MagicMock())
        failed.assert_awaited_once_with(79, "unknown")
        notify.assert_awaited_once()

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
            patch("database.add_yookassa_pending", new=AsyncMock()) as persist_payment,
            patch("webhook._process_payment", new=AsyncMock()) as fulfill,
        ):
            await autorenew.process_auto_renewals(bot)

        persist_payment.assert_awaited_once_with("yk-1", 12346, 30, 2, 89)
        fulfill.assert_awaited_once_with(bot, "yk-1")


if __name__ == "__main__":
    unittest.main()
