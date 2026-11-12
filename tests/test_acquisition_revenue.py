"""Marketing report keeps currencies separate and avoids synthetic profit."""
import os
import tempfile
import unittest

import database
from acquisition_revenue import source_revenue_snapshot


class SourceRevenueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = database.DB_FILE
        await database.close_db()
        database.DB_FILE = os.path.join(self.tmp.name, "source.db")
        await database.init_db()
        await database.ensure_user(10012)
        await database.ensure_user(10013)

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.old
        self.tmp.cleanup()

    async def test_rub_and_stars_are_never_combined(self):
        await database.set_user_source(10012, "telegram_ad_001")
        await database.set_user_source(10013, "telegram_ad_001")
        await database.record_payment_idempotent(
            user_id=10012, amount=200, currency="RUB", method="test",
            days=30, payload="rub-1", status="success", tariff="basic",
            devices=2, provider="test", provider_payment_id="rub-1",
        )
        await database.record_payment_idempotent(
            user_id=10013, amount=150, currency="XTR", method="stars",
            days=30, payload="xtr-1", status="success", tariff="basic",
            devices=2, provider="stars", provider_payment_id="xtr-1",
        )
        report = await source_revenue_snapshot()
        cohort = report["by_source"]["telegram_ad_001"]
        self.assertEqual(cohort["gross_by_currency"], {"RUB": 200, "XTR": 150})
        self.assertNotIn("profit", cohort)
        self.assertNotIn("net_revenue", cohort)


if __name__ == "__main__":
    unittest.main()
