"""HTTP contract tests for the additive adapter; providers use mocks only."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import mobile_v2_legacy as legacy


class FakeDatabase:
    def __init__(self, path):
        self.path = path
        self.DB_FILE = path
        self.connection = None

    async def get_db(self):
        if self.connection is None:
            self.connection = await aiosqlite.connect(self.path)
            self.connection.row_factory = aiosqlite.Row
        return self.connection

    async def add_yookassa_pending(self, *args):
        return None

    async def add_crypto_pending(self, *args):
        return None


class LegacyMobileV2HttpTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.db = FakeDatabase(self.path)
        self.runtime = (self.db, SimpleNamespace())
        self.user_id = 1001
        self.app = FastAPI()
        self.app.include_router(legacy.create_router(), prefix="/api/v1/mobile/v2")
        self.app.dependency_overrides[legacy.current_user_id] = lambda: self.user_id

        @self.app.post("/test-hooks/yookassa")
        async def yookassa_hook(request: Request):
            payment = await request.json()
            allowed = await legacy.validate_yookassa_mobile_payment(payment)
            if allowed and payment.get("status") == "succeeded":
                await legacy.mark_mobile_order_fulfilled("yookassa", payment.get("id"))
            return {"allowed": allowed}

        @self.app.post("/test-hooks/crypto")
        async def crypto_hook(request: Request):
            invoice = await request.json()
            allowed = await legacy.validate_crypto_mobile_invoice(invoice)
            if allowed and invoice.get("status") == "paid":
                await legacy.mark_mobile_order_fulfilled("crypto", invoice.get("invoice_id"))
            return {"allowed": allowed}

        self.client = TestClient(self.app)
        self.guard = patch.dict(os.environ, {"MOBILE_V2_WEBHOOK_GUARD_ENABLED": "1"})
        self.guard.start()
        self.runtime_patch = patch.object(legacy, "_runtime", return_value=self.runtime)
        self.runtime_patch.start()

    def tearDown(self):
        self.guard.stop()
        self.runtime_patch.stop()
        if self.db.connection:
            import asyncio
            asyncio.run(self.db.connection.close())
        os.unlink(self.path)

    def test_catalog_returns_authoritative_prices_days_and_device_tiers(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOBILE_V2_WEBHOOK_GUARD_ENABLED", None)
            response = self.client.get("/api/v1/mobile/v2/plans")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["payment_enabled"])
        self.assertFalse(response.json()["purchase_enabled"])
        plans = response.json()["plans"]
        expected = {
            2: (89, 316, 552, 885),
            5: (109, 396, 712, 1185),
            10: (129, 476, 872, 1485),
        }
        for devices, prices in expected.items():
            for index, period in enumerate(("1m", "3m", "6m", "12m")):
                option = plans[index]["device_options"][(2, 5, 10).index(devices)]
                self.assertEqual(option, {"plan_code": f"{period}_d{devices}", "devices": devices,
                                          "price_rub": prices[index],
                                          "days": (30, 120, 240, 450)[index]})
        self.assertTrue(all(plan["purchase_enabled"] is False for plan in plans))

        selected = self.client.get("/api/v1/mobile/v2/plans?devices=10").json()["plans"]
        self.assertEqual([(p["code"], p["devices"]) for p in selected],
                         [(f"{period}_d10", 10) for period in ("1m", "3m", "6m", "12m")])

    def test_create_is_idempotent_and_client_cannot_override_price_or_limit(self):
        fake_create = AsyncMock(return_value={"id": "yk-test-1", "url": "https://pay.invalid/1"})
        with patch.object(legacy, "_create_yookassa", fake_create):
            body = {"plan_code": "1m_d5", "request_id": "req_12345678", "provider": "yookassa"}
            first = self.client.post("/api/v1/mobile/v2/payment/create", json=body)
            second = self.client.post("/api/v1/mobile/v2/payment/create", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(fake_create.await_count, 1)
        self.assertEqual(fake_create.await_args.args[1]["price_rub"], 109)
        self.assertEqual(fake_create.await_args.args[1]["days"], 30)
        self.assertEqual(fake_create.await_args.args[1]["devices"], 5)
        conflicting_reuse = self.client.post("/api/v1/mobile/v2/payment/create", json={
            "plan_code": "3m_d5", "request_id": "req_12345678", "provider": "yookassa"})
        self.assertEqual(conflicting_reuse.status_code, 409)
        row = self.client.get("/api/v1/mobile/v2/plans").json()["plans"][0]
        tier = row["device_options"][1]
        self.assertEqual((tier["price_rub"], tier["days"], tier["devices"]), (109, 30, 5))
        tampered = self.client.post("/api/v1/mobile/v2/payment/create", json={
            **body, "request_id": "req_tamper123", "amount_rub": 1, "days": 1, "devices": 10})
        self.assertEqual(tampered.status_code, 422)

    def test_invoice_check_is_owner_scoped(self):
        fake_create = AsyncMock(return_value={"id": "yk-owner-1", "url": "https://pay.invalid/owner"})
        with patch.object(legacy, "_create_yookassa", fake_create):
            response = self.client.post("/api/v1/mobile/v2/payment/create", json={
                "plan_code": "1m_d2", "request_id": "owner_12345678", "provider": "yookassa"})
        self.assertEqual(response.status_code, 200)
        self.user_id = 2002
        check = self.client.get("/api/v1/mobile/v2/payment/check",
                                params={"invoice_id": "yk-owner-1", "provider": "yookassa"})
        self.assertEqual(check.status_code, 404)

    def test_yookassa_requires_confirmed_matching_webhook_and_duplicate_finalize_is_idempotent(self):
        fake_create = AsyncMock(return_value={"id": "yk-hook-1", "url": "https://pay.invalid/hook"})
        with patch.object(legacy, "_create_yookassa", fake_create):
            created = self.client.post("/api/v1/mobile/v2/payment/create", json={
                "plan_code": "1m_d5", "request_id": "hook_12345678", "provider": "yookassa"})
        self.assertEqual(created.status_code, 200)
        base_payment = {"id": "yk-hook-1", "status": "pending", "amount": {"value": "109.00", "currency": "RUB"},
                        "metadata": {"mobile_v2": "1", "mobile_order": __import__("hashlib").sha256(b"1001:hook_12345678").hexdigest()[:24],
                                     "user_id": "1001", "days": "30", "devices": "5",
                                     "plan_code": "1m_d5", "months": "1"}}
        unconfirmed = self.client.post("/test-hooks/yookassa", json=base_payment)
        self.assertEqual(unconfirmed.status_code, 200, unconfirmed.text)
        self.assertFalse(unconfirmed.json()["allowed"])
        base_payment["status"] = "succeeded"
        altered = dict(base_payment, amount={"value": "1.00", "currency": "RUB"})
        self.assertFalse(self.client.post("/test-hooks/yookassa", json=altered).json()["allowed"])
        self.assertTrue(self.client.post("/test-hooks/yookassa", json=base_payment).json()["allowed"])
        self.assertTrue(self.client.post("/test-hooks/yookassa", json=base_payment).json()["allowed"])
        check = self.client.get("/api/v1/mobile/v2/payment/check",
                                params={"invoice_id": "yk-hook-1", "provider": "yookassa"})
        self.assertEqual(check.json()["status"], "succeeded")
        self.assertTrue(check.json()["is_active"])

    def test_yookassa_early_webhook_binds_order_before_provider_response(self):
        async def provider_create(config, plan, user_id, description, order_hash):
            payment = {"id": "yk-early", "status": "succeeded",
                       "amount": {"value": "89.00", "currency": "RUB"},
                       "metadata": {"mobile_v2": "1", "mobile_order": order_hash,
                                    "user_id": str(user_id), "days": "30", "devices": "2",
                                    "plan_code": "1m_d2", "months": "1"}}
            # Emulate the provider webhook arriving while create() is still in flight.
            self.assertTrue(await legacy.validate_yookassa_mobile_payment(payment))
            await legacy.mark_mobile_order_fulfilled("yookassa", payment["id"])
            return {"id": payment["id"], "url": "https://pay.invalid/early"}
        with patch.object(legacy, "_create_yookassa", provider_create):
            response = self.client.post("/api/v1/mobile/v2/payment/create", json={
                "plan_code": "1m_d2", "request_id": "early_yk_12345", "provider": "yookassa"})
        self.assertEqual(response.status_code, 200, response.text)
        check = self.client.get("/api/v1/mobile/v2/payment/check",
                                params={"invoice_id": "yk-early", "provider": "yookassa"})
        self.assertTrue(check.json()["is_active"])

    def test_crypto_early_webhook_binds_order_and_duplicate_is_safe(self):
        async def provider_create(config, plan, description, payload):
            invoice = {"invoice_id": "crypto-early", "payload": payload, "status": "paid",
                       "amount": "89", "currency_type": "fiat", "fiat": "RUB"}
            self.assertTrue(await legacy.validate_crypto_mobile_invoice(invoice))
            await legacy.mark_mobile_order_fulfilled("crypto", invoice["invoice_id"])
            return ("https://pay.invalid/crypto-early", "crypto-early")
        with patch.object(legacy, "_create_crypto", provider_create):
            response = self.client.post("/api/v1/mobile/v2/payment/create", json={
                "plan_code": "1m_d2", "request_id": "early_crypto_123", "provider": "crypto"})
        self.assertEqual(response.status_code, 200, response.text)
        invoice = {"invoice_id": "crypto-early", "payload": "bmv2_" +
                   __import__("hashlib").sha256(b"1001:early_crypto_123").hexdigest()[:24],
                   "status": "paid", "amount": "89", "currency_type": "fiat", "fiat": "RUB"}
        self.assertTrue(self.client.post("/test-hooks/crypto", json=invoice).json()["allowed"])
        self.assertTrue(self.client.post("/test-hooks/crypto", json=invoice).json()["allowed"])
        check = self.client.get("/api/v1/mobile/v2/payment/check",
                                params={"invoice_id": "crypto-early", "provider": "crypto"})
        self.assertTrue(check.json()["is_active"])

    def test_uncertain_provider_create_can_recover_without_new_order(self):
        fake_create = AsyncMock(side_effect=[None, {"id": "yk-recovered", "url": "https://pay.invalid/recovered"}])
        body = {"plan_code": "1m_d2", "request_id": "uncertain_12345", "provider": "yookassa"}
        with patch.object(legacy, "_create_yookassa", fake_create):
            first = self.client.post("/api/v1/mobile/v2/payment/create", json=body)
            second = self.client.post("/api/v1/mobile/v2/payment/create", json=body)
        self.assertEqual(first.status_code, 502)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(fake_create.await_count, 2)
        self.assertEqual(fake_create.await_args_list[0].args[-1], fake_create.await_args_list[1].args[-1])
        self.assertEqual(second.json()["invoice_id"], "yk-recovered")

        crypto_create = AsyncMock(return_value=None)
        crypto_body = {"plan_code": "1m_d2", "request_id": "uncertain_crypto_123",
                       "provider": "crypto"}
        with patch.object(legacy, "_create_crypto", crypto_create):
            failed = self.client.post("/api/v1/mobile/v2/payment/create", json=crypto_body)
        self.assertEqual(failed.status_code, 502)
        recovered = {"invoice_id": "crypto-recovered", "payload": "bmv2_" +
                     __import__("hashlib").sha256(b"1001:uncertain_crypto_123").hexdigest()[:24],
                     "bot_invoice_url": "https://pay.invalid/recovered"}
        with patch("payments.find_crypto_invoice_by_payload", new=AsyncMock(return_value=(True, recovered))):
            retry = self.client.post("/api/v1/mobile/v2/payment/create", json=crypto_body)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["invoice_id"], "crypto-recovered")
        self.assertEqual(crypto_create.await_count, 1)

    def test_webhook_validation_closes_connections_on_all_rejection_paths(self):
        class TrackedConnection:
            def __init__(self, connection):
                self.connection = connection
                self.closed = False
            def __getattr__(self, name):
                return getattr(self.connection, name)
            async def close(self):
                self.closed = True
                await self.connection.close()

        tracked = []
        real_db_factory = legacy._db
        async def tracked_db():
            connection = TrackedConnection(await real_db_factory())
            tracked.append(connection)
            return connection
        with patch.object(legacy, "_create_yookassa", AsyncMock(return_value={
                "id": "yk-close", "url": "https://pay.invalid/close"})), \
             patch.object(legacy, "_create_crypto", AsyncMock(return_value=(
                "https://pay.invalid/close-crypto", "crypto-close"))):
            self.client.post("/api/v1/mobile/v2/payment/create", json={
                "plan_code": "1m_d2", "request_id": "close_yk_12345", "provider": "yookassa"})
            self.client.post("/api/v1/mobile/v2/payment/create", json={
                "plan_code": "1m_d2", "request_id": "close_crypto_123", "provider": "crypto"})
        with patch.object(legacy, "_db", tracked_db):
            # HTTP test hooks exercise the same validators used by the runtime.
            self.assertFalse(self.client.post("/test-hooks/yookassa", json={
                "id": "orphan", "status": "succeeded", "amount": {"value": "1", "currency": "RUB"},
                "metadata": {"mobile_v2": "1"}}).json()["allowed"])
            self.assertFalse(self.client.post("/test-hooks/crypto", json={
                "invoice_id": "orphan", "payload": "bmv2_missing", "status": "paid",
                "amount": "1", "currency_type": "fiat", "fiat": "RUB"}).json()["allowed"])
            self.assertFalse(self.client.post("/test-hooks/yookassa", json={
                "id": "yk-close", "status": "pending",
                "amount": {"value": "89.00", "currency": "RUB"},
                "metadata": {"mobile_v2": "1", "mobile_order":
                    __import__("hashlib").sha256(b"1001:close_yk_12345").hexdigest()[:24],
                    "user_id": "1001", "days": "30", "devices": "2", "plan_code": "1m_d2",
                    "months": "1"}}).json()["allowed"])
            self.assertFalse(self.client.post("/test-hooks/crypto", json={
                "invoice_id": "crypto-close", "payload": "bmv2_" +
                    __import__("hashlib").sha256(b"1001:close_crypto_123").hexdigest()[:24],
                "status": "active", "amount": "89", "currency_type": "fiat", "fiat": "RUB"
            }).json()["allowed"])
        self.assertGreaterEqual(len(tracked), 2)
        self.assertTrue(all(connection.closed for connection in tracked))

    def test_crypto_invoice_requires_linked_opaque_payload_and_paid_status(self):
        fake_create = AsyncMock(return_value=("https://pay.invalid/crypto", "crypto-71"))
        with patch.object(legacy, "_create_crypto", fake_create):
            created = self.client.post("/api/v1/mobile/v2/payment/create", json={
                "plan_code": "3m_d10", "request_id": "crypto_12345678", "provider": "crypto"})
        self.assertEqual(created.status_code, 200, created.text)
        payload = "bmv2_" + __import__("hashlib").sha256(b"1001:crypto_12345678").hexdigest()[:24]
        invoice = {"invoice_id": "crypto-71", "payload": payload, "status": "active",
                   "amount": "476", "currency_type": "fiat", "fiat": "RUB"}
        self.assertFalse(self.client.post("/test-hooks/crypto", json=invoice).json()["allowed"])
        invoice["status"] = "paid"
        bad_amount = dict(invoice, amount="1")
        self.assertFalse(self.client.post("/test-hooks/crypto", json=bad_amount).json()["allowed"])
        self.assertTrue(self.client.post("/test-hooks/crypto", json=invoice).json()["allowed"])
        self.assertTrue(self.client.post("/test-hooks/crypto", json=invoice).json()["allowed"])
        check = self.client.get("/api/v1/mobile/v2/payment/check",
                                params={"invoice_id": "crypto-71", "provider": "crypto"})
        self.assertTrue(check.json()["is_active"])

    def test_payment_creation_is_closed_until_webhook_guard_flag_is_enabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOBILE_V2_WEBHOOK_GUARD_ENABLED", None)
            response = self.client.post("/api/v1/mobile/v2/payment/create", json={
                "plan_code": "1m_d2", "request_id": "closed_12345678", "provider": "yookassa"})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
