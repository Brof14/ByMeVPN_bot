"""Tests that referral deep links welcome new users and credit only valid inviters."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.start import cmd_start


class TestReferralStart(unittest.IsolatedAsyncioTestCase):
    async def test_first_start_via_valid_link_shows_referral_welcome_and_awards_inviter(self):
        inviter_id, new_user_id = 811, 812
        message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=new_user_id, full_name="New User", first_name="New", username="new_user",
            ),
            text=f"/start {inviter_id}",
            answer=AsyncMock(),
        )
        bot = SimpleNamespace()
        state = SimpleNamespace(update_data=AsyncMock())
        with (
            patch("handlers.start.find_user_by_id", new=AsyncMock(side_effect=[None, {"user_id": inviter_id}])),
            patch("handlers.start.ensure_user", new=AsyncMock()),
            patch("database.log_analytics_event", new=AsyncMock()),
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[])),
            patch("database.set_referrer", new=AsyncMock()),
            patch("database.set_user_source", new=AsyncMock()),
            patch("database.get_user_keys", new=AsyncMock(return_value=[])),
            patch("handlers.start.get_referrer", new=AsyncMock(return_value=inviter_id)),
            patch("referral_system_new.process_referral_click", new=AsyncMock(return_value=True)),
            patch("referral_system_new.award_referral_signup_bonus", new=AsyncMock(return_value=True)) as award,
            patch("handlers.start._send_main_menu", new=AsyncMock()) as menu,
        ):
            await cmd_start(message, bot, state)

        award.assert_awaited_once_with(bot, inviter_id, new_user_id)
        self.assertTrue(menu.await_args.kwargs["is_new_referral"])

    async def test_unknown_inviter_does_not_trigger_a_reward(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=822, full_name="New", first_name="New", username=None),
            text="/start 999999", answer=AsyncMock(),
        )
        state = SimpleNamespace(update_data=AsyncMock())
        with (
            patch("handlers.start.find_user_by_id", new=AsyncMock(side_effect=[None, None])),
            patch("handlers.start.ensure_user", new=AsyncMock()),
            patch("database.log_analytics_event", new=AsyncMock()),
            patch("handlers.start._send_main_menu", new=AsyncMock()) as menu,
            patch("referral_system_new.award_referral_signup_bonus", new=AsyncMock()) as award,
        ):
            await cmd_start(message, SimpleNamespace(), state)
        award.assert_not_awaited()
        self.assertFalse(menu.await_args.kwargs["is_new_referral"])

    async def test_giveaway_deep_link_saves_pending_entry_and_does_not_open_main_menu(self):
        from handlers.start import cmd_start

        message = SimpleNamespace(
            from_user=SimpleNamespace(id=833, full_name="New", first_name="New", username=None),
            text="/start gw_44", answer=AsyncMock(),
        )
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="ByMeVPNbot")))
        state = SimpleNamespace(update_data=AsyncMock())
        with (
            patch("handlers.start.find_user_by_id", new=AsyncMock(return_value=None)),
            patch("handlers.start.ensure_user", new=AsyncMock()),
            patch("database.log_analytics_event", new=AsyncMock()),
            patch("handlers.start._send_main_menu", new=AsyncMock()) as menu,
            patch("database.set_user_source", new=AsyncMock()),
            patch("database.get_giveaway_by_id", new=AsyncMock(return_value={
                "id": 44, "prize_days": 30, "winners_count": 3, "end_date": 4102444800,
            })),
            patch("database.get_giveaway_entry_status", new=AsyncMock(side_effect=[
                {"is_open": True, "joined": False, "qualified": False, "referral_count": 0},
                {"is_open": True, "joined": True, "qualified": False, "referral_count": 0},
            ])),
            patch("database.join_giveaway", new=AsyncMock(return_value=True)) as join,
        ):
            await cmd_start(message, bot, state)

        join.assert_awaited_once_with(44, 833)
        menu.assert_not_awaited()
        sent_text = message.answer.await_args.args[0]
        self.assertIn("Заявка сохранена", sent_text)
        self.assertIn("минимум одного нового пользователя", sent_text)
        self.assertIn("+30 дней VPN", sent_text)
        self.assertEqual(message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text, "🤝 Пригласить друга")
