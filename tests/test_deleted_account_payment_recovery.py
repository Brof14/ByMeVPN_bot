"""Post-deletion provider settlements remain visible for manual refund review."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webhook import _quarantine_deleted_account_payment


class DeletedAccountPaymentRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def kwargs(self):
        return {
            "provider": "yookassa",
            "payment_id": "provider-charge-1",
            "user_id": 123,
            "amount_rub": 89,
            "days": 30,
            "devices": 2,
        }

    async def test_new_paid_order_after_erasure_is_quarantined(self):
        bot = SimpleNamespace()
        with (
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=True)),
            patch("database.record_payment_idempotent", new=AsyncMock(return_value=(True, 77))) as record,
            patch("database.get_payment_by_id", new=AsyncMock(return_value={
                "status": "recovery_required",
            })),
            patch("database.update_payment_status", new=AsyncMock()) as update,
            patch("webhook._notify_admin", new=AsyncMock()) as notify,
        ):
            result = await _quarantine_deleted_account_payment(bot, **self.kwargs())
        self.assertTrue(result)
        self.assertEqual(record.await_args.kwargs["status"], "recovery_required")
        self.assertEqual(record.await_args.kwargs["provider_payment_id"], "provider-charge-1")
        update.assert_not_awaited()
        notify.assert_awaited_once()

    async def test_previously_fulfilled_order_is_not_flagged_as_new(self):
        with (
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=True)),
            patch("database.record_payment_idempotent", new=AsyncMock(return_value=(False, 77))),
            patch("database.get_payment_by_id", new=AsyncMock(return_value={
                "status": "success",
            })),
            patch("database.update_payment_status", new=AsyncMock()) as update,
            patch("webhook._notify_admin", new=AsyncMock()) as notify,
        ):
            result = await _quarantine_deleted_account_payment(
                SimpleNamespace(), **self.kwargs(),
            )
        self.assertTrue(result)
        update.assert_not_awaited()
        notify.assert_not_awaited()

    async def test_live_account_uses_standard_fulfillment_path(self):
        with (
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=False)),
            patch("database.record_payment_idempotent", new=AsyncMock()) as record,
            patch("webhook._notify_admin", new=AsyncMock()) as notify,
        ):
            result = await _quarantine_deleted_account_payment(
                SimpleNamespace(), **self.kwargs(),
            )
        self.assertFalse(result)
        record.assert_not_awaited()
        notify.assert_not_awaited()

    async def test_failed_old_charge_is_quarantined_instead_of_retried(self):
        with (
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=True)),
            patch("database.record_payment_idempotent", new=AsyncMock(return_value=(False, 77))),
            patch("database.get_payment_by_id", new=AsyncMock(return_value={
                "status": "failed",
            })),
            patch("database.update_payment_status", new=AsyncMock()) as update,
            patch("webhook._notify_admin", new=AsyncMock()) as notify,
        ):
            result = await _quarantine_deleted_account_payment(
                SimpleNamespace(), **{**self.kwargs(), "provider": "cryptobot"},
            )
        self.assertTrue(result)
        update.assert_awaited_once_with(77, "recovery_required")
        notify.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
