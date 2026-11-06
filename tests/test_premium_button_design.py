"""Regression contracts for the V3 Telegram button design."""
import os
import unittest
from unittest.mock import patch

from bot_style import themed_button
from keyboards import (
    main_menu_with_keys, main_menu_new_user, tariff_selection_kb,
    key_detail_kb, key_device_detail_kb,
)


def buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


class PremiumButtonDesignTests(unittest.TestCase):
    def test_core_buttons_use_primary_style_and_catalog_icons(self):
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            menu = main_menu_with_keys(trial_used=True)
        by_callback = {button.callback_data: button for button in buttons(menu)}
        self.assertEqual(by_callback["my_keys"].style, "primary")
        self.assertEqual(by_callback["buy_vpn"].style, "primary")
        self.assertEqual(by_callback["my_keys"].icon_custom_emoji_id, "6005570495603282482")
        self.assertEqual(by_callback["buy_vpn"].icon_custom_emoji_id, "5927169041595634481")
        self.assertEqual(by_callback["free_vpn"].icon_custom_emoji_id, "5406756500108501710")

    def test_trial_and_destructive_actions_are_distinct(self):
        trial = buttons(main_menu_new_user())[0]
        self.assertEqual(trial.callback_data, "trial_free")
        self.assertEqual(trial.style, "success")
        delete = themed_button(text="Удалить", callback_data="key_device_delete:5:10")
        back = themed_button(text="Назад", callback_data="back_to_menu")
        self.assertEqual(delete.style, "danger")
        self.assertIsNone(back.style)

    def test_custom_icons_can_be_disabled_without_changing_callbacks(self):
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "0"}):
            off = main_menu_with_keys()
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            on = main_menu_with_keys()
        self.assertEqual([b.callback_data for b in buttons(off)], [b.callback_data for b in buttons(on)])
        self.assertTrue(all(b.icon_custom_emoji_id is None for b in buttons(off)))
        self.assertTrue(any(b.icon_custom_emoji_id for b in buttons(on)))

    def test_existing_tariffs_and_key_actions_remain_intact(self):
        tariffs = [b.callback_data for b in buttons(tariff_selection_kb(devices=5))]
        self.assertEqual([s for s in tariffs if s and s.startswith("devtier_")], ["devtier_2","devtier_5","devtier_10"])
        self.assertEqual([s for s in tariffs if s and s.startswith("tariff_")], ["tariff_1_5","tariff_3_5","tariff_6_5","tariff_12_5"])
        key = [b.callback_data for b in buttons(key_detail_kb(42, subscription_url="https://example.com/sub/42"))]
        self.assertIn("key_instructions:42", key)
        self.assertIn("key_renew:42", key)
        device = [b.callback_data for b in buttons(key_device_detail_kb(42,3))]
        self.assertIn("key_device_delete_prompt:42:3",device)


if __name__ == "__main__":
    unittest.main()
