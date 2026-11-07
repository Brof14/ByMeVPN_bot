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


class TestCryptoPayInvoiceValidation(unittest.TestCase):
    def test_promo_is_read_only_from_matching_telegram_order_payload(self):
        pending = {"user_id": 98, "days": 30, "devices": 2, "payload": "98:30:2:SALE30"}
        self.assertEqual(webhook._crypto_pending_promo(pending), "SALE30")
        self.assertIsNone(webhook._crypto_pending_promo({**pending, "payload": "bm1_mobile_order"}))
        self.assertIsNone(webhook._crypto_pending_promo({**pending, "payload": "99:30:2:SALE30"}))
        self.assertIsNone(webhook._crypto_pending_promo({**pending, "payload": None}))

    def test_paid_crypto_invoice_must_match_server_owned_rub_order(self):
        pending = {"amount_rub": 79, "payload": "bm1_expected"}

        valid = {
            "status": "paid", "currency_type": "fiat", "fiat": "RUB",
            "amount": "79.00", "payload": "bm1_expected",
        }
        self.assertTrue(webhook._crypto_invoice_matches_pending(valid, pending))

        for changed in (
            {**valid, "amount": "1.00"},
            {**valid, "fiat": "USD"},
            {**valid, "currency_type": "crypto"},
            {**valid, "payload": "bm1_other"},
            {**valid, "amount": "not-a-number"},
        ):
            self.assertFalse(webhook._crypto_invoice_matches_pending(changed, pending))

    def test_legacy_crypto_invoice_without_expected_payload_still_checks_amount(self):
        invoice = {"currency_type": "fiat", "fiat": "RUB", "amount": "120"}
        self.assertTrue(webhook._crypto_invoice_matches_pending(invoice, {"amount_rub": 120}))


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
