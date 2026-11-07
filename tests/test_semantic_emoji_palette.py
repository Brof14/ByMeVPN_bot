"""Guard ByMeVPN's audited semantic emoji palette and customer visual flows."""
import os
import unittest
from unittest.mock import patch

from bot_style import ICON_IDS, themed_button, normalize_navigation
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

    def test_tariff_icons_use_calendar_without_implying_a_gift(self):
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            for devices in (2, 5, 10):
                marked = by_callback(tariff_selection_kb(devices=devices))
                roles = {1: "tariff_1", 3: "tariff_3", 6: "tariff_6", 12: "tariff_12"}
                icons = []
                for months, role in roles.items():
                    action = f"tariff_{months}_{devices}"
                    self.assertIn(action, marked)
                    icons.append(marked[action].icon_custom_emoji_id)
                    self.assertEqual(marked[action].icon_custom_emoji_id, ICON_IDS["tariff_1"])
                self.assertEqual(len(set(icons)), 1)
                self.assertEqual(
                    [marked[f"tariff_{m}_{devices}"].style for m in (1, 3, 6, 12)],
                    [None, None, "primary", None],
                )
                self.assertTrue(all("🎁" not in marked[f"tariff_{m}_{devices}"].text for m in roles))

    def test_payment_methods_have_separate_branded_icons(self):
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            marked = by_callback(payment_kb(316, 120))
        for cb, role in (("pay_stars", "stars"), ("pay_yookassa", "card"), ("pay_crypto", "crypto")):
            self.assertEqual(marked[cb].icon_custom_emoji_id, ICON_IDS[role])
        self.assertEqual(len({marked[cb].icon_custom_emoji_id for cb in ("pay_stars", "pay_yookassa", "pay_crypto")}), 3)

    def test_active_customer_has_direct_connection_and_only_one_blue_cta(self):
        kb = main_menu_active(42)
        actions = [b.callback_data for b in all_buttons(kb)]
        self.assertEqual(actions[0], "key_instructions:42")
        self.assertIn("my_keys", actions)
        self.assertEqual(sum(b.style == "primary" for b in all_buttons(kb)), 1)
        self.assertEqual(kb.inline_keyboard[-1][-1].callback_data, "trouble")

    def test_delivery_and_platform_guide_prioritize_connection(self):
        kb = after_key_kb("https://example.test/sub")
        self.assertEqual(all_buttons(kb)[0].callback_data, "connection_guide")
        guide = connection_guide_kb(subscription_url="https://example.test/sub")
        actions = [b.callback_data for b in all_buttons(guide)]
        self.assertGreater(actions.index("guide_ios"), -1)
        self.assertIsNone(all_buttons(guide)[0].copy_text)
        self.assertTrue(any(b.copy_text for b in all_buttons(guide)))
        self.assertEqual(normalize_navigation(guide).inline_keyboard[-1][0].callback_data, "back_to_menu")

    def test_banners_only_for_key_landmarks(self):
        active = {MENU, PLANS, KEY, CONNECTION, ABOUT, REFERRALS, AD_PROGRAM}
        self.assertTrue(all(display_artwork(path) == path for path in active))


if __name__ == "__main__":
    unittest.main()
