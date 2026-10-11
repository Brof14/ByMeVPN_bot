"""Regression tests for payment recovery after bot downtime."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse, parse_qs

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DB_FILE", "/tmp/test_payment_monitor_recovery.db")

from payment_monitor import PaymentMonitor


class _Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class _AsyncClient:
    def __init__(self, pages=None):
        self.url = None
        self.headers = None
        self.params = []
        self.pages = list(pages or [{"items": [{"id": "paid-during-downtime"}]}])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None, params=None):
        self.url = url
        self.headers = headers
        self.params.append(dict(params or {}))
        page = self.pages.pop(0) if self.pages else {"items": []}
        return _Response(page)


class TestPaymentMonitorRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_query_window_is_not_clamped_to_monitor_start_time(self):
        client = _AsyncClient()
        monitor = PaymentMonitor(MagicMock())
        # Simulate a clock/start marker later than the requested recovery window.
        monitor.start_time = datetime.utcnow() + timedelta(hours=2)

        with patch("payment_monitor.httpx.AsyncClient", return_value=client):
            payments = await monitor.get_successful_payments(minutes_back=60)

        self.assertEqual(payments, [{"id": "paid-during-downtime"}])
        query = client.params[0]
        cutoff_text = query["created_at.gte"]
        cutoff = datetime.fromisoformat(cutoff_text.removesuffix("Z"))
        now = datetime.utcnow()
        self.assertLess(cutoff, now - timedelta(minutes=55))
        self.assertGreater(cutoff, now - timedelta(minutes=65))



    async def test_yookassa_recovery_follows_cursor_until_all_pages_are_read(self):
        client = _AsyncClient(pages=[
            {"items": [{"id": "payment-1"}], "next_cursor": "cursor-1"},
            {"items": [{"id": "payment-2"}], "next_cursor": "cursor-2"},
            {"items": [{"id": "payment-3"}]},
        ])
        monitor = PaymentMonitor(MagicMock())

        with patch("payment_monitor.httpx.AsyncClient", return_value=client):
            payments = await monitor.get_successful_payments(minutes_back=60)

        self.assertEqual([p["id"] for p in payments], ["payment-1", "payment-2", "payment-3"])
        self.assertEqual(len(client.params), 3)
        self.assertEqual(client.params[0]["limit"], 100)
        self.assertEqual(client.params[1]["cursor"], "cursor-1")
        self.assertEqual(client.params[2]["cursor"], "cursor-2")
        self.assertEqual(
            client.params[0]["created_at.gte"],
            client.params[2]["created_at.gte"],
            "the recovery window must remain stable across pages",
        )

    async def test_yookassa_monitor_delegates_marked_payment_to_recovery_handler(self):
        monitor = PaymentMonitor(MagicMock())
        with (
            patch.object(monitor, "get_successful_payments", new=AsyncMock(return_value=[{"id": "legacy-marked-payment"}])),
            patch.object(monitor, "_get_failed_ledger_payment_ids", new=AsyncMock(return_value=[])),
            patch("payment_monitor._process_payment", new=AsyncMock()) as process_payment,
        ):
            await monitor.check_and_process_payments()

        process_payment.assert_awaited_once_with(monitor.bot, "legacy-marked-payment")


    async def test_old_failed_ledger_payment_is_retried_outside_recent_window(self):
        monitor = PaymentMonitor(MagicMock())
        with (
            patch.object(monitor, "get_successful_payments", new=AsyncMock(return_value=[])),
            patch.object(monitor, "_get_failed_ledger_payment_ids", new=AsyncMock(return_value=["old-failed-payment"])),
            patch("payment_monitor._process_payment", new=AsyncMock()) as process_payment,
        ):
            await monitor.check_and_process_payments(minutes_back=5)

        process_payment.assert_awaited_once_with(monitor.bot, "old-failed-payment")

    async def test_failed_payment_retry_is_throttled(self):
        monitor = PaymentMonitor(MagicMock())
        with (
            patch.object(monitor, "get_successful_payments", new=AsyncMock(return_value=[{"id": "failed-payment"}])),
            patch.object(monitor, "_get_failed_ledger_payment_ids", new=AsyncMock(return_value=["failed-payment"])),
            patch("payment_monitor._process_payment", new=AsyncMock()) as process_payment,
        ):
            await monitor.check_and_process_payments(minutes_back=5)
            await monitor.check_and_process_payments(minutes_back=5)

        process_payment.assert_awaited_once_with(monitor.bot, "failed-payment")

    async def test_crypto_monitor_does_not_skip_old_paid_invoice_after_restart(self):
        from payment_monitor import CryptoPaymentMonitor

        monitor = CryptoPaymentMonitor(MagicMock())
        old_invoice = {"invoice_id": 987654, "status": "paid", "created_at": 1}
        with (
            patch("payment_monitor.get_paid_crypto_invoices", new=AsyncMock(return_value=[old_invoice])),
            patch("payment_monitor._process_crypto_invoice", new=AsyncMock()) as process_invoice,
        ):
            await monitor.check_and_process_payments()

        process_invoice.assert_awaited_once_with(monitor.bot, old_invoice)


if __name__ == "__main__":
    unittest.main(verbosity=2)
