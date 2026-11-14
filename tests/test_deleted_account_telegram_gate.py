"""An erased identity must never be able to create new Telegram checkouts."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from main import UserProfileMiddleware


class DeletedAccountTelegramGateTests(unittest.IsolatedAsyncioTestCase):
    def event(self):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=123, username="old_user"),
            answer=AsyncMock(),
        )

    async def test_removed_user_is_not_dispatched_to_payment_handlers(self):
        event = self.event()
        handler = AsyncMock(return_value="dispatched")
        with (
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=True)),
            patch("database.ensure_user", new=AsyncMock()) as sync,
        ):
            result = await UserProfileMiddleware()(handler, event, {})
        self.assertIsNone(result)
        handler.assert_not_awaited()
        sync.assert_not_awaited()
        self.assertIn("удалён", event.answer.await_args.args[0])

    async def test_late_stars_payment_is_dispatched_for_reconciliation(self):
        event = self.event()
        event.successful_payment = SimpleNamespace(
            telegram_payment_charge_id="late-charge",
        )
        handler = AsyncMock(return_value="accounted")
        with (
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=True)),
            patch("database.ensure_user", new=AsyncMock()) as sync,
        ):
            result = await UserProfileMiddleware()(handler, event, {})
        self.assertEqual(result, "accounted")
        handler.assert_awaited_once()
        sync.assert_not_awaited()

    async def test_live_user_still_reaches_handlers(self):
        event = self.event()
        handler = AsyncMock(return_value="dispatched")
        with (
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=False)),
            patch("database.ensure_user", new=AsyncMock()) as sync,
        ):
            result = await UserProfileMiddleware()(handler, event, {})
        self.assertEqual(result, "dispatched")
        handler.assert_awaited_once()
        sync.assert_awaited_once_with(123, "old_user")

    async def test_explicit_start_reactivates_admin_deleted_user(self):
        event = self.event()
        event.text = "/start"
        handler = AsyncMock(return_value="started")
        with (
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=True)),
            patch("database.reactivate_admin_deleted_account", new=AsyncMock(return_value=True)) as reactivate,
            patch("database.ensure_user", new=AsyncMock()) as sync,
        ):
            result = await UserProfileMiddleware()(handler, event, {})
        self.assertEqual(result, "started")
        reactivate.assert_awaited_once_with(123, "old_user")
        sync.assert_awaited_once_with(123, "old_user")
        handler.assert_awaited_once()

    async def test_non_start_update_cannot_reactivate_admin_deleted_user(self):
        event = self.event()
        event.text = "привет"
        handler = AsyncMock(return_value="dispatched")
        with (
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=True)),
            patch("database.reactivate_admin_deleted_account", new=AsyncMock()) as reactivate,
            patch("database.ensure_user", new=AsyncMock()) as sync,
        ):
            result = await UserProfileMiddleware()(handler, event, {})
        self.assertIsNone(result)
        reactivate.assert_not_awaited()
        sync.assert_not_awaited()
        handler.assert_not_awaited()

    async def test_unavailable_account_database_fails_closed(self):
        event = self.event()
        handler = AsyncMock(return_value="dispatched")
        with (
            patch("database.is_account_tombstoned", new=AsyncMock(side_effect=OSError("db unavailable"))),
            patch("database.ensure_user", new=AsyncMock()) as sync,
        ):
            result = await UserProfileMiddleware()(handler, event, {})
        self.assertIsNone(result)
        handler.assert_not_awaited()
        sync.assert_not_awaited()
        self.assertIn("временно недоступен", event.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
