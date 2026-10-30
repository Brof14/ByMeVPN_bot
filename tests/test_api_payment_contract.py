"""Contract tests for server-priced, idempotent in-app checkout."""
import os
import secrets
import sys
import unittest
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DB_FILE", "/tmp/test_api_payment_contract_import.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import api_router
from constants import get_price_for_months


class TestMobilePaymentContract(unittest.IsolatedAsyncioTestCase):
    async def test_plans_use_server_catalog_and_free_trial(self):
        result = await api_router.get_plans()
        self.assertEqual(result["trial"]["price_rub"], 0)
        self.assertEqual(result["trial"]["days"], 3)
        for code, months in (("1m", 1), ("3m", 3), ("6m", 6), ("12m", 12)):
            item = next(plan for plan in result["plans"] if plan["code"] == code)
            amount, days = get_price_for_months(months, 2)
            self.assertEqual(item["price_rub"], amount)
            self.assertEqual(item["days"], days)
            self.assertEqual(item["devices"], 2)

    async def test_client_amount_days_and_device_values_are_ignored(self):
        reserved = {"plan_code": "3m", "provider": "yookassa", "payment_id": None, "confirmation_url": None}
        created = {"payment_url": "https://pay.example.invalid/checkout", "payment_id": "invoice-123", "status": "pending"}
        request = api_router.PaymentCreateRequest(
            plan_code="3m", request_id="12345678-1234-1234-1234-123456789012",
            amount_rub=1, days=3, devices=10, provider="yookassa",
        )
        with (
            patch.object(api_router.database, "ensure_mobile_payment_request", new=AsyncMock(return_value=reserved)),
            patch.object(api_router.database, "save_mobile_payment_request", new=AsyncMock(return_value=True)),
            patch("payments.create_yookassa_payment", new=AsyncMock(return_value=created)) as create,
        ):
            result = await api_router.api_create_payment(request, user_id=123)

        self.assertEqual(result["invoice_id"], "invoice-123")
        kwargs = create.await_args.kwargs
        self.assertEqual(kwargs["amount_rub"], get_price_for_months(3, 2)[0])
        self.assertEqual(kwargs["days"], get_price_for_months(3, 2)[1])
        self.assertEqual(kwargs["devices"], 2)
        self.assertFalse(kwargs["save_payment_method"])

    async def test_repeated_request_returns_same_saved_invoice(self):
        reserved = {
            "plan_code": "1m", "provider": "yookassa", "payment_id": "invoice-existing",
            "confirmation_url": "https://pay.example.invalid/checkout",
        }
        request = api_router.PaymentCreateRequest(
            plan_code="1m", request_id="12345678-1234-1234-1234-123456789012", provider="yookassa",
        )
        with (
            patch.object(api_router.database, "ensure_mobile_payment_request", new=AsyncMock(return_value=reserved)),
            patch("payments.create_yookassa_payment", new=AsyncMock()) as create,
        ):
            result = await api_router.api_create_payment(request, user_id=123)
        self.assertEqual(result["invoice_id"], "invoice-existing")
        create.assert_not_awaited()

    async def test_other_users_invoice_is_not_disclosed_or_queried(self):
        from fastapi import HTTPException
        from starlette.requests import Request

        request = Request({
            "type": "http", "http_version": "1.1", "method": "GET",
            "scheme": "https", "path": "/api/v1/payment/check", "raw_path": b"/api/v1/payment/check",
            "query_string": b"", "headers": [], "client": ("127.0.0.1", 1),
            "server": ("test.invalid", 443),
        })
        with (
            patch.object(api_router.database, "get_mobile_payment_record", new=AsyncMock(return_value={"user_id": 456, "status": "pending"})),
            patch("payments.get_yookassa_payment_status", new=AsyncMock()) as provider,
        ):
            with self.assertRaises(HTTPException) as result:
                await api_router.api_check_payment(request, "invoice-123", "yookassa", user_id=123)
        self.assertEqual(result.exception.status_code, 404)
        provider.assert_not_awaited()

    async def test_provider_success_does_not_activate_without_fulfilled_ledger(self):
        from starlette.requests import Request

        request = Request({
            "type": "http", "http_version": "1.1", "method": "GET",
            "scheme": "https", "path": "/api/v1/payment/check", "raw_path": b"/api/v1/payment/check",
            "query_string": b"", "headers": [], "client": ("127.0.0.1", 1),
            "server": ("test.invalid", 443),
        })
        with (
            patch.object(api_router.database, "get_mobile_payment_record", new=AsyncMock(return_value={"user_id": 123, "status": "processing"})),
            patch("payments.get_yookassa_payment_status", new=AsyncMock(return_value="succeeded")),
            patch.object(api_router, "get_user_real_subscription", new=AsyncMock()) as subscription,
        ):
            result = await api_router.api_check_payment(request, "invoice-123", "yookassa", user_id=123)
        self.assertEqual(result["status"], "processing")
        self.assertFalse(result["is_active"])
        subscription.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
