"""A created YooKassa invoice is not safe to show until polling can recover it."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import payments


class FakeResponse:
    def __init__(self, *, payment_id="yk-test", failure=False):
        self.status_code = 403 if failure else 200
        self.payment_id = payment_id
        self.failure = failure
        self.text = "recurring payments disabled" if failure else ""

    def raise_for_status(self):
        if self.failure:
            request = httpx.Request("POST", "https://api.yookassa.ru/v3/payments")
            response = httpx.Response(403, request=request, text=self.text)
            raise httpx.HTTPStatusError("recurring unsupported", request=request, response=response)

    def json(self):
        return {
            "id": self.payment_id, "status": "pending",
            "confirmation": {"confirmation_url": "https://yookassa.example.test/pay/123"},
        }


class FakeYooKassa:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *_args, **kwargs):
        self.payloads.append(kwargs.get("json"))
        return self.responses.pop(0)


class YooKassaCheckoutDurabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_id_required_before_creating_visible_checkout(self):
        with patch("database.add_yookassa_pending", new=AsyncMock()) as pending:
            self.assertFalse(await payments._remember_yookassa_checkout(None, 7, 30, 2, 89))
            pending.assert_not_awaited()

    async def test_failed_pending_persistence_never_exposes_payment_url(self):
        client = FakeYooKassa([FakeResponse()])
        with (
            patch.object(payments, "YOOKASSA_SHOP_ID", "fake-shop"),
            patch.object(payments, "YOOKASSA_SECRET_KEY", "fake-secret"),
            patch.object(payments, "is_yookassa_recurring_supported", return_value=False),
            patch.object(payments, "yookassa_client", return_value=client),
            patch("database.add_yookassa_pending", new=AsyncMock(side_effect=OSError("disk full"))),
        ):
            result = await payments.create_yookassa_payment(
                89, "test purchase", 7, 30, 2, return_payment_details=True,
            )
        self.assertIsNone(result)

    async def test_successfully_persisted_checkout_is_available(self):
        client = FakeYooKassa([FakeResponse()])
        with (
            patch.object(payments, "YOOKASSA_SHOP_ID", "fake-shop"),
            patch.object(payments, "YOOKASSA_SECRET_KEY", "fake-secret"),
            patch.object(payments, "is_yookassa_recurring_supported", return_value=False),
            patch.object(payments, "yookassa_client", return_value=client),
            patch("database.add_yookassa_pending", new=AsyncMock()) as pending,
        ):
            result = await payments.create_yookassa_payment(
                89, "test purchase", 7, 30, 2, return_payment_details=True,
            )
        self.assertEqual(result["payment_id"], "yk-test")
        self.assertEqual(result["status"], "pending")
        self.assertIn("https://yookassa.example.test/", result["payment_url"])
        pending.assert_awaited_once_with("yk-test", 7, 30, 2, 89)

    def test_one_yookassa_button_with_visible_default_renewal_toggle(self):
        """Current checkout has one recurring YooKassa action (3.2 excluded)."""
        from keyboards import payment_kb
        buttons = [button for row in payment_kb(89, 30).inline_keyboard for button in row]
        callbacks = [button.callback_data for button in buttons]
        self.assertEqual(callbacks.count("pay_yookassa"), 1)
        self.assertNotIn("pay_yookassa_autorenew", callbacks)
        self.assertNotIn("autorenew_checkout_toggle", callbacks)

    async def test_saving_payment_method_requires_explicit_opt_in(self):
        client = FakeYooKassa([FakeResponse(), FakeResponse()])
        with (
            patch.object(payments, "YOOKASSA_SHOP_ID", "fake-shop"),
            patch.object(payments, "YOOKASSA_SECRET_KEY", "fake-secret"),
            patch.object(payments, "yookassa_client", return_value=client),
            patch("database.add_yookassa_pending", new=AsyncMock()),
        ):
            await payments.create_yookassa_payment(89, "one-time", 7, 30, 2)
            await payments.create_yookassa_payment(
                89, "recurring", 7, 30, 2,
                extra_metadata={"auto_renew_consent": "1"},
                save_payment_method=True,
            )
        self.assertNotIn("save_payment_method", client.payloads[0])
        self.assertTrue(client.payloads[1]["save_payment_method"])
        self.assertNotIn("auto_renew_consent", client.payloads[0]["metadata"])
        self.assertEqual(client.payloads[1]["metadata"]["auto_renew_consent"], "1")

    async def test_recurring_fallback_must_persist_before_returning_link(self):
        client = FakeYooKassa([FakeResponse(failure=True), FakeResponse()])
        with (
            patch.object(payments, "YOOKASSA_SHOP_ID", "fake-shop"),
            patch.object(payments, "YOOKASSA_SECRET_KEY", "fake-secret"),
            patch.object(payments, "is_yookassa_recurring_supported", return_value=True),
            patch.object(payments, "_yookassa_recurring_supported", True),
            patch.object(payments, "yookassa_client", return_value=client),
            patch("database.add_yookassa_pending", new=AsyncMock(side_effect=OSError("database unavailable"))),
        ):
            result = await payments.create_yookassa_payment(
                89, "test purchase", 7, 30, 2,
            )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
