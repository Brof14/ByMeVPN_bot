"""Admin deletion is an irreversible, billing-sensitive two-step operation."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from handlers import admin


class AdminDeletionHandlerTests(unittest.IsolatedAsyncioTestCase):
    def callback(self, data: str):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )

    async def test_delete_button_requires_separate_confirmation(self):
        callback = self.callback("admin_del_user:123")
        with (
            patch("handlers.admin._is_admin", return_value=True),
            patch("handlers.admin.safe_answer", new=AsyncMock()),
            patch("handlers.admin.delete_xui_user", new=AsyncMock()) as panel,
        ):
            await admin.cb_del_user_prompt(callback)
        panel.assert_not_awaited()
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertIn("admin_del_user_confirm:123", [
            button.callback_data for row in markup.inline_keyboard for button in row
        ])

    async def attempt(self, *, billing_error=None, panel_result=True, db_error=None, lease=True):
        callback = self.callback("admin_del_user_confirm:123")
        events = []

        async def billing(_uid):
            events.append("stop_billing")
            if billing_error:
                raise billing_error
            return 2

        async def remove_panel(_uid):
            events.append("panel")
            return panel_result

        async def erase_db(_uid):
            events.append("db")
            if db_error:
                raise db_error
            return []

        with (
            patch("handlers.admin._is_admin", return_value=True),
            patch("handlers.admin.safe_answer", new=AsyncMock()),
            patch("database.try_acquire_delivery_lease", new=AsyncMock(return_value=lease)),
            patch("database.renew_delivery_lease", new=AsyncMock(return_value=True)),
            patch("database.release_delivery_lease", new=AsyncMock()) as release,
            patch("database.suspend_all_user_autorenew", new=AsyncMock(side_effect=billing)),
            patch("handlers.admin.delete_xui_user", new=AsyncMock(side_effect=remove_panel)),
            patch("handlers.admin.delete_user_and_keys", new=AsyncMock(side_effect=erase_db)),
        ):
            await admin.cb_del_user(callback, object())
        return events, callback.message.edit_text.await_args.args[0], release.await_count

    async def test_billing_failure_does_not_touch_server_or_database(self):
        events, text, released = await self.attempt(billing_error=OSError("disk"))
        self.assertEqual(events, ["stop_billing"])
        self.assertIn("автосписания", text)
        self.assertEqual(released, 1)

    async def test_failed_panel_verification_leaves_database_intact(self):
        events, text, released = await self.attempt(panel_result=False)
        self.assertEqual(events, ["stop_billing", "panel"])
        self.assertIn("не подтверждено", text)
        self.assertEqual(released, 1)

    async def test_successful_deletion_follows_billing_panel_database_order(self):
        events, text, released = await self.attempt()
        self.assertEqual(events, ["stop_billing", "panel", "db"])
        self.assertIn("удалён", text)
        self.assertEqual(released, 1)

    async def test_database_failure_after_panel_delete_requires_reconciliation(self):
        events, text, released = await self.attempt(db_error=OSError("database"))
        self.assertEqual(events, ["stop_billing", "panel", "db"])
        self.assertIn("сверка", text)
        self.assertEqual(released, 1)

    async def test_active_provisioning_prevents_any_deletion(self):
        events, text, released = await self.attempt(lease=False)
        self.assertEqual(events, [])
        self.assertIn("другая операция", text)
        self.assertEqual(released, 0)


class CancelAllMethodsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_path, self.old_pool = database.DB_FILE, database._db_pool
        self.tmp = tempfile.TemporaryDirectory()
        database.DB_FILE, database._db_pool = str(Path(self.tmp.name) / "cancel.db"), None
        await database.init_db()
        await database.ensure_user(123)

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE, database._db_pool = self.old_path, self.old_pool
        self.tmp.cleanup()

    async def test_all_saved_methods_are_removed_without_touching_paid_access(self):
        first = await database.add_key(123, "https://sub.test/one", "one", "uuid-1", 30, 2)
        second = await database.add_key(123, "https://sub.test/two", "two", "uuid-2", 30, 5)
        expiry_before = [(key["id"], key["expiry"]) for key in await database.get_user_keys(123)]
        for key_id in (first, second):
            await database.save_auto_renew_subscription(123, key_id, f"pm-{key_id}")
        self.assertEqual(await database.suspend_all_user_autorenew(123), 2)
        for key_id in (first, second):
            sub = await database.get_auto_renew_subscription(123, key_id)
            self.assertEqual(sub["status"], "cancelled")
            self.assertFalse(sub["payment_method_id"])
        self.assertEqual(
            expiry_before,
            [(key["id"], key["expiry"]) for key in await database.get_user_keys(123)],
        )
        self.assertEqual(await database.suspend_all_user_autorenew(123), 0)


if __name__ == "__main__":
    unittest.main()
