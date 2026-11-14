"""Payment and entitlement regressions for self-service HWID upgrades."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from handlers.device_upgrades import (
    device_upgrade_checkout_error, process_device_upgrade_charge,
    upgrade_price_stars,
)


class DeviceUpgradePricingTests(unittest.TestCase):
    def test_surcharge_is_only_for_remaining_paid_time(self):
        self.assertEqual(upgrade_price_stars(2, 5, 30 * 86400), 20)
        self.assertEqual(upgrade_price_stars(2, 10, 30 * 86400), 40)
        self.assertEqual(upgrade_price_stars(5, 10, 15 * 86400), 10)
        self.assertEqual(upgrade_price_stars(2, 5, 1), 1)
        with self.assertRaises(ValueError):
            upgrade_price_stars(5, 2, 86400)
        with self.assertRaises(ValueError):
            upgrade_price_stars(10, 10, 86400)


class DeviceUpgradeStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.prev_path, self.prev_pool = database.DB_FILE, database._db_pool
        database.DB_FILE = str(Path(self.temp.name) / "orders.sqlite")
        database._db_pool = None
        await database.init_db()
        await database.ensure_user(101)
        self.expires = int(time.time()) + 30 * 86400
        self.key_id = await database.add_key(
            101, "https://vpn.example/sub/test", "Test",
            "11111111-1111-4111-8111-111111111111", 30, 2,
            expiry=self.expires,
        )

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE, database._db_pool = self.prev_path, self.prev_pool
        self.temp.cleanup()

    async def create_order(self, **kwargs):
        return await database.create_device_upgrade_order(
            101, self.key_id, 2, 5, self.expires, 20, **kwargs,
        )

    async def test_invoices_are_exclusive_until_expired(self):
        first = await self.create_order()
        self.assertIsNotNone(first)
        self.assertIsNone(await self.create_order())
        row = await database.get_device_upgrade_order(first)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["new_devices"], 5)
        await database.expire_device_upgrade_order(first)
        second = await self.create_order()
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    async def test_precheckout_rejects_changed_price_and_owner(self):
        order_id = await self.create_order()
        pre = SimpleNamespace(
            invoice_payload=f"devup_{order_id}",
            from_user=SimpleNamespace(id=101),
            currency="XTR", total_amount=20,
        )
        self.assertIsNone(await device_upgrade_checkout_error(pre))
        self.assertIsNotNone(await device_upgrade_checkout_error(
            SimpleNamespace(**{**vars(pre), "total_amount": 21})
        ))
        self.assertIsNotNone(await device_upgrade_checkout_error(
            SimpleNamespace(**{**vars(pre), "from_user": SimpleNamespace(id=102)})
        ))
        await database.set_key_expiry(self.key_id, self.expires + 86400)
        self.assertIsNotNone(await device_upgrade_checkout_error(pre))

    async def test_fulfillment_does_not_extend_expiry_and_is_idempotent(self):
        order_id = await self.create_order()
        payment = SimpleNamespace(
            invoice_payload=f"devup_{order_id}",
            total_amount=20, currency="XTR",
        )
        message = SimpleNamespace(from_user=SimpleNamespace(id=101))
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("xui_client.get_xui_client_details", new=AsyncMock(return_value={
                "enable": True, "limit_hwid": 2, "expiry_time": self.expires * 1000,
            })),
            patch("xui_client.update_xui_user", new=AsyncMock(return_value={
                "expiry_time": self.expires * 1000, "limit_hwid": 5,
            })) as panel_update,
            patch("xui_client.get_xui_devices", new=AsyncMock(return_value={"limit_hwid": 5, "devices": []})),
        ):
            await process_device_upgrade_charge(message, bot, payment, "test-charge-101")
            await process_device_upgrade_charge(message, bot, payment, "test-charge-101")
        key = await database.get_key_by_id(self.key_id)
        self.assertEqual(int(key["limit_ip"]), 5)
        self.assertEqual(int(key["expiry"]), self.expires)
        panel_update.assert_awaited_once()
        order = await database.get_device_upgrade_order(order_id)
        self.assertEqual(order["status"], "fulfilled")
        self.assertEqual(order["charge_id"], "test-charge-101")
        db = await database.get_db()
        n = (await (await db.execute("SELECT COUNT(*) FROM payments WHERE provider_payment_id='test-charge-101'")).fetchone())[0]
        self.assertEqual(n, 1)

    async def test_future_auto_renewal_rate_changes_only_after_fulfillment(self):
        db = await database.get_db()
        await db.execute(
            "INSERT INTO auto_renew_subscriptions "
            "(id,user_id,key_id,payment_method_id,months,days,devices,amount_rub,status) "
            "VALUES(1,101,?,'saved-method',1,30,2,89,'active')",
            (self.key_id,),
        )
        await db.commit()
        order_id = await self.create_order(
            auto_renew_id=1, prior_auto_renew_amount=89,
            next_auto_renew_amount=109,
        )
        first = await database.get_auto_renew_subscription(101, self.key_id)
        self.assertEqual(first["amount_rub"], 89)
        order = await database.get_device_upgrade_order(order_id)
        self.assertTrue(await database.finalize_device_upgrade_order(order, "charge-103"))
        latest = await database.get_auto_renew_subscription(101, self.key_id)
        self.assertEqual((latest["amount_rub"], latest["devices"]), (109, 5))
        self.assertEqual((await database.get_key_by_id(self.key_id))["expiry"], self.expires)
