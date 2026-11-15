"""Provider payment dispatch regression: upgrades are never ordinary renewals."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webhook import _process_payment, _process_crypto_invoice


class DeviceUpgradeWebhookRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_yookassa_upgrade_does_not_extend_subscription(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        payment = {
            "id": "upgrade-yoo-1", "status": "succeeded",
            "metadata": {"purpose": "device_upgrade", "days": "0"},
        }
        with (
            patch("handlers.device_upgrades.process_provider_device_upgrade",
                  new=AsyncMock(return_value=True)) as upgrade,
            patch("webhook.deliver_key", new=AsyncMock()) as deliver,
        ):
            await _process_payment(bot, "upgrade-yoo-1", verified_payment=payment)
        upgrade.assert_awaited_once_with(
            bot, "yookassa", "upgrade-yoo-1", payment,
        )
        deliver.assert_not_awaited()

    async def test_verified_cryptobot_upgrade_does_not_extend_subscription(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        invoice = {
            "invoice_id": 12881, "status": "paid",
            "payload": "devup_safe-order",
            "currency_type": "fiat", "fiat": "RUB", "amount": "20",
        }
        with (
            patch("database.get_crypto_pending", new=AsyncMock(return_value={
                "payload": "devup_safe-order",
            })),
            patch("handlers.device_upgrades.process_provider_device_upgrade",
                  new=AsyncMock(return_value=True)) as upgrade,
            patch("webhook.deliver_key", new=AsyncMock()) as deliver,
        ):
            await _process_crypto_invoice(bot, invoice)
        upgrade.assert_awaited_once_with(bot, "cryptobot", "12881", invoice)
        deliver.assert_not_awaited()

    async def test_corrupted_crypto_upgrade_payload_cannot_fall_into_normal_subscription(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        invoice = {"invoice_id": 199, "status": "paid", "payload": "not-an-upgrade"}
        with (
            patch("database.get_crypto_pending", new=AsyncMock(return_value={
                "payload": "devup_bound-order",
            })),
            patch("handlers.device_upgrades.process_provider_device_upgrade",
                  new=AsyncMock(return_value=False)) as upgrade,
            patch("webhook.deliver_key", new=AsyncMock()) as deliver,
        ):
            await _process_crypto_invoice(bot, invoice)
        upgrade.assert_awaited_once()
        deliver.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
