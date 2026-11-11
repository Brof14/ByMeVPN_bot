"""UX contracts for the November 2026 Telegram design refinements."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot_style import ICON_IDS
from keyboards import tariff_selection_kb, payment_kb


def buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


class TariffDesignTests(unittest.TestCase):
    def test_bound_discount_is_shown_only_on_eligible_tariff(self):
        labels = {b.callback_data: b.text for b in buttons(
            tariff_selection_kb(devices=2, discount_percent=30, promo_tariff_months=3)
        )}
        self.assertIn("89 ₽", labels["tariff_1_2"])
        self.assertIn("55 ₽/мес.", labels["tariff_3_2"])
        self.assertNotIn("−30%", labels["tariff_1_2"])
        self.assertIn("−30%", labels["tariff_3_2"])

    def test_tariffs_show_monthly_rates_and_highlight_bonus_months(self):
        keyboard = tariff_selection_kb(devices=2)
        labels = {b.callback_data: b.text for b in buttons(keyboard)}
        self.assertIn("3+1 мес.", labels["tariff_3_2"])
        self.assertIn("6+2 мес.", labels["tariff_6_2"])
        self.assertIn("12+3 мес.", labels["tariff_12_2"])
        self.assertIn("59 ₽/мес.", labels["tariff_12_2"])
        discounted = {b.callback_data: b.text for b in buttons(tariff_selection_kb(devices=2, discount_percent=100))}
        self.assertNotIn("Выгоднее", discounted["tariff_12_2"])
        self.assertIn("· 316 ₽", labels["tariff_3_2"])
        with patch.dict(os.environ, {"BOT_PREMIUM_BUTTON_ICONS": "1"}):
            premium = tariff_selection_kb(devices=2)
        bonus_buttons = [b for b in buttons(premium) if b.callback_data and b.callback_data.startswith("tariff_") and not b.callback_data.startswith("tariff_1_")]
        self.assertEqual(
            [b.icon_custom_emoji_id for b in bonus_buttons],
            [ICON_IDS["gift"]] * 3,
        )

    def test_five_device_selector_is_short_and_three_plus_one_has_no_extra_claim(self):
        keyboard = tariff_selection_kb(devices=5)
        labels = {b.callback_data: b.text for b in buttons(keyboard)}
        self.assertIn("5 устройств", labels["devtier_5"])
        self.assertNotIn("оптимально", labels["devtier_5"].lower())
        self.assertNotIn("выгодно", labels["tariff_3_5"].lower())

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
    async def test_private_invite_keeps_case(self):
        from handlers.ad_program import msg_ad_channel_input
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), text="https://t.me/+AbCdEf012345")
        state = SimpleNamespace(update_data=AsyncMock())
        with (
            patch("handlers.ad_program.log_ad_analytics_event", new=AsyncMock()),
            patch("handlers.ad_program._ask_subscriber_count", new=AsyncMock()),
        ):
            await msg_ad_channel_input(message, object(), state)
        self.assertEqual(state.update_data.await_args.kwargs["ad_channel_ref"], "+AbCdEf012345")

    async def test_channel_title_is_escaped_in_manual_count_prompt(self):
        from handlers.ad_program import msg_ad_channel_input
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), text="@channelname")
        state = SimpleNamespace(update_data=AsyncMock())
        bot = SimpleNamespace(
            get_chat=AsyncMock(return_value=SimpleNamespace(id=9, title="News <VPN> & more", type="channel")),
            get_chat_member_count=AsyncMock(side_effect=RuntimeError("not available")),
        )
        with patch("handlers.ad_program._ask_subscriber_count", new=AsyncMock()) as ask:
            await msg_ad_channel_input(message, bot, state)
        self.assertIn("News &lt;VPN&gt; &amp; more", ask.await_args.kwargs["prompt"])

    async def test_public_group_cannot_enter_channel_ad_program(self):
        from handlers.ad_program import msg_ad_channel_input
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), text="@somegroup", answer=AsyncMock())
        state = SimpleNamespace(update_data=AsyncMock(), clear=AsyncMock())
        bot = SimpleNamespace(
            get_chat=AsyncMock(return_value=SimpleNamespace(id=9, title="Group", type="supergroup")),
            get_chat_member_count=AsyncMock(return_value=100),
        )
        with patch("handlers.ad_program._create_application_and_reply", new=AsyncMock()) as create:
            await msg_ad_channel_input(message, bot, state)
        create.assert_not_awaited()
        bot.get_chat_member_count.assert_not_awaited()
        self.assertIn("канал", message.answer.await_args.args[0].lower())

    async def test_invalid_channel_member_count_requires_manual_review(self):
        from handlers.ad_program import msg_ad_channel_input
        for count in (0, -1, "unknown"):
            with self.subTest(count=count):
                message = SimpleNamespace(from_user=SimpleNamespace(id=123), text="@channelname")
                state = SimpleNamespace(update_data=AsyncMock())
                bot = SimpleNamespace(
                    get_chat=AsyncMock(return_value=SimpleNamespace(id=9, title="Channel", type="channel")),
                    get_chat_member_count=AsyncMock(return_value=count),
                )
                with (
                    patch("handlers.ad_program._ask_subscriber_count", new=AsyncMock()) as ask,
                    patch("handlers.ad_program._create_application_and_reply", new=AsyncMock()) as create,
                ):
                    await msg_ad_channel_input(message, bot, state)
                ask.assert_awaited_once()
                create.assert_not_awaited()

    async def test_private_invite_is_not_written_to_audit_or_analytics(self):
        from handlers.ad_program import _create_application_and_reply
        message = SimpleNamespace(answer=AsyncMock())
        with (
            patch("handlers.ad_program.channel_already_used", new=AsyncMock(return_value=False)),
            patch("handlers.ad_program.get_active_ad_campaign", new=AsyncMock(return_value=None)),
            patch("handlers.ad_program.create_ad_application", new=AsyncMock(return_value=7)) as create,
            patch("handlers.ad_program.log_ad_analytics_event", new=AsyncMock()) as analytics,
            patch("handlers.ad_program.log_ad_audit", new=AsyncMock()) as audit,
        ):
            await _create_application_and_reply(
                object(), message, 123, channel_id=None, channel_username="",
                channel_ref="+AbCdEf012345", chat_title="private", subscriber_count=20,
                bonus_months=2, manual_count=True,
            )
        self.assertEqual(create.await_args.kwargs["channel_ref"], "+AbCdEf012345")
        self.assertNotIn("+AbCdEf012345", repr(analytics.await_args))
        self.assertNotIn("+AbCdEf012345", repr(audit.await_args))

    async def test_channel_subscriber_count_must_fit_storage_and_be_positive(self):
        from handlers.ad_program import msg_ad_subscriber_count
        for value in ("0", "9" * 5000):
            message = SimpleNamespace(from_user=SimpleNamespace(id=123), text=value, answer=AsyncMock())
            state = SimpleNamespace(get_data=AsyncMock(return_value={}), clear=AsyncMock())
            with patch("handlers.ad_program._create_application_and_reply", new=AsyncMock()) as create:
                await msg_ad_subscriber_count(message, object(), state)
            create.assert_not_awaited()
            state.clear.assert_not_awaited()
            message.answer.assert_awaited_once()

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
        self.assertIn("После проверки выполнения условий", text)
        self.assertIn("наш рекламный пост", text)
        actions = [b.callback_data for b in buttons(send.await_args.args[3])]
        self.assertIn("ad_check_channel", actions)
        self.assertNotIn("partner", actions)

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

    async def test_post_url_is_stored_only_in_application_not_event_logs(self):
        from handlers.ad_program import msg_ad_post_url
        post_url = "https://t.me/mychannel/55"
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=123), text=post_url, answer=AsyncMock(),
        )
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={"current_app_id": 7}), clear=AsyncMock(),
        )
        app = {"id": 7, "user_id": 123, "status": "waiting_for_post", "channel_username": "mychannel"}
        with (
            patch("handlers.ad_program.get_ad_application_by_id", new=AsyncMock(return_value=app)),
            patch("handlers.ad_program.update_ad_application", new=AsyncMock(return_value=True)) as update,
            patch("handlers.ad_program.log_ad_audit", new=AsyncMock()) as audit,
            patch("handlers.ad_program.log_ad_analytics_event", new=AsyncMock()) as analytics,
        ):
            await msg_ad_post_url(message, object(), state)
        self.assertEqual(update.await_args.kwargs["post_url"], post_url)
        self.assertNotIn(post_url, repr(audit.await_args))
        self.assertNotIn(post_url, repr(analytics.await_args))
        state.clear.assert_awaited_once()

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

    async def test_about_and_free_days_keep_their_requested_minimal_actions(self):
        from handlers.start import cb_about, cb_free_vpn
        callback = SimpleNamespace(from_user=SimpleNamespace(id=123))
        for handler in (cb_about, cb_free_vpn):
            with (
                patch("handlers.start.safe_answer", new=AsyncMock()),
                patch("handlers.start.send_with_photo", new=AsyncMock()) as send,
            ):
                await handler(callback, object())
            kb = send.await_args.args[3]
            self.assertFalse(any(b.text == "Сообщить об ошибке или идее" for b in buttons(kb)))
            self.assertEqual(kb.inline_keyboard[-1][0].callback_data, "back_to_menu")
            if handler is cb_about:
                self.assertFalse(any(b.text in {"Тарифы", "Подключение"} for b in buttons(kb)))

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
