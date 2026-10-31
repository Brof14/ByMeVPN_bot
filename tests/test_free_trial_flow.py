"""Regression tests for the Telegram bot's free 3-day trial flow."""
import os
import secrets
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DB_FILE", "/tmp/test_free_trial_flow_import.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

from handlers import start


def _callback(user_id=987650):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(answer=AsyncMock()),
    )


class TestFreeTrialFlow(unittest.IsolatedAsyncioTestCase):
    async def test_new_user_gets_free_trial_without_payment_or_saved_card(self):
        callback = _callback()
        bot = MagicMock()
        with (
            patch("handlers.start.safe_answer", new=AsyncMock()),
            patch("handlers.start.has_trial_used", new=AsyncMock(return_value=False)),
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value=None)),
            patch("database.try_claim_trial", new=AsyncMock(return_value=True)) as claim,
            patch("subscription.deliver_key", new=AsyncMock(return_value=True)) as deliver,
        ):
            await start._start_free_trial(bot, callback)

        claim.assert_awaited_once_with(987650)
        deliver.assert_awaited_once()
        args = deliver.await_args.kwargs
        self.assertEqual(args["days"], 3)
        self.assertEqual(args["limit_ip"], 2)
        self.assertFalse(args["is_paid"])
        self.assertEqual(args["amount"], 0)
        self.assertEqual(args["method"], "trial")
        self.assertFalse(args["extend_existing"])

    async def test_account_with_existing_panel_client_is_not_given_trial(self):
        callback = _callback()
        bot = MagicMock()
        with (
            patch("handlers.start.safe_answer", new=AsyncMock()),
            patch("handlers.start.has_trial_used", new=AsyncMock(return_value=False)),
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[])),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={"id": "existing"})),
            patch("database.try_claim_trial", new=AsyncMock()) as claim,
            patch("subscription.deliver_key", new=AsyncMock()) as deliver,
        ):
            await start._start_free_trial(bot, callback)

        claim.assert_not_awaited()
        deliver.assert_not_awaited()
        callback.message.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
