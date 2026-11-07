"""Funnels distinguish paid orders from free access and unsuccessful invoices."""
import tempfile
import unittest
from pathlib import Path


class ConfirmedPaymentFunnelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import database
        self.database = database
        self.old_path, self.old_pool = database.DB_FILE, database._db_pool
        self.tmp = tempfile.TemporaryDirectory()
        database.DB_FILE, database._db_pool = str(Path(self.tmp.name) / "funnel.sqlite3"), None
        await database.init_db()
        await database.ensure_user(721)
        await database.log_analytics_event(721, "bot_started", source="direct")
        await database.log_analytics_event(721, "tariffs_viewed")

    async def asyncTearDown(self):
        await self.database.close_db()
        self.database.DB_FILE, self.database._db_pool = self.old_path, self.old_pool
        self.tmp.cleanup()

    async def payment(self, name, amount, status):
        return await self.database.record_payment_idempotent(
            user_id=721, amount=amount, currency="RUB", method="test",
            days=30, payload=name, status=status, tariff="1 month",
            devices=2, provider="test", provider_payment_id=name,
        )

    async def test_free_and_failed_orders_never_count_as_buyers(self):
        await self.database.log_analytics_event(721, "subscription_created", amount=0)
        await self.database.log_analytics_event(721, "subscription_renewed", amount=0)
        await self.payment("failed", 89, "failed")
        await self.payment("free", 0, "success")
        funnel = await self.database.get_conversion_funnel(30)
        report = await self.database.get_checkout_conversion_snapshot(30)
        self.assertEqual(funnel["totals"]["started"], 1)
        self.assertEqual(funnel["totals"]["paid"], 0)
        self.assertEqual(funnel["totals"]["renewed"], 0)
        self.assertEqual(report["paid"], 0)
        self.assertEqual(report["paid_from_views"], 0)
        self.assertEqual(report["by_currency"], {})

    async def test_verified_positive_payment_counts_once(self):
        await self.database.log_analytics_event(721, "subscription_created", amount=0)
        await self.payment("verified", 89, "success")
        await self.payment("verified-again", 89, "success")
        await self.database.log_analytics_event(721, "subscription_renewed", amount=89)
        funnel = await self.database.get_conversion_funnel(30)
        report = await self.database.get_checkout_conversion_snapshot(30)
        self.assertEqual(funnel["totals"]["paid"], 1)
        self.assertEqual(funnel["totals"]["renewed"], 1)
        self.assertEqual(report["paid"], 1)
        self.assertEqual(report["paid_from_views"], 1)
        self.assertEqual(report["by_currency"]["RUB"]["payments"], 2)
        self.assertEqual(report["by_currency"]["RUB"]["gross_amount"], 178)


if __name__ == "__main__":
    unittest.main()
