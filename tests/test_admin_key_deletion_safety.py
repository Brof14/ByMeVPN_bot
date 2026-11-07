"""Regression coverage for admin key deletion and recurring-charge safety."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import admin


class AdminKeyDeletionTests(unittest.IsolatedAsyncioTestCase):
    def callback(self, *, key_id=17, user_id=123):
        return SimpleNamespace(
            data=f"admin_del_key:{key_id}:{user_id}",
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
        )

    async def call(self, *, keys=None, key=None, invoice=False, lease_failure=False, panel=True):
        cb = self.callback()
        events = []
        record = key if key is not None else {
            "id": 17, "user_id": 123, "uuid": "", "expiry": 1890000000,
        }
        user_keys = keys if keys is not None else [record]

        async def under_lease(user_id, operation):
            events.append("lease")
            if lease_failure:
                raise TimeoutError("busy")
            return await operation()

        async def pending(user_id):
            events.append("pending")
            return invoice

        async def suspend(user_id, key_id):
            events.append("suspend")
            return 1

        async def delete_panel(user_id):
            events.append("panel")
            return panel

        async def delete_db(key_id):
            events.append("db")
            return True

        async def get_keys(_user_id):
            # Emulate the database before and after successful removal.
            return ([item for item in user_keys if item["id"] != 17]
                    if "db" in events else user_keys)

        with (
            patch("handlers.admin._is_admin", return_value=True),
            patch("handlers.admin.safe_answer", new=AsyncMock()),
            patch("handlers.keys._run_billing_change_under_lease",
                  new=AsyncMock(side_effect=under_lease)),
            patch("database.get_key_by_id", new=AsyncMock(return_value=record)),
            patch("database.has_recent_unsettled_checkout", new=AsyncMock(side_effect=pending)),
            patch("database.suspend_key_autorenew", new=AsyncMock(side_effect=suspend)),
            patch("handlers.admin.get_user_keys", new=AsyncMock(side_effect=get_keys)),
            patch("handlers.admin.delete_xui_user", new=AsyncMock(side_effect=delete_panel)),
            patch("database.delete_key_by_id", new=AsyncMock(side_effect=delete_db)),
        ):
            await admin.cb_admin_del_key(cb, object())
        return events, cb

    async def test_missing_sqlite_uuid_still_requires_panel_confirmation(self):
        events, cb = await self.call(panel=False)
        self.assertEqual(events, ["lease", "pending", "suspend", "panel"])
        self.assertIn("не подтверждено", cb.message.answer.await_args.args[0])
        cb.message.edit_text.assert_not_awaited()

    async def test_success_removes_last_key_only_after_billing_and_panel(self):
        events, cb = await self.call()
        self.assertEqual(events, ["lease", "pending", "suspend", "panel", "db"])
        self.assertIn("Других ключей нет", cb.message.edit_text.await_args.args[0])

    async def test_remaining_keys_keep_shared_vpn_client(self):
        keys = [
            {"id": 17, "user_id": 123, "uuid": ""},
            {"id": 18, "user_id": 123, "uuid": "other", "expiry": 1890000000},
        ]
        events, cb = await self.call(keys=keys)
        self.assertEqual(events, ["lease", "pending", "suspend", "db"])
        self.assertIn("Остальные подписки", cb.message.edit_text.await_args.args[0])

    async def test_uncertain_payment_prevents_all_mutations(self):
        events, cb = await self.call(invoice=True)
        self.assertEqual(events, ["lease", "pending"])
        self.assertIn("платёж", cb.message.answer.await_args.args[0])

    async def test_unavailable_lease_never_mutates_subscription(self):
        events, cb = await self.call(lease_failure=True)
        self.assertEqual(events, ["lease"])
        self.assertIn("Не удалось безопасно", cb.message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
