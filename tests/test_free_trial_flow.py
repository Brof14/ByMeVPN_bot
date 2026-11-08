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
            patch("handlers.start.has_ever_had_key", new=AsyncMock(return_value=False)),
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[])),
            patch("handlers.start.has_active_subscription", new=AsyncMock(return_value=False)),
            patch("handlers.start.get_trial_claim_reservation", new=AsyncMock(return_value=None)),
            patch("xui_client.get_xui_client_details", new=AsyncMock(side_effect=[
                None, {"enable": True, "expiry_time": (int(start.time.time()) + 3 * 86400) * 1000},
            ])),
            patch("xui_client.get_xui_devices", new=AsyncMock(return_value={
                "limit_hwid": 2, "limit_ip": 0, "devices": [],
            })),
            patch("handlers.start.reserve_trial_claim", new=AsyncMock(return_value="claim-1")) as reserve,
            patch("handlers.start.complete_trial_claim", new=AsyncMock(return_value=True)) as complete,
            patch("subscription.deliver_key", new=AsyncMock(return_value=True)) as deliver,
        ):
            await start._start_free_trial(bot, callback)

        reserve.assert_awaited_once_with(987650)
        complete.assert_awaited_once_with(987650, "claim-1")
        deliver.assert_awaited_once()
        args = deliver.await_args.kwargs
        self.assertEqual(args["days"], 3)
        self.assertEqual(args["limit_ip"], 2)
        self.assertFalse(args["is_paid"])
        self.assertEqual(args["amount"], 0)
        self.assertEqual(args["method"], "trial")
        self.assertFalse(args["extend_existing"])

    async def test_unused_trial_can_reuse_an_expired_precreated_panel_client(self):
        callback = _callback()
        bot = MagicMock()
        now = int(start.time.time())
        with (
            patch("handlers.start.safe_answer", new=AsyncMock()),
            patch("handlers.start.has_trial_used", new=AsyncMock(return_value=False)),
            patch("handlers.start.has_ever_had_key", new=AsyncMock(return_value=False)),
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[])),
            patch("handlers.start.has_active_subscription", new=AsyncMock(return_value=False)),
            patch("handlers.start.get_trial_claim_reservation", new=AsyncMock(return_value=None)),
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                "id": "existing", "enable": False, "expiry_time": (now - 60) * 1000,
            })),
            patch("handlers.start.reserve_trial_claim", new=AsyncMock(return_value="claim-1")) as reserve,
            patch("subscription.deliver_key", new=AsyncMock(return_value=False)) as deliver,
            patch("handlers.start.time.time", return_value=now),
        ):
            await start._start_free_trial(bot, callback)

        reserve.assert_awaited_once_with(987650)
        deliver.assert_awaited_once()
        self.assertEqual(deliver.await_args.kwargs["days"], 3)
        self.assertEqual(deliver.await_args.kwargs["limit_ip"], 2)


if __name__ == "__main__":
    unittest.main()
