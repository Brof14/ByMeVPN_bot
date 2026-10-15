"""Regression tests for simplified customer menus and card-management controls."""
import unittest

from keyboards import (
    main_menu_new_user,
    main_menu_existing,
    main_menu_with_keys,
    manage_payment_methods_kb,
)


def callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


class TestCustomerMenus(unittest.TestCase):
    def test_users_without_a_subscription_do_not_see_connection_guide_or_account_switch(self):
        for markup in (main_menu_new_user(), main_menu_existing()):
            actions = callbacks(markup)
            self.assertNotIn("connection_guide", actions)
            self.assertNotIn("auth_existing_client", actions)

    def test_expired_or_active_key_menu_keeps_guide_inside_key_flow(self):
        actions = callbacks(main_menu_with_keys(trial_used=True))
        self.assertNotIn("connection_guide", actions)
        self.assertNotIn("auth_existing_client", actions)

    def test_card_controls_distinguish_resume_from_unlink(self):
        active = callbacks(manage_payment_methods_kb(
            card_title="*1234", key_id=5, has_card=True, autorenew_active=True,
        ))
        self.assertIn("autorenew_unbind_prompt:5", active)
        self.assertNotIn("autorenew_enable:5", active)
        self.assertNotIn("card_toggle_check:5:1", active)

        inactive = callbacks(manage_payment_methods_kb(
            card_title="*1234", key_id=5, has_card=True, autorenew_active=False,
        ))
        self.assertIn("autorenew_enable:5", inactive)
        self.assertIn("autorenew_unbind_prompt:5", inactive)


if __name__ == "__main__":
    unittest.main()
