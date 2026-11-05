"""HTTP-to-fulfillment coverage for mobile YooKassa invoices.

The provider and access-delivery boundary are mocked; order ownership and
idempotency use a disposable SQLite database.
"""
import asyncio
import os
import secrets
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

os.environ.setdefault("DB_FILE", "/tmp/mobile-yookassa-webhook-import.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import constants
import database
import webhook


class MobileYooKassaWebhookHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.close_db()
        self.original_db_file = database.DB_FILE
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mobile-yookassa-webhook-")
        database.DB_FILE = os.path.join(self.temp_dir.name, "isolated.sqlite3")
        await database.init_db()
        self.user_id = 98765001
        self.request_id = "request-98765001"
        self.payment_id = "yk-mobile-98765001"
        self.plan_code = "3m"
        self.devices = 5
        self.amount_rub, self.days = constants.get_price_for_months(3, self.devices)
        await database.ensure_user(self.user_id)
        await database.ensure_mobile_payment_request(
            self.user_id, self.request_id, self.plan_code, "yookassa", self.devices,
        )
        await database.save_mobile_payment_request(
            self.user_id, self.request_id, self.plan_code, "yookassa",
            self.payment_id, "https://payment.invalid/isolated-test",
        )

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.original_db_file
        self.temp_dir.cleanup()

    def _verified_payment(self, status="succeeded"):
        return {
            "id": self.payment_id,
            "status": status,
            "amount": {"value": f"{self.amount_rub}.00", "currency": "RUB"},
            "payment_method": {"saved": False},
            "metadata": {
                "user_id": str(self.user_id),
                "days": str(self.days),
                "devices": str(self.devices),
                "months": "3",
                "amount_rub": str(self.amount_rub),
                "plan_code": f"{self.plan_code}_d{self.devices}",
                "mobile_v2": "1",
                "mobile_api_checkout": "1",
            },
        }

    async def _post_webhook(self, client):
        return await client.post(
            "/webhook/yookassa",
            json={"event": "payment.succeeded", "object": {"id": self.payment_id}},
        )

    async def test_mobile_v2_http_webhook_uses_api_order_and_fulfills_once(self):
        payment = self._verified_payment()
        webhook.app.state.bot = MagicMock()
        webhook.app.state.bot.send_message = AsyncMock()
        with (
            patch.object(webhook, "_is_allowed_yookassa_source", return_value=True),
            patch.object(webhook, "_fetch_yookassa_payment", new=AsyncMock(return_value=payment)),
            patch.object(webhook, "deliver_key", new=AsyncMock(return_value=True)) as deliver,
        ):
            transport = httpx.ASGITransport(app=webhook.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = await self._post_webhook(client)
                self.assertEqual(first.status_code, 200)
                for _ in range(20):
                    if await database.is_yookassa_processed(self.payment_id):
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(await database.is_yookassa_processed(self.payment_id))

                second = await self._post_webhook(client)
                self.assertEqual(second.status_code, 200)
                await asyncio.sleep(0.02)

        deliver.assert_awaited_once()
        record = await database.get_mobile_payment_record("yookassa", self.payment_id)
        self.assertEqual(record["user_id"], self.user_id)
        self.assertEqual(record["status"], "success")

    async def test_http_webhook_does_not_fulfill_when_provider_readback_is_pending(self):
        payment = self._verified_payment(status="pending")
        webhook.app.state.bot = MagicMock()
        with (
            patch.object(webhook, "_is_allowed_yookassa_source", return_value=True),
            patch.object(webhook, "_fetch_yookassa_payment", new=AsyncMock(return_value=payment)),
            patch.object(webhook, "deliver_key", new=AsyncMock(return_value=True)) as deliver,
        ):
            transport = httpx.ASGITransport(app=webhook.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await self._post_webhook(client)
                self.assertEqual(response.status_code, 200)
                await asyncio.sleep(0.02)

        deliver.assert_not_awaited()
        self.assertFalse(await database.is_yookassa_processed(self.payment_id))


if __name__ == "__main__":
    unittest.main()
