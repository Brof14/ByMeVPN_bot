"""Manual-review referral analytics must never be sold as proof of fraud."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class ReferralRiskContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import database
        self.database = database
        self.old_path, self.old_pool = database.DB_FILE, database._db_pool
        self.tmp = tempfile.TemporaryDirectory()
        database.DB_FILE, database._db_pool = str(Path(self.tmp.name) / "risk.db"), None
        await database.init_db()

    async def asyncTearDown(self):
        await self.database.close_db()
        self.database.DB_FILE, self.database._db_pool = self.old_path, self.old_pool
        self.tmp.cleanup()

    async def test_a_high_rate_referrer_is_a_review_signal_not_automatic_block(self):
        from growth_metrics import referral_risk_snapshot
        db = self.database
        await db.ensure_user(100)
        for uid in range(200, 205):
            await db.ensure_user(uid)
            await db.set_referrer(uid, 100)
        report = await referral_risk_snapshot(days=1, min_invites=5)
        self.assertEqual(report["suspicious_accounts"], [
            {"referrer_id": 100, "invitations": 5, "one_rub_trials": 0}
        ])
        self.assertEqual(report["promised_days_from_one_rub"], 0)
        await db.record_payment_idempotent(
            user_id=200, amount=1, currency="RUB", method="yookassa",
            days=3, payload="paid-trial-200", status="success",
            tariff="trial", devices=2, provider="yookassa",
            provider_payment_id="paid-trial-200",
        )
        await db.record_referral_payment_reward(100, 200, "paid-trial-200")
        report = await referral_risk_snapshot(days=1, min_invites=5)
        self.assertEqual(report["suspicious_accounts"][0]["one_rub_trials"], 1)
        self.assertEqual(report["one_rub_referral_rewards"], 1)
        self.assertEqual(report["promised_days_from_one_rub"], 30)

    async def test_report_is_admin_only(self):
        from handlers.admin import cb_admin_referral_risk
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=999),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        with (
            patch("handlers.admin._is_admin", return_value=False),
            patch("handlers.admin.safe_answer", new=AsyncMock()) as reply,
            patch("growth_metrics.referral_risk_snapshot", new=AsyncMock()) as read,
        ):
            await cb_admin_referral_risk(callback)
        reply.assert_awaited_once()
        read.assert_not_awaited()
        callback.message.edit_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
