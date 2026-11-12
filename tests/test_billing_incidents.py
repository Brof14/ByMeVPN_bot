"""Reconciliation queue is read only and inaccessible to customers."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from billing_incidents import read_billing_incidents


class TestBillingIncidentQueue(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_FILE
        await database.close_db()
        database.DB_FILE = os.path.join(self.temp.name, "incidents.db")
        await database.init_db()
        await database.ensure_user(3456)

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.old_path
        self.temp.cleanup()

    async def test_paid_incident_and_webhook_appear_without_any_fulfillment(self):
        await database.record_payment_idempotent(
            user_id=3456, amount=89, currency="RUB", method="yookassa",
            days=30, payload="needs-review", status="recovery_required",
            provider="yookassa", provider_payment_id="needs-review",
        )
        await database.record_yookassa_webhook("webhook-pending")
        result = await read_billing_incidents()
        self.assertEqual({r["invoice_id"] for r in result},
                         {"needs-review", "webhook-pending"})

    async def test_successfully_settled_payment_does_not_appear(self):
        await database.record_payment_idempotent(
            user_id=3456, amount=89, currency="RUB", method="yookassa",
            days=30, payload="settled", status="success",
            provider="yookassa", provider_payment_id="settled",
        )
        await database.record_yookassa_webhook("settled")
        self.assertEqual(await read_billing_incidents(), [])

    async def test_customer_cannot_open_admin_queue(self):
        from handlers.admin import cb_admin_payment_incidents
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=22222),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        )
        with patch("handlers.admin._is_admin", return_value=False), \
             patch("handlers.admin.safe_answer", new=AsyncMock()) as response:
            await cb_admin_payment_incidents(callback)
        response.assert_awaited_once_with(callback, "Нет доступа.", alert=True)
        callback.message.edit_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
