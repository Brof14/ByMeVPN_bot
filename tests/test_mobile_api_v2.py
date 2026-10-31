"""Mobile v2 contract tests with isolated storage and mocked payment providers."""
import os
import secrets
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DB_FILE", "/tmp/test_mobile_api_v2.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import api_router


class TestMobileApiV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(api_router.api_router, prefix="/api/v1")
        app.dependency_overrides[api_router.get_current_user_id] = lambda: 987654321
        cls.client = TestClient(app)

    def test_catalog_uses_tier_bound_plan_codes_and_android_response_fields(self):
        expected_prices = {
            2: (89, 316, 552, 885),
            5: (109, 396, 712, 1185),
            10: (129, 476, 872, 1485),
        }
        expected_days = (30, 120, 240, 450)
        for devices in (2, 5, 10):
            with self.subTest(devices=devices):
                response = self.client.get(
                    f"/api/v1/mobile/v2/plans?devices={devices}"
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(set(payload), {"trial", "plans"})
                self.assertEqual(len(payload["plans"]), 4)
                for plan, period, price, days in zip(
                    payload["plans"], ("1m", "3m", "6m", "12m"),
                    expected_prices[devices], expected_days,
                ):
                    self.assertEqual(plan["code"], f"{period}_d{devices}")
                    self.assertEqual(plan["price_rub"], price)
                    self.assertEqual(plan["days"], days)
                    self.assertEqual(plan["devices"], devices)
                    self.assertEqual(
                        {option["devices"] for option in plan["device_options"]},
                        {2, 5, 10},
                    )

    def test_create_derives_price_term_and_hwid_tier_from_plan_code(self):
        reservation = {
            "plan_code": "1m", "provider": "yookassa", "devices": 5,
            "payment_id": None, "confirmation_url": None, "generation": 0,
            "creation_state": "ready",
        }
        with patch.object(
            api_router.database, "ensure_mobile_payment_request",
            new=AsyncMock(return_value=reservation),
        ), patch.object(
            api_router.database, "claim_mobile_payment_creation", new=AsyncMock(return_value=True),
        ), patch.object(
            api_router.database, "save_mobile_payment_request", new=AsyncMock(return_value=True),
        ), patch(
            "payments.create_yookassa_payment", new=AsyncMock(return_value={
                "payment_url": "https://pay.example.invalid/checkout",
                "payment_id": "test-invoice-001", "status": "pending",
            }),
        ) as create_payment:
            response = self.client.post(
                "/api/v1/mobile/v2/payment/create",
                json={
                    "plan_code": "1m_d5",
                    "request_id": "request_12345678",
                    "provider": "yookassa",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        args = create_payment.await_args.kwargs
        self.assertEqual(
            (args["amount_rub"], args["days"], args["devices"], args["months"]),
            (109, 30, 5, 1),
        )
        self.assertTrue(args["idempotency_key"])
        self.assertEqual(args["extra_metadata"]["mobile_v2"], "1")
        self.assertEqual(args["extra_metadata"]["plan_code"], "1m_d5")
        self.assertEqual(response.json()["invoice_id"], "test-invoice-001")

    def test_repeated_request_id_returns_same_invoice_without_creating_another(self):
        reservation = {
            "plan_code": "1m", "provider": "yookassa", "devices": 5,
            "payment_id": "existing-invoice", "confirmation_url": "https://pay.example.invalid/existing",
            "generation": 0, "creation_state": "created",
        }
        with patch.object(
            api_router.database, "ensure_mobile_payment_request",
            new=AsyncMock(return_value=reservation),
        ), patch(
            "payments.get_yookassa_payment_status", new=AsyncMock(return_value="pending"),
        ), patch(
            "payments.create_yookassa_payment", new=AsyncMock(),
        ) as create_payment:
            response = self.client.post(
                "/api/v1/mobile/v2/payment/create",
                json={"plan_code": "1m_d5", "request_id": "request_12345678", "provider": "yookassa"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["invoice_id"], "existing-invoice")
        self.assertEqual(response.json()["payment_url"], "https://pay.example.invalid/existing")
        create_payment.assert_not_awaited()

    def test_crypto_create_uses_same_server_catalog_terms(self):
        reservation = {
            "plan_code": "6m", "provider": "crypto", "devices": 10,
            "payment_id": None, "confirmation_url": None, "generation": 0,
            "creation_state": "ready",
        }
        with patch.object(
            api_router.database, "ensure_mobile_payment_request",
            new=AsyncMock(return_value=reservation),
        ), patch.object(
            api_router.database, "claim_mobile_payment_creation", new=AsyncMock(return_value=True),
        ), patch.object(
            api_router.database, "save_mobile_crypto_checkout", new=AsyncMock(return_value=True),
        ), patch(
            "payments.create_crypto_payment",
            new=AsyncMock(return_value=("https://pay.example.invalid/crypto", "crypto-test-invoice")),
        ) as create_payment:
            response = self.client.post(
                "/api/v1/mobile/v2/payment/create",
                json={
                    "plan_code": "6m_d10",
                    "request_id": "request_crypto_123456",
                    "provider": "crypto",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        args = create_payment.await_args.kwargs
        self.assertEqual(
            (args["amount_rub"], args["days"], args["devices"]),
            (872, 240, 10),
        )
        self.assertEqual(response.json()["invoice_id"], "crypto-test-invoice")

    def test_create_rejects_client_supplied_price_or_device_fields(self):
        response = self.client.post(
            "/api/v1/mobile/v2/payment/create",
            json={
                "plan_code": "1m_d5", "request_id": "request_12345678",
                "provider": "yookassa", "devices": 10, "amount_rub": 1,
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_foreign_invoice_is_not_visible_to_current_user(self):
        with patch.object(
            api_router.database, "get_mobile_payment_record", new=AsyncMock(return_value={
                "user_id": 123, "status": "success",
            }),
        ):
            response = self.client.get(
                "/api/v1/mobile/v2/payment/check",
                params={"invoice_id": "test-invoice-foreign", "provider": "yookassa"},
            )
        self.assertEqual(response.status_code, 404)

    def test_provider_success_without_fulfillment_record_does_not_activate(self):
        with patch.object(
            api_router.database, "get_mobile_payment_record", new=AsyncMock(return_value={
                "user_id": 987654321, "status": "pending",
            }),
        ), patch(
            "payments.get_yookassa_payment_status", new=AsyncMock(return_value="succeeded"),
        ):
            response = self.client.get(
                "/api/v1/mobile/v2/payment/check",
                params={"invoice_id": "test-invoice-pending", "provider": "yookassa"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "processing")
        self.assertFalse(response.json()["is_active"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
