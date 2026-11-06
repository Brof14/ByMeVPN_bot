"""Every customer section uses its matching bundled artwork."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot_assets


class TestSectionArtwork(unittest.IsolatedAsyncioTestCase):
    def test_landmark_only_photo_policy(self):
        for photo in (bot_assets.MENU, bot_assets.ABOUT, bot_assets.KEY):
            self.assertEqual(bot_assets.display_artwork(photo), photo)
        for photo in (bot_assets.PLANS, bot_assets.PAYMENT, bot_assets.CONNECTION, bot_assets.AD_PROGRAM, bot_assets.REFERRALS, bot_assets.SUPPORT):
            self.assertIsNone(bot_assets.display_artwork(photo))


    def test_all_twenty_artworks_are_bundled(self):
        names = (
            "MENU", "SUPPORT", "ABOUT", "FREE_VPN", "REFERRALS", "MY_INVITATIONS",
            "AD_PROGRAM", "PLANS", "KEY", "DEVICES", "CONNECTION", "ANDROID",
            "IPHONE", "WINDOWS", "LINUX", "RENEWAL", "PAYMENT", "PROMO_CODE",
            "LEGAL", "AUTO_RENEWAL",
        )
        self.assertEqual(len(names), 20)
        self.assertEqual(len({Path(getattr(bot_assets, name)).name for name in names}), 20)
        for name in names:
            path = Path(getattr(bot_assets, name))
            self.assertTrue(path.is_file(), name)

    async def test_free_days_hub_uses_its_banner(self):
        from handlers.start import cb_free_vpn

        callback = SimpleNamespace(from_user=SimpleNamespace(id=5))
        with (
            patch("handlers.start.safe_answer", new=AsyncMock()),
            patch("handlers.start.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_free_vpn(callback, object())
        self.assertEqual(send.call_args.kwargs["photo"], bot_assets.FREE_VPN)

    async def test_tariff_screen_uses_plans_banner(self):
        from handlers.buy import cb_buy_vpn

        callback = SimpleNamespace(from_user=SimpleNamespace(id=5))
        state = SimpleNamespace(
            set_state=AsyncMock(), get_data=AsyncMock(return_value={}), update_data=AsyncMock(),
        )
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_buy_vpn(callback, object(), state)
        self.assertEqual(send.call_args.kwargs["photo"], bot_assets.PLANS)

    async def test_platform_guides_use_matching_artwork(self):
        from handlers.guide import _send_platform_guide

        callback = SimpleNamespace(from_user=SimpleNamespace(id=5))
        expected = {
            "ios": bot_assets.IPHONE,
            "android": bot_assets.ANDROID,
            "windows": bot_assets.WINDOWS,
            "linux": bot_assets.LINUX,
            "macos": bot_assets.CONNECTION,
        }
        with (
            patch("handlers.guide.safe_answer", new=AsyncMock()),
            patch("handlers.guide._track_guide_event", new=AsyncMock()),
            patch("handlers.guide.send_or_edit", new=AsyncMock()) as send,
        ):
            for platform, artwork in expected.items():
                await _send_platform_guide(callback, object(), platform)
                self.assertEqual(send.call_args.kwargs["photo"], artwork, platform)

    async def test_ad_program_applies_its_artwork_to_all_screens(self):
        from handlers.ad_program import send_with_photo

        target_bot, target = object(), object()
        with patch("handlers.ad_program._send_with_photo", new=AsyncMock()) as send:
            await send_with_photo(target_bot, target, "screen")
        self.assertEqual(send.call_args.kwargs["photo"], bot_assets.AD_PROGRAM)
