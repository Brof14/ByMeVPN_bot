"""Regression for direct renewal, connection onboarding and provider failure recovery."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class ExpiryReminderRegression(unittest.IsolatedAsyncioTestCase):
    async def test_tariff_bound_promo_cannot_be_used_on_another_period(self):
        from handlers.buy import cb_select_tariff
        callback = SimpleNamespace(data="tariff_1_2", from_user=SimpleNamespace(id=123))
        promo = {"code": "BOUND", "promo_type": "percent", "discount_value": 30, "tariff_binding": 3}
        state = SimpleNamespace(get_data=AsyncMock(return_value={"promo_info": promo, "promo_code": "BOUND", "promo_discount": 30}), update_data=AsyncMock())
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
            patch("handlers.buy.validate_promo_code", new=AsyncMock()) as validate,
            patch("database.save_checkout_draft", new=AsyncMock()),
            patch("database.log_analytics_event", new=AsyncMock()),
        ):
            await cb_select_tariff(callback, object(), state)
        validate.assert_not_awaited()
        self.assertIn("Итого: <b>89 ₽</b>", send.await_args.args[2])
        self.assertIn("Промокод действует на другой срок", send.await_args.args[2])
        self.assertEqual(state.update_data.await_args.kwargs["promo_code"], "")

    async def test_expired_promo_does_not_create_discounted_order(self):
        from handlers.buy import cb_select_tariff
        callback = SimpleNamespace(data="tariff_1_2", from_user=SimpleNamespace(id=123))
        promo = {"code": "EXPIRED", "promo_type": "percent", "discount_value": 30, "tariff_binding": None}
        state = SimpleNamespace(get_data=AsyncMock(return_value={"promo_info": promo, "promo_code": "EXPIRED", "promo_discount": 30}), update_data=AsyncMock())
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
            patch("handlers.buy.validate_promo_code", new=AsyncMock(return_value=None)),
        ):
            await cb_select_tariff(callback, object(), state)
        self.assertNotIn("Ваш заказ", send.await_args.args[2])
        self.assertEqual(state.update_data.await_args.kwargs["promo_code"], "")

    async def test_valid_bound_promo_still_builds_discounted_order(self):
        from handlers.buy import cb_select_tariff
        callback = SimpleNamespace(data="tariff_3_2", from_user=SimpleNamespace(id=123))
        promo = {"code": "BOUND", "promo_type": "percent", "discount_value": 30, "tariff_binding": 3}
        state = SimpleNamespace(get_data=AsyncMock(return_value={"promo_info": promo, "promo_code": "BOUND", "promo_discount": 30}), update_data=AsyncMock())
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
            patch("handlers.buy.validate_promo_code", new=AsyncMock(return_value=promo)),
            patch("handlers.buy.has_user_used_promo", new=AsyncMock(return_value=False)),
            patch("database.save_checkout_draft", new=AsyncMock()),
            patch("database.log_analytics_event", new=AsyncMock()),
        ):
            await cb_select_tariff(callback, object(), state)
        self.assertIn("Итого: <b>221 ₽</b>", send.await_args.args[2])
        self.assertEqual(state.update_data.await_args.kwargs["promo_code"], "BOUND")

    async def test_valid_promo_allows_confirmed_checkout(self):
        from handlers.buy import _get_checkout_params
        promo = {"code": "BOUND", "promo_type": "percent", "discount_value": 30, "tariff_binding": 3}
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            "months": 3, "devices": 2, "price_rub": 221, "days": 120,
            "promo_code": "BOUND", "promo_info": promo,
        }), update_data=AsyncMock())
        with (
            patch("handlers.buy.validate_promo_code", new=AsyncMock(return_value=promo)),
            patch("handlers.buy.has_user_used_promo", new=AsyncMock(return_value=False)),
        ):
            result = await _get_checkout_params(state, 123)
        self.assertEqual((result["price_rub"], result["days"], result["promo_code"]), (221, 120, "BOUND"))
        state.update_data.assert_not_awaited()

    async def test_changed_promo_requires_new_order_confirmation(self):
        from handlers.buy import _get_checkout_params
        promo = {"code": "BOUND", "promo_type": "percent", "discount_value": 30, "tariff_binding": 3}
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            "months": 3, "devices": 2, "price_rub": 221, "days": 120,
            "promo_code": "BOUND", "promo_info": promo,
        }), update_data=AsyncMock())
        with (
            patch("handlers.buy.validate_promo_code", new=AsyncMock(return_value={**promo, "discount_value": 10})),
            patch("handlers.buy.has_user_used_promo", new=AsyncMock(return_value=False)),
        ):
            self.assertIsNone(await _get_checkout_params(state, 123))
        self.assertIsNone(state.update_data.await_args.kwargs["price_rub"])

    async def test_promo_expiring_after_order_confirmation_does_not_create_invoice(self):
        from handlers.buy import cb_pay_yookassa
        callback = SimpleNamespace(from_user=SimpleNamespace(id=123))
        data = {
            "months": 1, "devices": 2, "price_rub": 62, "days": 30,
            "promo_code": "EXPIRED", "promo_info": {"code": "EXPIRED", "promo_type": "percent", "discount_value": 30, "tariff_binding": None},
        }
        async def update_data(**kwargs):
            data.update(kwargs)
        state = SimpleNamespace(get_data=AsyncMock(return_value=data), update_data=AsyncMock(side_effect=update_data))
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy.validate_promo_code", new=AsyncMock(return_value=None)),
            patch("handlers.buy._ask_tariff_again", new=AsyncMock()) as retry,
            patch("handlers.buy.create_yookassa_payment", new=AsyncMock()) as create,
        ):
            await cb_pay_yookassa(callback, object(), state)
            await cb_pay_yookassa(callback, object(), state)
        create.assert_not_awaited()
        self.assertEqual(retry.await_count, 2)
        self.assertEqual(state.update_data.await_args.kwargs["promo_code"], "")
    async def test_other_key_autorenew_does_not_hide_manual_renewal(self):
        from notifications import _send_urgent_notification
        bot = SimpleNamespace(send_message=AsyncMock())
        item = {"user_id": 123, "key_id": 7, "expiry": int(time.time()) + 2 * 86400}
        async def lookup(user_id, key_id=None, **kwargs):
            return {"status": "active", "payment_method_id": "other-key"} if key_id is None else None
        with (
            patch("database.get_auto_renew_subscription", new=AsyncMock(side_effect=lookup)),
            patch("notifications._optional_renewal_promo", new=AsyncMock(return_value=(None, None))),
        ):
            await _send_urgent_notification(bot, item)
        text = bot.send_message.await_args.args[1]
        self.assertNotIn("Ничего делать не нужно", text)
        self.assertEqual(bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "key_renew:7")

    async def test_direct_renewal_preserves_expiring_key(self):
        from notifications import _send_urgent_notification
        bot = SimpleNamespace(send_message=AsyncMock())
        item = {"user_id": 123, "key_id": 456, "expiry": int(time.time()) + 2 * 86400}
        with (
            patch("notifications._get_active_autorenew", new=AsyncMock(return_value=None)),
            patch("notifications._optional_renewal_promo", new=AsyncMock(return_value=(None, None))),
        ):
            await _send_urgent_notification(bot, item)
        buttons = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(buttons[0][0].callback_data, "key_renew:456")

    async def test_optional_promo_failure_never_blocks_expiry_notice(self):
        from notifications import _send_warning_notification
        bot = SimpleNamespace(send_message=AsyncMock())
        item = {"user_id": 123, "key_id": 7, "expiry": int(time.time()) + 10 * 86400}
        with (
            patch("notifications._get_active_autorenew", new=AsyncMock(return_value=None)),
            patch("notifications.is_promotional_opted_out", new=AsyncMock(return_value=False)),
            patch("notifications.get_or_create_renewal_promo", new=AsyncMock(side_effect=RuntimeError("db outage"))),
        ):
            await _send_warning_notification(bot, item)
        text = bot.send_message.await_args.args[1]
        self.assertNotIn("30% скидки", text)
        self.assertEqual(
            bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
            "key_renew:7",
        )


class ExpiryDatabaseRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import database
        self.db = database
        self.old_path, self.old_pool = database.DB_FILE, database._db_pool
        self.tmp = tempfile.TemporaryDirectory()
        database.DB_FILE, database._db_pool = str(Path(self.tmp.name) / "expiry.db"), None
        await database.init_db()
        await database.ensure_user(9801)

    async def asyncTearDown(self):
        await self.db.close_db()
        self.db.DB_FILE, self.db._db_pool = self.old_path, self.old_pool
        self.tmp.cleanup()

    async def test_expiry_lookup_returns_stable_key_id(self):
        key_id = await self.db.add_key(9801, "https://sub.test/123", "VPN", "uuid-1", 30, 2)
        database = await self.db.get_db()
        await database.execute("UPDATE keys SET expiry=? WHERE id=?", (int(time.time()) + 2*86400, key_id))
        await database.commit()
        rows = await self.db.get_keys_nearing_expiry(1, 3)
        self.assertEqual(rows[0]["key_id"], key_id)
        self.assertEqual(rows[0]["user_id"], 9801)

    async def test_crypto_checkout_consumes_promo_after_success_only_once(self):
        from handlers.buy import cb_pay_crypto
        from webhook import _process_crypto_invoice
        await self.db.create_promo_code("SALE30", promo_type="percent", discount_value=30, max_uses=10)
        promo = await self.db.validate_promo_code("SALE30", tariff_months=1)
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            "months": 1, "devices": 2, "price_rub": 62, "days": 30,
            "promo_code": "SALE30", "promo_info": promo,
        }))
        callback = SimpleNamespace(from_user=SimpleNamespace(id=9801), message=SimpleNamespace(answer=AsyncMock()))
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy.create_crypto_payment", new=AsyncMock(return_value=("https://example.test/invoice", "crypto-1"))),
            patch("handlers.buy.send_with_photo", new=AsyncMock()),
        ):
            await cb_pay_crypto(callback, bot, state)
        pending = await self.db.get_crypto_pending("crypto-1")
        self.assertEqual(pending["payload"], "9801:30:2:SALE30")
        self.assertFalse(await self.db.has_user_used_promo("SALE30", 9801))
        invoice = {"invoice_id": "crypto-1", "status": "paid", "currency_type": "fiat", "fiat": "RUB", "amount": "62.00", "payload": pending["payload"]}
        with (
            patch("webhook.deliver_key", new=AsyncMock(return_value=True)) as deliver,
            patch("referral_system_new.award_referral_for_payment", new=AsyncMock()),
        ):
            await _process_crypto_invoice(bot, invoice)
            await _process_crypto_invoice(bot, invoice)
        self.assertTrue(await self.db.has_user_used_promo("SALE30", 9801))
        self.assertEqual((await self.db.validate_promo_code("SALE30"))["uses_count"], 1)
        deliver.assert_awaited_once()

    async def test_reminder_uses_autorenew_for_expiring_key_only(self):
        from notifications import _get_active_autorenew
        key1 = await self.db.add_key(9801, "https://sub.test/1", "one", "uuid-one", 30, 2)
        key2 = await self.db.add_key(9801, "https://sub.test/2", "two", "uuid-two", 30, 2)
        await self.db.save_auto_renew_subscription(
            user_id=9801, key_id=key2, payment_method_id="pm-two", payment_method_title="Card",
            payment_method_type="bank_card", months=1, days=30, devices=2, amount_rub=89,
        )
        self.assertIsNone(await _get_active_autorenew(9801, key1))
        self.assertEqual((await _get_active_autorenew(9801, key2))["key_id"], key2)
        await self.db.save_auto_renew_subscription(
            user_id=9801, key_id=None, payment_method_id="pm-legacy", payment_method_title="Card",
            payment_method_type="bank_card", months=1, days=30, devices=2, amount_rub=89,
        )
        self.assertIsNone((await _get_active_autorenew(9801, key1))["key_id"])


class PlatformGuideRegression(unittest.IsolatedAsyncioTestCase):
    async def test_one_active_key_is_selected_from_general_guide(self):
        from handlers.guide import cb_guide_menu
        callback = SimpleNamespace(from_user=SimpleNamespace(id=123))
        keys = [
            {"id": 41, "expiry": int(time.time()) + 86400},
            {"id": 2, "expiry": int(time.time()) - 86400},
        ]
        with (
            patch("handlers.guide.safe_answer", new=AsyncMock()),
            patch("handlers.guide._track_guide_event", new=AsyncMock()),
            patch("database.get_user_keys", new=AsyncMock(return_value=keys)),
            patch("handlers.guide.send_or_edit", new=AsyncMock()) as send,
        ):
            await cb_guide_menu(callback, object())
        markup = send.await_args.args[3]
        actions = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("guide_android:41", actions)
        self.assertIn("guide_ios:41", actions)

    async def test_multiple_active_keys_avoid_choosing_wrong_subscription(self):
        from handlers.guide import cb_guide_menu
        callback = SimpleNamespace(from_user=SimpleNamespace(id=123))
        keys = [{"id": i, "expiry": int(time.time()) + 86400} for i in (41, 42)]
        with (
            patch("handlers.guide.safe_answer", new=AsyncMock()),
            patch("handlers.guide._track_guide_event", new=AsyncMock()),
            patch("database.get_user_keys", new=AsyncMock(return_value=keys)),
            patch("handlers.guide.send_or_edit", new=AsyncMock()) as send,
        ):
            await cb_guide_menu(callback, object())
        actions = [b.callback_data for row in send.await_args.args[3].inline_keyboard for b in row]
        self.assertIn("guide_android", actions)
        self.assertNotIn("guide_android:41", actions)


class PaymentProviderFailures(unittest.IsolatedAsyncioTestCase):
    def callback(self):
        return SimpleNamespace(from_user=SimpleNamespace(id=123), message=SimpleNamespace(answer=AsyncMock()))

    def state(self):
        return SimpleNamespace(get_data=AsyncMock(return_value={
            "months": 6, "devices": 2, "price_rub": 552, "days": 240, "promo_code": "",
        }))

    async def test_yookassa_error_does_not_crash_or_leave_checkout_lock(self):
        from handlers.buy import cb_pay_yookassa
        callback = self.callback()
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy.claim_yookassa_checkout", return_value=True),
            patch("handlers.buy.release_yookassa_checkout") as release,
            patch("handlers.buy.create_yookassa_payment", new=AsyncMock(side_effect=RuntimeError("network"))),
        ):
            await cb_pay_yookassa(callback, object(), self.state())
        release.assert_called_once_with(123)
        self.assertIn("Не удалось открыть оплату", callback.message.answer.await_args.args[0])

    async def test_crypto_invoice_never_shown_if_persistence_failed(self):
        from handlers.buy import cb_pay_crypto
        callback = self.callback()
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy.create_crypto_payment", new=AsyncMock(return_value=("https://pay.example/invoice", "inv1"))),
            patch("database.add_crypto_pending", new=AsyncMock(side_effect=RuntimeError("storage down"))),
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_pay_crypto(callback, object(), self.state())
        send.assert_not_awaited()
        self.assertIn("Не удалось сохранить заказ", callback.message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
