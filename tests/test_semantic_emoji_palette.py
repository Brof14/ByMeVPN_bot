"""Guard ByMeVPN's audited semantic emoji palette and customer visual flows."""
import os
import unittest
from unittest.mock import patch

from bot_style import ICON_IDS, MINIMALIST_BW_ICON_IDS, themed_button, normalize_navigation
from bot_assets import display_artwork, MENU, PLANS, KEY, CONNECTION, ABOUT, REFERRALS, AD_PROGRAM
from keyboards import (
    main_menu_active, app_open_kb, key_detail_kb,
    tariff_selection_kb, after_key_kb, payment_kb,
)


def all_buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def by_callback(markup):
    return {b.callback_data: b for b in all_buttons(markup) if b.callback_data}


class SemanticPremiumEmojiTests(unittest.TestCase):
    def test_every_semantic_role_has_its_own_numeric_catalog_id(self):
        self.assertGreaterEqual(len(ICON_IDS), 40)
        self.assertEqual(len(ICON_IDS), len(set(ICON_IDS.values())))
        self.assertTrue(all(id.isdecimal() and len(id) >= 16 for id in ICON_IDS.values()))
        self.assertGreater(
            sum(icon_id in MINIMALIST_BW_ICON_IDS for icon_id in ICON_IDS.values()) / len(ICON_IDS),
            0.70,
        )

    def test_subscription_about_and_tariff_buttons_use_the_new_catalog_icons(self):
        self.assertEqual(ICON_IDS["subscription"], "6005570495603282482")
        self.assertEqual(ICON_IDS["about"], "5879785854284599288")
        self.assertEqual(ICON_IDS["plans"], "5983399041197675256")

    def test_current_connection_actions_have_semantic_icons(self):
        roles = {
            "key_instructions:42": "connect",
            "key_devices:42": "devices",
            "key_renew:42": "renew",
            "manage_payment_methods:42": "card",
        }
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            for callback, role in roles.items():
                button = themed_button(text="Действие", callback_data=callback)
                self.assertEqual(button.icon_custom_emoji_id, ICON_IDS[role], callback)
        self.assertEqual(len({ICON_IDS[x] for x in ("ios", "android", "windows", "linux")}), 4)

    def test_customer_platform_keyboard_has_the_right_icons(self):
        from app_links import render_connection_choice_page
        page = render_connection_choice_page("https://panel.example/sub/test")
        for platform in ("ios", "android", "windows", "linux", "macos"):
            self.assertIn(platform, page)
        self.assertEqual(len({ICON_IDS[x] for x in ("ios", "android", "windows", "linux")}), 4)

    def test_tariff_icons_and_bonus_labels_match_each_term(self):
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            for devices in (2, 5, 10):
                marked = by_callback(tariff_selection_kb(devices=devices))
                icons = []
                for months in (1, 3, 6, 12):
                    action = f"tariff_{months}_{devices}"
                    self.assertIn(action, marked)
                    icons.append(marked[action].icon_custom_emoji_id)
                self.assertEqual(icons[0], ICON_IDS["tariff_1"])
                self.assertEqual(icons[1:], [ICON_IDS["gift"]] * 3)
                self.assertEqual(
                    [marked[f"tariff_{m}_{devices}"].style for m in (1, 3, 6, 12)],
                    ["primary", "primary", "primary", "primary"],
                )
                self.assertIn("₽", marked[f"tariff_3_{devices}"].text)
                self.assertNotIn("🎁", marked[f"tariff_3_{devices}"].text)

    def test_payment_methods_have_separate_branded_icons(self):
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            marked = by_callback(payment_kb(316, 120))
        for cb, role in (("pay_stars", "stars"), ("pay_yookassa", "card"), ("pay_crypto", "crypto")):
            self.assertEqual(marked[cb].icon_custom_emoji_id, ICON_IDS[role])
        self.assertEqual(len({marked[cb].icon_custom_emoji_id for cb in ("pay_stars", "pay_yookassa", "pay_crypto")}), 3)

    def test_active_customer_actions_are_colored(self):
        kb = main_menu_active(42)
        buttons = all_buttons(kb)
        actions = [b.callback_data for b in buttons]
        self.assertEqual(buttons[0].text, "Подключить VPN")
        self.assertEqual(actions[0], "key_instructions:42")
        self.assertIn("my_keys", actions)
        self.assertTrue(all(b.style in {"primary", "success", "danger"} for b in buttons))
        self.assertTrue(kb.inline_keyboard[-1][-1].url.startswith("https://t.me/ByMeVPNSupportBot"))

    def test_explicit_none_still_receives_a_visible_color(self):
        button = themed_button(text="Тарифы", callback_data="buy_vpn", style=None)
        self.assertEqual(button.style, "primary")

    def test_delivery_and_platform_guide_prioritize_connection(self):
        with (patch("app_links.APP_CONNECT_PAGE_URL", "https://example.test"),
              patch("app_links.BOT_TOKEN", "test-bot-token")):
            kb = after_key_kb("https://panel.example/sub/42")
            detail = key_detail_kb(42, subscription_url="https://panel.example/sub/42")
        primary = all_buttons(kb)[0]
        self.assertIsNone(primary.callback_data)
        self.assertIn("/connect?ticket=", primary.url)
        self.assertIn("/connect?ticket=", all_buttons(detail)[0].url)
        self.assertNotIn("panel.example", primary.url)

    def test_copy_button_has_only_telegram_copy_glyph_without_duplicate_custom_icon(self):
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            markup = app_open_kb("https://example.test/app", "happ://add/safe-short-link")
        copy_button = next(button for button in all_buttons(markup) if button.copy_text)
        self.assertEqual(copy_button.text, "Скопировать ссылку входа")
        self.assertEqual(copy_button.icon_custom_emoji_id, ICON_IDS["copy"])

    def test_banners_only_for_key_landmarks(self):
        active = {MENU, PLANS, KEY, CONNECTION, ABOUT, REFERRALS, AD_PROGRAM}
        self.assertTrue(all(display_artwork(path) == path for path in active))


if __name__ == "__main__":
    unittest.main()
