"""Regression contracts for the V3 Telegram button design."""
import os
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

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
        self.assertEqual(by_callback["my_keys"].icon_custom_emoji_id, "6019290828759898301")
        self.assertEqual(by_callback["buy_vpn"].icon_custom_emoji_id, "6030573613357209704")
        self.assertEqual(by_callback["free_vpn"].icon_custom_emoji_id, "6023826881160157558")

    def test_support_and_about_are_available_and_colored(self):
        by_callback = {button.callback_data: button for button in buttons(main_menu_with_keys())}
        self.assertIn("about", by_callback)
        self.assertEqual(by_callback["about"].style, "primary")
        self.assertEqual(by_callback["trouble"].style, "primary")
        self.assertEqual(by_callback["my_keys"].style, "primary")

    def test_trial_and_destructive_actions_are_distinct(self):
        trial = buttons(main_menu_new_user())[0]
        self.assertEqual(trial.callback_data, "trial_free")
        self.assertEqual(trial.style, "success")
        delete = themed_button(text="Удалить", callback_data="key_device_delete:5:10")
        back = themed_button(text="Назад", callback_data="back_to_menu")
        self.assertEqual(delete.style, "danger")
        self.assertEqual(back.style, "danger")

    def test_legacy_back_buttons_are_red(self):
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            for title in ("🔙 Главное меню", "🏠 Главное меню", "← Назад", "◀️ Назад"):
                button = themed_button(text=title, callback_data="admin_menu")
                self.assertEqual(button.style, "danger")
                self.assertEqual(button.icon_custom_emoji_id, "5805509901048356965")
                self.assertNotIn("Главное менюГлавное меню", button.text)

    def test_custom_icons_can_be_disabled_without_changing_callbacks(self):
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "0"}):
            off = main_menu_with_keys()
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            on = main_menu_with_keys()
        self.assertEqual([b.callback_data for b in buttons(off)], [b.callback_data for b in buttons(on)])
        self.assertTrue(all(b.icon_custom_emoji_id is None for b in buttons(off)))
        self.assertTrue(any(b.icon_custom_emoji_id for b in buttons(on)))

    async def test_help_topics_cover_payments_and_devices(self):
        from handlers.guide import cb_support_topic
        for topic in ("billing", "devices"):
            callback = SimpleNamespace(data=f"support_topic:{topic}")
            with (
                patch("handlers.guide.safe_answer", new=AsyncMock()),
                patch("handlers.guide.send_with_photo", new=AsyncMock()) as send,
            ):
                await cb_support_topic(callback, object())
            text = send.await_args.args[2]
            self.assertIn("Подписка", text)
            actions = [b.callback_data for row in send.await_args.args[3].inline_keyboard for b in row]
            self.assertIn("my_keys", actions)
            self.assertIn("trouble", actions)

    def test_existing_tariffs_and_key_actions_remain_intact(self):
        tariffs = [b.callback_data for b in buttons(tariff_selection_kb(devices=5))]
        self.assertEqual([s for s in tariffs if s and s.startswith("devtier_")], ["devtier_2","devtier_5","devtier_10"])
        self.assertEqual([s for s in tariffs if s and s.startswith("tariff_")], ["tariff_1_5","tariff_3_5","tariff_6_5","tariff_12_5"])
        with patch("app_links.BOT_TOKEN", "test-bot-token"):
            key = buttons(key_detail_kb(42, subscription_url="https://example.com/sub/42"))
        self.assertTrue(any(b.url and "/connect?ticket=" in b.url for b in key))
        self.assertIn("key_renew:42", [b.callback_data for b in key])
        device = [b.callback_data for b in buttons(key_device_detail_kb(42,3))]
        self.assertIn("key_device_delete_prompt:42:3",device)


if __name__ == "__main__":
    unittest.main()
