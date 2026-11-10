"""Tests for the YooKassa notification boundary."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


class TestTelegramWebhookHardening(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(webhook.app)

    def test_missing_secret_fails_closed_before_dispatch(self):
        dispatcher = SimpleNamespace(feed_update=AsyncMock())
        with patch.object(webhook, "dispatcher", dispatcher), \
             patch.object(webhook, "TELEGRAM_WEBHOOK_SECRET", "", create=True):
            response = self.client.post("/webhook/telegram", json={"update_id": 1})
        self.assertEqual(response.status_code, 503)
        dispatcher.feed_update.assert_not_awaited()

    def test_wrong_secret_is_rejected_before_dispatch(self):
        dispatcher = SimpleNamespace(feed_update=AsyncMock())
        with patch.object(webhook, "dispatcher", dispatcher), \
             patch.object(webhook, "TELEGRAM_WEBHOOK_SECRET", "expected-secret", create=True):
            response = self.client.post(
                "/webhook/telegram", json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            )
        self.assertEqual(response.status_code, 401)
        dispatcher.feed_update.assert_not_awaited()

    def test_valid_secret_dispatches_and_internal_errors_are_retryable(self):
        dispatcher = SimpleNamespace(feed_update=AsyncMock())
        bot = object()
        webhook.app.state.bot = bot
        headers = {"X-Telegram-Bot-Api-Secret-Token": "expected-secret"}
        with patch.object(webhook, "dispatcher", dispatcher), \
             patch.object(webhook, "TELEGRAM_WEBHOOK_SECRET", "expected-secret", create=True):
            response = self.client.post("/webhook/telegram", json={"update_id": 1}, headers=headers)
            self.assertEqual(response.status_code, 200)
            dispatcher.feed_update.assert_awaited_once()

            dispatcher.feed_update.side_effect = RuntimeError("dispatch failed")
            failed = self.client.post("/webhook/telegram", json={"update_id": 2}, headers=headers)
        self.assertEqual(failed.status_code, 500)

    def test_app_login_accepts_encrypted_ticket_without_subscription_query(self):
        from app_links import build_connect_ticket

        with patch.dict("os.environ", {"APP_HANDOFF_SECRET": "isolated-test-secret"}):
            ticket = build_connect_ticket("https://sub.example/private")
            response = self.client.get("/app/login", params={"ticket": ticket})
        self.assertEqual(response.status_code, 200)
        self.assertIn("bymevpn://login?sub=", response.text)
        self.assertIn("no-store", response.headers["cache-control"])

    def test_app_login_rejects_legacy_raw_subscription_query(self):
        response = self.client.get(
            "/app/login", params={"sub": "https://sub.example/private-token"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("private-token", response.text)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")


if __name__ == "__main__":
    unittest.main()
