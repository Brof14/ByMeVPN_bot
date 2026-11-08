"""Plain-message promo entry from /promo."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class PromoCommandFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_bare_command_prompts_for_plain_code(self):
        from handlers.buy import cmd_promo
        from states import PromoFlow

        state = SimpleNamespace(set_state=AsyncMock())
        message = SimpleNamespace(
            text="/promo", bot=object(), from_user=SimpleNamespace(id=123), answer=AsyncMock(),
        )
        with patch("handlers.buy.send_with_photo", new=AsyncMock()) as send:
            await cmd_promo(message, state)
        state.set_state.assert_awaited_once_with(PromoFlow.waiting_code)
        self.assertIn("следующим сообщением", send.await_args.args[2])

    async def test_plain_code_is_validated_and_applied_without_command_prefix(self):
        from handlers.buy import receive_promo_code
        from states import PromoFlow

        state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
        message = SimpleNamespace(
            text=" save20 ", bot=object(), from_user=SimpleNamespace(id=123), answer=AsyncMock(),
        )
        promo = {"code": "SAVE20", "promo_type": "percent", "discount_value": 20}
        with (
            patch("handlers.buy.validate_promo_code", new=AsyncMock(return_value=promo)) as validate,
            patch("handlers.buy.has_user_used_promo", new=AsyncMock(return_value=False)),
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
        ):
            await receive_promo_code(message, state)

        validate.assert_awaited_once_with("SAVE20")
        state.update_data.assert_awaited_once_with(
            promo_info=promo, promo_code="SAVE20", promo_discount=20,
        )
        state.set_state.assert_awaited_once_with(None)
        self.assertIn("Промокод применён", send.await_args.args[2])

    async def test_invalid_code_keeps_entry_open_for_retry(self):
        from handlers.buy import receive_promo_code

        state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
        message = SimpleNamespace(
            text="NOPE", bot=object(), from_user=SimpleNamespace(id=123), answer=AsyncMock(),
        )
        with (
            patch("handlers.buy.validate_promo_code", new=AsyncMock(return_value=None)),
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
        ):
            await receive_promo_code(message, state)
        self.assertIn("Не удалось применить", send.await_args.args[2])
        state.set_state.assert_not_awaited()

    async def test_cancel_leaves_promo_state(self):
        from handlers.buy import receive_promo_code

        state = SimpleNamespace(set_state=AsyncMock())
        message = SimpleNamespace(
            text="/cancel", bot=object(), from_user=SimpleNamespace(id=123), answer=AsyncMock(),
        )
        await receive_promo_code(message, state)
        state.set_state.assert_awaited_once_with(None)
        message.answer.assert_awaited_once()
