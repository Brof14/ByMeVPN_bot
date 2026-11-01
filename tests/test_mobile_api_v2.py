"""Mobile v2 contract tests with isolated storage and mocked payment providers."""
import os
import secrets
import time
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

    def setUp(self):
        self.payment_guard = patch.dict(os.environ, {"MOBILE_V2_WEBHOOK_GUARD_ENABLED": "1"})
        self.payment_guard.start()

    def tearDown(self):
        self.payment_guard.stop()

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
                self.assertEqual(set(payload), {"trial", "plans", "payment_enabled", "purchase_enabled"})
                self.assertTrue(payload["payment_enabled"])
                self.assertTrue(payload["purchase_enabled"])
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
                    self.assertTrue(plan["purchase_enabled"])

    def test_catalog_displays_server_prices_but_keeps_purchase_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOBILE_V2_WEBHOOK_GUARD_ENABLED", None)
            response = self.client.get("/api/v1/mobile/v2/plans?devices=5")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["payment_enabled"])
        self.assertFalse(body["purchase_enabled"])
        self.assertEqual(body["plans"][0]["price_rub"], 109)
        self.assertFalse(any(plan["purchase_enabled"] for plan in body["plans"]))

    def test_mobile_checkout_route_is_closed_when_guard_is_not_enabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOBILE_V2_WEBHOOK_GUARD_ENABLED", None)
            response = self.client.post("/api/v1/mobile/v2/payment/create", json={
                "plan_code": "1m_d2", "request_id": "guard_off_12345", "provider": "yookassa",
            })
        self.assertEqual(response.status_code, 503)

    def test_mobile_subscription_status_reflects_updated_bot_entitlement_and_devices(self):
        now = int(time.time())
        first = {"id": 1, "key": "https://sub.example.invalid/a", "remark": "Подписка",
                 "uuid": "safe-test-uuid", "short_id": "", "days": 30, "limit_ip": 2,
                 "created": now - 100, "expiry": now + 7200}
        updated = {**first, "days": 120, "limit_ip": 5, "expiry": now + 120 * 86400}
        with patch.object(api_router.database, "get_user_keys", new=AsyncMock(side_effect=[[first], [updated]])), \
             patch.object(api_router, "fetch_and_parse_real_nodes", new=AsyncMock(return_value=[])), \
             patch("xui_client.get_xui_devices", new=AsyncMock(side_effect=[
                 {"limit_hwid": 2, "devices": [{"id": 1}]},
                 {"limit_hwid": 5, "devices": [{"id": 1}, {"id": 2}]},
             ])):
            first_response = self.client.get("/api/v1/subscription/status")
            second_response = self.client.get("/api/v1/subscription/status")
        self.assertEqual(first_response.status_code, 200, first_response.text)
        self.assertEqual(second_response.status_code, 200, second_response.text)
        first_body, second_body = first_response.json(), second_response.json()
        self.assertEqual(first_body["status"], "active")
        self.assertEqual(first_body["max_devices"], 2)
        self.assertEqual(first_body["active_devices"], 1)
        self.assertIn("server_time", first_body)
        self.assertGreater(second_body["seconds_remaining"], first_body["seconds_remaining"])
        self.assertEqual(second_body["max_devices"], 5)
        self.assertEqual(second_body["active_devices"], 2)
        self.assertEqual(second_body["plan_code"], "3m_d5")

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
        self.assertEqual(args["extra_metadata"]["mobile_api_checkout"], "1")
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

    def test_legacy_mobile_checkout_is_also_tagged_for_order_bound_fulfillment(self):
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
                "payment_id": "legacy-mobile-invoice", "status": "pending",
            }),
        ) as create_payment:
            response = self.client.post(
                "/api/v1/payment/create",
                json={
                    "plan_code": "1m", "devices": 5,
                    "request_id": "legacy_mobile_123456",
                    "provider": "yookassa",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        metadata = create_payment.await_args.kwargs["extra_metadata"]
        self.assertEqual(metadata["mobile_api_checkout"], "1")
        self.assertNotIn("mobile_v2", metadata)

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
