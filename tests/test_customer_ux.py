"""Regression tests for simplified customer menus and card-management controls."""
import unittest
from unittest.mock import AsyncMock, patch

from keyboards import (
    main_menu_new_user,
    main_menu_existing,
    main_menu_with_keys,
    manage_payment_methods_kb,
    connection_guide_kb,
    back_to_menu,
    tariff_selection_kb,
)
from handlers.keys import _get_payment_methods_text, _load_autorenew_context


def callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


class TestCustomerMenus(unittest.IsolatedAsyncioTestCase):
    def test_users_without_a_subscription_do_not_see_connection_guide_or_account_switch(self):
        for markup in (main_menu_new_user(), main_menu_existing()):
            actions = callbacks(markup)
            self.assertNotIn("connection_guide", actions)
            self.assertNotIn("auth_existing_client", actions)

    def test_main_menu_uses_compact_rows_and_limited_semantic_colors(self):
        for markup in (main_menu_new_user(), main_menu_existing(), main_menu_with_keys()):
            rows = markup.inline_keyboard
            self.assertEqual(len(rows[-1]), 1)  # support stays separate
            self.assertEqual(rows[-1][0].text, "Поддержка")
            self.assertLessEqual(sum(button.style is not None for row in rows for button in row), 2)
        menu = main_menu_with_keys()
        self.assertEqual([len(row) for row in menu.inline_keyboard], [2, 2, 2, 1])
        self.assertEqual(menu.inline_keyboard[0][0].style, "success")
        self.assertEqual(menu.inline_keyboard[0][1].style, "primary")
        used_trial = main_menu_existing(trial_used=True)
        self.assertNotIn("trial_1r", callbacks(used_trial))

    def test_expired_or_active_key_menu_keeps_guide_inside_key_flow(self):
        actions = callbacks(main_menu_with_keys(trial_used=True))
        self.assertNotIn("connection_guide", actions)
        self.assertNotIn("auth_existing_client", actions)
        self.assertIn("ad_program", actions)

    def test_guide_device_selector_uses_compact_two_column_rows(self):
        markup = connection_guide_kb()
        self.assertEqual([len(row) for row in markup.inline_keyboard[:2]], [2, 2])
        actions = callbacks(markup)
        self.assertIn("guide_android_tv", actions)
        self.assertIn("guide_ios", actions)
        self.assertIn("guide_macos", actions)

    def test_back_is_full_width_and_support_is_not_mixed_into_navigation(self):
        back = back_to_menu().inline_keyboard
        self.assertEqual(len(back), 1)
        self.assertEqual(len(back[0]), 1)
        self.assertEqual(back[0][0].callback_data, "back_to_menu")

        for markup in (tariff_selection_kb(), connection_guide_kb()):
            for row in markup.inline_keyboard:
                self.assertFalse(any(button.url for button in row))

    def test_tariff_grid_restores_bonus_marketing_with_one_highlight(self):
        markup = tariff_selection_kb()
        tariff_buttons = [
            button for row in markup.inline_keyboard for button in row
            if button.callback_data and button.callback_data.startswith("tariff_")
        ]
        three_months = next(button for button in tariff_buttons if button.callback_data == "tariff_3_2")
        self.assertIn("3 мес. + 1 мес. 🎁 — 79 ₽ / мес.", three_months.text)
        self.assertEqual(three_months.style, "success")
        self.assertTrue(all(button.style is None for button in tariff_buttons if button is not three_months))

    async def test_saved_sbp_method_without_title_is_still_manageable(self):
        sub = {
            "payment_method_id": "opaque-test-value",
            "payment_method_title": "",
            "payment_method_type": "sbp",
            "status": "active",
            "amount_rub": 89,
            "months": 1,
            "days": 30,
            "devices": 2,
        }
        with (
            patch("database.get_auto_renew_subscription", new=AsyncMock(return_value=sub)),
            patch("database.get_user_keys", new=AsyncMock(return_value=[])),
            patch("handlers.keys.get_key_by_id", new=AsyncMock(return_value={"user_id": 123, "expiry": 1_800_000_000})),
        ):
            loaded, title, _ = await _load_autorenew_context(123, 5)

        self.assertIs(loaded, sub)
        self.assertEqual(title, "Счёт СБП")
        text = _get_payment_methods_text(card_title=title, sub=sub)
        self.assertIn("Автопродление включено", text)
        self.assertIn("Счёт СБП", text)
        actions = callbacks(manage_payment_methods_kb(
            card_title=title,
            key_id=5,
            has_card=bool(sub["payment_method_id"]),
            autorenew_active=True,
        ))
        self.assertIn("autorenew_unbind_prompt:5", actions)

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
