"""Regression tests for provider-bound device upgrades and fail-closed billing."""
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from handlers.device_upgrades import process_provider_device_upgrade


class ProviderDeviceUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path, self.old_pool = database.DB_FILE, database._db_pool
        database.DB_FILE, database._db_pool = str(Path(self.tmp.name) / "provider-upgrades.db"), None
        await database.init_db()
        await database.ensure_user(8101)
        self.expiry = int(time.time()) + 30 * 86400
        self.key_id = await database.add_key(
            8101, "https://vpn.example/sub/active", "VPN",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 30, 2, expiry=self.expiry,
        )
        self.bot = SimpleNamespace(send_message=AsyncMock())

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE, database._db_pool = self.old_path, self.old_pool
        self.tmp.cleanup()

    async def create_bound_order(self, provider):
        order_id = await database.create_device_upgrade_order(
            8101, self.key_id, 2, 5, self.expiry, 20, provider=provider,
        )
        self.assertIsInstance(order_id, str)
        invoice_id = f"invoice-{provider}"
        self.assertTrue(await database.bind_device_upgrade_invoice(
            order_id, 8101, provider, invoice_id,
        ))
        self.assertFalse(await database.bind_device_upgrade_invoice(
            order_id, 8101, provider, "second-charge",
        ))
        self.assertEqual(
            (await database.get_device_upgrade_order_for_invoice(provider, invoice_id))["id"],
            order_id,
        )
        self.assertIsNone(await database.get_device_upgrade_order_for_invoice(
            provider, "second-charge",
        ))
        return order_id, invoice_id

    async def test_provider_binding_cannot_be_reassigned_to_another_customer(self):
        order_id, invoice_id = await self.create_bound_order("yookassa")
        self.assertFalse(await database.bind_device_upgrade_invoice(
            order_id, 9999, "yookassa", invoice_id,
        ))
        self.assertFalse(await database.bind_device_upgrade_invoice(
            order_id, 8101, "cryptobot", invoice_id,
        ))
        self.assertIsNone(await database.create_device_upgrade_order(
            8101, self.key_id, 2, 10, self.expiry, 40, provider="stars",
        ))
        order = await database.get_device_upgrade_order(order_id)
        self.assertEqual(order["provider"], "yookassa")
        self.assertEqual(order["provider_payment_id"], invoice_id)

    def _yookassa_verified(self, order_id, invoice_id, amount="20.00"):
        return {
            "id": invoice_id, "status": "succeeded",
            "amount": {"value": amount, "currency": "RUB"},
            "metadata": {
                "purpose": "device_upgrade", "device_upgrade_order_id": order_id,
                "user_id": "8101", "days": "0", "devices": "5",
                "key_id": str(self.key_id), "old_devices": "2", "amount_rub": "20",
            },
        }

    def _cryptobot_verified(self, order_id, invoice_id, amount="20"):
        return {
            "invoice_id": invoice_id, "status": "paid",
            "currency_type": "fiat", "fiat": "RUB", "amount": amount,
            "payload": f"devup_{order_id}",
        }

    async def _mock_confirmed_panel(self, order_id, invoice_id, provider, invoice):
        with (
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                "enable": True, "expiry_time": self.expiry * 1000, "limit_hwid": 2,
            })),
            patch("xui_client.update_xui_user", new=AsyncMock(return_value={
                "expiry_time": self.expiry * 1000, "limit_hwid": 5,
            })) as panel_update,
            patch("xui_client.get_xui_devices", new=AsyncMock(return_value={
                "limit_hwid": 5, "devices": [],
            })),
            patch("app_links.build_user_connect_page_url", new=AsyncMock(
                return_value="https://connect.example/connect?u=8101&s=signed",
            )),
        ):
            self.assertTrue(await process_provider_device_upgrade(
                self.bot, provider, invoice_id, invoice,
            ))
            self.assertTrue(await process_provider_device_upgrade(
                self.bot, provider, invoice_id, invoice,
            ))
        panel_update.assert_awaited_once()
        item = await database.get_key_by_id(self.key_id)
        self.assertEqual(item["expiry"], self.expiry)
        self.assertEqual(item["limit_ip"], 5)
        order = await database.get_device_upgrade_order(order_id)
        self.assertEqual(order["status"], "fulfilled")
        self.assertEqual(order["charge_id"], f"{provider}:{invoice_id}")
        db = await database.get_db()
        payments = await (await db.execute(
            "SELECT status,COUNT(*) FROM payments WHERE provider=? AND provider_payment_id=? GROUP BY status",
            (provider, invoice_id),
        )).fetchall()
        self.assertEqual(payments, [("success", 1)])
        self.bot.send_message.assert_awaited_once()

    async def test_yookassa_payment_verified_and_fulfilled_only_once(self):
        order_id, invoice_id = await self.create_bound_order("yookassa")
        await database.add_yookassa_pending(invoice_id, 8101, 0, 5, 20)
        await self._mock_confirmed_panel(
            order_id, invoice_id, "yookassa",
            self._yookassa_verified(order_id, invoice_id),
        )
        self.assertIsNone(await database.get_yookassa_pending(invoice_id))

    async def test_cryptobot_payment_verified_and_fulfilled_only_once(self):
        order_id, invoice_id = await self.create_bound_order("cryptobot")
        await database.add_crypto_pending(
            invoice_id, 8101, 0, 5, 20, payload=f"devup_{order_id}",
        )
        await self._mock_confirmed_panel(
            order_id, invoice_id, "cryptobot",
            self._cryptobot_verified(order_id, invoice_id),
        )
        self.assertIsNone(await database.get_crypto_pending(invoice_id))

    async def test_success_ledger_with_unfulfilled_upgrade_is_quarantined(self):
        order_id, invoice_id = await self.create_bound_order("yookassa")
        await database.add_yookassa_pending(invoice_id, 8101, 0, 5, 20)
        await database.record_payment_idempotent(
            user_id=8101, amount=20, currency="RUB",
            method="device_upgrade", days=0, payload=invoice_id,
            status="success", tariff="Увеличение лимита устройств", devices=5,
            provider="yookassa", provider_payment_id=invoice_id,
        )
        with patch("xui_client.update_xui_user", new=AsyncMock()) as panel:
            self.assertFalse(await process_provider_device_upgrade(
                self.bot, "yookassa", invoice_id,
                self._yookassa_verified(order_id, invoice_id),
            ))
        panel.assert_not_awaited()
        db = await database.get_db()
        status = await (await db.execute(
            "SELECT status FROM payments "
            "WHERE provider='yookassa' AND provider_payment_id=?",
            (invoice_id,),
        )).fetchone()
        self.assertEqual(status, ("recovery_required",))
        self.assertEqual((await database.get_device_upgrade_order(order_id))["status"], "pending")

    async def test_changed_invoice_amount_never_mutates_panel(self):
        order_id, invoice_id = await self.create_bound_order("yookassa")
        await database.add_yookassa_pending(invoice_id, 8101, 0, 5, 20)
        with patch("xui_client.update_xui_user", new=AsyncMock()) as update:
            self.assertFalse(await process_provider_device_upgrade(
                self.bot, "yookassa", invoice_id,
                self._yookassa_verified(order_id, invoice_id, "19.00"),
            ))
        update.assert_not_awaited()
        self.assertEqual((await database.get_key_by_id(self.key_id))["limit_ip"], 2)

    async def test_bad_cryptobot_payload_never_mutates_panel(self):
        order_id, invoice_id = await self.create_bound_order("cryptobot")
        await database.add_crypto_pending(invoice_id, 8101, 0, 5, 20, payload=f"devup_{order_id}")
        invoice = self._cryptobot_verified(order_id, invoice_id)
        invoice["payload"] = "8101:0:5"
        with patch("xui_client.update_xui_user", new=AsyncMock()) as update:
            self.assertFalse(await process_provider_device_upgrade(
                self.bot, "cryptobot", invoice_id, invoice,
            ))
        update.assert_not_awaited()
        self.assertEqual((await database.get_key_by_id(self.key_id))["limit_ip"], 2)

    async def test_yookassa_checkout_binds_invoice_before_exposing_url(self):
        from handlers.device_upgrades import cb_device_upgrade_provider
        event = SimpleNamespace(
            data=f"device_upgrade_provider:yookassa:{self.key_id}:5",
            from_user=SimpleNamespace(id=8101),
        )
        context = {
            "key": await database.get_key_by_id(self.key_id),
            "expiry": self.expiry, "limit": 2, "recurring": None,
        }

        async def create_provider_invoice(amount, description, user_id, days, **kwargs):
            self.assertEqual((amount, user_id, days), (20, 8101, 0))
            self.assertEqual(kwargs["extra_metadata"]["purpose"], "device_upgrade")
            self.assertFalse(kwargs["save_payment_method"])
            await database.add_yookassa_pending("yoo-created", 8101, 0, 5, 20)
            return {
                "payment_url": "https://pay.example.invalid/yoo-created",
                "payment_id": "yoo-created", "status": "pending",
            }

        with (
            patch("handlers.device_upgrades._upgrade_context", new=AsyncMock(return_value=context)),
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=False)),
            patch("handlers.device_upgrades.safe_answer", new=AsyncMock()),
            patch("payments.create_yookassa_payment", new=AsyncMock(side_effect=create_provider_invoice)),
            patch("handlers.device_upgrades.send_with_photo", new=AsyncMock()) as sent,
        ):
            await cb_device_upgrade_provider(event, self.bot)
        db = await database.get_db()
        current = await (await db.execute(
            "SELECT id,provider,provider_payment_id FROM device_upgrade_orders "
            "WHERE user_id=8101",
        )).fetchone()
        self.assertEqual((current[1], current[2]), ("yookassa", "yoo-created"))
        self.assertEqual(
            sent.await_args.args[3].inline_keyboard[0][0].url,
            "https://pay.example.invalid/yoo-created",
        )

    async def test_recurring_customer_sees_new_future_price_before_provider_payment(self):
        from handlers.device_upgrades import cb_device_upgrade_provider
        await database.save_auto_renew_subscription(
            user_id=8101, key_id=self.key_id, payment_method_id="saved-card",
            months=1, days=30, devices=2, amount_rub=89,
        )
        recurring = await database.get_auto_renew_subscription(8101, self.key_id)
        event = SimpleNamespace(
            data=f"device_upgrade_provider:yookassa:{self.key_id}:5",
            from_user=SimpleNamespace(id=8101),
        )
        context = {
            "key": await database.get_key_by_id(self.key_id),
            "expiry": self.expiry, "limit": 2, "recurring": recurring,
        }

        async def checkout(amount, description, user_id, days, **kwargs):
            await database.add_yookassa_pending("recurring-invoice", 8101, 0, 5, 20)
            return {
                "payment_url": "https://pay.example.invalid/recurring-invoice",
                "payment_id": "recurring-invoice",
            }

        with (
            patch("handlers.device_upgrades._upgrade_context", new=AsyncMock(return_value=context)),
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=False)),
            patch("handlers.device_upgrades.safe_answer", new=AsyncMock()),
            patch("payments.create_yookassa_payment", new=AsyncMock(side_effect=checkout)),
            patch("handlers.device_upgrades.send_with_photo", new=AsyncMock()) as sent,
        ):
            await cb_device_upgrade_provider(event, self.bot)
        text = sent.await_args.args[2]
        self.assertIn("109 ₽", text)
        self.assertIn("30 дней", text)
        self.assertIn("Отключить", text)
        old = await database.get_auto_renew_subscription(8101, self.key_id)
        self.assertEqual(old["amount_rub"], 89)
        order = await database.get_latest_device_upgrade_for_user(8101)
        self.assertEqual(order["prior_auto_renew_amount"], 89)
        self.assertEqual(order["next_auto_renew_amount"], 109)

    async def test_crypto_checkout_persists_invoice_before_exposing_url(self):
        from handlers.device_upgrades import cb_device_upgrade_provider
        event = SimpleNamespace(
            data=f"device_upgrade_provider:cryptobot:{self.key_id}:5",
            from_user=SimpleNamespace(id=8101),
        )
        context = {
            "key": await database.get_key_by_id(self.key_id),
            "expiry": self.expiry, "limit": 2, "recurring": None,
        }
        with (
            patch("handlers.device_upgrades._upgrade_context", new=AsyncMock(return_value=context)),
            patch("database.is_account_tombstoned", new=AsyncMock(return_value=False)),
            patch("handlers.device_upgrades.safe_answer", new=AsyncMock()),
            patch("payments.create_crypto_payment", new=AsyncMock(return_value=(
                "https://pay.example.invalid/crypto-created", "crypto-created",
            ))),
            patch("handlers.device_upgrades.send_with_photo", new=AsyncMock()) as sent,
        ):
            await cb_device_upgrade_provider(event, self.bot)
        db = await database.get_db()
        current = await (await db.execute(
            "SELECT id,provider,provider_payment_id FROM device_upgrade_orders "
            "WHERE user_id=8101",
        )).fetchone()
        self.assertEqual((current[1], current[2]), ("cryptobot", "crypto-created"))
        pending = await database.get_crypto_pending("crypto-created")
        self.assertEqual(pending["payload"], f"devup_{current[0]}")
        self.assertEqual(
            sent.await_args.args[3].inline_keyboard[0][0].url,
            "https://pay.example.invalid/crypto-created",
        )

    async def test_paid_status_keeps_invoice_reserved_without_issuing_second_charge(self):
        from handlers.device_upgrades import cb_device_upgrade_status
        order_id, invoice_id = await self.create_bound_order("yookassa")
        event = SimpleNamespace(from_user=SimpleNamespace(id=8101))
        with (
            patch("handlers.device_upgrades.safe_answer", new=AsyncMock()),
            patch("payments.get_yookassa_payment_status", new=AsyncMock(return_value="succeeded")),
            patch("handlers.device_upgrades.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_device_upgrade_status(event, self.bot)
        self.assertIn("Провайдер подтвердил оплату", send.await_args.args[2])
        self.assertEqual(
            (await database.get_device_upgrade_order(order_id))["status"], "pending"
        )
        self.assertIsNone(await database.create_device_upgrade_order(
            8101, self.key_id, 2, 10, self.expiry, 40, provider="stars",
        ))

    async def test_terminal_provider_status_releases_invoice_reservation(self):
        from handlers.device_upgrades import cb_device_upgrade_status
        order_id, invoice_id = await self.create_bound_order("yookassa")
        event = SimpleNamespace(from_user=SimpleNamespace(id=8101))
        with (
            patch("handlers.device_upgrades.safe_answer", new=AsyncMock()),
            patch("payments.get_yookassa_payment_status", new=AsyncMock(return_value="canceled")),
            patch("handlers.device_upgrades.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_device_upgrade_status(event, self.bot)
        self.assertIn("Провайдер подтвердил отмену", send.await_args.args[2])
        self.assertEqual(
            (await database.get_device_upgrade_order(order_id))["status"], "expired"
        )
        self.assertIsNotNone(await database.create_device_upgrade_order(
            8101, self.key_id, 2, 10, self.expiry, 40, provider="stars",
        ))

    async def test_unknown_provider_status_never_releases_pending_invoice(self):
        from handlers.device_upgrades import cb_device_upgrade_status
        order_id, invoice_id = await self.create_bound_order("cryptobot")
        event = SimpleNamespace(from_user=SimpleNamespace(id=8101))
        with (
            patch("handlers.device_upgrades.safe_answer", new=AsyncMock()),
            patch("payments.get_crypto_invoice", new=AsyncMock(return_value=None)),
            patch("handlers.device_upgrades.send_with_photo", new=AsyncMock()) as send,
        ):
            await cb_device_upgrade_status(event, self.bot)
        self.assertIn("ещё ожидается", send.await_args.args[2])
        self.assertEqual(
            (await database.get_device_upgrade_order(order_id))["status"], "pending"
        )

    async def test_expiration_does_not_discard_payable_yookassa_invoices(self):
        order_id, invoice_id = await self.create_bound_order("yookassa")
        db = await database.get_db()
        await db.execute(
            "UPDATE device_upgrade_orders SET created_at=? WHERE id=?",
            (int(time.time()) - 7200, order_id),
        )
        await db.commit()
        self.assertIsNone(await database.create_device_upgrade_order(
            8101, self.key_id, 2, 10, self.expiry, 40, provider="stars",
        ))
        self.assertEqual(
            (await database.get_device_upgrade_order(order_id))["status"], "pending"
        )

    async def test_unknown_provider_invoice_is_never_provisioned(self):
        with patch("xui_client.update_xui_user", new=AsyncMock()) as update:
            self.assertFalse(await process_provider_device_upgrade(
                self.bot, "cryptobot", "unknown-paid-invoice",
                {"status": "paid", "amount": "20", "payload": "devup_unknown"},
            ))
        update.assert_not_awaited()

    async def test_failed_panel_update_quarantines_paid_upgrade_without_extending_days(self):
        order_id, invoice_id = await self.create_bound_order("yookassa")
        await database.add_yookassa_pending(invoice_id, 8101, 0, 5, 20)
        with (
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                "enable": True, "expiry_time": self.expiry * 1000, "limit_hwid": 2,
            })),
            patch("xui_client.update_xui_user", new=AsyncMock(return_value=None)),
        ):
            self.assertFalse(await process_provider_device_upgrade(
                self.bot, "yookassa", invoice_id, self._yookassa_verified(order_id, invoice_id),
            ))
        self.assertEqual((await database.get_key_by_id(self.key_id))["expiry"], self.expiry)
        self.assertEqual((await database.get_key_by_id(self.key_id))["limit_ip"], 2)
        self.assertEqual((await database.get_device_upgrade_order(order_id))["status"], "pending")
        db = await database.get_db()
        status = await (await db.execute(
            "SELECT status FROM payments WHERE provider='yookassa' AND provider_payment_id=?",
            (invoice_id,),
        )).fetchone()
        self.assertEqual(status[0], "recovery_required")


if __name__ == "__main__":
    unittest.main()
