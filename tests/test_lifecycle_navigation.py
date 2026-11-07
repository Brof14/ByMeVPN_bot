"""Customer lifecycle and help-path UX: user actions stay one tap away."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from keyboards import main_menu_expired


def actions(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class CustomerNavigationTests(unittest.IsolatedAsyncioTestCase):
    def test_expired_single_key_has_direct_renewal(self):
        markup = main_menu_expired(key_ids=[7])
        self.assertEqual(actions(markup)[0], "key_renew:7")
        self.assertNotIn("trial_free", actions(markup))
        self.assertIn("free_vpn", actions(markup))
        self.assertIn("trouble", actions(markup))

    def test_expired_multiple_keys_choose_subscription_first(self):
        self.assertEqual(actions(main_menu_expired(key_ids=[7, 8]))[0], "my_keys")

    def test_expired_no_key_can_choose_plan(self):
        self.assertEqual(actions(main_menu_expired(key_ids=[]))[0], "buy_vpn")

    async def test_expired_home_picks_latest_key(self):
        from handlers.start import _send_main_menu
        user = SimpleNamespace()
        with (
            patch("handlers.start._user_state", new=AsyncMock(return_value="expired")),
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[
                {"id": 3, "expiry": 100}, {"id": 7, "expiry": 700},
            ])),
            patch("handlers.start.send_with_photo", new=AsyncMock()) as send,
        ):
            await _send_main_menu(object(), user, 123, "Аня")
        keys = actions(send.await_args.args[3])
        self.assertEqual(keys[0], "my_keys")
        self.assertNotIn("trial_free", keys)

    async def test_help_has_direct_connection_and_subscription_paths(self):
        from handlers.guide import cb_support_topic
        cases = {
            "no_connection": "connection_guide",
            "no_internet": "connection_guide",
            "import": "connection_guide",
            "billing": "my_keys",
            "devices": "my_keys",
        }
        for topic, desired in cases.items():
            callback = SimpleNamespace(data="support_topic:" + topic, from_user=SimpleNamespace(id=123))
            with (
                patch("handlers.guide.safe_answer", new=AsyncMock()),
                patch("handlers.guide.send_with_photo", new=AsyncMock()) as send,
            ):
                await cb_support_topic(callback, object())
            keyboard = send.await_args.args[3]
            self.assertIn(desired, actions(keyboard), topic)
            self.assertEqual(actions(keyboard)[-1], "trouble")


class PromotionalControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_optin_preserves_clear_return_to_menu(self):
        from handlers.marketing import cb_marketing_optin
        callback = SimpleNamespace(from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(edit_text=AsyncMock()))
        with (
            patch("handlers.marketing.safe_answer", new=AsyncMock()),
            patch("handlers.marketing.set_promotional_opt_out", new=AsyncMock()),
        ):
            await cb_marketing_optin(callback)
        keyboard = callback.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertIn("marketing_optout", actions(keyboard))
        self.assertEqual(actions(keyboard)[-1], "back_to_menu")


if __name__ == "__main__":
    unittest.main()
