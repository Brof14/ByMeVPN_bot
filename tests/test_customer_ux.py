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
    async def test_trial_delivery_edits_the_originating_screen(self):
        from subscription import _send_delivery_message
        from bot_assets import KEY

        bot = object()
        target = object()
        with (
            patch("subscription.send_or_edit", new=AsyncMock()) as edit,
            patch("subscription.send_photo_message", new=AsyncMock()) as send,
        ):
            delivered = await _send_delivery_message(
                bot, 123, "<b>Пробный период активирован</b>", "https://example.test/sub",
                edit_target=target,
            )

        self.assertTrue(delivered)
        edit.assert_awaited_once()
        self.assertIs(edit.await_args.args[0], bot)
        self.assertIs(edit.await_args.args[1], target)
        self.assertEqual(edit.await_args.kwargs["photo"], KEY)
        send.assert_not_awaited()

    async def test_paid_delivery_keeps_sending_a_new_key_card(self):
        from subscription import _send_delivery_message
        from bot_assets import KEY

        bot = object()
        with (
            patch("subscription.send_or_edit", new=AsyncMock()) as edit,
            patch("subscription.send_photo_message", new=AsyncMock()) as send,
        ):
            delivered = await _send_delivery_message(bot, 123, "Подписка", "https://example.test/sub")

        self.assertTrue(delivered)
        edit.assert_not_awaited()
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[2], KEY)

    async def test_trial_handler_delivers_into_the_callback_message(self):
        from types import SimpleNamespace
        from handlers.start import _start_free_trial

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        bot = object()
        base_time = 1_800_000_000
        with (
            patch("handlers.start.safe_answer", new=AsyncMock()),
            patch("handlers.start.has_trial_used", new=AsyncMock(return_value=False)),
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=[None, {
                "enable": True, "expiry_time": (base_time + 3 * 86400) * 1000,
            }])),
            patch("handlers.start.get_trial_claim_reservation", new=AsyncMock(return_value=None)),
            patch("handlers.start.reserve_trial_claim", new=AsyncMock(return_value="claim-1")),
            patch("subscription.deliver_key", new=AsyncMock(return_value=True)) as deliver,
            patch("xui_client.get_xui_devices", new=AsyncMock(return_value={
                "limit_hwid": 2, "limit_ip": 0, "devices": [],
            })),
            patch("handlers.start.complete_trial_claim", new=AsyncMock(return_value=True)),
            patch("handlers.start.time.time", return_value=base_time),
            patch("database.log_analytics_event", new=AsyncMock()),
        ):
            await _start_free_trial(bot, callback)

        self.assertIs(deliver.await_args.kwargs["delivery_target"], callback)
        callback.message.answer.assert_not_awaited()

    def test_home_labels_for_subscriber(self):
        menu = main_menu_with_keys(trial_used=True)
        self.assertEqual(
            [b.text for row in menu.inline_keyboard for b in row],
            ["Подписка", "Тарифы", "Бесплатные дни", "О сервисе", "Поддержка"],
        )

    def test_device_tiers_and_periods_unchanged(self):
        actions = callbacks(tariff_selection_kb(devices=5))
        self.assertEqual(
            [a for a in actions if a.startswith("tariff_")],
            ["tariff_1_5", "tariff_3_5", "tariff_6_5", "tariff_12_5"],
        )
        self.assertEqual(
            [a for a in actions if a.startswith("devtier_")],
            ["devtier_2", "devtier_5", "devtier_10"],
        )

    def test_users_without_a_subscription_do_not_see_connection_guide_or_account_switch(self):
        for markup in (main_menu_new_user(), main_menu_existing()):
            actions = callbacks(markup)
            self.assertNotIn("connection_guide", actions)
            self.assertNotIn("auth_existing_client", actions)

    def test_main_menu_uses_distinct_primary_and_secondary_actions(self):
        for markup in (main_menu_new_user(), main_menu_existing(), main_menu_with_keys()):
            rows = markup.inline_keyboard
            self.assertEqual(rows[-1][0].text, "О сервисе")
            self.assertEqual(rows[-1][0].callback_data, "about")
            self.assertEqual(rows[-1][1].text, "Поддержка")
            self.assertTrue(rows[-1][1].url.startswith("https://t.me/ByMeVPNSupportBot"))
            buttons = [button for row in rows for button in row]
            by_action = {button.callback_data: button for button in buttons}
            self.assertEqual(by_action["about"].style, "primary")
            self.assertEqual(by_action["buy_vpn"].style, "primary")
            self.assertEqual(by_action["free_vpn"].style, "primary")
        menu = main_menu_with_keys()
        self.assertEqual([len(row) for row in menu.inline_keyboard], [1, 1, 1, 2])
        self.assertEqual(menu.inline_keyboard[0][0].style, "primary")
        self.assertEqual(menu.inline_keyboard[0][0].callback_data, "my_keys")
        self.assertEqual(menu.inline_keyboard[0][0].style, "primary")
        used_trial = main_menu_existing(trial_used=True)
        self.assertNotIn("trial_free", callbacks(used_trial))
        self.assertIn("trial_free", callbacks(main_menu_new_user()))

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
            patch("handlers.ad_program.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_ad_admin_approve(callback, bot)
            self.assertIn("пока не выдан", send.await_args.args[2])
            await cb_ad_admin_approve(callback, bot)

        self.assertEqual(deliver.await_count, 2)
        for call in deliver.await_args_list:
            self.assertEqual(call.kwargs["payload"], "ad_bonus_71")
            self.assertEqual(call.kwargs["method"], "ad_bonus")
            self.assertEqual(call.kwargs["amount"], 0)
            self.assertEqual(call.kwargs["days"], 60)
        self.assertEqual(update.await_args_list[0].kwargs["status"], "grant_pending")
        self.assertEqual(update.await_args_list[-1].kwargs["status"], "approved")

    async def test_ad_admin_is_not_told_application_closed_if_status_update_failed(self):
        from types import SimpleNamespace
        from handlers.ad_program import cb_ad_admin_approve
        app = {"id": 72, "user_id": 123, "status": "post_submitted", "bonus_months": 2}
        callback = SimpleNamespace(from_user=SimpleNamespace(id=1), data="ad_admin_approve:72")
        with (
            patch("handlers.ad_program._is_admin", return_value=True),
            patch("handlers.ad_program.safe_answer", new=AsyncMock()),
            patch("handlers.ad_program.get_ad_application_by_id", new=AsyncMock(side_effect=[app, {**app, "status": "grant_pending"}])),
            patch("handlers.ad_program.update_ad_application", new=AsyncMock(side_effect=[True, False])),
            patch("handlers.ad_program._deliver_verified_ad_reward", new=AsyncMock(return_value=True)),
            patch("handlers.ad_program.send_with_photo", new=AsyncMock()) as send,
            patch("handlers.ad_program.log_ad_audit", new=AsyncMock()),
            patch("handlers.ad_program.log_ad_analytics_event", new=AsyncMock()),
        ):
            await cb_ad_admin_approve(callback, SimpleNamespace())
        self.assertIn("сверк", send.await_args.args[2].lower())

    def test_guide_device_selector_uses_compact_two_column_rows(self):
        markup = connection_guide_kb()
        self.assertEqual([len(row) for row in markup.inline_keyboard[:2]], [2, 2])
        actions = callbacks(markup)
        self.assertIn("guide_ios", actions)
        self.assertIn("guide_android", actions)
        self.assertIn("guide_linux", actions)
        self.assertIn("guide_more", actions)
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
        self.assertIn("Добро пожаловать", welcome)
        self.assertIn("Приглашение учтено", welcome)
        self.assertIn("Начните с пробного доступа", welcome)

    def test_key_settings_hide_destructive_action(self):
        markup = key_settings_kb(9)
        actions = callbacks(markup)
        self.assertNotIn("rf_apps_guide:9", actions)
        self.assertNotIn("key_delete:9", actions)
        self.assertIn("key_reissue_confirm:9", actions)
        reissue = next(button for row in markup.inline_keyboard for button in row if button.callback_data == "key_reissue_confirm:9")
        self.assertEqual(reissue.text, "Перевыпустить подписку")
        self.assertIsNone(reissue.url)
        self.assertIn("manage_payment_methods:9", actions)

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
        self.assertIn("<b>Приглашайте друзей. Получайте больше.</b>", text)
        self.assertIn("15 дней", text)
        self.assertIn("+30 дней", text)
        self.assertIn("Получено: <b>15 дней</b>", text)
        self.assertIn("Приглашено: <b>1</b>\nОплатили: <b>1</b>", text)
        buttons = [b for row in send.call_args.args[3].inline_keyboard for b in row]
        copied = next(b for b in buttons if b.text == "Скопировать ссылку")
        self.assertEqual(copied.copy_text.text, "https://t.me/ByMeVPNbot?start=123")
        self.assertFalse(any(b.text == "Скопировать текст для канала" for b in buttons))

    def test_back_is_full_width_and_support_is_not_mixed_into_navigation(self):
        back = back_to_menu().inline_keyboard
        self.assertEqual(len(back), 1)
        self.assertEqual(len(back[0]), 1)
        self.assertEqual(back[0][0].callback_data, "back_to_menu")

        for markup in (tariff_selection_kb(), connection_guide_kb()):
            for row in markup.inline_keyboard:
                self.assertFalse(any(button.url for button in row))

    def test_tariff_grid_shows_monthly_price_and_semantic_button_colors(self):
        markup = tariff_selection_kb()
        tariff_buttons = [
            button for row in markup.inline_keyboard for button in row
            if button.callback_data and button.callback_data.startswith("tariff_")
        ]
        three_months = next(button for button in tariff_buttons if button.callback_data == "tariff_3_2")
        self.assertIn("3 мес. + 1 мес. 🎁 — 79 ₽ / мес.", three_months.text)
        self.assertEqual(three_months.style, "success")
        self.assertEqual(
            [button.style for button in tariff_buttons],
            ["primary", "success", "primary", "primary"],
        )

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
            patch("database.get_user_keys", new=AsyncMock(return_value=[{"id": 5, "expiry": 1_800_000_000}])),
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
