"""Regression tests for payment recovery after bot downtime."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse, parse_qs

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DB_FILE", "/tmp/test_payment_monitor_recovery.db")

from payment_monitor import PaymentMonitor


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"items": [{"id": "paid-during-downtime"}]}


class _AsyncClient:
    def __init__(self):
        self.url = None
        self.headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None):
        self.url = url
        self.headers = headers
        return _Response()


class TestPaymentMonitorRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_query_window_is_not_clamped_to_monitor_start_time(self):
        client = _AsyncClient()
        monitor = PaymentMonitor(MagicMock())
        # Simulate a clock/start marker later than the requested recovery window.
        monitor.start_time = datetime.utcnow() + timedelta(hours=2)

        with patch("payment_monitor.httpx.AsyncClient", return_value=client):
            payments = await monitor.get_successful_payments(minutes_back=60)

        self.assertEqual(payments, [{"id": "paid-during-downtime"}])
        query = parse_qs(urlparse(client.url).query)
        cutoff_text = query["created_at.gte"][0]
        cutoff = datetime.fromisoformat(cutoff_text.removesuffix("Z"))
        now = datetime.utcnow()
        self.assertLess(cutoff, now - timedelta(minutes=55))
        self.assertGreater(cutoff, now - timedelta(minutes=65))


if __name__ == "__main__":
    unittest.main(verbosity=2)
