"""Guard ByMeVPN's audited semantic emoji palette and customer visual flows."""
import os
import unittest
from unittest.mock import patch

from bot_style import ICON_IDS, MINIMALIST_BW_ICON_IDS, themed_button, normalize_navigation
from bot_assets import display_artwork, MENU, PLANS, KEY, CONNECTION, ABOUT, REFERRALS, AD_PROGRAM
from keyboards import (
    main_menu_active, connection_guide_kb, additional_platforms_kb,
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

    def test_exact_platform_icons_beat_generic_guide_fallback(self):
        roles = {
            "guide_ios": "ios",
            "guide_android": "android",
            "guide_windows": "windows",
            "guide_linux": "linux",
            "guide_macos": "macos",
            "guide_android_tv": "android_tv",
            "guide_ios_mac": "ipad",
            "guide_linux_ubuntu": "linux",
        }
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            for action, role in roles.items():
                for callback in (action, action + ":42"):
                    button = themed_button(text="Устройство", callback_data=callback)
                    self.assertEqual(button.icon_custom_emoji_id, ICON_IDS[role], callback)
        # iPhone, Android, Windows, Linux must never get a shared generic icon.
        self.assertEqual(len({ICON_IDS[x] for x in ("ios", "android", "windows", "linux")}), 4)

    def test_customer_platform_keyboard_has_the_right_icons(self):
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            primary = by_callback(connection_guide_kb(key_id=31))
            secondary = by_callback(additional_platforms_kb(key_id=31))
        for platform in ("ios", "android", "windows", "linux"):
            self.assertEqual(primary[f"guide_{platform}:31"].icon_custom_emoji_id, ICON_IDS[platform])
        self.assertEqual(secondary["guide_macos:31"].icon_custom_emoji_id, ICON_IDS["macos"])
        self.assertEqual(secondary["guide_android_tv:31"].icon_custom_emoji_id, ICON_IDS["android_tv"])

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
                self.assertIn("₽ всего", marked[f"tariff_3_{devices}"].text)

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
        kb = after_key_kb("https://example.test/sub")
        self.assertEqual(all_buttons(kb)[0].callback_data, "connection_guide")
        guide = connection_guide_kb(subscription_url="https://example.test/sub")
        actions = [b.callback_data for b in all_buttons(guide)]
        self.assertGreater(actions.index("guide_ios"), -1)
        self.assertIsNone(all_buttons(guide)[0].copy_text)
        self.assertTrue(any(b.copy_text for b in all_buttons(guide)))
        self.assertEqual(normalize_navigation(guide).inline_keyboard[-1][0].callback_data, "back_to_menu")

    def test_copy_button_has_only_telegram_copy_glyph_without_duplicate_custom_icon(self):
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            guide = connection_guide_kb(subscription_url="https://example.test/sub")
        copy_button = next(button for button in all_buttons(guide) if button.copy_text)
        self.assertEqual(copy_button.text, "Скопировать ссылку")
        self.assertIsNone(copy_button.icon_custom_emoji_id)

    def test_banners_only_for_key_landmarks(self):
        active = {MENU, PLANS, KEY, CONNECTION, ABOUT, REFERRALS, AD_PROGRAM}
        self.assertTrue(all(display_artwork(path) == path for path in active))


if __name__ == "__main__":
    unittest.main()
