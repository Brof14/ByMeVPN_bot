"""UX contracts for the November 2026 Telegram design refinements."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from keyboards import tariff_selection_kb, payment_kb


def buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


class TariffDesignTests(unittest.TestCase):
    def test_bonus_months_are_explicit_without_unicode_gifts(self):
        keyboard = tariff_selection_kb(devices=2)
        labels = {b.callback_data: b.text for b in buttons(keyboard)}
        self.assertIn("4 мес. (3+1)", labels["tariff_3_2"])
        self.assertIn("8 мес. (6+2)", labels["tariff_6_2"])
        self.assertIn("15 мес. (12+3)", labels["tariff_12_2"])
        self.assertIn("Лучшая цена", labels["tariff_12_2"])
        self.assertTrue(all("🎁" not in text for text in labels.values()))
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            premium = tariff_selection_kb(devices=2)
        bonus_buttons = [b for b in buttons(premium) if b.callback_data and b.callback_data.startswith("tariff_") and not b.callback_data.startswith("tariff_1_")]
        self.assertEqual(
            [b.icon_custom_emoji_id for b in bonus_buttons],
            ["5301299389245985629", "5301163762768707871", "5301192569114366400"],
        )

    def test_crypto_has_no_bitcoin_character(self):
        keyboard = payment_kb(316, 120, devices=2)
        crypto = next(b for b in buttons(keyboard) if b.callback_data == "pay_crypto")
        self.assertEqual(crypto.text, "CryptoBot · 316 ₽")
        self.assertEqual(buttons(keyboard)[-1].text, "← Назад")

    def test_back_is_always_last_separate_row(self):
        from bot_style import normalize_navigation
        from aiogram.types import InlineKeyboardMarkup
        from bot_style import themed_button as Button
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [Button(text="← Назад", callback_data="back_to_menu"), Button(text="Удалить", callback_data="key_delete:3")],
            [Button(text="Тарифы", callback_data="buy_vpn")],
        ])
        normalized = normalize_navigation(markup)
        self.assertEqual([b.callback_data for b in buttons(normalized)], ["key_delete:3", "buy_vpn", "back_to_menu"])
        self.assertEqual(len(normalized.inline_keyboard[-1]), 1)


class AdForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_offer_explains_review_and_personal_link(self):
        from handlers.ad_program import cb_ad_program
        callback = SimpleNamespace(from_user=SimpleNamespace(id=123))
        state = SimpleNamespace(clear=AsyncMock())
        with (
            patch("handlers.ad_program.safe_answer", new=AsyncMock()),
            patch("handlers.ad_program.get_active_ad_campaign", new=AsyncMock(return_value=None)),
            patch("handlers.ad_program.log_ad_notification", new=AsyncMock()),
            patch("handlers.ad_program.log_ad_analytics_event", new=AsyncMock()),
            patch("handlers.ad_program.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_ad_program(callback, object(), state)
        text = send.await_args.args[2]
        self.assertIn("после проверки", text)
        self.assertIn("отдельным сообщением", text)
        self.assertIn("partner", [b.callback_data for b in buttons(send.await_args.args[3])])

    def test_ad_post_link_only_from_official_channel(self):
        from handlers.ad_program import official_ad_post_url
        with patch.dict(os.environ, {"AD_SOURCE_POST_URL": "https://t.me/ByMeVPN/42"}):
            self.assertEqual(official_ad_post_url(), "https://t.me/ByMeVPN/42")
        with patch.dict(os.environ, {"AD_SOURCE_POST_URL": "https://example.org/phish"}):
            self.assertIsNone(official_ad_post_url())

    def test_post_must_belong_to_application_channel(self):
        from handlers.ad_program import valid_ad_post_for_application
        application = {"channel_username": "mychannel"}
        self.assertTrue(valid_ad_post_for_application("https://t.me/mychannel/42", application))
        self.assertFalse(valid_ad_post_for_application("https://t.me/otherchannel/42", application))

    async def test_post_cannot_skip_channel_review(self):
        from handlers.ad_program import msg_ad_post_url
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            text="https://t.me/mychannel/55",
            answer=AsyncMock(),
        )
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={"current_app_id": 7}),
            clear=AsyncMock(),
        )
        app = {"id": 7, "user_id": 123, "status": "channel_pending", "channel_username": "mychannel"}
        with (
            patch("handlers.ad_program.get_ad_application_by_id", new=AsyncMock(return_value=app)),
            patch("handlers.ad_program.update_ad_application", new=AsyncMock()) as update,
        ):
            await msg_ad_post_url(message, object(), state)
        update.assert_not_awaited()
        self.assertIn("Дождитесь проверки", message.answer.await_args.args[0])

    async def test_post_url_from_another_channel_is_rejected(self):
        from handlers.ad_program import msg_ad_post_url
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            text="https://t.me/anotherchannel/55",
            answer=AsyncMock(),
        )
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={"current_app_id": 7}),
            clear=AsyncMock(),
        )
        app = {"id": 7, "user_id": 123, "status": "waiting_for_post", "channel_username": "mychannel"}
        with (
            patch("handlers.ad_program.get_ad_application_by_id", new=AsyncMock(return_value=app)),
            patch("handlers.ad_program.update_ad_application", new=AsyncMock()) as update,
        ):
            await msg_ad_post_url(message, object(), state)
        update.assert_not_awaited()
        self.assertIn("канале из вашей заявки", message.answer.await_args.args[0])

    async def test_about_and_free_days_offer_feedback_without_promising_automatic_bonus(self):
        from handlers.start import cb_about, cb_free_vpn
        callback = SimpleNamespace(from_user=SimpleNamespace(id=123))
        for handler in (cb_about, cb_free_vpn):
            with (
                patch("handlers.start.safe_answer", new=AsyncMock()),
                patch("handlers.start.send_with_photo", new=AsyncMock()) as send,
            ):
                await handler(callback, object())
            text = send.await_args.args[2]
            self.assertIn("ошибк", text)
            self.assertIn("можем начислить", text)
            kb = send.await_args.args[3]
            feedback = next(b for b in buttons(kb) if b.text == "Сообщить об ошибке или идее")
            self.assertTrue(feedback.url.startswith("https://t.me/ByMeVPNSupportBot"))
            self.assertEqual(kb.inline_keyboard[-1][0].callback_data, "back_to_menu")

    async def test_get_post_displays_forward_instruction_not_template(self):
        from handlers.ad_program import cb_ad_get_post
        callback = SimpleNamespace(data="ad_get_post:7", from_user=SimpleNamespace(id=123))
        app = {"id": 7, "user_id": 123, "status": "waiting_for_post"}
        with (
            patch("handlers.ad_program.get_ad_application_by_id", new=AsyncMock(return_value=app)),
            patch("handlers.ad_program.safe_answer", new=AsyncMock()),
            patch("handlers.ad_program.send_with_photo", new=AsyncMock()) as send,
            patch.dict(os.environ, {"AD_SOURCE_POST_URL": "https://t.me/ByMeVPN/42"}),
        ):
            await cb_ad_get_post(callback, object())
        text = send.await_args.args[2]
        self.assertIn("Перешлите", text)
        self.assertNotIn("Скопируйте и опубликуйте текст", text)
        keyboard = send.await_args.args[3]
        self.assertIn("https://t.me/ByMeVPN/42", [b.url for b in buttons(keyboard) if b.url])


if __name__ == "__main__":
    unittest.main()
