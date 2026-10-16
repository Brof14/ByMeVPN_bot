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
        with (
            patch("handlers.start.find_user_by_id", new=AsyncMock(side_effect=[None, {"user_id": inviter_id}])),
            patch("handlers.start.ensure_user", new=AsyncMock()),
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[])),
            patch("database.set_referrer", new=AsyncMock()),
            patch("database.get_user_keys", new=AsyncMock(return_value=[])),
            patch("handlers.start.get_referrer", new=AsyncMock(return_value=inviter_id)),
            patch("referral_system_new.process_referral_click", new=AsyncMock(return_value=True)),
            patch("referral_system_new.award_referral_signup_bonus", new=AsyncMock(return_value=True)) as award,
            patch("handlers.start._send_main_menu", new=AsyncMock()) as menu,
        ):
            await cmd_start(message, bot)

        award.assert_awaited_once_with(bot, inviter_id, new_user_id)
        self.assertTrue(menu.await_args.kwargs["is_new_referral"])

    async def test_unknown_inviter_does_not_trigger_a_reward(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=822, full_name="New", first_name="New", username=None),
            text="/start 999999", answer=AsyncMock(),
        )
        with (
            patch("handlers.start.find_user_by_id", new=AsyncMock(side_effect=[None, None])),
            patch("handlers.start.ensure_user", new=AsyncMock()),
            patch("handlers.start._send_main_menu", new=AsyncMock()) as menu,
            patch("referral_system_new.award_referral_signup_bonus", new=AsyncMock()) as award,
        ):
            await cmd_start(message, SimpleNamespace())
        award.assert_not_awaited()
        self.assertFalse(menu.await_args.kwargs["is_new_referral"])
