"""Reject incorrect Telegram Stars invoices before a customer is charged."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from constants import get_price_for_months
from handlers.buy import pre_checkout


class StarsPreCheckoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = patch("database.is_account_tombstoned", new=AsyncMock(return_value=False))
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoice(self, *, uid=911, months=3, devices=2, days=None,
                amount=None, promo="", currency="XTR"):
        expected_amount, expected_days = get_price_for_months(months, devices)
        payload = f"stars_{uid}_{days if days is not None else expected_days}_{devices}_{months}_{promo}_{int(time.time())}"
        return SimpleNamespace(
            currency=currency,
            total_amount=amount if amount is not None else expected_amount,
            invoice_payload=payload,
            from_user=SimpleNamespace(id=uid),
            answer=AsyncMock(),
        )

    async def test_canonical_invoice_is_authorized(self):
        pre = self.invoice()
        await pre_checkout(pre)
        pre.answer.assert_awaited_once_with(ok=True)

    async def test_deleted_account_invoice_is_rejected(self):
        pre = self.invoice()
        with patch("database.is_account_tombstoned", new=AsyncMock(return_value=True)):
            await pre_checkout(pre)
        self.assertFalse(pre.answer.await_args.kwargs["ok"])
        self.assertIn("удалён", pre.answer.await_args.kwargs["error_message"])

    async def test_account_state_outage_fails_closed(self):
        pre = self.invoice()
        with patch("database.is_account_tombstoned", new=AsyncMock(side_effect=OSError("disk"))):
            await pre_checkout(pre)
        self.assertFalse(pre.answer.await_args.kwargs["ok"])
        self.assertIn("Повторите", pre.answer.await_args.kwargs["error_message"])

    async def test_cross_account_invoice_is_rejected(self):
        pre = self.invoice(uid=11)
        pre.from_user.id = 12
        await pre_checkout(pre)
        self.assertFalse(pre.answer.await_args.kwargs["ok"])

    async def test_invalid_amount_is_rejected_before_charge(self):
        pre = self.invoice(amount=1)
        await pre_checkout(pre)
        self.assertFalse(pre.answer.await_args.kwargs["ok"])

    async def test_invalid_days_are_rejected(self):
        pre = self.invoice(days=99999999)
        await pre_checkout(pre)
        self.assertFalse(pre.answer.await_args.kwargs["ok"])

    async def test_invalid_currency_is_rejected(self):
        pre = self.invoice(currency="RUB")
        await pre_checkout(pre)
        self.assertFalse(pre.answer.await_args.kwargs["ok"])

    async def test_legitimate_discount_invoice_keeps_working(self):
        price, days = get_price_for_months(3, 2)
        pre = self.invoice(amount=max(1, price-10), days=days,
                           promo="SAVED10")
        await pre_checkout(pre)
        pre.answer.assert_awaited_once_with(ok=True)

    async def test_free_days_promo_keeps_working(self):
        price, days = get_price_for_months(3, 2)
        pre = self.invoice(amount=price, days=days+2, promo="BONUS2")
        await pre_checkout(pre)
        pre.answer.assert_awaited_once_with(ok=True)

    async def test_promotional_invoice_cannot_exceed_canonical_price(self):
        price, _ = get_price_for_months(3, 2)
        pre = self.invoice(amount=price+100, promo="FAKE")
        await pre_checkout(pre)
        self.assertFalse(pre.answer.await_args.kwargs["ok"])

    async def test_bad_payload_is_rejected(self):
        pre = self.invoice()
        pre.invoice_payload = "hello"
        await pre_checkout(pre)
        self.assertFalse(pre.answer.await_args.kwargs["ok"])


if __name__ == "__main__":
    unittest.main()
