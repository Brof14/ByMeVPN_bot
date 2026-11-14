"""Focused regressions for the follow-through on the November customer UX audit."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.buy import _activate_promo_code, _tariff_intro
from handlers.partner import cb_partner_referrals_list
from handlers.start import cb_free_vpn
from keyboards import main_menu_active


def button_actions(markup):
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


class CustomerUxFollowthroughTests(unittest.IsolatedAsyncioTestCase):
    def callback(self, data=""):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=1001),
            message=SimpleNamespace(),
        )

    def test_renew_only_near_expiration(self):
        long_term = main_menu_active(9, "", user_id=1001, remaining_days=24)
        urgent = main_menu_active(9, "", user_id=1001, remaining_days=2)
        self.assertNotIn("key_renew:9", button_actions(long_term))
        self.assertIn("key_renew:9", button_actions(urgent))
        self.assertEqual(next(iter(urgent.inline_keyboard[0])).callback_data, "key_instructions:9")

    def test_plan_intro_separates_devices_and_duration(self):
        copy = _tariff_intro(2)
        self.assertIn("<b>Устройства:</b>", copy)
        self.assertIn("<b>Срок:</b>", copy)
        self.assertIn("до 2 устройств", copy)
        self.assertIn("3+1 мес.", copy)

    async def test_promo_preview_uses_selected_term_and_discount(self):
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={"months": 3, "devices": 2}),
            update_data=AsyncMock(), set_state=AsyncMock(),
        )
        promo = {"code": "SALE20", "promo_type": "percent", "discount_value": 20}
        event = self.callback()
        with (
            patch("handlers.buy.validate_promo_code", new=AsyncMock(return_value=promo)),
            patch("handlers.buy.has_user_used_promo", new=AsyncMock(return_value=False)),
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as sent,
        ):
            self.assertTrue(await _activate_promo_code(object(), event, state, "sale20"))
        text = sent.await_args.args[2]
        self.assertIn("253 ₽", text)
        self.assertIn("120 дней", text)
        state.update_data.assert_awaited_once()

    async def test_free_bonus_shows_reserved_without_claiming_award(self):
        with (
            patch("handlers.start.safe_answer", new=AsyncMock()),
            patch("database.log_analytics_event", new=AsyncMock()),
            patch("database.get_channel_subscription_bonus_status", new=AsyncMock(return_value="reserved")),
            patch("handlers.start.send_with_photo", new=AsyncMock()) as sent,
        ):
            await cb_free_vpn(self.callback(), object())
        self.assertIn("Бонус за канал проверяется", sent.await_args.args[2])
        self.assertIn("channel_bonus_check", button_actions(sent.await_args.args[3]))

    async def test_earned_bonus_shows_expiry_and_no_repeat_button(self):
        with (
            patch("handlers.start.safe_answer", new=AsyncMock()),
            patch("database.log_analytics_event", new=AsyncMock()),
            patch("database.get_channel_subscription_bonus_status", new=AsyncMock(return_value="awarded")),
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[{
                "expiry": int(time.time()) + 15 * 86400,
            }])),
            patch("handlers.start.send_with_photo", new=AsyncMock()) as sent,
        ):
            await cb_free_vpn(self.callback(), object())
        self.assertIn("Бонус за подписку на канал начислен", sent.await_args.args[2])
        self.assertNotIn("channel_bonus_check", button_actions(sent.await_args.args[3]))

    async def test_subscription_diagnostics_use_the_selected_key_not_another_key(self):
        from handlers.keys import cb_key_diagnostics
        expiry = int(time.time()) + 86400
        key = {
            "id": 77, "user_id": 1001,
            "key": "https://vpn.example/sub/selected",
            "expiry": expiry, "limit_ip": 2,
        }
        callback = SimpleNamespace(
            data="key_diagnostics:77",
            from_user=SimpleNamespace(id=1001, first_name="Test"),
        )
        with (
            patch("handlers.keys.get_key_by_id", new=AsyncMock(return_value=key)),
            patch("handlers.keys.safe_answer", new=AsyncMock()),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                "enable": True, "expiry_time": expiry * 1000,
            })),
            patch("handlers.keys.get_xui_devices", new=AsyncMock(return_value={
                "limit_hwid": 2, "devices": [],
            })),
            patch(
                "app_links.build_connect_page_url",
                return_value="https://example.test/connect?u=1001&k=77&s=valid",
            ) as build,
            patch("handlers.keys.send_or_edit", new=AsyncMock()) as sent,
        ):
            await cb_key_diagnostics(callback, object())
        build.assert_called_once_with(
            "https://vpn.example/sub/selected", "Test",
            user_id=1001, key_id=77,
        )
        self.assertIn("key_info:77", button_actions(sent.await_args.args[3]))

    def test_android_failed_download_reopens_the_real_selected_source(self):
        from app_links import render_connection_choice_page
        page = render_connection_choice_page("https://panel.example/sub/test")
        self.assertIn("external.href=download.href", page)
        self.assertNotIn('const apk="/app"', page)
        self.assertIn("Приложение не установлено?", page)

    async def test_ad_retention_due_shows_final_review_instead_of_stale_date(self):
        from handlers.ad_program import cb_ad_my_applications
        item = {
            "id": 87, "user_id": 1001, "channel_username": "test_channel",
            "status": "approved", "bonus_months": 2,
            "verification_due_at": int(time.time()) - 86400,
        }
        callback = self.callback("ad_my_applications")
        state = SimpleNamespace(clear=AsyncMock())
        with (
            patch("handlers.ad_program.safe_answer", new=AsyncMock()),
            patch(
                "handlers.ad_program.get_user_ad_applications",
                new=AsyncMock(return_value=[item]),
            ),
            patch("handlers.ad_program.send_with_photo", new=AsyncMock()) as sent,
        ):
            await cb_ad_my_applications(callback, object(), state)
        self.assertIn(
            "Ожидается итоговая проверка публикации", sent.await_args.args[2],
        )


    async def test_referral_rewards_have_independent_earned_and_pending_states(self):
        users = [
            {"registration_date": 1_700_000_000, "bonus_days_awarded": 15,
             "signup_bonus_awarded": True, "has_paid": False},
            {"registration_date": 1_700_100_000, "bonus_days_awarded": 0,
             "signup_bonus_awarded": False, "has_paid": False},
        ]
        with (
            patch("handlers.partner.safe_answer", new=AsyncMock()),
            patch("handlers.partner.get_referred_users_list", new=AsyncMock(return_value=users)),
            patch("handlers.partner.send_with_photo", new=AsyncMock()) as sent,
        ):
            await cb_partner_referrals_list(self.callback("partner_referrals_list"), object())
        text = sent.await_args.args[2]
        self.assertIn("+15 дней начислены", text)
        self.assertIn("Бонус за первую оплату ожидается", text)
        self.assertIn("Регистрация учтена · начисление проверяется", text)
