"""Behavioral growth-loop contracts, using the existing SQLite schema."""
import os
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from keyboards import after_key_kb, tariff_selection_kb


class ConversionUXTests(unittest.TestCase):
    def test_first_delivery_promotes_connection_then_voluntary_referral(self):
        menu = after_key_kb("https://example.test/sub/1")
        rows = menu.inline_keyboard
        self.assertEqual(rows[0][0].callback_data, "connection_guide")
        self.assertEqual([b.callback_data for b in rows[1]], ["my_keys", "partner"])
        self.assertEqual(rows[-1][0].callback_data, "back_to_menu")

    def test_tariffs_show_real_payable_total_and_monthly_rate(self):
        for limit, totals in [(2, (89, 316, 552, 885)), (5, (109, 396, 712, 1185)), (10, (129, 476, 872, 1485))]:
            kb = tariff_selection_kb(devices=limit)
            actions = {b.callback_data: b.text for row in kb.inline_keyboard for b in row if b.callback_data}
            for months, rubles in zip((1, 3, 6, 12), totals):
                text = actions[f"tariff_{months}_{limit}"]
                self.assertIn(f"{rubles // (months + {1: 0, 3: 1, 6: 2, 12: 3}[months])} ₽/мес.", text)
                self.assertIn("/мес.", text)
                self.assertIn(f"{rubles} ₽ всего", text)
                if months > 1:
                    self.assertIn(f"{months}+{{3: 1, 6: 2, 12: 3}[months]} мес.", text)
                else:
                    self.assertIn("1 мес.", text)


class OrganicGrowthMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import database
        self.db = database
        self.orig_path = database.DB_FILE
        self.orig_pool = database._db_pool
        self.path = "/tmp/bymevpn_growth_funnel_test.db"
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + ext)
            except FileNotFoundError:
                pass
        database.DB_FILE = self.path
        database._db_pool = None
        await database.init_db()

    async def asyncTearDown(self):
        await self.db.close_db()
        self.db.DB_FILE = self.orig_path
        self.db._db_pool = self.orig_pool
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + ext)
            except FileNotFoundError:
                pass

    async def test_failed_payment_does_not_mark_friend_paid(self):
        await self.db.ensure_user(100)
        await self.db.ensure_user(200)
        await self.db.set_referrer(200, 100)
        await self.db.record_payment_idempotent(
            user_id=200, amount=89, currency="RUB", method="yookassa",
            days=30, payload="failed-200", status="failed", tariff="test",
            devices=2, provider="yookassa", provider_payment_id="failed-200"
        )
        rows = await self.db.get_referred_users_list(100)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["has_paid"])
        await self.db.add_referral_event(
            referrer_id=100, referred_id=200, event_type="payment_bonus",
            days_awarded=30,
        )
        rows = await self.db.get_referred_users_list(100)
        self.assertTrue(rows[0]["has_paid"])

    async def test_cohort_counts_real_conversions_and_bonus_days(self):
        from growth_metrics import referral_growth_snapshot
        for uid in (100, 200, 300, 400):
            await self.db.ensure_user(uid)
        await self.db.set_referrer(200, 100)
        await self.db.set_referrer(300, 100)
        await self.db.set_referrer(400, 300)
        await self.db.add_referral_event(referrer_id=100, referred_id=200, event_type="registration_bonus", days_awarded=15)
        await self.db.add_referral_event(referrer_id=100, referred_id=200, event_type="payment_bonus", days_awarded=30)
        await self.db.add_referral_event(referrer_id=100, referred_id=300, event_type="registration_bonus", days_awarded=15)
        await self.db.add_referral_event(referrer_id=300, referred_id=400, event_type="registration_bonus", days_awarded=15)
        report = await referral_growth_snapshot(days=30)
        self.assertEqual(report["invited"], 3)
        self.assertEqual(report["paying"], 1)
        self.assertEqual(report["advocates"], 2)
        self.assertEqual(report["bonus_days"], 75)
        self.assertAlmostEqual(report["conversion_pct"], 100/3)
        self.assertEqual(report["unconverted"], 2)

    async def test_growth_dashboard_is_admin_only(self):
        from handlers.admin import cb_admin_referral_funnel
        callback = SimpleNamespace(from_user=SimpleNamespace(id=700), message=SimpleNamespace(edit_text=AsyncMock()))
        with (
            patch("handlers.admin._is_admin", return_value=False),
            patch("handlers.admin.safe_answer", new=AsyncMock()) as answer,
            patch("growth_metrics.referral_growth_snapshot", new=AsyncMock()) as report,
        ):
            await cb_admin_referral_funnel(callback)
        report.assert_not_awaited()
        callback.message.edit_text.assert_not_awaited()
        answer.assert_awaited_once_with(callback, "Нет доступа.", alert=True)


if __name__ == "__main__":
    unittest.main()
