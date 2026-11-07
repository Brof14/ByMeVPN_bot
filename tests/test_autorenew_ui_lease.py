"""Customer payment-method changes use the same lease as recurring invoice creation."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import keys


class AutoRenewCustomerLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_change_requires_account_lease(self):
        action = AsyncMock(return_value={"unlinked": True})
        with (
            patch("database.try_acquire_delivery_lease", new=AsyncMock(return_value=True)) as acquire,
            patch("database.release_delivery_lease", new=AsyncMock()) as release,
        ):
            result = await keys._run_billing_change_under_lease(123, action)
        self.assertTrue(result["unlinked"])
        acquire.assert_awaited_once()
        release.assert_awaited_once()
        self.assertEqual(
            acquire.await_args.args[1], release.await_args.args[1],
        )

    async def test_card_change_releases_lease_on_database_error(self):
        action = AsyncMock(side_effect=OSError("database unavailable"))
        with (
            patch("database.try_acquire_delivery_lease", new=AsyncMock(return_value=True)),
            patch("database.release_delivery_lease", new=AsyncMock()) as release,
        ):
            with self.assertRaises(OSError):
                await keys._run_billing_change_under_lease(123, action)
        release.assert_awaited_once()

    async def test_card_change_never_mutates_without_lease(self):
        action = AsyncMock()
        with (
            patch("database.try_acquire_delivery_lease", new=AsyncMock(return_value=False)),
            patch("database.release_delivery_lease", new=AsyncMock()) as release,
            patch("asyncio.sleep", new=AsyncMock(side_effect=TimeoutError("busy"))),
        ):
            with self.assertRaises(TimeoutError):
                await keys._run_billing_change_under_lease(123, action)
        action.assert_not_awaited()
        release.assert_not_awaited()

    async def test_unlink_callback_calls_protected_operation(self):
        callback = SimpleNamespace(
            data="autorenew_unbind_confirm:7",
            from_user=SimpleNamespace(id=123),
        )
        with (
            patch("handlers.keys.safe_answer", new=AsyncMock()),
            patch("handlers.keys._run_billing_change_under_lease", new=AsyncMock(
                return_value={"unlinked": True, "expires_at": None},
            )) as guarded,
            patch("handlers.keys.send_or_edit", new=AsyncMock()),
            patch("database.unlink_auto_renew_payment_method", new=AsyncMock()) as unlink,
        ):
            await keys.cb_autorenew_unbind_confirm(callback, object())
        guarded.assert_awaited_once()
        unlink.assert_not_awaited()  # Never call an unguarded DB mutation.
        self.assertEqual(guarded.await_args.args[0], 123)


if __name__ == "__main__":
    unittest.main()
