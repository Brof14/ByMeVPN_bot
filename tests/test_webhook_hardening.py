"""Tests for the YooKassa notification boundary."""
import json
import unittest
from types import SimpleNamespace

import webhook


class _Request:
    def __init__(self, source_ip, body=b"{}", headers=None):
        self.client = SimpleNamespace(host=source_ip)
        self.headers = headers or {}
        self._body = body

    async def stream(self):
        yield self._body


class TestYooKassaWebhookHardening(unittest.IsolatedAsyncioTestCase):
    def test_source_allowlist_handles_ipv4_ipv6_and_rejects_invalid_addresses(self):
        self.assertTrue(webhook._is_allowed_yookassa_source("185.71.76.12"))
        self.assertTrue(webhook._is_allowed_yookassa_source("2a02:5180::1"))
        self.assertTrue(webhook._is_allowed_yookassa_source("::ffff:185.71.76.12"))
        self.assertFalse(webhook._is_allowed_yookassa_source("8.8.8.8"))
        self.assertFalse(webhook._is_allowed_yookassa_source("not-an-ip"))
        self.assertFalse(webhook._is_allowed_yookassa_source(None))

    async def test_public_source_is_rejected_before_body_is_read(self):
        request = _Request("8.8.8.8")
        response = await webhook.yookassa_webhook(request)
        self.assertEqual(response.status_code, 403)

    async def test_oversized_notification_is_rejected(self):
        request = _Request(
            "185.71.76.12",
            body=b" " * (webhook.MAX_YOOKASSA_WEBHOOK_BYTES + 1),
        )
        response = await webhook.yookassa_webhook(request)
        self.assertEqual(response.status_code, 413)

    async def test_unexpected_event_is_acknowledged_without_payment_processing(self):
        body = json.dumps({"event": "payment.canceled", "object": {"id": "payment_123"}}).encode()
        response = await webhook.yookassa_webhook(_Request("185.71.76.12", body))
        self.assertEqual(response.status_code, 200)

    async def test_malformed_payment_id_is_rejected(self):
        body = json.dumps({"event": "payment.succeeded", "object": {"id": "../../bad"}}).encode()
        response = await webhook.yookassa_webhook(_Request("185.71.76.12", body))
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
