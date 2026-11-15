"""Post-audit customer recovery and billing-status regression contracts."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class CheckoutRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_duplicate_access_prevents_new_checkout(self):
        import time
        from handlers.buy import _block_unresolved_access_payment
        now = int(time.time())
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=12021),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        with (
            patch("database.get_user_keys", new=AsyncMock(return_value=[
                {"id": 1, "expiry": now + 1000},
                {"id": 2, "expiry": now + 1000},
            ])),
            patch("billing_incidents.has_unresolved_access_delivery", new=AsyncMock()) as checker,
        ):
            self.assertTrue(await _block_unresolved_access_payment(callback))
        checker.assert_not_awaited()
        self.assertIn("Не оплачивайте повторно", callback.message.answer.await_args.args[0])

    async def test_archived_renewal_cannot_override_different_live_key(self):
        import time
        from handlers.keys import cb_key_renew
        now = int(time.time())
        callback = SimpleNamespace(
            data="key_renew:77",
            from_user=SimpleNamespace(id=12021),
        )
        state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
        archived = {"id": 77, "user_id": 12021, "expiry": now - 86400, "limit_ip": 2}
        live = {"id": 88, "user_id": 12021, "expiry": now + 86400, "limit_ip": 5}
        with (
            patch("handlers.keys.safe_answer", new=AsyncMock()) as answer,
            patch("handlers.keys.get_key_by_id", new=AsyncMock(return_value=archived)),
            patch("handlers.keys.get_user_keys", new=AsyncMock(return_value=[archived, live])),
        ):
            await cb_key_renew(callback, object(), state)
        state.update_data.assert_not_awaited()
        self.assertIn("Другая подписка уже активна", answer.await_args.args[1])

    async def test_pending_yookassa_payment_can_be_checked_without_creating_another_charge(self):
        from handlers.buy import cb_checkout_check_payment
        callback = SimpleNamespace(from_user=SimpleNamespace(id=12021))
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("database.get_yookassa_pending_by_user", new=AsyncMock(return_value={"payment_id": "safe-test-id"})),
            patch("payments.get_yookassa_payment_status", new=AsyncMock(return_value="succeeded")) as lookup,
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_checkout_check_payment(callback, object())
        lookup.assert_awaited_once_with("safe-test-id")
        message = send.await_args.args[2]
        self.assertIn("Оплата получена", message)
        self.assertIn("Повторно платить не нужно", message)
        actions = [
            (button.callback_data, button.url) for row in send.await_args.args[3].inline_keyboard
            for button in row
        ]
        self.assertIn(("my_keys", None), actions)
        self.assertIn(("checkout_check_payment", None), actions)
        self.assertFalse(any(action in {"pay_yookassa", "pay_crypto", "pay_stars"} for action, _ in actions))

    async def test_unknown_provider_status_fails_closed(self):
        from handlers.buy import cb_checkout_check_payment
        callback = SimpleNamespace(from_user=SimpleNamespace(id=12021))
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("database.get_yookassa_pending_by_user", new=AsyncMock(return_value={"payment_id": "safe-test-id"})),
            patch("payments.get_yookassa_payment_status", new=AsyncMock(return_value=None)),
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_checkout_check_payment(callback, object())
        self.assertIn("Статус оплаты временно недоступен", send.await_args.args[2])
        self.assertIn("Не оплачивайте повторно", send.await_args.args[2])

    async def test_missing_pending_invoice_does_not_infer_success(self):
        from handlers.buy import cb_checkout_check_payment
        callback = SimpleNamespace(from_user=SimpleNamespace(id=12021))
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("database.get_yookassa_pending_by_user", new=AsyncMock(return_value=None)),
            patch("payments.get_yookassa_payment_status", new=AsyncMock()) as lookup,
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_checkout_check_payment(callback, object())
        lookup.assert_not_awaited()
        self.assertIn("Платёж для проверки не найден", send.await_args.args[2])


if __name__ == "__main__":
    unittest.main()
