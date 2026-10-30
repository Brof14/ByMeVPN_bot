"""Regression tests for simplified customer menus and card-management controls."""
import unittest
from unittest.mock import AsyncMock, patch

from keyboards import (
    main_menu_new_user,
    main_menu_existing,
    main_menu_with_keys,
    manage_payment_methods_kb,
    key_settings_kb,
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
            self.assertEqual(rows[-1][0].text, "О сервисе")
            self.assertEqual(rows[-1][1].text, "Поддержка")
            self.assertEqual(rows[-1][1].callback_data, "trouble")
            self.assertLessEqual(sum(button.style is not None for row in rows for button in row), 2)
        menu = main_menu_with_keys()
        self.assertEqual([len(row) for row in menu.inline_keyboard], [1, 1, 1, 1, 2])
        self.assertEqual(menu.inline_keyboard[0][0].style, "success")
        self.assertEqual(menu.inline_keyboard[1][0].callback_data, "my_keys")
        self.assertEqual(menu.inline_keyboard[1][0].style, "primary")
        used_trial = main_menu_existing(trial_used=True)
        self.assertNotIn("trial_1r", callbacks(used_trial))

    def test_expired_or_active_key_menu_keeps_guide_inside_key_flow(self):
        actions = callbacks(main_menu_with_keys(trial_used=True))
        self.assertNotIn("connection_guide", actions)
        self.assertNotIn("auth_existing_client", actions)
        self.assertIn("free_vpn", actions)
        self.assertNotIn("ad_program", actions)

    async def test_free_vpn_hub_keeps_referrals_and_ads_separate(self):
        from types import SimpleNamespace
        from handlers.start import cb_free_vpn
        callback = SimpleNamespace(from_user=SimpleNamespace(id=123))
        with (
            patch("handlers.start.safe_answer", new=AsyncMock()),
            patch("handlers.start.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_free_vpn(callback, None)
        actions = callbacks(send.call_args.args[3])
        self.assertEqual(actions, ["partner", "ad_program", "back_to_menu"])

    async def test_ad_bonus_retry_uses_same_idempotent_grant_and_keeps_application_open(self):
        from types import SimpleNamespace
        from handlers.ad_program import cb_ad_admin_approve

        app = {
            "id": 71,
            "user_id": 123,
            "status": "post_submitted",
            "bonus_months": 2,
        }
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            data="ad_admin_approve:71",
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        )
        bot = SimpleNamespace()
        with (
            patch("handlers.ad_program._is_admin", return_value=True),
            patch("handlers.ad_program.safe_answer", new=AsyncMock()),
            patch("handlers.ad_program.get_ad_application_by_id", new=AsyncMock(side_effect=[app, {**app, "status": "grant_pending"}])),
            patch("handlers.ad_program.update_ad_application", new=AsyncMock(return_value=True)) as update,
            patch("handlers.ad_program.get_user_keys", new=AsyncMock(return_value=[])),
            patch("subscription.deliver_key", new=AsyncMock(side_effect=[False, True])) as deliver,
            patch("handlers.ad_program.log_ad_audit", new=AsyncMock()),
            patch("handlers.ad_program.log_ad_analytics_event", new=AsyncMock()),
        ):
            await cb_ad_admin_approve(callback, bot)
            self.assertIn("пока не выдан", callback.message.edit_text.await_args.args[0])
            await cb_ad_admin_approve(callback, bot)

        self.assertEqual(deliver.await_count, 2)
        for call in deliver.await_args_list:
            self.assertEqual(call.kwargs["payload"], "ad_bonus_71")
            self.assertEqual(call.kwargs["method"], "ad_bonus")
            self.assertEqual(call.kwargs["amount"], 0)
            self.assertEqual(call.kwargs["days"], 60)
        self.assertEqual(update.await_args_list[0].kwargs["status"], "grant_pending")
        self.assertEqual(update.await_args_list[-1].kwargs["status"], "approved")

    def test_guide_device_selector_uses_compact_two_column_rows(self):
        markup = connection_guide_kb()
        self.assertEqual([len(row) for row in markup.inline_keyboard[:2]], [2, 2])
        actions = callbacks(markup)
        self.assertIn("guide_ios", actions)
        self.assertIn("guide_android", actions)
        self.assertIn("guide_linux", actions)
        self.assertNotIn("guide_more", actions)
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(labels[:4], ["iPhone / iPad", "Android", "Windows", "Linux"])

    def test_additional_platforms_keep_mac_and_tv_accessible(self):
        from keyboards import additional_platforms_kb
        actions = callbacks(additional_platforms_kb(9))
        self.assertIn("guide_macos:9", actions)
        self.assertIn("guide_android_tv:9", actions)
        self.assertIn("connection_guide:9", actions)

    async def test_referral_start_uses_a_welcome_variant(self):
        from types import SimpleNamespace
        from handlers.start import _send_main_menu

        with (
            patch("handlers.start._user_state", new=AsyncMock(return_value="new")),
            patch("handlers.start.send_with_photo", new=AsyncMock()) as send,
        ):
            await _send_main_menu(SimpleNamespace(), SimpleNamespace(), 123, "Алексей", is_new_referral=True)
        welcome = send.call_args.args[2]
        self.assertIn("Добро пожаловать в ByMeVPN", welcome)
        self.assertIn("Приглашение учтено", welcome)
        self.assertIn("3 бесплатных дня", welcome)

    def test_key_settings_remove_rf_routing_and_highlight_only_destructive_action(self):
        markup = key_settings_kb(9)
        actions = callbacks(markup)
        self.assertNotIn("rf_apps_guide:9", actions)
        self.assertIn("key_delete:9", actions)
        self.assertEqual(next(b for row in markup.inline_keyboard for b in row if b.callback_data == "key_delete:9").style, "danger")

    async def test_referral_offer_separates_rewards_link_and_statistics(self):
        from types import SimpleNamespace
        from handlers.partner import cb_partner

        callback = SimpleNamespace(from_user=SimpleNamespace(id=123))
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="ByMeVPNbot")))
        with (
            patch("handlers.partner.safe_answer", new=AsyncMock()),
            patch("handlers.partner.get_referral_stats_enhanced", new=AsyncMock(return_value={
                "total_registrations": 1, "total_clicks": 1, "total_bonus_days": 15,
                "active_clients": 1, "paid_clients": 1,
            })),
            patch("referral_system_new.retry_pending_signup_bonuses", new=AsyncMock(return_value=0)),
            patch("handlers.partner.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_partner(callback, bot)

        text = send.call_args.args[2]
        self.assertIn("<b>VPN бесплатно за друзей</b>", text)
        self.assertIn("+15 дней", text)
        self.assertIn("+30 дней", text)
        self.assertIn("<b>Ваша ссылка</b>", text)
        self.assertIn("Бонусные дни добавляются к вашему VPN", text)
        self.assertIn("Зарегистрировались: <b>1</b> · оплатили: <b>1</b>", text)
        self.assertIn("бот создаст его автоматически", text)

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
