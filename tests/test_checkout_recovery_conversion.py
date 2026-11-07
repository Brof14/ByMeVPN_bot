"""Checkout restoration and honest provider-paid conversion contracts."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class CheckoutPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import database
        self.db = database
        self.original_path = database.DB_FILE
        self.original_pool = database._db_pool
        self.tmp = tempfile.TemporaryDirectory()
        database.DB_FILE = str(Path(self.tmp.name) / "checkout.db")
        database._db_pool = None
        await database.init_db()
        for uid in (101, 202, 303):
            await database.ensure_user(uid)

    async def asyncTearDown(self):
        await self.db.close_db()
        self.db.DB_FILE = self.original_path
        self.db._db_pool = self.original_pool
        self.tmp.cleanup()

    async def test_plan_draft_has_no_price_and_expires(self):
        await self.db.save_checkout_draft(101, 6, 5)
        self.assertEqual(await self.db.get_checkout_draft(101), {"months": 6, "devices": 5, "renew_key_id": 0})
        await self.db.save_checkout_draft(101, 12, 10)
        self.assertEqual(await self.db.get_checkout_draft(101), {"months": 12, "devices": 10, "renew_key_id": 0})
        db = await self.db.get_db()
        await db.execute("UPDATE checkout_drafts SET updated_at=? WHERE user_id=101",
                         (int(time.time()) - 2 * 86400,))
        await db.commit()
        self.assertIsNone(await self.db.get_checkout_draft(101))

    async def test_renewal_draft_cannot_turn_into_a_new_purchase(self):
        from handlers.buy import _ask_tariff_again
        await self.db.save_checkout_draft(101, 3, 5, renew_key_id=75)
        self.assertEqual(
            await self.db.get_checkout_draft(101),
            {"months": 3, "devices": 5, "renew_key_id": 75},
        )
        callback = SimpleNamespace(from_user=SimpleNamespace(id=101))
        with patch("handlers.buy.send_with_photo", new=AsyncMock()) as send:
            await _ask_tariff_again(object(), callback)
        keyboard = send.await_args.args[3]
        actions = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertEqual(actions, ["key_renew:75", "back_to_menu"])
        self.assertIn("продление", send.await_args.args[2].lower())

    async def test_invalid_checkout_never_persisted(self):
        for months, devices in [(99, 2), (1, 99), (0, 5)]:
            with self.assertRaises(ValueError):
                await self.db.save_checkout_draft(101, months, devices)
        self.assertIsNone(await self.db.get_checkout_draft(101))

    async def test_recovery_requires_order_confirmation_before_any_payment(self):
        from handlers.buy import _ask_tariff_again
        await self.db.save_checkout_draft(101, 6, 5)
        callback = SimpleNamespace(from_user=SimpleNamespace(id=101))
        with patch("handlers.buy.send_with_photo", new=AsyncMock()) as send:
            await _ask_tariff_again(object(), callback)
        text = send.await_args.args[2]
        kb = send.await_args.args[3]
        self.assertIn("Оплата пока не создаётся", text)
        self.assertIn("промокод", text)
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "tariff_6_5")
        self.assertEqual(kb.inline_keyboard[1][0].callback_data, "devtier_5")

    async def test_successful_purchases_only_in_funnel_and_currencies_separated(self):
        await self.db.log_analytics_event(101, "tariffs_viewed")
        await self.db.log_analytics_event(101, "tariff_selected")
        await self.db.log_analytics_event(101, "checkout_started")
        await self.db.log_analytics_event(202, "tariffs_viewed")
        for user_id,amount,currency,status,payload in [
            (101, 552, "RUB", "success", "paid-101"),
            (202, 89, "RUB", "failed", "failed-202"),
            (303, 200, "XTR", "success", "stars-303"),
        ]:
            await self.db.record_payment_idempotent(
                user_id=user_id, amount=amount, currency=currency,
                method="test", days=30, payload=payload, status=status,
                tariff="plan", devices=2, provider="test", provider_payment_id=payload,
            )
        snapshot = await self.db.get_checkout_conversion_snapshot(30)
        self.assertEqual(snapshot["registered"], 3)
        self.assertEqual(snapshot["views"], 2)
        self.assertEqual(snapshot["selections"], 1)
        self.assertEqual(snapshot["started"], 1)
        self.assertEqual(snapshot["paid"], 2)
        self.assertEqual(snapshot["paid_from_views"], 1)
        self.assertAlmostEqual(snapshot["purchase_pct"], 50)
        self.assertEqual(snapshot["by_currency"]["RUB"]["gross_amount"], 552)
        self.assertEqual(snapshot["by_currency"]["XTR"]["gross_amount"], 200)
        self.assertEqual(snapshot["by_currency"]["RUB"]["payments"], 1)


class AdminCheckoutMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_financial_dashboard_does_not_leak_to_non_admin(self):
        from handlers.admin import cb_admin_checkout_funnel
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=999),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        with (
            patch("handlers.admin._is_admin", return_value=False),
            patch("handlers.admin.safe_answer", new=AsyncMock()) as answer,
            patch("database.get_checkout_conversion_snapshot", new=AsyncMock()) as read,
        ):
            await cb_admin_checkout_funnel(callback)
        read.assert_not_awaited()
        callback.message.edit_text.assert_not_awaited()
        answer.assert_awaited_once_with(callback, "Нет доступа.", alert=True)


if __name__ == "__main__":
    unittest.main()
