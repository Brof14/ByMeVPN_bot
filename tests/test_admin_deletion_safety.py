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
            patch("database.has_recent_unsettled_checkout", new=AsyncMock(return_value=False)),
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

    async def test_recent_checkout_blocks_admin_deletion(self):
        callback = self.callback("admin_del_user_confirm:123")
        with (
            patch("handlers.admin._is_admin", return_value=True),
            patch("handlers.admin.safe_answer", new=AsyncMock()),
            patch("database.try_acquire_delivery_lease", new=AsyncMock(return_value=True)),
            patch("database.suspend_all_user_autorenew", new=AsyncMock(return_value=1)),
            patch("database.has_recent_unsettled_checkout", new=AsyncMock(return_value=True)),
            patch("database.release_delivery_lease", new=AsyncMock()),
            patch("handlers.admin.delete_xui_user", new=AsyncMock()) as panel,
            patch("handlers.admin.delete_user_and_keys", new=AsyncMock()) as erase,
        ):
            await admin.cb_del_user(callback, object())
        panel.assert_not_awaited()
        erase.assert_not_awaited()
        self.assertIn("оплата", callback.message.edit_text.await_args.args[0])

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

    async def test_account_erase_blocks_resurrection_and_preserves_payment_evidence(self):
        from api_router import _ensure_auth_db
        await _ensure_auth_db()
        db = await database.get_db()
        await db.execute("UPDATE users SET username='Original' WHERE user_id=123")
        await db.execute(
            "INSERT INTO app_users(user_id,email,password_hash,salt,name,created_at) "
            "VALUES (123,'test@example.invalid','hash','salt','Name',1)"
        )
        await db.execute(
            "INSERT INTO payments(user_id,amount,currency,method,days,created,payload) "
            "VALUES (123,89,'RUB','yookassa',30,1,'receipt-1')"
        )
        await db.commit()
        await database.add_key(123, "https://sub.test/one", "one", "uuid-1", 30, 2)
        await database.save_client_diagnostic(123, "android", "connect", "TUNNEL_TIMEOUT", "1.0")
        await database.create_mobile_refresh_session(
            "a" * 64, 123, "test@example.invalid", "issued-jti", 2147483000, 2147483500,
        )
        await database.claim_yookassa_checkout_attempt(123, "b" * 64)

        uuids = await database.delete_user_and_keys(123)
        self.assertEqual(uuids, ["uuid-1"])
        self.assertEqual(await database.get_user_keys(123), [])
        self.assertTrue(await database.is_account_tombstoned(123))
        self.assertFalse(await database.try_acquire_delivery_lease(123, "late-provision"))
        await database.ensure_user(123, "Resurrected")
        row = await (await db.execute(
            "SELECT username,is_banned FROM users WHERE user_id=123"
        )).fetchone()
        self.assertEqual(row, (None, 1))
        self.assertIsNone((await (await db.execute(
            "SELECT user_id FROM app_users WHERE user_id=123"
        )).fetchone()))
        self.assertEqual((await (await db.execute(
            "SELECT amount FROM payments WHERE payload='receipt-1'"
        )).fetchone())[0], 89)
        for table in ("mobile_refresh_sessions", "client_diagnostics", "yookassa_checkout_attempts"):
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=123")
            self.assertEqual((await cursor.fetchone())[0], 0, table)

    async def test_checkout_lease_blocks_account_deletion_during_invoice_creation(self):
        attempt = await database.claim_yookassa_checkout_attempt(123, "c" * 64)
        self.assertTrue(await database.has_recent_unsettled_checkout(123))
        await database.release_yookassa_checkout_attempt(123, attempt["token"])
        self.assertFalse(await database.has_recent_unsettled_checkout(123))

    async def test_pending_mobile_checkout_blocks_erasure(self):
        import time
        db = await database.get_db()
        now = int(time.time())
        self.assertFalse(await database.has_recent_unsettled_checkout(123))
        await db.execute(
            "INSERT INTO mobile_payment_requests("
            "user_id,request_id,plan_code,provider,created_at,creation_state) "
            "VALUES(123,'req-one','1m_d2','yookassa',?,'creating')",
            (now,),
        )
        await db.commit()
        self.assertTrue(await database.has_recent_unsettled_checkout(123))
        await db.execute(
            "UPDATE mobile_payment_requests SET creation_state='ready' "
            "WHERE request_id='req-one'",
        )
        await db.commit()
        self.assertFalse(await database.has_recent_unsettled_checkout(123))
        await db.execute(
            "UPDATE mobile_payment_requests SET payment_id='invoice-one' "
            "WHERE request_id='req-one'",
        )
        await db.commit()
        self.assertTrue(await database.has_recent_unsettled_checkout(123))
        await db.execute(
            "INSERT INTO payments(user_id,amount,currency,method,days,created,payload,"
            "provider,provider_payment_id,status) "
            "VALUES(123,89,'RUB','yookassa',30,?,'invoice-one',"
            "'yookassa','invoice-one','success')",
            (now,),
        )
        await db.commit()
        self.assertFalse(await database.has_recent_unsettled_checkout(123))

    async def test_old_pending_checkout_does_not_block_erasure_forever(self):
        import time
        db = await database.get_db()
        await db.execute(
            "INSERT INTO yookassa_pending(payment_id,user_id,days,devices,amount_rub,created) "
            "VALUES('stale',123,30,2,89,?)",
            (int(time.time()) - 86400,),
        )
        await db.commit()
        self.assertFalse(await database.has_recent_unsettled_checkout(123))

    async def test_deleted_account_referrals_cannot_reattach(self):
        await database.ensure_user(999)
        await database.delete_user_and_keys(123)
        await database.set_referrer(123, 999)
        user = await database.find_user_by_id(123)
        self.assertIsNone(user.get("referrer_id"))
        db = await database.get_db()
        count = (await (await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referred_id=123"
        )).fetchone())[0]
        self.assertEqual(count, 0)

    async def test_suspend_target_key_preserves_other_key_bound_recurring_method(self):
        first = await database.add_key(123, "https://sub.test/a", "a", "uuid-a", 30, 2)
        second = await database.add_key(123, "https://sub.test/b", "b", "uuid-b", 30, 2)
        await database.save_auto_renew_subscription(123, first, "card-first")
        await database.save_auto_renew_subscription(123, second, "card-second")
        await database.save_auto_renew_subscription(123, None, "card-legacy")

        changed = await database.suspend_key_autorenew(123, first)
        self.assertEqual(changed, 2)
        db = await database.get_db()
        rows = await (await db.execute(
            "SELECT key_id,status,payment_method_id FROM auto_renew_subscriptions "
            "WHERE user_id=123 ORDER BY id"
        )).fetchall()
        by_key = {row[0]: (row[1], row[2]) for row in rows}
        self.assertEqual(by_key[first], ("cancelled", ""))
        self.assertEqual(by_key[None], ("cancelled", ""))
        self.assertEqual(by_key[second], ("active", "card-second"))

    async def test_key_deletion_atomically_erases_target_recurring_token(self):
        first = await database.add_key(123, "https://sub.test/a", "a", "uuid-a", 30, 2)
        second = await database.add_key(123, "https://sub.test/b", "b", "uuid-b", 30, 2)
        await database.save_auto_renew_subscription(123, first, "card-first")
        await database.save_auto_renew_subscription(123, second, "card-second")
        self.assertTrue(await database.delete_key(first))
        db = await database.get_db()
        rows = await (await db.execute(
            "SELECT key_id,status,payment_method_id FROM auto_renew_subscriptions "
            "WHERE user_id=123 ORDER BY key_id"
        )).fetchall()
        by_key = {row[0]: (row[1], row[2]) for row in rows}
        self.assertEqual(by_key[first], ("cancelled", ""))
        self.assertEqual(by_key[second], ("active", "card-second"))

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
