"""Mobile invoice fulfillment must match its durable server-owned order."""
import os
import secrets
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DB_FILE", "/tmp/test_mobile_webhook_contract.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

import webhook


class TestMobileWebhookContract(unittest.IsolatedAsyncioTestCase):
    async def test_api_checkout_marker_uses_mobile_payment_request_ledger_first(self):
        payment = {
            "id": "api-invoice-test",
            "status": "succeeded",
            "metadata": {"mobile_v2": "1", "mobile_api_checkout": "1"},
        }
        with (
            patch.object(webhook, "_mobile_yookassa_order_matches", new=AsyncMock(return_value=True)) as api_order,
            patch.object(webhook, "_mobile_v2_yookassa_order_matches", new=AsyncMock(return_value=False)) as legacy_order,
        ):
            valid = await webhook._mobile_yookassa_order_matches_for_payment(
                payment, "api-invoice-test", 987654321, 120, 5, 396,
                {"mobile_v2": "1", "mobile_api_checkout": "1", "plan_code": "3m_d5"},
            )
        self.assertTrue(valid)
        api_order.assert_awaited_once_with(
            "api-invoice-test", 987654321, 120, 5, 396,
            {"mobile_v2": "1", "mobile_api_checkout": "1", "plan_code": "3m_d5"},
        )
        legacy_order.assert_not_awaited()
        self.assertFalse(webhook._uses_legacy_mobile_v2_order_ledger(
            {"mobile_v2": "1", "mobile_api_checkout": "1"},
        ))

    async def test_only_legacy_v2_checkout_uses_legacy_validator_and_finalizer(self):
        payment = {"status": "succeeded", "metadata": {"mobile_v2": "1"}}
        with patch.object(
            webhook, "_mobile_v2_yookassa_order_matches", new=AsyncMock(return_value=True),
        ) as legacy_order:
            valid = await webhook._mobile_yookassa_order_matches_for_payment(
                payment, "legacy-invoice", 987654321, 30, 2, 99,
                {"mobile_v2": "1"},
            )
        self.assertTrue(valid)
        legacy_order.assert_awaited_once_with(payment)
        self.assertTrue(webhook._uses_legacy_mobile_v2_order_ledger({"mobile_v2": "1"}))

    async def test_telegram_payment_without_mobile_markers_keeps_legacy_fulfillment(self):
        payment = {"status": "succeeded", "metadata": {"user_id": "987654321"}}
        with (
            patch.object(webhook, "_mobile_yookassa_order_matches", new=AsyncMock()) as api_order,
            patch.object(webhook, "_mobile_v2_yookassa_order_matches", new=AsyncMock()) as legacy_v2_order,
        ):
            valid = await webhook._mobile_yookassa_order_matches_for_payment(
                payment, "telegram-invoice", 987654321, 30, 2, 99,
                payment["metadata"],
            )
        self.assertTrue(valid)
        api_order.assert_not_awaited()
        legacy_v2_order.assert_not_awaited()
        self.assertFalse(webhook._uses_legacy_mobile_v2_order_ledger(payment["metadata"]))

    async def test_modern_api_checkout_still_requires_final_provider_status(self):
        payment = {
            "id": "api-invoice-test",
            "status": "pending",
            "metadata": {"mobile_v2": "1", "mobile_api_checkout": "1"},
        }
        with patch.object(
            webhook, "_mobile_yookassa_order_matches", new=AsyncMock(return_value=True),
        ) as api_order:
            valid = await webhook._mobile_yookassa_order_matches_for_payment(
                payment, "api-invoice-test", 987654321, 120, 5, 396,
                {"mobile_v2": "1", "mobile_api_checkout": "1"},
            )
        self.assertFalse(valid)
        api_order.assert_not_awaited()

    async def test_v2_yookassa_uses_legacy_runtime_order_ledger(self):
        payment = {
            "id": "invoice-v2-test",
            "status": "succeeded",
            "metadata": {"mobile_v2": "1"},
        }
        with patch(
            "mobile_v2_legacy.validate_yookassa_mobile_payment",
            new=AsyncMock(return_value=True),
        ) as validate:
            self.assertTrue(await webhook._mobile_v2_yookassa_order_matches(payment))
        validate.assert_awaited_once_with(payment)

    async def test_v2_yookassa_does_not_validate_unsettled_payment(self):
        payment = {
            "id": "invoice-v2-test",
            "status": "pending",
            "metadata": {"mobile_v2": "1"},
        }
        with patch(
            "mobile_v2_legacy.validate_yookassa_mobile_payment",
            new=AsyncMock(return_value=True),
        ) as validate:
            self.assertFalse(await webhook._mobile_v2_yookassa_order_matches(payment))
        validate.assert_not_awaited()

    async def test_v2_fulfillment_status_requires_matching_order(self):
        with patch(
            "mobile_v2_legacy.mark_mobile_order_fulfilled",
            new=AsyncMock(return_value=False),
        ) as mark:
            self.assertFalse(await webhook._mark_mobile_v2_yookassa_fulfilled("invoice-v2-test"))
        mark.assert_awaited_once_with("yookassa", "invoice-v2-test")

    async def test_yookassa_accepts_only_exact_owner_plan_and_server_terms(self):
        order = {
            "user_id": 987654321, "request_id": "request-test", "plan_code": "3m",
            "provider": "yookassa", "devices": 5, "amount_rub": 396, "payment_id": "invoice-test",
        }
        metadata = {"plan_code": "3m_d5", "amount_rub": "396", "days": "120", "months": "3"}
        with patch(
            "database.get_mobile_payment_request_by_invoice", new=AsyncMock(return_value=order),
        ) as lookup:
            valid = await webhook._mobile_yookassa_order_matches(
                "invoice-test", 987654321, 120, 5, 396,
                metadata,
            )
            wrong_owner = await webhook._mobile_yookassa_order_matches(
                "invoice-test", 123, 120, 5, 396,
                metadata,
            )
            wrong_price = await webhook._mobile_yookassa_order_matches(
                "invoice-test", 987654321, 120, 5, 1,
                metadata,
            )
            wrong_tier = await webhook._mobile_yookassa_order_matches(
                "invoice-test", 987654321, 120, 5, 396,
                {**metadata, "plan_code": "3m_d10"},
            )
        self.assertTrue(valid)
        self.assertFalse(wrong_owner)
        self.assertFalse(wrong_price)
        self.assertFalse(wrong_tier)
        self.assertEqual(lookup.await_count, 4)

    async def test_legacy_mobile_invoice_marker_uses_same_order_guard(self):
        order = {
            "user_id": 987654321, "request_id": "request-test", "plan_code": "1m",
            "provider": "yookassa", "devices": 5, "amount_rub": 109,
            "payment_id": "legacy-mobile-invoice",
        }
        with patch(
            "database.get_mobile_payment_request_by_invoice", new=AsyncMock(return_value=order),
        ) as lookup:
            with patch.object(webhook, "_mobile_v2_yookassa_order_matches", new=AsyncMock(return_value=True)) as legacy:
                self.assertTrue(await webhook._mobile_yookassa_order_matches_for_payment(
                    {"status": "succeeded"}, "legacy-mobile-invoice", 987654321,
                    30, 5, 109, {"mobile_v2": "1"},
                ))
        legacy.assert_awaited_once_with({"status": "succeeded"})
        lookup.assert_not_awaited()

    async def test_crypto_requires_matching_invoice_owner_and_catalog_terms(self):
        order = {
            "user_id": 987654321, "request_id": "request-test", "plan_code": "6m",
            "provider": "crypto", "devices": 10, "amount_rub": 654,
            "payment_id": "crypto-invoice-test",
        }
        pending = {"user_id": 987654321, "days": 240, "devices": 10, "amount_rub": 654}
        with patch(
            "database.get_mobile_payment_request_by_invoice", new=AsyncMock(return_value=order),
        ):
            self.assertTrue(await webhook._mobile_crypto_order_matches("crypto-invoice-test", pending))
            self.assertFalse(await webhook._mobile_crypto_order_matches(
                "crypto-invoice-test", {**pending, "days": 30},
            ))
            self.assertFalse(await webhook._mobile_crypto_order_matches(
                "crypto-invoice-test", {**pending, "user_id": 123},
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
