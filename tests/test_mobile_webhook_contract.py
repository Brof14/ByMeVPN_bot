"""Mobile invoice fulfillment must match its durable server-owned order."""
import os
import secrets
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DB_FILE", "/tmp/test_mobile_webhook_contract.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import webhook


class TestMobileWebhookContract(unittest.IsolatedAsyncioTestCase):
    async def test_yookassa_accepts_only_exact_owner_plan_and_server_terms(self):
        order = {
            "user_id": 987654321, "request_id": "request-test", "plan_code": "3m",
            "provider": "yookassa", "devices": 5, "payment_id": "invoice-test",
        }
        with patch(
            "database.get_mobile_payment_request_by_invoice", new=AsyncMock(return_value=order),
        ) as lookup:
            valid = await webhook._mobile_yookassa_order_matches(
                "invoice-test", 987654321, 120, 5, 396,
                {"plan_code": "3m_d5"},
            )
            wrong_owner = await webhook._mobile_yookassa_order_matches(
                "invoice-test", 123, 120, 5, 396,
                {"plan_code": "3m_d5"},
            )
            wrong_price = await webhook._mobile_yookassa_order_matches(
                "invoice-test", 987654321, 120, 5, 1,
                {"plan_code": "3m_d5"},
            )
            wrong_tier = await webhook._mobile_yookassa_order_matches(
                "invoice-test", 987654321, 120, 5, 396,
                {"plan_code": "3m_d10"},
            )
        self.assertTrue(valid)
        self.assertFalse(wrong_owner)
        self.assertFalse(wrong_price)
        self.assertFalse(wrong_tier)
        self.assertEqual(lookup.await_count, 4)

    async def test_legacy_mobile_invoice_marker_uses_same_order_guard(self):
        order = {
            "user_id": 987654321, "request_id": "request-test", "plan_code": "1m",
            "provider": "yookassa", "devices": 5, "payment_id": "legacy-mobile-invoice",
        }
        with patch(
            "database.get_mobile_payment_request_by_invoice", new=AsyncMock(return_value=order),
        ) as lookup:
            self.assertTrue(await webhook._mobile_yookassa_order_matches(
                "legacy-mobile-invoice", 987654321, 30, 5, 109,
                {"plan_code": "1m_d5"},
            ))
        lookup.assert_awaited_once_with("yookassa", "legacy-mobile-invoice")

    async def test_crypto_requires_matching_invoice_owner_and_catalog_terms(self):
        order = {
            "user_id": 987654321, "request_id": "request-test", "plan_code": "6m",
            "provider": "crypto", "devices": 10, "payment_id": "crypto-invoice-test",
        }
        pending = {"user_id": 987654321, "days": 240, "devices": 10, "amount_rub": 872}
        with patch(
            "database.get_mobile_payment_request_by_invoice", new=AsyncMock(return_value=order),
        ):
            self.assertTrue(await webhook._mobile_crypto_order_matches("crypto-invoice-test", pending))
            self.assertFalse(await webhook._mobile_crypto_order_matches(
                "crypto-invoice-test", {**pending, "amount_rub": 1},
            ))

    async def test_missing_mobile_checkout_record_fails_closed(self):
        with patch(
            "database.get_mobile_payment_request_by_invoice", new=AsyncMock(return_value=None),
        ):
            self.assertFalse(await webhook._mobile_yookassa_order_matches(
                "unlinked-invoice", 987654321, 30, 2, 89, {"plan_code": "1m_d2"},
            ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
