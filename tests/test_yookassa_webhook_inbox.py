"""Persistent YooKassa inbox survives restarts and retries safely."""
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database
import webhook


class TestYooKassaWebhookInbox(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_path = database.DB_FILE
        await database.close_db()
        database.DB_FILE = os.path.join(self.tmp.name, "inbox.sqlite")
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.saved_path
        self.tmp.cleanup()

    async def test_duplicate_notification_is_persisted_only_once(self):
        await database.record_yookassa_webhook("charge-one")
        await database.record_yookassa_webhook("charge-one")
        self.assertEqual(await database.get_due_yookassa_webhooks(), ["charge-one"])

    async def test_unconfirmed_delivery_stays_queued_for_reconciliation(self):
        await database.record_yookassa_webhook("charge-pending")
        with patch.object(webhook, "_fetch_yookassa_payment", new=AsyncMock(return_value={
            "status": "succeeded", "id": "charge-pending"
        })), patch.object(webhook, "_process_payment", new=AsyncMock()), \
             patch.object(database, "is_yookassa_fulfilled", new=AsyncMock(return_value=False)):
            await webhook._process_recorded_yookassa_webhook(object(), "charge-pending")
        db = await database.get_db()
        row = await (await db.execute(
            "SELECT attempts,last_status FROM yookassa_webhook_inbox WHERE payment_id='charge-pending'"
        )).fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "fulfillment_pending")

    async def test_confirmed_delivery_removes_inbox_record(self):
        await database.record_yookassa_webhook("charge-success")
        with patch.object(database, "is_yookassa_fulfilled", new=AsyncMock(return_value=True)):
            await webhook._process_recorded_yookassa_webhook(object(), "charge-success")
        db = await database.get_db()
        row = await (await db.execute(
            "SELECT 1 FROM yookassa_webhook_inbox WHERE payment_id='charge-success'"
        )).fetchone()
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
