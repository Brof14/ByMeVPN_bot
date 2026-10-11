"""Regression tests for signed Telegram Stars orders."""
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DB_FILE", "/tmp/test_stars_payment_security.db")

from handlers.buy import _build_stars_payload, _parse_stars_payload


class TestStarsPaymentSecurity(unittest.TestCase):
    def test_signed_order_round_trips(self):
        payload = _build_stars_payload(
            user_id=12345, days=120, devices=5, months=3,
            amount=237, promo_code="WELCOME_10", timestamp=1790000000,
        )
        order = _parse_stars_payload(payload)
        self.assertEqual(order, {
            "user_id": 12345,
            "days": 120,
            "devices": 5,
            "months": 3,
            "amount": 237,
            "promo_code": "WELCOME_10",
            "timestamp": 1790000000,
        })
        self.assertLessEqual(len(payload.encode("utf-8")), 128)

    def test_tampered_amount_is_rejected(self):
        payload = _build_stars_payload(
            user_id=12345, days=30, devices=2, months=1,
            amount=89, promo_code="", timestamp=1790000000,
        )
        tampered = payload.replace("_89__", "_1__")
        self.assertIsNone(_parse_stars_payload(tampered))

    def test_unsigned_payload_is_rejected(self):
        self.assertIsNone(_parse_stars_payload("stars_12345_30_2_1_89__1790000000_deadbeef"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
