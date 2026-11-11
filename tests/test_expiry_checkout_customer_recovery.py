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
        self.assertIn("К оплате: 89 ₽", send.await_args.args[2])
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
        self.assertIn("К оплате: 221 ₽", send.await_args.args[2])
        self.assertEqual(state.update_data.await_args.kwargs["promo_code"], "BOUND")

    async def test_zero_price_promo_is_delivered_without_an_invoice(self):
        from handlers.buy import cb_select_tariff
        callback = SimpleNamespace(data="tariff_1_2", from_user=SimpleNamespace(id=123))
        promo = {"code": "FREE", "promo_type": "percent", "discount_value": 100, "tariff_binding": None}
        state = SimpleNamespace(get_data=AsyncMock(return_value={"promo_info": promo, "promo_code": "FREE"}), update_data=AsyncMock(), clear=AsyncMock())
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
            patch("handlers.buy.validate_promo_code", new=AsyncMock(return_value=promo)),
            patch("handlers.buy.has_user_used_promo", new=AsyncMock(return_value=False)),
            patch("handlers.buy.deliver_key", new=AsyncMock(return_value=True)) as deliver,
            patch("database.reserve_free_promo_order", new=AsyncMock(return_value=True)),
            patch("database.complete_free_promo_order", new=AsyncMock(return_value=True)),
            patch("handlers.buy.record_fulfilled_promo", new=AsyncMock(return_value=True)),
        ):
            await cb_select_tariff(callback, object(), state)
        self.assertIn("Доступ активирован", send.await_args.args[2])
        self.assertEqual(deliver.await_args.kwargs["method"], "promo")
        self.assertFalse(any(button.callback_data and button.callback_data.startswith("pay_") for row in send.await_args.args[3].inline_keyboard for button in row))
    async def test_stale_zero_price_order_cannot_create_invoice(self):
        from handlers.buy import _get_checkout_params
        state = SimpleNamespace(get_data=AsyncMock(return_value={"price_rub": 0, "days": 30, "months": 1, "devices": 2}))
        self.assertIsNone(await _get_checkout_params(state, 123))

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

    async def test_legacy_fixed_and_free_day_promotions_apply_to_order(self):
        from handlers.buy import cb_select_tariff
        for code, legacy_type, canonical_type, expected in (
            ("FIXED10", "fixed", "fixed_rub", "К оплате: 79 ₽"),
            ("DAYS5", "days", "free_days", "Доступ: <b>35 дней</b>"),
        ):
            await self.db.create_promo_code(code, promo_type=legacy_type, discount_value=10 if legacy_type == "fixed" else 5)
            db = await self.db.get_db()
            await db.execute("UPDATE promo_codes SET promo_type=? WHERE code=?", (legacy_type, code))
            await db.commit()
            promo = await self.db.validate_promo_code(code)
            self.assertEqual(promo["promo_type"], canonical_type)
            state = SimpleNamespace(get_data=AsyncMock(return_value={"promo_info": promo, "promo_code": code}), update_data=AsyncMock())
            callback = SimpleNamespace(data="tariff_1_2", from_user=SimpleNamespace(id=9801))
            with (
                patch("handlers.buy.safe_answer", new=AsyncMock()),
                patch("handlers.buy.send_with_photo", new=AsyncMock()) as send,
            ):
                await cb_select_tariff(callback, object(), state)
            self.assertIn(expected, send.await_args.args[2])
        listed = {p["code"]: p["promo_type"] for p in await self.db.get_all_promo_codes()}
        self.assertEqual((listed["FIXED10"], listed["DAYS5"]), ("fixed_rub", "free_days"))

    async def test_fractional_day_or_ruble_promo_is_rejected_before_checkout(self):
        for code, promo_type in (("FRACTIONALDAY", "days"), ("FRACTIONALRUB", "fixed")):
            await self.db.create_promo_code(code, promo_type=promo_type, discount_value=2.5)
            self.assertIsNone(await self.db.validate_promo_code(code))

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

    async def test_crypto_payload_mismatch_never_delivers_or_consumes_promo(self):
        from webhook import _process_crypto_invoice
        await self.db.create_promo_code("SALE30", promo_type="percent", discount_value=30, max_uses=10)
        await self.db.add_crypto_pending("crypto-bad", 9801, 30, 2, 62, payload="9801:30:2:SALE30")
        invoice = {"invoice_id": "crypto-bad", "status": "paid", "currency_type": "fiat", "fiat": "RUB", "amount": "62.00", "payload": "9801:30:2:OTHER"}
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch("webhook.deliver_key", new=AsyncMock()) as deliver:
            await _process_crypto_invoice(bot, invoice)
        deliver.assert_not_awaited()
        self.assertFalse(await self.db.has_user_used_promo("SALE30", 9801))
        self.assertIsNotNone(await self.db.get_crypto_pending("crypto-bad"))

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
        self.assertIsNone(await _get_active_autorenew(9801, key1))

    async def test_key_screen_and_unlink_choose_active_legacy_subscription(self):
        from handlers.keys import _load_autorenew_context
        key_id = await self.db.add_key(9801, "https://sub.test/legacy", "legacy", "uuid-legacy", 30, 2)
        await self.db.save_auto_renew_subscription(
            user_id=9801, key_id=key_id, payment_method_id="inactive", payment_method_title="Old",
            payment_method_type="bank_card", months=1, days=30, devices=2, amount_rub=89,
        )
        await self.db.set_auto_renew_status_by_key(9801, key_id, "cancelled")
        await self.db.save_auto_renew_subscription(
            user_id=9801, key_id=None, payment_method_id="legacy-active", payment_method_title="Card",
            payment_method_type="bank_card", months=1, days=30, devices=2, amount_rub=89,
        )
        sub, title, _ = await _load_autorenew_context(9801, key_id)
        self.assertEqual((sub["key_id"], title), (None, "Card"))
        result = await self.db.unlink_auto_renew_payment_method(9801, key_id)
        self.assertTrue(result["unlinked"])
        db = await self.db.get_db()
        cursor = await db.execute(
            "SELECT status, payment_method_id FROM auto_renew_subscriptions "
            "WHERE user_id=? AND key_id IS NULL",
            (9801,),
        )
        legacy = await cursor.fetchone()
        self.assertEqual(legacy[0], "cancelled")
        self.assertFalse(legacy[1])

    async def test_account_payment_controls_prefer_active_token_over_newer_canceled_card(self):
        first = await self.db.add_key(9801, "https://sub.test/first", "first", "uuid-first", 30, 2)
        second = await self.db.add_key(9801, "https://sub.test/second", "second", "uuid-second", 30, 2)
        await self.db.save_auto_renew_subscription(
            9801, first, "pm-active", payment_method_title="Active",
        )
        await self.db.save_auto_renew_subscription(
            9801, second, "pm-canceled", payment_method_title="Canceled",
        )
        await self.db.set_auto_renew_status_by_key(9801, second, "cancelled")
        selected = await self.db.get_auto_renew_subscription(9801)
        self.assertEqual(selected["key_id"], first)

        result = await self.db.unlink_auto_renew_payment_method(9801)
        self.assertTrue(result["unlinked"])
        active = await self.db.get_auto_renew_subscription(9801, first)
        canceled = await self.db.get_auto_renew_subscription(9801, second)
        self.assertEqual((active["status"], active["payment_method_id"]), ("cancelled", ""))
        self.assertEqual((canceled["status"], canceled["payment_method_id"]), ("cancelled", "pm-canceled"))

    async def test_account_resume_does_not_activate_every_saved_card(self):
        first = await self.db.add_key(9801, "https://sub.test/first", "first", "uuid-first", 30, 2)
        second = await self.db.add_key(9801, "https://sub.test/second", "second", "uuid-second", 30, 2)
        for key_id in (first, second):
            await self.db.save_auto_renew_subscription(
                9801, key_id, f"pm-{key_id}", payment_method_title="Saved",
            )
            await self.db.set_auto_renew_status_by_key(9801, key_id, "cancelled")
        self.assertTrue(await self.db.resume_auto_renew(9801))
        old = await self.db.get_auto_renew_subscription(9801, first)
        newest = await self.db.get_auto_renew_subscription(9801, second)
        self.assertEqual((old["status"], newest["status"]), ("cancelled", "active"))

    async def test_legacy_resume_requires_exactly_one_key(self):
        first = await self.db.add_key(9801, "https://sub.test/one", "one", "uuid-one", 30, 2)
        await self.db.save_auto_renew_subscription(
            9801, None, "legacy-token", payment_method_title="Old card",
        )
        legacy = await self.db.get_auto_renew_subscription(9801)
        await self.db.set_auto_renew_status(legacy["id"], "cancelled")

        self.assertTrue(await self.db.resume_auto_renew(9801, first))
        self.assertEqual((await self.db.get_auto_renew_subscription(9801))["status"], "active")

        await self.db.set_auto_renew_status(legacy["id"], "cancelled")
        await self.db.add_key(9801, "https://sub.test/two", "two", "uuid-two", 30, 2)
        self.assertFalse(await self.db.resume_auto_renew(9801, first))
        self.assertEqual((await self.db.get_auto_renew_subscription(9801))["status"], "cancelled")

    async def test_deleting_one_key_cannot_reactivate_unbound_legacy_billing(self):
        now = int(time.time())
        first = await self.db.add_key(
            9801, "https://sub.test/first", "first", "uuid-first", 30, 2, expiry=now + 86400,
        )
        await self.db.add_key(
            9801, "https://sub.test/expired", "expired", "uuid-expired", 30, 2,
            expiry=now - 60,
        )
        await self.db.save_auto_renew_subscription(
            9801, None, "legacy-active", payment_method_title="Legacy",
        )
        self.assertEqual(await self.db.get_expiring_auto_renew_subscriptions(now=now), [])
        self.assertTrue(await self.db.delete_key(first))
        self.assertEqual(await self.db.get_expiring_auto_renew_subscriptions(now=now), [])
        legacy = await self.db.get_auto_renew_subscription(9801)
        self.assertEqual(legacy["status"], "cancelled")
        # A consciously deleted key must not leave a merchant token that
        # could later be re-enabled by an unrelated legacy subscription.
        self.assertEqual(legacy["payment_method_id"], "")

    async def test_expiry_cleanup_cannot_enable_legacy_renewal(self):
        now = int(time.time())
        await self.db.add_key(
            9801, "https://sub.test/expired", "expired", "uuid-expired", 30, 2,
            expiry=now - 60,
        )
        await self.db.add_key(
            9801, "https://sub.test/current", "current", "uuid-current", 30, 2,
            expiry=now + 86400,
        )
        await self.db.save_auto_renew_subscription(
            9801, None, "legacy-token", payment_method_title="Saved",
        )
        self.assertEqual(await self.db.cleanup_expired_keys(), 1)
        legacy = await self.db.get_auto_renew_subscription(9801)
        self.assertEqual(legacy["status"], "cancelled")

    async def test_deletion_by_uuid_cannot_enable_legacy_renewal(self):
        now = int(time.time())
        await self.db.add_key(
            9801, "https://sub.test/removed", "removed", "uuid-removed", 30, 2,
            expiry=now + 86400,
        )
        await self.db.add_key(
            9801, "https://sub.test/current", "current", "uuid-current", 30, 2,
            expiry=now - 60,
        )
        await self.db.save_auto_renew_subscription(
            9801, None, "legacy-token", payment_method_title="Saved",
        )
        self.assertTrue(await self.db.delete_key_by_uuid("uuid-removed"))
        legacy = await self.db.get_auto_renew_subscription(9801)
        self.assertEqual(legacy["status"], "cancelled")
        self.assertEqual(await self.db.get_expiring_auto_renew_subscriptions(now=now), [])

    async def test_ambiguous_legacy_autorenew_never_charges_and_is_explained(self):
        from handlers.keys import _load_autorenew_context, _get_payment_methods_text
        now = int(time.time())
        expired = await self.db.add_key(9801, "https://sub.test/a", "a", "uuid-a", 30, 2, expiry=now - 60)
        await self.db.add_key(9801, "https://sub.test/b", "b", "uuid-b", 30, 2, expiry=now + 86400)
        await self.db.save_auto_renew_subscription(
            user_id=9801, key_id=None, payment_method_id="legacy-active", payment_method_title="Card",
            payment_method_type="bank_card", months=1, days=30, devices=2, amount_rub=89,
        )
        self.assertFalse(await self.db.get_expiring_auto_renew_subscriptions(now=now))
        sub, title, expiry = await _load_autorenew_context(9801, expired)
        self.assertIn("нескольк", _get_payment_methods_text(title, sub=sub, expiry_str=expiry).lower())

    async def test_autorenew_without_payment_token_is_not_scheduled(self):
        now = int(time.time())
        key_id = await self.db.add_key(
            9801, "https://sub.test/no-token", "no-token", "uuid-no-token",
            30, 2, expiry=now - 60,
        )
        await self.db.save_auto_renew_subscription(
            user_id=9801, key_id=key_id, payment_method_id="  ",
            payment_method_title="Card", months=1, days=30, devices=2, amount_rub=89,
        )
        self.assertEqual(await self.db.get_expiring_auto_renew_subscriptions(now=now), [])

    async def test_unambiguous_legacy_autorenew_remains_eligible(self):
        from notifications import _get_active_autorenew
        now = int(time.time())
        key_id = await self.db.add_key(9801, "https://sub.test/one", "one", "uuid-one", 30, 2, expiry=now - 60)
        sub_id = await self.db.save_auto_renew_subscription(
            user_id=9801, key_id=None, payment_method_id="legacy-active", payment_method_title="Card",
            payment_method_type="bank_card", months=1, days=30, devices=2, amount_rub=89,
        )
        self.assertEqual([item["sub_id"] for item in await self.db.get_expiring_auto_renew_subscriptions(now=now)], [sub_id])
        self.assertIsNone((await _get_active_autorenew(9801, key_id))["key_id"])


