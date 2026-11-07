"""Customer-facing loss-of-access confirmation regression tests."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from keyboards import confirm_delete_kb


class DeleteAccessUXTests(unittest.IsolatedAsyncioTestCase):
    def test_delete_and_cancel_billing_are_not_the_same_control(self):
        keyboard = confirm_delete_kb(42)
        by_callback = {b.callback_data: b for row in keyboard.inline_keyboard for b in row}
        self.assertEqual(
            [row[0].callback_data for row in keyboard.inline_keyboard],
            ["key_delete_confirm:42", "manage_payment_methods:42", "key_info:42"],
        )
        self.assertEqual(by_callback["key_delete_confirm:42"].style, "danger")
        self.assertEqual(by_callback["key_info:42"].style, "primary")

    async def test_active_key_warns_that_remaining_term_is_lost(self):
        from handlers.keys import cb_key_delete
        callback = SimpleNamespace(data="key_delete:42", from_user=SimpleNamespace(id=100))
        with (
            patch("handlers.keys.safe_answer", new=AsyncMock()),
            patch("handlers.keys.get_key_by_id", new=AsyncMock(return_value={
                "id": 42, "user_id": 100, "expiry": int(time.time()) + 30 * 86400, "remark": "VPN",
            })),
            patch("handlers.keys.send_or_edit", new=AsyncMock()) as send,
        ):
            await cb_key_delete(callback, object())
        text = send.await_args.args[2]
        self.assertIn("ещё активна", text)
        self.assertIn("Удаление сразу отключит VPN", text)
        self.assertIn("отменить будущие списания", text)
        self.assertIn("не сохранит оставшийся срок", text)


if __name__ == "__main__":
    unittest.main()