class PromoAccountingAlerts(unittest.IsolatedAsyncioTestCase):
    async def test_missing_usage_notifies_without_failing_fulfilled_order(self):
        from payments import record_fulfilled_promo
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("config.ADMIN_IDS", [100]),
            patch("database.use_promo_code", new=AsyncMock(return_value=False)),
            patch("database.has_user_used_promo", new=AsyncMock(return_value=False)),
        ):
            self.assertFalse(await record_fulfilled_promo(bot, "SALE", 42, "stars", "charge-1"))
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertIn("charge-1", bot.send_message.await_args.args[1])

    async def test_already_recorded_usage_is_not_an_error(self):
        from payments import record_fulfilled_promo
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("database.use_promo_code", new=AsyncMock(return_value=False)),
            patch("database.has_user_used_promo", new=AsyncMock(return_value=True)),
        ):
            self.assertTrue(await record_fulfilled_promo(bot, "SALE", 42, "stars", "charge-1"))
        bot.send_message.assert_not_awaited()

    async def test_alert_failure_does_not_raise_after_delivery(self):
        from payments import record_fulfilled_promo
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("telegram unavailable")))
        with (
            patch("config.ADMIN_IDS", [100]),
            patch("database.use_promo_code", new=AsyncMock(side_effect=RuntimeError("database unavailable"))),
        ):
            self.assertFalse(await record_fulfilled_promo(bot, "SALE", 42, "yookassa", "payment-1"))


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
        return SimpleNamespace(data="pay_yookassa", from_user=SimpleNamespace(id=123), message=SimpleNamespace(answer=AsyncMock()))

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
